"""Host-runner safeguards. These tests do not certify Android or patient access."""

import importlib.util
import json
import subprocess
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import Mock

import pytest

spec = importlib.util.spec_from_file_location(
    "pb21_runner", Path(__file__).with_name("run_e2e.py")
)
runner = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = runner
spec.loader.exec_module(runner)


@pytest.mark.parametrize(
    "url",
    [
        "https://project.supabase.co",
        "http://10.0.2.2:54321",
        "http://127.0.0.1.evil.test",
        "",
    ],
)
def test_remote_service_rejected__AC9(url):
    with pytest.raises(runner.MissingEnvironment):
        runner.redeem.require_local(url, "local DB")


def test_missing_device_exits_pending_before_any_mutation__AC9(monkeypatch, capsys):
    monkeypatch.setattr(runner.redeem, "require_tool", lambda _name: "adb")
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(
            a, 0, "List of devices attached\n", ""
        ),
    )
    execute = Mock()
    monkeypatch.setattr(runner, "execute", execute)
    assert runner.main([]) == 2
    execute.assert_not_called()
    assert "PENDING" in capsys.readouterr().out


def test_existing_test_package_is_never_overwritten__AC9(monkeypatch):
    monkeypatch.setattr(runner.redeem, "require_tool", lambda _name: "adb")
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(
            a, 0, "List of devices attached\nx\tdevice\n"
        ),
    )
    monkeypatch.setattr(
        runner.redeem,
        "adb_call",
        lambda *a, **kw: subprocess.CompletedProcess(
            a, 0, f"package:{runner.APP_ID}\n"
        ),
    )
    local = Mock()
    monkeypatch.setattr(runner.redeem, "preflight", local)
    with pytest.raises(runner.MissingEnvironment, match="đã tồn tại"):
        runner.preflight(None)
    local.assert_not_called()


def test_redaction_removes_jwt_password_and_known_secrets__AC9():
    value = runner.redeem.redact(
        "eyJhbGci.eyJzdWI.c2ln postgresql://api_role:pw@localhost/db api-key",
        ("api-key",),
    )
    assert "eyJ" not in value and ":pw@" not in value and "api-key" not in value


def test_fixture_uses_stdin_and_private_atomic_command__AC9(monkeypatch):
    call = Mock()
    monkeypatch.setattr(runner.redeem, "adb_call", call)
    payload = {"access_token": "secret-local-bearer"}
    runner.write_private("adb", "x", runner.PRIVATE_FILES[0], payload)
    assert "secret-local-bearer" not in repr(call.call_args.args)
    assert json.loads(call.call_args.kwargs["input_text"]) == payload
    assert "run-as" in call.call_args.args and runner.APP_ID in call.call_args.args
    runner.write_private(
        "adb",
        "x",
        runner.PRIVATE_FILES[2],
        {"command": "revocation_committed"},
        atomic=True,
    )
    assert (
        " && mv files/adherence_command.json.tmp files/adherence_command.json"
        in str(call.call_args.args)
    )
    with pytest.raises(ValueError):
        runner.write_private("adb", "x", "../../shared_prefs/session", payload)


def test_cleanup_attempts_all_steps_after_failure_and_never_targets_user_app__AC9(
    monkeypatch,
):
    calls = []

    def adb(_adb, _device, *args, **kwargs):
        calls.append(args)
        if "force-stop" in args:
            raise subprocess.CalledProcessError(1, args)
        return subprocess.CompletedProcess(args, 0, f"package:{runner.APP_ID}\n")

    reverse = Mock()
    monkeypatch.setattr(runner.redeem, "adb_call", adb)
    monkeypatch.setattr(runner.redeem, "remove_reverse", reverse)
    with pytest.raises(AssertionError, match="cleanup failed"):
        runner.cleanup_device("adb", "x", 1234, True)
    assert len(calls) == 4 and calls[-1] == ("uninstall", runner.APP_ID)
    assert "vn.drugtime.drugtime_mobile" not in repr(calls)
    reverse.assert_called_once_with("adb", "x", 1234)


def test_cleanup_accepts_already_removed_test_package__AC9(monkeypatch):
    call = Mock(return_value=subprocess.CompletedProcess([], 0, ""))
    monkeypatch.setattr(runner.redeem, "adb_call", call)
    runner.cleanup_device("adb", "x", None, True)
    call.assert_called_once_with(
        "adb", "x", "shell", "pm", "list", "packages", runner.APP_ID
    )


def test_cleanup_before_install_does_not_remove_an_app__AC9(monkeypatch):
    call = Mock()
    monkeypatch.setattr(runner.redeem, "adb_call", call)
    runner.cleanup_device("adb", "x", None, False)
    call.assert_not_called()


def test_preflight_disposes_engine_and_does_not_execute__AC9(monkeypatch):
    engine = Mock()
    monkeypatch.setattr(
        runner, "preflight", lambda _device: ({}, engine, "x", "adb", "flutter")
    )
    execute = Mock()
    monkeypatch.setattr(runner, "execute", execute)
    assert runner.main(["--preflight"]) == 0
    engine.dispose.assert_called_once()
    execute.assert_not_called()


