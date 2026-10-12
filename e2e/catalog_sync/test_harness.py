"""Meaningful helper regressions; fakes are limited to unit boundaries."""

import io
import json
import socket
import struct
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest
from amqp.basic_message import Message

from .amqp_proxy import Decoder, Frame, Observer
from .dav_server import DavServer
from .environment import (
    ROOT,
    Deadline,
    PrerequisiteError,
    Redactor,
    SanitizedStream,
    docker_endpoint_is_local,
    isolated_env,
    local_url,
    preflight,
    save_json,
    verify_owner,
)
from .harness import Case, Process, wait_for, wait_for_management
from .run_e2e import Results, main


@pytest.mark.parametrize("url", ["", "amqp://remote:5672/v", "amqp://localhost:5672/v",
                                  "amqp://127.0.0.1.evil:5672/v", "amqp://127.0.0.1/v"])
def test_preflight_rejects_remote_urls__AC10(url):
    with pytest.raises(PrerequisiteError):
        local_url(url, {"amqp"})


@pytest.mark.parametrize("endpoint", ["ssh://remote", "tcp://127.0.0.1:2375", "", "tcp://remote"])
def test_preflight_rejects_remote_docker_context__AC10(endpoint):
    with pytest.raises(PrerequisiteError):
        docker_endpoint_is_local(endpoint)


def owner_data():
    run_id = "abcdef123456"
    manifest = {"run_id": run_id, "project_name": f"catalog-e2e-{run_id}"}
    labels = {"com.docker.compose.project": manifest["project_name"],
              "drugtime.catalog-e2e": run_id}
    return manifest, {"db_name": f"drugtime_catalog_e2e_{run_id}", "marker": run_id,
                      "labels": labels}


@pytest.mark.parametrize("field,value", [("db_name", "postgres"), ("marker", "other"),
                                         ("labels", {}), ("db_name", "drugtime_catalog_e2e_other")])
def test_ownership_guard_rejects_wrong_database_or_manifest__AC10(field, value):
    manifest, data = owner_data()
    data[field] = value
    with pytest.raises(PrerequisiteError):
        verify_owner(manifest, **data)


def test_ownership_accepts_only_matching_marker_and_resource_labels__AC10():
    manifest, data = owner_data()
    verify_owner(manifest, **data)
    data["labels"]["drugtime.catalog-e2e"] = "other"
    with pytest.raises(PrerequisiteError):
        verify_owner(manifest, **data)


def test_env_isolated_from_production_settings__AC10(monkeypatch, tmp_path):
    for key in ("DATABASE_URL", "CELERY_BROKER_URL", "HTTP_PROXY", "PYTHONPATH", "LLM_API_KEY",
                "SUPABASE_SERVICE_KEY", "FCM_PROJECT_ID"):
        monkeypatch.setenv(key, "production-secret")
    env = isolated_env(db="postgresql+psycopg://worker_role:local@127.0.0.1:5432/test",
                       broker="amqp://test:local@127.0.0.1:5672/test",
                       dav="http://127.0.0.1:1234/dav", empty_file=tmp_path / "empty.env",
                       run_id="abcdef123456", secrets="local")
    assert "production-secret" not in json.dumps(env)
    assert "HTTP_PROXY" not in env
    assert env["PYTHONPATH"] == str(ROOT / "backend/src")
    assert env["LLM_API_KEY"] == env["SUPABASE_SERVICE_KEY"] == env["FCM_PROJECT_ID"] == ""


def test_redaction_precedes_all_log_sinks__AC11(tmp_path):
    secret = "s3cr3t"
    raw = (f"amqp://user:{secret}@127.0.0.1:5672/v password={secret} "
           "Bearer opaque-token eyJhbGciOiJ9.eyJzdWIiOjF9.signature api_key=unknown-key")
    redact = Redactor([secret])
    path = tmp_path / "evidence.json"
    save_json(path, {"exception": raw}, redact)
    assert all(value not in path.read_text() for value in
               (secret, "opaque-token", "eyJ", "unknown-key"))


def wire_task(observer, task_id, *, connection=1, channel=1, tag=1,
              redelivered=False, event="deliver", retries=0):
    if event == "deliver":
        method = struct.pack("!HH", 60, 60) + b"\x01c" + struct.pack("!QB", tag, redelivered)
        direction = "server"
    else:
        method = struct.pack("!HHH", 60, 40, 0) + b"\x00\x06celery\x00"
        direction = "client"
    body = json.dumps([[], {"source": "dav_drugbank", "triggered_by": "scheduler"}, {}]).encode()
    message = Message(application_headers={"task": "catalog.sync", "id": task_id,
                                           "retries": retries}, delivery_mode=2)
    header = struct.pack("!HHQ", 60, 0, len(body)) + message._serialize_properties()
    for frame in (Frame(1, channel, method), Frame(2, channel, header),
                  Frame(3, channel, body[:17]), Frame(3, channel, body[17:])):
        assert observer.observe(connection, direction, frame)


