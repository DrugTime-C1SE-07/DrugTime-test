"""Owned local infra, real prefork processes and bounded evidence collection."""

import hashlib
import importlib.metadata
import json
import os
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import quote

from .amqp_proxy import Proxy
from .dav_server import DavServer
from .environment import (
    EVIDENCE,
    ROOT,
    WORK,
    Deadline,
    PrerequisiteError,
    Redactor,
    isolated_env,
    preflight,
    save_json,
    verify_owner,
)


def wait_for(check, deadline, description):
    event = threading.Event()
    try:
        while True:
            deadline.remaining()
            value = check()
            deadline.remaining()
            if value:
                return value
            event.wait(deadline.remaining(0.05))
    except TimeoutError:
        raise AssertionError(f"Timed out waiting for {description}") from None


def wait_for_management(api, deadline):
    import httpx
    def ready():
        try:
            return api("GET", "/overview")
        except httpx.TransportError:
            return None
    return wait_for(ready, deadline, "RabbitMQ Management API ready")


class Process:
    def __init__(self, command, *, env, cwd, redact, log, deadline):
        self.process = subprocess.Popen(command, cwd=cwd, env=env, start_new_session=True,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        text=True, bufsize=1, umask=0o077)
        self.pid = self.process.pid
        self.pgid = os.getpgid(self.pid)
        self.started = time.monotonic()
        self.lines = []
        self.log = log
        self.redact = redact
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()
        self.deadline = deadline

    def _read(self):
        with self.log.open("w", encoding="utf-8") as sink:
            for line in self.process.stdout:
                clean = self.redact(line)
                self.lines.append(clean)
                sink.write(clean)
                sink.flush()

    def text(self):
        return "".join(self.lines)

    def stop(self, *, kill=False, deadline=None):
        deadline = deadline or Deadline(5)
        # The unreaped Popen child owns its PID; never look up/kill a process by name.
        if self.process.returncode is None:
            # Do not poll/reap first: an exited parent may still have live prefork children.
            try:
                current_group = os.getpgid(self.pid)
            except ProcessLookupError:
                current_group = None
            if self.pgid != self.pid or current_group not in (None, self.pgid):
                raise PrerequisiteError("Process group ownership changed")
            if current_group is not None:
                try:
                    os.killpg(self.pgid, signal.SIGKILL if kill else signal.SIGTERM)
                except ProcessLookupError:
                    pass
            try:
                self.process.wait(timeout=deadline.remaining(3))
            except subprocess.TimeoutExpired:
                os.killpg(self.pgid, signal.SIGKILL)
                self.process.wait(timeout=deadline.remaining(2))
        self.thread.join(deadline.remaining(2))
        if self.thread.is_alive():
            raise RuntimeError("Process log pipe remained open after shutdown")


