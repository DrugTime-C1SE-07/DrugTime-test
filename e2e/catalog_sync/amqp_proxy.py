"""AMQP 0-9-1 frame observer and ack gate; forwards real broker traffic.

No auth frame, raw body or credentials are retained. Delivery identity includes
connection and channel because delivery tags are scoped to a channel.
"""

import json
import socket
import struct
import threading
import time
from dataclasses import dataclass


@dataclass
class Frame:
    kind: int
    channel: int
    payload: bytes

    def encode(self):
        header = struct.pack("!BHI", self.kind, self.channel, len(self.payload))
        return header + self.payload + b"\xce"


class Decoder:
    def __init__(self):
        self.buffer = bytearray()
        self.protocol = False

    def feed(self, data):
        self.buffer.extend(data)
        output = []
        if not self.protocol and self.buffer[:4] == b"AMQP":
            if len(self.buffer) < 8:
                return output
            output.append(bytes(self.buffer[:8]))
            del self.buffer[:8]
            self.protocol = True
        while len(self.buffer) >= 7:
            kind, channel, size = struct.unpack("!BHI", self.buffer[:7])
            if size > 16 * 1024 * 1024:
                raise ValueError("AMQP frame exceeds observer limit")
            if len(self.buffer) < size + 8:
                break
            if self.buffer[size + 7] != 0xCE:
                raise ValueError("Invalid AMQP frame terminator")
            output.append(Frame(kind, channel, bytes(self.buffer[7:size + 7])))
            del self.buffer[:size + 8]
        return output


def shortstr(payload, offset):
    size = payload[offset]
    return payload[offset + 1:offset + 1 + size].decode(), offset + 1 + size


class Observer:
    def __init__(self):
        self.ledger = []
        self.pending = {}
        self.deliveries = {}
        self.targets = set()
        self.held = []
        self.lock = threading.RLock()

    def arm(self, task_id):
        with self.lock:
            self.targets.add(task_id)

    def observe(self, connection, direction, frame):
        if isinstance(frame, bytes):
            return True
        with self.lock:
            return self._observe(connection, direction, frame)

    def _observe(self, connection, direction, frame):
        key = (connection, direction, frame.channel)
        payload = frame.payload
        if frame.kind == 1:
            cls, method = struct.unpack("!HH", payload[:4])
            if cls == 60 and method in (40, 60):  # Basic.Publish / Basic.Deliver
                meta = {"event": "publish" if method == 40 else "deliver",
                        "connection": connection, "channel": frame.channel}
                if method == 60:
                    _, pos = shortstr(payload, 4)  # consumer tag
                    meta["tag"] = struct.unpack("!Q", payload[pos:pos + 8])[0]
                    meta["redelivered"] = bool(payload[pos + 8] & 1)
                self.pending[key] = {"meta": meta, "body": bytearray()}
            elif direction == "client" and cls == 60 and method in (80, 90, 120):
                tag = struct.unpack("!Q", payload[4:12])[0]
                multiple = method != 90 and bool(payload[12] & 1)
                affected = [(k, v) for k, v in self.deliveries.items()
                            if k[:2] == (connection, frame.channel)
                            and (k[2] == tag or multiple and (tag == 0 or k[2] <= tag))]
                gate = method == 80 and any(v in self.targets for _, v in affected)
                event = "held_ack" if gate else {80: "ack", 90: "reject", 120: "nack"}[method]
                for delivery, task_id in affected:
                    self.ledger.append({"event": event, "id": task_id,
                                        "connection": connection, "channel": frame.channel,
                                        "tag": delivery[2], "at": time.monotonic()})
                    if not gate:
                        self.deliveries.pop(delivery)
                if gate:
                    self.held.append((connection, frame.channel, tag))
                    return False  # Never flush held acks on shutdown.
            elif (cls, method) in {(20, 40), (20, 41)}:  # channel.close / close-ok
                self.deliveries = {k: v for k, v in self.deliveries.items()
                                   if k[:2] != (connection, frame.channel)}
        elif frame.kind == 2 and key in self.pending:
            pending = self.pending[key]
            pending["size"] = struct.unpack("!Q", payload[4:12])[0]
            if pending["size"] > 1024 * 1024:
                raise ValueError("Task body exceeds observer limit")
            # Decode the real content header with installed py-amqp, never serialize it.
            from amqp.basic_message import Message
            message = Message()
            message.inbound_header(payload)
            headers = message.properties.get("application_headers", {})
            pending["meta"].update({"task": headers.get("task"), "id": headers.get("id"),
                                    "retries": headers.get("retries", 0),
                                    "delivery_mode": message.properties.get("delivery_mode")})
        elif frame.kind == 3 and key in self.pending:
            pending = self.pending[key]
            pending["body"].extend(payload)
            if len(pending["body"]) >= pending["size"]:
                meta = pending["meta"]
                if meta.get("task") in {"catalog.sync", "runtime.tick", "runtime.heartbeat"}:
                    body = json.loads(pending["body"])
                    kwargs = body[1]
                    meta["kwargs"] = {k: kwargs[k] for k in ("source", "triggered_by")
                                      if k in kwargs}
                    meta["arg_count"] = len(body[0])
                    meta["kwarg_keys"] = sorted(kwargs)
                    meta["at"] = time.monotonic()
                    self.ledger.append(meta)
                    if meta["event"] == "deliver":
                        self.deliveries[(connection, frame.channel, meta["tag"])] = meta["id"]
                del self.pending[key]
        return True

    def disconnect(self, connection):
        with self.lock:
            self.pending = {k: v for k, v in self.pending.items() if k[0] != connection}
            self.deliveries = {k: v for k, v in self.deliveries.items() if k[0] != connection}

    def events(self, event=None, task=None, task_id=None):
        with self.lock:
            return [dict(v) for v in self.ledger
                    if (event is None or v["event"] == event)
                    and (task is None or v.get("task") == task)
                    and (task_id is None or v.get("id") == task_id)]