def ack(tag, *, channel=1, multiple=False, method=80):
    return Frame(1, channel, struct.pack("!HHQB", 60, method, tag, multiple))


def test_amqp_frame_assembly_and_task_correlation__AC8():
    decoder = Decoder()
    frames = [Frame(8, 0, b""), ack(3)]
    wire = b"AMQP\x00\x00\x09\x01" + b"".join(f.encode() for f in frames)
    decoded = []
    for byte in wire:
        decoded.extend(decoder.feed(bytes([byte])))
    assert decoded[0] == b"AMQP\x00\x00\x09\x01" and decoded[1:] == frames
    assert Decoder().feed(b"".join(f.encode() for f in frames)) == frames
    observer = Observer()
    wire_task(observer, "same-id", event="publish")
    wire_task(observer, "same-id")
    observer.disconnect(1)
    wire_task(observer, "same-id", connection=2, redelivered=True)
    events = observer.events("deliver", task_id="same-id")
    assert [e["redelivered"] for e in events] == [False, True]
    assert all(e["delivery_mode"] == 2 for e in events)
    assert all(e["kwargs"] == {"source": "dav_drugbank", "triggered_by": "scheduler"}
               for e in events)


@pytest.mark.parametrize("multiple,tag", [(False, 2), (True, 3), (True, 0)])
def test_ack_gate_preserves_target_and_forwards_heartbeats__AC8(multiple, tag):
    observer = Observer()
    observer.arm("target")
    wire_task(observer, "other", tag=1)
    wire_task(observer, "target", tag=2)
    wire_task(observer, "last", tag=3)
    assert not observer.observe(1, "client", ack(tag, multiple=multiple))
    assert observer.events("held_ack", task_id="target")
    assert observer.observe(1, "client", Frame(8, 0, b""))
    assert observer.observe(1, "client", ack(1))
    wire_task(observer, "another-channel", channel=2, tag=2)
    assert observer.observe(1, "client", ack(2, channel=2))
    observer.disconnect(1)
    wire_task(observer, "reused-tag", connection=2, tag=2)
    assert observer.observe(2, "client", ack(2))
    assert not observer.deliveries


@pytest.mark.parametrize("method", [90, 120])
def test_reject_and_nack_are_traced_without_ack_gate__AC8(method):
    observer = Observer()
    observer.arm("target")
    wire_task(observer, "target")
    assert observer.observe(1, "client", ack(1, method=method))
    assert not observer.deliveries and not observer.held


def test_auth_frames_are_forwarded_but_never_retained__AC11():
    observer = Observer()
    secret = b"real-password"
    assert observer.observe(1, "client", Frame(1, 0, struct.pack("!HH", 10, 11) + secret))
    assert secret.decode() not in repr(observer.__dict__)


def test_deadline_is_bounded_by_parent__AC10():
    current = [0.0]
    parent = Deadline(5, clock=lambda: current[0])
    child = Deadline(20, parent=parent, clock=lambda: current[0])
    current[0] = 4
    assert child.remaining(3) == 1
    current[0] = 5
    with pytest.raises(TimeoutError):
        child.remaining()


def test_wait_timeout_does_not_report_success__AC10():
    with pytest.raises(AssertionError, match="missing commit"):
        wait_for(lambda: False, Deadline(0.01), "missing commit")


def test_process_cleanup_refuses_unowned_group__AC10(monkeypatch):
    class FakePopen:
        returncode = None
        def poll(self):
            return None
    process = Process.__new__(Process)
    process.process = FakePopen()
    process.pid, process.pgid = 100, 99
    called = []
    monkeypatch.setattr("os.killpg", lambda *args: called.append(args), raising=False)
    monkeypatch.setattr("os.getpgid", lambda *args: 100, raising=False)
    with pytest.raises(PrerequisiteError):
        process.stop()
    assert not called


def test_stream_redacts_secrets_split_across_writes__AC11():
    screen, sink = io.StringIO(), io.StringIO()
    stream = SanitizedStream(screen, sink, Redactor(["password-value"]))
    stream.write("amqp://user:pass")
    stream.flush()
    assert screen.getvalue() == sink.getvalue() == ""
    stream.write("word-value@127.0.0.1:5672/v\n")
    assert "password-value" not in screen.getvalue() + sink.getvalue()
    assert "[REDACTED]" in screen.getvalue()


@pytest.mark.parametrize("collected,executed,skipped,failed,code,expected", [
    (1, {"a"}, set(), set(), 0, 0), (0, set(), set(), set(), 0, 1),
    (1, set(), set(), set(), 0, 1), (1, {"a"}, {"a"}, set(), 0, 1),
    (1, {"a"}, set(), {"a"}, 1, 1), (1, {"a"}, set(), set(), 1, 1),
])
def test_runner_exit_codes_require_executed_cases__AC10(
    collected, executed, skipped, failed, code, expected,
):
    results = Results()
    results.collected, results.executed = collected, executed
    results.skipped, results.failed = skipped, failed
    assert results.exit_code(code) == expected


