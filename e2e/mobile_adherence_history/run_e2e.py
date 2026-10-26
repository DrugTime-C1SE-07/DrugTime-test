"""PB21 local Android/API/DB runner. Exit 2 = missing prerequisite, never Pass."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import importlib.util
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[3]
BACKEND = ROOT / "backend"
MOBILE = ROOT / "frontend/mobile"
APP_ID = "vn.drugtime.adherence_e2e"
SCENARIOS = ("linked", "revoke", "cross_patient")
PRIVATE_FILES = (
    "files/adherence_fixture.json",
    "files/adherence_phase.json",
    "files/adherence_command.json",
    "files/adherence_command.json.tmp",
)


def load_helper(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


# Reuse the existing local Supabase guard, device selection and secret redaction.
redeem = load_helper("pb21_redeem_helpers", ROOT / "tests/e2e/mobile_redeem/run_e2e.py")
MissingEnvironment = redeem.MissingEnvironment


def preflight(wanted):
    # Check Android first: missing device must not seed DB, build or install.
    adb = redeem.require_tool("adb")
    result = subprocess.run(
        [adb, "devices"], capture_output=True, text=True, timeout=30, check=True
    )
    device = redeem.select_android_device(result.stdout, wanted)
    package = redeem.adb_call(adb, device, "shell", "pm", "list", "packages", APP_ID)
    if f"package:{APP_ID}" in package.stdout.splitlines():
        raise MissingEnvironment(
            "Package E2E đã tồn tại; kiểm tra dữ liệu cũ trước khi chạy"
        )
    # This also verifies DB URL matches the local Supabase CLI instance.
    return redeem.preflight(device)


def prepare_mobile(target):
    """Copy sources only; always isolate emulator and physical-device packages."""
    target = Path(target)
    for name in ("lib", "assets", "integration_test"):
        shutil.copytree(MOBILE / name, target / name)
    for name in ("pubspec.yaml", "pubspec.lock", "analysis_options.yaml", ".metadata"):
        shutil.copy2(MOBILE / name, target / name)
    shutil.copytree(
        MOBILE / "android",
        target / "android",
        ignore=shutil.ignore_patterns(
            ".gradle",
            ".cxx",
            "build",
            "key.properties",
            "*.jks",
            "*.keystore",
            "google-services.json",
        ),
    )
    build = target / "android/app/build.gradle.kts"
    original = 'applicationId = "vn.drugtime.drugtime_mobile"'
    content = build.read_text(encoding="utf-8")
    if content.count(original) != 1:
        raise MissingEnvironment("Không xác minh được applicationId để cô lập package")
    build.write_text(
        content.replace(original, f'applicationId = "{APP_ID}"'), encoding="utf-8"
    )
    return target


def write_private(adb, device, name, payload, *, atomic=False):
    if name not in PRIVATE_FILES:
        raise ValueError("Unexpected private fixture path")
    temporary = name + ".tmp" if atomic else name
    command = f"umask 077; mkdir -p files; cat > {temporary}"
    if atomic:
        command += f" && mv {temporary} {name}"
    # Bearer goes through stdin directly into app-private storage, never adb push.
    redeem.adb_call(
        adb,
        device,
        "shell",
        "run-as",
        APP_ID,
        "sh",
        "-c",
        "'" + command + "'",
        input_text=json.dumps(payload),
    )


def cleanup_device(adb, device, port, installed):
    errors = []
    actions = []
    if installed:
        try:
            listed = redeem.adb_call(
                adb, device, "shell", "pm", "list", "packages", APP_ID
            )
            installed = f"package:{APP_ID}" in listed.stdout.splitlines()
        except (OSError, subprocess.SubprocessError):
            errors.append("cannot verify package cleanup")
        actions.extend(
            [
                lambda: redeem.adb_call(
                    adb, device, "shell", "am", "force-stop", APP_ID
                ),
                lambda: redeem.adb_call(
                    adb, device, "shell", "run-as", APP_ID, "rm", "-f", *PRIVATE_FILES
                ),
                lambda: redeem.adb_call(adb, device, "uninstall", APP_ID),
            ]
            if installed
            else []
        )
    if port is not None:
        actions.append(lambda: redeem.remove_reverse(adb, device, port))
    for action in actions:
        try:
            action()
        except (OSError, subprocess.SubprocessError):
            errors.append("device cleanup failed")
    if errors:
        raise AssertionError("; ".join(errors))


def seed(engine, secret, scenario):
    import jwt
    from sqlalchemy import text

    factory_module = load_helper(
        "pb21_db_factory", BACKEND / "tests/integration/conftest.py"
    )
    current = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh"))
    # Midnight today is already due, including runs shortly after midnight.
    scheduled = current.replace(hour=0, minute=0, second=0, microsecond=0)
    medicine = "PB21 local fixture"
    with engine.begin() as db:
        make = factory_module.Factory(db)
        patient = make.user()
        family = make.family_of(patient)
        other = make.user()
        mid = make.catalog_medication(name=medicine)
        rx = db.execute(
            text(
                "INSERT INTO prescriptions (user_id,entry_source) "
                "VALUES (:p,'thu_cong') RETURNING id"
            ),
            {"p": patient},
        ).scalar_one()
        um = db.execute(
            text(
                "INSERT INTO user_medications (user_id,prescription_id,"
                "medication_id,entry_method,quantity_per_dose,doses_per_day,dosing_mode) "
                "VALUES (:p,:rx,:m,'thu_cong',1,1,'theo_gio') RETURNING id"
            ),
            {"p": patient, "rx": rx, "m": mid},
        ).scalar_one()
        sid = db.execute(
            text(
                "INSERT INTO medication_schedules (user_medication_id,intake_time,"
                "days_of_week,effective_from) VALUES (:u,'00:00',:days,:at) RETURNING id"
            ),
            {"u": um, "days": [1, 2, 3, 4, 5, 6, 7], "at": scheduled},
        ).scalar_one()
        db.execute(
            text(
                "INSERT INTO dose_logs (medication_schedule_id,scheduled_at,taken_at,status) "
                "VALUES (:s,:at,:at,'da_uong')"
            ),
            {"s": sid, "at": scheduled},
        )
    expires = datetime.now(UTC) + timedelta(hours=1)
    token = jwt.encode(
        {
            "sub": str(family),
            "aud": "drugtime",
            "typ": "mobile",
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int(expires.timestamp()),
        },
        secret,
        algorithm="HS256",
    )
    return {
        "patient_id": str(patient),
        "other_patient_id": str(other),
        "medication_name": medicine,
        "scenario": scenario,
        "session": {
            "user_id": str(family),
            "access_token": token,
            "token_type": "bearer",
            "session_type": "mobile",
            "expires_at": expires.isoformat(),
            "expires_in": 3600,
            "profile_complete": True,
        },
    }


def revoke(engine, fixture):
    from sqlalchemy import text

    with engine.begin() as db:
        result = db.execute(
            text(
                "UPDATE user_consents SET status='thu_hoi',revoked_at=now() "
                "WHERE user_id=:p AND consent_type='family_sharing' "
                "AND status='dong_y'"
            ),
            {"p": fixture["patient_id"]},
        )
        assert result.rowcount == 1, "Expected one committed consent revocation"


def disable_link(engine, fixture):
    from sqlalchemy import text

    with engine.begin() as db:
        db.execute(
            text(
                "UPDATE caregiver_links SET sharing_enabled=false "
                "WHERE patient_user_id=:p AND family_user_id=:f"
            ),
            {"p": fixture["patient_id"], "f": fixture["session"]["user_id"]},
        )


class Relay:
    """Forward only GET /adherence to local API; retain no bearer or health payload."""

    def __init__(self, upstream, bearer):
        self.upstream = upstream
        self.key = secrets.token_bytes(32)
        self.expected = self.fingerprint("Bearer " + bearer)
        self.records = []

    def fingerprint(self, value):
        return hmac.new(self.key, value.encode(), hashlib.sha256).hexdigest()

    def start(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                parsed = urlsplit(self.path)
                if parsed.path != "/adherence" or parsed.scheme or parsed.netloc:
                    self.send_error(404)
                    return
                bearer = self.headers.get("Authorization", "")
                request = urllib.request.Request(
                    f"http://127.0.0.1:{owner.upstream}{self.path}",
                    headers={"Authorization": bearer},
                )
                try:
                    with urllib.request.urlopen(request, timeout=15) as response:
                        status, payload, headers = (
                            response.status,
                            response.read(),
                            response.headers,
                        )
                except urllib.error.HTTPError as exc:
                    status, payload, headers = exc.code, exc.read(), exc.headers
                except OSError:
                    self.send_error(502)
                    return
                owner.records.append(
                    {
                        "patient": parse_qs(parsed.query).get("patient_id", [None])[0],
                        "status": status,
                        "same_bearer": hmac.compare_digest(
                            owner.fingerprint(bearer), owner.expected
                        ),
                        "no_store": headers.get("Cache-Control") == "no-store",
                    }
                )
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", headers.get("Cache-Control", ""))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self.server.server_port

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def verify_db(engine, fixture, records):
    from sqlalchemy import text

    patient, other = fixture["patient_id"], fixture["other_patient_id"]
    expected = [(patient, 200)]
    if fixture["scenario"] == "revoke":
        expected.append((patient, 403))
    elif fixture["scenario"] == "cross_patient":
        expected.append((other, 403))
    assert [(r["patient"], r["status"]) for r in records] == expected, (
        "Unexpected app HTTP sequence"
    )
    assert all(r["same_bearer"] for r in records), "Bearer changed between app requests"
    assert all(r["no_store"] for r in records if r["status"] == 200), "Missing no-store"
    with engine.connect() as db:
        rows = db.execute(
            text(
                "SELECT patient_user_id::text, result::text, data_scope::text, "
                "rejection_reason FROM access_logs WHERE actor_user_id=:f ORDER BY id"
            ),
            {"f": fixture["session"]["user_id"]},
        ).all()
    expected_audit = [
        (
            p,
            "allowed" if status == 200 else "denied",
            "lich_su_tuan_thu",
            None if status == 200 else "khong_co_quyen",
        )
        for p, status in expected
    ]
    assert [tuple(row) for row in rows] == expected_audit, (
        "DB access log differs from app requests"
    )


def run_scenario(engine, secret, backend_port, mobile, adb, device, flutter, scenario):
    print(f"START {scenario}: local fixture and isolated Android package", flush=True)
    fixture = seed(engine, secret, scenario)
    relay = Relay(backend_port, fixture["session"]["access_token"])
    port = None
    installed = False
    stop = threading.Event()
    watcher = None
    watcher_errors = []
    try:
        port = relay.start()
        redeem.adb_call(adb, device, "reverse", f"tcp:{port}", f"tcp:{port}")
        define = f"--dart-define=DRUGTIME_API_BASE_URL=http://127.0.0.1:{port}"
        for command in (
            [flutter, "pub", "get", "--offline"],
            [
                flutter,
                "build",
                "apk",
                "--debug",
                "--target",
                "integration_test/adherence_history_e2e/history_test.dart",
                define,
            ],
        ):
            build = subprocess.run(
                command,
                cwd=mobile,
                capture_output=True,
                text=True,
                timeout=900,
                check=False,
            )
            if build.returncode:
                safe = redeem.redact(
                    (build.stdout + build.stderr)[-2500:],
                    (secret, fixture["session"]["access_token"]),
                )
                raise AssertionError("Isolated Flutter build failed: " + safe)
        # Set the flag before install: cleanup also runs after a partially failed install.
        installed = True
        redeem.adb_call(
            adb,
            device,
            "install",
            "-r",
            str(mobile / "build/app/outputs/flutter-apk/app-debug.apk"),
        )
        write_private(adb, device, PRIVATE_FILES[0], fixture)

        def watch():
            try:
                deadline = time.monotonic() + 180
                while not stop.is_set():
                    read = subprocess.run(
                        [
                            adb,
                            "-s",
                            device,
                            "shell",
                            "run-as",
                            APP_ID,
                            "cat",
                            PRIVATE_FILES[1],
                        ],
                        capture_output=True,
                        text=True,
                        timeout=10,
                        check=False,
                    )
                    if read.returncode == 0:
                        if json.loads(read.stdout) != {"phase": "history_loaded"}:
                            raise AssertionError("Invalid app phase")
                        revoke(
                            engine, fixture
                        )  # transaction commits before command is written
                        write_private(
                            adb,
                            device,
                            PRIVATE_FILES[2],
                            {"command": "revocation_committed"},
                            atomic=True,
                        )
                        return
                    if time.monotonic() >= deadline:
                        raise AssertionError("App did not signal loaded history")
                    stop.wait(0.1)  # bounded protocol polling, not a timing oracle
            except Exception as exc:  # noqa: BLE001 - do not leak credential-bearing DB errors
                watcher_errors.append(
                    f"Revocation handshake failed ({type(exc).__name__})"
                )

        if scenario == "revoke":
            watcher = threading.Thread(target=watch, daemon=True)
            watcher.start()
        result = subprocess.run(
            [
                flutter,
                "test",
                "--no-pub",
                "integration_test/adherence_history_e2e/history_test.dart",
                "-d",
                device,
                define,
            ],
            cwd=mobile,
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
        stop.set()
        if watcher:
            watcher.join(timeout=15)
        if result.returncode:
            safe = redeem.redact(
                (result.stdout + result.stderr)[-2500:],
                (secret, fixture["session"]["access_token"]),
            )
            raise AssertionError(
                "Flutter route test failed: " + safe + " host=" + repr(watcher_errors)
            )
        if watcher:
            assert not watcher.is_alive() and not watcher_errors, (
                "; ".join(watcher_errors) or "Revocation watcher did not stop"
            )
        verify_db(engine, fixture, relay.records)
        print(
            f"PASS {scenario}: production route / local API / DB audit; "
            f"requests={len(relay.records)}; patient-selector UI unverified",
            flush=True,
        )
    finally:
        stop.set()
        if watcher:
            watcher.join(timeout=15)
        try:
            disable_link(engine, fixture)
        finally:
            try:
                cleanup_device(adb, device, port, installed)
            finally:
                if port is not None:
                    relay.stop()


def stop_backend(backend):
    if backend.poll() is None:
        if os.name == "nt":
            # venv python.exe can be a launcher; terminate its uvicorn child too.
            subprocess.run(
                ["taskkill", "/PID", str(backend.pid), "/T", "/F"],
                capture_output=True,
                timeout=15,
                check=True,
            )
        else:
            backend.terminate()
        try:
            backend.wait(timeout=10)
        except subprocess.TimeoutExpired:
            backend.kill()
            backend.wait(timeout=10)


def execute(status, engine, device, adb, flutter, scenarios):
    from sqlalchemy import text

    secret = secrets.token_hex(32)
    password = os.getenv("TEST_API_ROLE_PASSWORD", "postgres")
    # Verify the existing API role credentials; do not change DB roles in this runner.
    from sqlalchemy import create_engine

    api_url = engine.url.set(username="api_role", password=password)
    api_engine = create_engine(api_url, connect_args={"connect_timeout": 2})
    try:
        with api_engine.connect() as db:
            assert db.execute(text("SELECT current_user")).scalar_one() == "api_role"
    finally:
        api_engine.dispose()
    port = redeem.free_port()
    env = {
        **os.environ,
        "PYTHONPATH": str(BACKEND / "src"),
        "DATABASE_URL": api_url.render_as_string(hide_password=False),
        "SESSION_JWT_SECRET": secret,
        "SUPABASE_URL": status["API_URL"],
        "SUPABASE_ANON_KEY": status["ANON_KEY"],
        "SUPABASE_SERVICE_KEY": status["SERVICE_ROLE_KEY"],
        "SUPABASE_JWKS_URL": "",
        "SUPABASE_JWT_SECRET": secrets.token_hex(32),
    }
    with tempfile.TemporaryDirectory(prefix="drugtime-pb21-") as directory:
        mobile = prepare_mobile(Path(directory) / "mobile")
        # Backend output stays temporary; never echo raw process errors or environment.
        with (Path(directory) / "backend.log").open("w", encoding="utf-8") as output:
            backend = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "drugtime.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                ],
                cwd=BACKEND,
                env=env,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
            try:
                deadline = time.monotonic() + 30
                while True:
                    if backend.poll() is not None or time.monotonic() >= deadline:
                        raise AssertionError("Local backend did not become ready")
                    try:
                        with urllib.request.urlopen(
                            f"http://127.0.0.1:{port}/docs", timeout=1
                        ):
                            break
                    except OSError:
                        threading.Event().wait(0.1)
                for scenario in scenarios:
                    run_scenario(
                        engine, secret, port, mobile, adb, device, flutter, scenario
                    )
            finally:
                stop_backend(backend)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device")
    parser.add_argument("--scenario", action="append", choices=SCENARIOS)
    parser.add_argument("--preflight", action="store_true")
    args = parser.parse_args(argv)
    engine = None
    try:
        status, engine, device, adb, flutter = preflight(args.device)
        if args.preflight:
            print(
                "Preflight OK: Android, isolated package available, Supabase/DB local"
            )
            return 0
        sys.path.insert(0, str(BACKEND / "src"))
        execute(status, engine, device, adb, flutter, args.scenario or SCENARIOS)
        return 0
    except MissingEnvironment as exc:
        print(f"PENDING: {exc}", flush=True)
        return 2
    except AssertionError as exc:
        print("FAIL: " + redeem.redact(str(exc)), flush=True)
        return 1
    except Exception as exc:  # noqa: BLE001 - sanitize all external process/DB failures at CLI boundary
        # SQL/subprocess exceptions can contain credentials. Report only the type.
        print(f"FAIL: {type(exc).__name__}; inspect phase evidence locally", flush=True)
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