class Proxy:
    def __init__(self, broker_port):
        self.port = broker_port
        self.observer = Observer()
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.listener.settimeout(0.2)
        self.listen_port = self.listener.getsockname()[1]
        self.stopped = threading.Event()
        self.connections = {}
        self.threads = []
        self.lock = threading.Lock()
        self.errors = []
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()

    def _accept(self):
        index = 0
        while not self.stopped.is_set():
            try:
                client, _ = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            try:
                server = socket.create_connection(("127.0.0.1", self.port), timeout=5)
            except OSError:
                client.close()
                continue
            index += 1
            with self.lock:
                self.connections[index] = (client, server)
            for source, destination, direction in ((client, server, "client"),
                                                    (server, client, "server")):
                thread = threading.Thread(target=self._forward,
                                          args=(index, source, destination, direction), daemon=True)
                self.threads.append(thread)
                thread.start()

    def _forward(self, connection, source, destination, direction):
        decoder = Decoder()
        try:
            source.settimeout(0.2)
            while not self.stopped.is_set():
                try:
                    data = source.recv(65536)
                except TimeoutError:
                    continue
                if not data:
                    break
                for frame in decoder.feed(data):
                    if self.observer.observe(connection, direction, frame):
                        destination.sendall(frame if isinstance(frame, bytes) else frame.encode())
        except OSError:
            pass
        except Exception as exc:
            # Exception type only: malformed traffic must never become a secret log.
            self.errors.append(type(exc).__name__)
        finally:
            self.close_connection(connection)

    def close_connection(self, connection):
        with self.lock:
            pair = self.connections.pop(connection, ())
        for sock in pair:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        self.observer.disconnect(connection)

    def close(self, timeout=5):
        end = time.monotonic() + timeout
        self.stopped.set()
        self.listener.close()
        for connection in list(self.connections):
            self.close_connection(connection)
        self.thread.join(max(0, min(2, end - time.monotonic())))
        for thread in self.threads:
            thread.join(max(0, min(2, end - time.monotonic())))
        if self.thread.is_alive() or any(thread.is_alive() for thread in self.threads):
            raise RuntimeError("Proxy threads failed to stop within cleanup deadline")