def test_runner_missing_environment_is_exit_two__AC10(monkeypatch, tmp_path, capsys):
    from . import run_e2e
    def missing():
        raise PrerequisiteError("Requires Linux")
    monkeypatch.setattr(run_e2e, "preflight", missing)
    monkeypatch.setattr(run_e2e, "EVIDENCE", tmp_path)
    assert main([]) == 2
    assert "NOT RUN" in capsys.readouterr().out
    assert json.loads((tmp_path / "test-catalog-result.json").read_text())["executed"] == 0


def test_http_fixture_holds_first_request_and_releases__AC3():
    fixture = DavServer()
    fixture.configure(hold=True)
    try:
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(httpx.post, fixture.url,
                                 json={"skipCount": 0, "maxResultCount": 10}, timeout=5,
                                 trust_env=False)
            assert fixture.entered.wait(2)
            assert not future.done()
            fixture.release.set()
            assert len(future.result(2).json()["result"]["items"]) == 10
        assert fixture.requests[0]["released_at"] >= fixture.requests[0]["received_at"]
    finally:
        fixture.close()


def test_runner_native_windows_refuses_before_resources__AC10(monkeypatch):
    from . import environment
    monkeypatch.setattr(environment.sys, "platform", "win32")
    def forbidden(*args, **kwargs):
        raise AssertionError("Platform guard must precede every Docker operation")
    monkeypatch.setattr(environment.subprocess, "run", forbidden)
    with pytest.raises(PrerequisiteError, match="Linux/WSL2"):
        preflight()


def test_runner_bootstrap_failure_is_exit_two__AC10():
    results = Results()
    results.prerequisite_error = True
    assert results.exit_code(1) == 2
    results.cleanup_error = True
    assert results.exit_code(1) == 1


def test_case_selection_does_not_confuse_ac_one_and_ten__AC11():
    one = SimpleNamespace(name="test_beat__AC1")
    ten = SimpleNamespace(name="test_cleanup__AC10")
    config = SimpleNamespace(hook=SimpleNamespace(pytest_deselected=lambda **kwargs: None))
    items = [one, ten]
    Results("AC1").pytest_collection_modifyitems(None, config, items)
    assert items == [one]


def test_cleanup_continues_after_process_error_and_taints_infra__AC10(tmp_path):
    requests = []
    infra = SimpleNamespace(deadline=Deadline(60), scratch=tmp_path, project="owned-project",
                            tainted=False, redact=Redactor(),
                            api=lambda *args: requests.append(args))
    case = Case(infra, "cleanup-unit")
    case.vhost_created = True
    def failed_stop(**kwargs):
        raise RuntimeError("owned process wait failed")
    case.processes = [SimpleNamespace(stop=failed_stop)]
    with pytest.raises(RuntimeError, match="Case cleanup failed"):
        case.close()
    assert requests == [("DELETE", "/vhosts/" + case.vhost)]
    assert infra.tainted


def test_cleanup_refuses_unowned_vhost__AC10(tmp_path):
    requests = []
    infra = SimpleNamespace(deadline=Deadline(60), scratch=tmp_path, project="owned-project",
                            tainted=False, redact=Redactor(),
                            api=lambda *args: requests.append(args))
    case = Case(infra, "cleanup-unit")
    case.vhost_created = True
    case.vhost = "unowned-sentinel"
    with pytest.raises(RuntimeError, match="Vhost ownership mismatch"):
        case.close()
    assert not requests


def test_late_success_cannot_escape_deadline__AC10():
    current = [0.0]
    deadline = Deadline(5, clock=lambda: current[0])
    def late_commit():
        current[0] = 6
        return True
    with pytest.raises(AssertionError, match="commit"):
        wait_for(late_commit, deadline, "commit")


def test_management_api_reset_during_boot_does_not_fail_ready_broker__AC10():
    calls = []
    def booting_api(method, path):
        calls.append((method, path))
        if len(calls) == 1:
            raise httpx.ReadError("connection reset during plugin startup")
        return {"rabbitmq_version": "fixture"}
    assert wait_for_management(booting_api, Deadline(1)) == {"rabbitmq_version": "fixture"}
    assert len(calls) == 2


def test_proxy_closes_connections_without_flushing_held_acks__AC8():
    from .amqp_proxy import Proxy
    listener = socket.create_server(("127.0.0.1", 0))
    proxy = Proxy(listener.getsockname()[1])
    client = socket.create_connection(("127.0.0.1", proxy.listen_port), timeout=2)
    listener.settimeout(2)
    broker, _ = listener.accept()
    try:
        proxy.observer.arm("target")
        wire_task(proxy.observer, "target")
        client.sendall(ack(1).encode() + Frame(8, 0, b"").encode())
        broker.settimeout(2)
        assert broker.recv(8) == Frame(8, 0, b"").encode()
        assert proxy.observer.held == [(1, 1, 1)]
        proxy.close_connection(1)
        assert broker.recv(1024) == b""
    finally:
        client.close()
        broker.close()
        proxy.close()
        listener.close()