class Infrastructure:
    def __init__(self):
        self.deadline = Deadline(1200)
        self.endpoint = preflight()
        self.run_id = uuid.uuid4().hex[:12]
        self.project = f"catalog-e2e-{self.run_id}"
        self.db_name = f"drugtime_catalog_e2e_{self.run_id}"
        self.password = secrets.token_hex(24)
        Redactor.runtime_secrets.add(self.password)
        self.redact = Redactor([self.password])
        self.scratch = WORK / "scratch" / self.run_id
        self.scratch.mkdir(parents=True, mode=0o700)
        if self.scratch.stat().st_mode & 0o077:
            # DrvFS without metadata ignores chmod. Keep the planned workspace
            # path, backed by a private Linux directory rather than a public env file.
            self.scratch.rmdir()  # Newly created, still empty, exact owned path.
            backing = Path(tempfile.mkdtemp(prefix=f"drugtime-catalog-e2e-{self.run_id}-",
                                             dir="/tmp"))
            self.scratch.symlink_to(backing, target_is_directory=True)
        if self.scratch.stat().st_mode & 0o077:
            raise PrerequisiteError("Runtime directory cannot enforce mode 0700")
        self.empty = self.scratch / "empty.env"
        self.empty.touch(mode=0o600)
        if self.empty.stat().st_mode & 0o077:
            raise PrerequisiteError("Runtime env file cannot enforce mode 0600")
        self.compose_env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG") if k in os.environ}
        self.compose_env.update({"CATALOG_DB": self.db_name, "CATALOG_PASSWORD": self.password,
                                 "CATALOG_RUN_ID": self.run_id})
        self.manifest = {"schema_version": 1, "run_id": self.run_id,
                         "project_name": self.project, "docker_endpoint": self.endpoint,
                         "database_name": self.db_name, "resources": [], "processes": [],
                         "cases": [], "migrations": [], "cleanup": []}
        self.created = False
        self.management = None
        self.tainted = False
        self.pg_port = self.amqp_port = self.http_port = None
        EVIDENCE.mkdir(parents=True, exist_ok=True)

    def write_manifest(self):
        save_json(self.scratch / "manifest.json", self.manifest, self.redact)
        os.chmod(self.scratch / "manifest.json", 0o600)

    def command(self, args, *, timeout=5, check=True, env=None):
        try:
            result = subprocess.run(args, env=env or self.compose_env, capture_output=True,
                                    text=True, timeout=self.deadline.remaining(timeout))
        except subprocess.SubprocessError as exc:
            raise PrerequisiteError(f"Command failed: {type(exc).__name__}") from None
        if check and result.returncode:
            raise PrerequisiteError(self.redact(result.stderr or result.stdout)[:2000])
        return result.stdout.strip()

    def compose(self, *args, timeout=5):
        return self.command(["docker", "compose", "--project-name", self.project,
                             "--env-file", str(self.empty), "--file",
                             str(Path(__file__).with_name("compose.yaml")), *args], timeout=timeout)

    def discover(self):
        resources = []
        for kind, args in (("container", ["ps", "-aq"]), ("volume", ["volume", "ls", "-q"]),
                           ("network", ["network", "ls", "-q"])):
            ids = self.command(["docker", *args, "--filter",
                                f"label=com.docker.compose.project={self.project}"]).splitlines()
            for resource_id in ids:
                obj = json.loads(self.command(["docker", kind, "inspect", resource_id]))[0]
                labels = obj.get("Config", {}).get("Labels", {}) if kind == "container" \
                    else obj.get("Labels", {})
                verify_owner(self.manifest, db_name=self.db_name, marker=self.run_id, labels=labels)
                resources.append({"kind": kind, "id": resource_id, "labels": labels})
        self.manifest["resources"] = resources
        self.write_manifest()
        return resources

    def start(self):
        import httpx
        startup = Deadline(180, parent=self.deadline)
        try:
            if self.discover():
                raise PrerequisiteError("Compose project already exists")
            self.created = True  # discover in finally also records partial startup resources
            try:
                self.compose("up", "-d", "--wait", "--wait-timeout", "170",
                             timeout=startup.remaining())
            finally:
                self.discover()
            self.pg_port = int(self.compose("port", "postgres", "5432").rsplit(":", 1)[1])
            self.amqp_port = int(self.compose("port", "rabbit", "5672").rsplit(":", 1)[1])
            self.http_port = int(self.compose("port", "rabbit", "15672").rsplit(":", 1)[1])
            self.manifest["ports"] = {"postgresql": self.pg_port, "amqp": self.amqp_port,
                                      "rabbitmq_management": self.http_port}
            self.management = httpx.Client(base_url=f"http://127.0.0.1:{self.http_port}",
                                           auth=("catalog_e2e", self.password), timeout=5,
                                           trust_env=False)
            overview = wait_for_management(self.api, startup)
            self.sql("CREATE SCHEMA catalog_e2e_owner; "
                     "CREATE TABLE catalog_e2e_owner.marker (run_id text NOT NULL)")
            self.sql("INSERT INTO catalog_e2e_owner.marker VALUES (%s)", (self.run_id,))
            versions = {k: importlib.metadata.version(k)
                        for k in ("celery", "kombu", "amqp", "psycopg", "sqlalchemy", "pytest")}
            versions["python"] = sys.version
            versions["postgresql"] = self.sql("SELECT version() AS version")[0]["version"]
            versions["rabbitmq"] = overview["rabbitmq_version"]
            versions["images"] = json.loads(self.compose("images", "--format", "json"))
            # Record immutable image IDs and repository digests from the actual daemon.
            versions["image_details"] = [json.loads(self.command(
                ["docker", "image", "inspect", image["ID"]]))[0].get("RepoDigests", [])
                for image in versions["images"]]
            self.manifest["runtime_versions"] = versions
            self.write_manifest()
            return self
        except BaseException as exc:
            self.close()
            if isinstance(exc, Exception):
                reason = self.redact(f"Infrastructure bootstrap failed: {exc}")
                raise PrerequisiteError(reason) from None
            raise

    def sql(self, query, params=()):
        import psycopg
        from psycopg.rows import dict_row
        with psycopg.connect(host="127.0.0.1", port=self.pg_port, dbname=self.db_name,
                             user="postgres", password=self.password, connect_timeout=5,
                             options="-c statement_timeout=5000 -c lock_timeout=5000",
                             row_factory=dict_row) as connection:
            cursor = connection.execute(query, params if params else None)
            return cursor.fetchall() if cursor.description else []

    def guard_db(self):
        row = self.sql("SELECT current_database() AS db, run_id FROM catalog_e2e_owner.marker")
        if len(row) != 1:
            raise PrerequisiteError("Missing unique database owner marker")
        for resource in self.discover():
            verify_owner(self.manifest, db_name=row[0]["db"], marker=row[0]["run_id"],
                         labels=resource["labels"])

    def reset(self):
        self.guard_db()
        # Supabase's initial dump references these platform roles. No grants are changed.
        self.sql("DO $$ BEGIN "
                 "IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='anon') THEN "
                 "CREATE ROLE anon NOLOGIN; END IF; "
                 "IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='authenticated') THEN "
                 "CREATE ROLE authenticated NOLOGIN; END IF; "
                 "IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='service_role') THEN "
                 "CREATE ROLE service_role NOLOGIN; END IF; END $$")
        self.sql("DROP SCHEMA public CASCADE; CREATE SCHEMA public; "
                 "CREATE SCHEMA IF NOT EXISTS extensions")
        migrations = []
        for migration in sorted((ROOT / "backend/supabase/migrations").glob("*.sql")):
            content = migration.read_bytes()
            self.sql(content.decode("utf-8"))
            migrations.append({"name": migration.name,
                               "sha256": hashlib.sha256(content).hexdigest()})
        if not migrations:
            raise PrerequisiteError("No production migrations found")
        # Password is generated hex, not user input; never include it in an exception.
        try:
            self.sql(f"ALTER ROLE worker_role PASSWORD '{self.password}'")
        except Exception:
            raise PrerequisiteError("Cannot configure local worker_role password") from None
        self.manifest["migrations"] = migrations
        self.write_manifest()

    def api(self, method, path, payload=None):
        response = self.management.request(method, "/api" + path, json=payload,
                                           timeout=self.deadline.remaining(5))
        response.raise_for_status()
        return response.json() if response.content else None

    def close(self):
        errors = []
        cleanup = Deadline(30)
        if self.management:
            self.management.close()
        if self.created:
            # Re-discover by exact unique project label, then verify every resource.
            original_deadline = self.deadline
            self.deadline = cleanup
            try:
                resources = self.discover()
                for kind, command in (("container", ["rm", "--force"]),
                                       ("volume", ["volume", "rm"]),
                                       ("network", ["network", "rm"])):
                    for resource in resources:
                        if resource["kind"] != kind:
                            continue
                        self.command(["docker", *command, resource["id"]],
                                     timeout=cleanup.remaining(5))
                        self.manifest["cleanup"].append(resource)
                if self.discover():
                    raise RuntimeError("Owned Docker resources remain after cleanup")
            except Exception as exc:
                errors.append(self.redact(exc))
            finally:
                self.deadline = original_deadline
        save_json(EVIDENCE / f"test-infra-{self.run_id}.json", self.manifest, self.redact)
        if errors:
            raise RuntimeError("Cleanup failed: " + "; ".join(errors))
        self.created = False


