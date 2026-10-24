"""Runner safeguards that do not require DB, backend, or Android."""

import json
import subprocess
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import ClassVar

import pytest
import run_e2e


def test_redaction_removes_token_code_and_database_password():
    output = run_e2e.redact(
        "token=eyJhbGci.eyJzdWI.c2ln code=K7M2Q9XAR4 "
        "postgresql+psycopg://api_role:secret@127.0.0.1/db",
        ("secret",),
    )
    assert "eyJ" not in output
    assert "K7M2Q9XAR4" not in output
    assert "secret" not in output


@pytest.mark.parametrize(
    "url", ["https://project.supabase.co", "http://10.0.2.2:54321", "", "http://evil.test"]
)
def test_preflight_rejects_nonlocal_service(url):
    with pytest.raises(run_e2e.MissingEnvironment):
        run_e2e.require_local(url, "DB")


@pytest.mark.parametrize("url", ["http://localhost:54321", "http://127.0.0.1:54321"])
def test_preflight_accepts_loopback(url):
    run_e2e.require_local(url, "DB")


def test_online_physical_device_is_selected_when_emulator_is_offline():
    listed = "List of devices attached\nphysical-1\tdevice\nemulator-5554\toffline\n"
    assert run_e2e.select_android_device(listed, None) == "physical-1"
    assert run_e2e.select_android_device(
        "List of devices attached\nemulator-5554\tdevice\n", None
    ) == "emulator-5554"


def test_multiple_physical_devices_require_explicit_choice():
    listed = "List of devices attached\nphysical-1\tdevice\nphysical-2\tdevice\n"
    with pytest.raises(run_e2e.MissingEnvironment):
        run_e2e.select_android_device(listed, None)
    assert run_e2e.select_android_device(listed, "physical-2") == "physical-2"


def test_missing_environment_returns_two_before_mutation(monkeypatch):
    created = []

    def missing(_device):
        raise run_e2e.MissingEnvironment("emulator chưa online")

    monkeypatch.setattr(run_e2e, "preflight", missing)
    monkeypatch.setattr(run_e2e, "create_fixture", lambda *_args: created.append(True))
    assert run_e2e.main(["--phase", "direct_pending"]) == 2
    assert created == []


def test_unknown_phase_exits_two_before_preflight(monkeypatch):
    called = []
    monkeypatch.setattr(run_e2e, "preflight", lambda _device: called.append(True))
    with pytest.raises(SystemExit) as exc:
        run_e2e.main(["--phase", "unknown"])
    assert exc.value.code == 2
    assert called == []


def test_response_lost_forwards_upstream_once_then_drops_reply():
    class Upstream(BaseHTTPRequestHandler):
        calls: ClassVar[list[dict]] = []

        def log_message(self, *_args):
            pass

        def do_POST(self):
            self.calls.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            status = 201 if len(self.calls) == 1 else 200
            payload = json.dumps({"id": "row", "status": "pending"}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    relay = run_e2e.Relay(server.server_port, "response_lost")
    port = relay.start()
    body = json.dumps({"code": "K7M2Q9XAR4", "client_uuid": "same"}).encode()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/family/link-requests",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises((urllib.error.URLError, ConnectionError)):
            urllib.request.urlopen(request, timeout=3)
        with urllib.request.urlopen(request, timeout=3) as response:
            assert response.status == 200
        assert [entry["status"] for entry in relay.records] == [201, 200]
        assert len(Upstream.calls) == 2
        assert Upstream.calls[0]["client_uuid"] == Upstream.calls[1]["client_uuid"]
    finally:
        relay.stop()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_backup_refuses_to_overwrite_recovery_file(monkeypatch):
    calls = []
    monkeypatch.setattr(
        run_e2e.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0),
    )
    monkeypatch.setattr(run_e2e, "adb_call", lambda *_args: calls.append(True))
    with pytest.raises(AssertionError, match="backup phiên E2E cũ"):
        run_e2e.backup_session("adb", "device")
    assert calls == []


def test_existing_session_is_copied_and_restored_inside_app(monkeypatch):
    checks = iter([1, 0])  # no stale backup, existing secure storage
    calls = []
    monkeypatch.setattr(
        run_e2e.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], next(checks)),
    )
    monkeypatch.setattr(run_e2e, "adb_call", lambda *_args: calls.append(_args[2:]))
    assert run_e2e.backup_session("adb", "device") is True
    run_e2e.restore_session("adb", "device", True)
    assert calls == [
        (
            "shell", "run-as", run_e2e.APP_ID, "cp",
            "shared_prefs/FlutterSecureStorage.xml", "files/redeem_e2e_session_backup.xml",
        ),
        (
            "shell", "run-as", run_e2e.APP_ID, "cp",
            "files/redeem_e2e_session_backup.xml", "shared_prefs/FlutterSecureStorage.xml",
        ),
        (
            "shell", "run-as", run_e2e.APP_ID, "rm", "-f",
            "files/redeem_e2e_session_backup.xml",
        ),
    ]


@pytest.mark.parametrize("remaining", ["", "device tcp:1234 tcp:1234\n"])
def test_reverse_cleanup_accepts_only_an_absent_mapping(monkeypatch, remaining):
    def adb_call(_adb, _device, *args):
        if args == ("reverse", "--remove", "tcp:1234"):
            raise subprocess.CalledProcessError(1, args)
        assert args == ("reverse", "--list")
        return subprocess.CompletedProcess(args, 0, stdout=remaining)

    monkeypatch.setattr(run_e2e, "adb_call", adb_call)
    if remaining:
        with pytest.raises(subprocess.CalledProcessError):
            run_e2e.remove_reverse("adb", "device", 1234)
    else:
        run_e2e.remove_reverse("adb", "device", 1234)