def test_failure_disposes_engine_without_printing_secret_exception__AC9(
    monkeypatch, capsys
):
    engine = Mock()
    monkeypatch.setattr(
        runner, "preflight", lambda _device: ({}, engine, "x", "adb", "flutter")
    )
    monkeypatch.setattr(
        runner, "execute", Mock(side_effect=RuntimeError("secret-local-bearer"))
    )
    assert runner.main([]) == 1
    engine.dispose.assert_called_once()
    assert "secret-local-bearer" not in capsys.readouterr().out


def test_copy_always_changes_package_and_excludes_local_config__AC9(
    tmp_path, monkeypatch
):
    source = tmp_path / "source"
    for name in ("lib", "assets", "integration_test", "android/app"):
        (source / name).mkdir(parents=True)
    for name in ("pubspec.yaml", "pubspec.lock", "analysis_options.yaml", ".metadata"):
        (source / name).write_text("fixture", encoding="utf-8")
    gradle = source / "android/app/build.gradle.kts"
    original = 'applicationId = "vn.drugtime.drugtime_mobile"'
    gradle.write_text(original, encoding="utf-8")
    (source / "android/app/google-services.json").write_text(
        "private config", encoding="utf-8"
    )
    monkeypatch.setattr(runner, "MOBILE", source)
    target = runner.prepare_mobile(tmp_path / "isolated")
    assert runner.APP_ID in (target / "android/app/build.gradle.kts").read_text()
    assert gradle.read_text() == original
    assert not (target / "android/app/google-services.json").exists()


def test_relay_only_forwards_history_and_records_no_token_or_health__AC9():
    seen = []

    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            seen.append((self.path, self.headers.get("Authorization")))
            payload = b'{"private_health":"never retain in records"}'
            self.send_response(200)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    relay = runner.Relay(upstream.server_port, "local-bearer")
    port = relay.start()
    try:
        for token in ("local-bearer", "changed-bearer"):
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/adherence?patient_id=p",
                headers={"Authorization": "Bearer " + token},
            )
            with urllib.request.urlopen(request, timeout=3) as response:
                assert response.status == 200
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/users", timeout=3)
        assert exc.value.code == 404
        assert len(seen) == 2 and seen[0][1] == "Bearer local-bearer"
        assert [row["same_bearer"] for row in relay.records] == [True, False]
        assert all(row["no_store"] for row in relay.records)
        assert "bearer" not in json.dumps(relay.records).replace("same_bearer", "")
        assert "private_health" not in json.dumps(relay.records)
    finally:
        relay.stop()
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=3)


def test_unknown_scenario_rejected_before_preflight__AC9(monkeypatch):
    preflight = Mock()
    monkeypatch.setattr(runner, "preflight", preflight)
    with pytest.raises(SystemExit) as exc:
        runner.main(["--scenario", "unknown"])
    assert exc.value.code == 2
    preflight.assert_not_called()


@pytest.mark.parametrize("failure", ["build", "app"])
def test_scenario_failure_still_cleans_device_reverse_and_link__AC9(
    monkeypatch, tmp_path, failure
):
    fixture = {
        "patient_id": "p",
        "other_patient_id": "q",
        "scenario": "linked",
        "session": {"user_id": "f", "access_token": "secret"},
    }
    monkeypatch.setattr(runner, "seed", lambda *args: fixture)
    relay = Mock()
    relay.start.return_value = 1234
    monkeypatch.setattr(runner, "Relay", lambda *args: relay)
    monkeypatch.setattr(runner.redeem, "adb_call", Mock())
    monkeypatch.setattr(runner, "write_private", Mock())
    cleanup = Mock()
    disable = Mock()
    monkeypatch.setattr(runner, "cleanup_device", cleanup)
    monkeypatch.setattr(runner, "disable_link", disable)
    results = iter([1] if failure == "build" else [0, 0, 1])
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a, next(results), "", ""),
    )
    engine = Mock()
    with pytest.raises(AssertionError):
        runner.run_scenario(
            engine, "s", 9000, tmp_path, "adb", "x", "flutter", "linked"
        )
    cleanup.assert_called_once_with("adb", "x", 1234, failure == "app")
    disable.assert_called_once_with(engine, fixture)
    relay.stop.assert_called_once()


def test_windows_server_cleanup_terminates_only_spawned_process_tree__AC9(monkeypatch):
    backend = Mock(pid=12345)
    backend.poll.return_value = None
    kill = Mock()
    monkeypatch.setattr(runner.os, "name", "nt")
    monkeypatch.setattr(runner.subprocess, "run", kill)
    runner.stop_backend(backend)
    assert kill.call_args.args[0] == ["taskkill", "/PID", "12345", "/T", "/F"]
    assert kill.call_args.kwargs["check"] is True
    backend.wait.assert_called_once_with(timeout=10)
    backend.terminate.assert_not_called()