class Case:
    def __init__(self, infra, name):
        self.infra = infra
        self.deadline = Deadline(60, parent=infra.deadline)
        self.name = name
        self.id = uuid.uuid4().hex[:12]
        self.vhost = f"{infra.project}-{self.id}"
        self.directory = infra.scratch / self.id
        self.directory.mkdir(mode=0o700)
        self.processes = []
        self.worker = self.beat = None
        self.proxy = self.dav = self.producer = None
        self.vhost_created = False

    @property
    def observer(self):
        return self.proxy.observer

    def start(self):
        from celery import Celery
        if self.infra.tainted:
            raise PrerequisiteError("Previous case cleanup failed; refusing database reset")
        try:
            self.infra.reset()
        except Exception as exc:
            reason = self.infra.redact(f"Migration bootstrap failed: {exc}")
            raise PrerequisiteError(reason) from None
        self.infra.api("PUT", "/vhosts/" + quote(self.vhost, safe=""), {})
        self.vhost_created = True
        self.infra.manifest["cases"].append({"id": self.id, "name": self.name,
                                             "vhost": self.vhost})
        self.infra.write_manifest()
        self.infra.api("PUT", "/permissions/" + quote(self.vhost, safe="") + "/catalog_e2e",
                       {"configure": ".*", "write": ".*", "read": ".*"})
        self.proxy = Proxy(self.infra.amqp_port)
        self.dav = DavServer()
        broker = (f"amqp://catalog_e2e:{self.infra.password}@127.0.0.1:"
                  f"{self.proxy.listen_port}/{self.vhost}")
        db = (f"postgresql+psycopg://worker_role:{self.infra.password}@127.0.0.1:"
              f"{self.infra.pg_port}/{self.infra.db_name}?application_name={self.infra.run_id}")
        self.env = isolated_env(db=db, broker=broker, dav=self.dav.url,
                                empty_file=self.infra.empty, run_id=self.infra.run_id,
                                secrets=self.infra.password)
        self.producer = Celery("catalog-e2e-producer", broker=broker)
        self.producer.conf.update(task_ignore_result=True, broker_connection_timeout=5,
                                  broker_transport_options={"confirm_publish": True})
        self.deadline = Deadline(60, parent=self.infra.deadline)
        return self

    def spawn(self, kind, args):
        # Report non-secret config from the SAME process before starting the real CLI.
        # This uses the unchanged production app and does not add a task or patch Celery.
        entry = (
            "import json,sys; from drugtime.worker import celery_app; "
            "print('CATALOG_E2E_RUNTIME '+json.dumps({"
            "'timezone':celery_app.conf.timezone,"
            "'result_backend':celery_app.conf.result_backend,"
            "'prefetch':celery_app.conf.worker_prefetch_multiplier}),flush=True); "
            "raise SystemExit(celery_app.start(sys.argv[1:]))"
        )
        process = Process([sys.executable, "-c", entry, *args],
                          env=self.env, cwd=self.directory, redact=self.infra.redact,
                          log=EVIDENCE / f"test-{self.id}-{kind}-{len(self.processes)}.log",
                          deadline=self.infra.deadline)
        self.processes.append(process)
        self.infra.manifest["processes"].append({"pid": process.pid, "pgid": process.pgid,
                                                 "started": process.started, "run_id": self.id})
        self.infra.write_manifest()
        return process

    def start_worker(self, concurrency=1):
        self.worker = self.spawn("worker", ["worker", "--pool=prefork", "--loglevel=INFO",
                                            f"--concurrency={concurrency}",
                                            "--without-gossip", "--without-mingle"])
        wait_for(lambda: " ready." in self.worker.text(),
                 Deadline(30, parent=self.infra.deadline), "prefork worker ready")
        return self.worker

    def start_beat(self):
        self.beat = self.spawn("beat", ["beat", "--loglevel=INFO", "--schedule",
                                        str(self.directory / "beat.db"), "--pidfile",
                                        str(self.directory / "beat.pid")])

    def stop_beat(self):
        if self.beat:
            self.beat.stop()
            self.beat = None

    def publish(self, *, task="catalog.sync", source="dav_drugbank", task_id=None):
        task_id = task_id or str(uuid.uuid4())
        kwargs = {"source": source, "triggered_by": "scheduler"} if task == "catalog.sync" else {}
        with self.producer.connection_for_write() as connection:
            connection.ensure_connection(max_retries=0)
            connection.transport_options["read_timeout"] = 5
            self.producer.send_task(task, kwargs=kwargs, task_id=task_id,
                                    connection=connection, retry=False)
        return task_id

    def wait(self, check, description, seconds=60):
        return wait_for(check, Deadline(seconds, parent=self.deadline), description)

    def task_finished(self, task_id):
        text = self.worker.text()
        return any(f"[{task_id}] succeeded" in line or f"[{task_id}] raised unexpected" in line
                   for line in text.splitlines())

    def complete_tick(self, *, beat=True):
        before = len(self.observer.events("publish", "runtime.tick"))
        if beat:
            self.start_beat()
        else:
            self.publish(task="runtime.tick")
        ticks = self.wait(lambda: self.observer.events("publish", "runtime.tick")[before:],
                          "tick published", 20)
        task_id = ticks[0]["id"]
        self.wait(lambda: self.task_finished(task_id), "tick completed", 20)
        self.stop_beat()
        # Every extra tick emitted while Beat stopped must also finish before assertions.
        for tick in self.observer.events("publish", "runtime.tick")[before:]:
            self.wait(lambda tick=tick: self.task_finished(tick["id"]),
                      "trailing tick completed", 20)
        assert "tick step catalog_sync_if_due failed" not in self.worker.text()
        return task_id

    def runs(self):
        return self.infra.sql("SELECT * FROM catalog_sync_runs ORDER BY id")

    def medications(self):
        return self.infra.sql("SELECT * FROM medications ORDER BY source_ref")

    def success(self, count=1):
        runs = self.runs()
        complete = len(runs) == count and all(r["status"] == "thanh_cong" for r in runs)
        return runs if complete else None

    def drained(self):
        queues = self.infra.api("GET", "/queues/" + quote(self.vhost, safe=""))
        return bool(queues) and all(q.get("messages", -1) == 0 for q in queues)

    def unacked(self):
        queues = self.infra.api("GET", "/queues/" + quote(self.vhost, safe=""))
        return sum(q.get("messages_unacknowledged", 0) for q in queues)

    def assert_snapshot(self, *, count=1):
        rows = self.medications()
        assert len(rows) == 10
        for index, row in enumerate(rows, 1):
            assert row["source_ref"] == f"E2E-{index:03}"
            assert row["source"] == "dav_drugbank"
            assert row["name"] == f"Thuốc thử {index}"
            assert row["raw_active_ingredient"] == "Paracetamol"
            assert row["strength_text"] == "500 mg"
            assert row["dosage_form"] == "Viên nén"
            assert row["manufacturer_name"] == "Công ty thử"
            assert row["manufacturer_country"] == "Việt Nam"
            assert row["packaging_spec"] == "Hộp 10 viên"
            assert (row["deleted_at"] is not None) == (index == 10)
        runs = self.success(count)
        assert runs is not None
        for index, run in enumerate(runs):
            assert run["finished_at"] and run["triggered_by"] == "scheduler"
            assert run["records_inserted"] == (10 if index == 0 else 0)
            assert run["records_updated"] == (0 if index == 0 else 10)
            assert run["records_failed"] == 0
        return {r["source_ref"]: r["id"] for r in rows}

    def kill_worker_for(self, task_id):
        delivered = self.observer.events("deliver", task_id=task_id)
        assert delivered, "Crash boundary lacks delivery proof"
        self.worker.stop(kill=True)
        for event in delivered:
            self.proxy.close_connection(event["connection"])

    def snapshot(self):
        value = {"case": self.name, "id": self.id,
                 "ledger": self.observer.events() if self.proxy else [],
                 "proxy_errors": self.proxy.errors if self.proxy else [],
                 "http": self.dav.requests if self.dav else []}
        try:
            value.update({"runs": self.runs(), "medications": self.medications()})
            value["worker_connections"] = self.infra.sql(
                "SELECT usename,application_name FROM pg_stat_activity WHERE application_name=%s",
                (self.infra.run_id,))
            if self.vhost_created:
                value["queues"] = [{k: q.get(k) for k in
                                    ("name", "durable", "messages", "messages_unacknowledged")}
                                   for q in self.infra.api(
                                       "GET", "/queues/" + quote(self.vhost, safe=""))]
        except Exception as exc:
            value["database_snapshot_error"] = self.infra.redact(str(exc))
        save_json(EVIDENCE / f"test-case-{self.id}.json", value, self.infra.redact)

    def close(self):
        errors = []
        cleanup = Deadline(30)
        for process in reversed(self.processes):
            try:
                process.stop(kill=True, deadline=cleanup)
            except Exception as exc:
                errors.append(self.infra.redact(exc))
        if self.producer:
            self.producer.close()
        if self.proxy:
            self.proxy.close()
        if self.dav:
            self.dav.close()
        if self.vhost_created:
            if self.vhost != f"{self.infra.project}-{self.id}":
                errors.append("Vhost ownership mismatch")
            else:
                try:
                    self.infra.api("DELETE", "/vhosts/" + quote(self.vhost, safe=""))
                except Exception as exc:
                    errors.append(self.infra.redact(exc))
        if errors:
            self.infra.tainted = True
            raise RuntimeError("Case cleanup failed: " + "; ".join(errors))
