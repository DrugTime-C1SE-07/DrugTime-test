"""Android redeem E2E against the local FastAPI and PostgreSQL instance.

Run from the superproject root with backend/.venv/Scripts/python.exe. Exit 0 means
the selected phases passed; 1 means an assertion/app failure; 2 means preflight
failed. The scanner callback phase does not exercise the physical camera.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[3]
BACKEND = ROOT / "backend"
MOBILE = ROOT / "frontend" / "mobile"
APP_ID = "vn.drugtime.drugtime_mobile"
ISOLATED_APP_ID = "vn.drugtime.redeem_e2e"
DEVICE_FIXTURE = "files/redeem_e2e_fixture.json"
PHASES = {
    "direct_pending": "manual_code_test.dart",
    "double_tap": "manual_code_test.dart",
    "response_lost": "manual_code_test.dart",
    "unauthorized": "manual_code_test.dart",
    "qr_valid": "qr_payload_test.dart",
    "qr_invalid": "qr_payload_test.dart",
}
LOCAL = {"localhost", "127.0.0.1"}
JWT = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
PASSWORD = re.compile(r"(://[^:/@\s]+:)[^@\s]+@")
CODE = re.compile(r"\b[A-HJKMNP-Z2-9]{10}\b")


class MissingEnvironment(Exception):
    pass


def redact(value: str, known: tuple[str, ...] = ()) -> str:
    for secret in known:
        if secret:
            value = value.replace(secret, "***")
    return CODE.sub("***", PASSWORD.sub(r"\1***@", JWT.sub("***", value)))


def require_local(value: str, label: str) -> None:
    if urlsplit(value).hostname not in LOCAL:
        raise MissingEnvironment(f"{label} phải trỏ tới localhost")


def require_tool(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise MissingEnvironment(f"Thiếu {name} trong PATH")
    return found


def select_android_device(output: str, wanted: str | None) -> str:
    online = {
        parts[0]
        for line in output.splitlines()[1:]
        if (parts := line.split()) and len(parts) == 2 and parts[1] == "device"
    }
    if wanted:
        if wanted not in online:
            raise MissingEnvironment("Thiết bị Android được chọn chưa online")
        return wanted
    emulators = sorted(device for device in online if device.startswith("emulator-"))
    if emulators:
        return emulators[0]
    if len(online) == 1:
        return next(iter(online))
    if not online:
        raise MissingEnvironment("Không có thiết bị Android online (adb devices)")
    raise MissingEnvironment("Có nhiều thiết bị Android; dùng --device để chọn")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def status_env(output: str) -> dict[str, str]:
    return {
        key.strip(): value.strip().strip('"')
        for line in output.splitlines()
        if "=" in line
        for key, value in [line.split("=", 1)]
    }


def preflight(device: str | None):
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import ArgumentError, SQLAlchemyError

    db_url = os.getenv(
        "TEST_ADMIN_DATABASE_URL",
        "postgresql+psycopg://postgres:postgres@127.0.0.1:54322/postgres",
    )
    try:
        requested_db = make_url(db_url)
    except ArgumentError:
        raise MissingEnvironment("TEST_ADMIN_DATABASE_URL không hợp lệ") from None
    if requested_db.host not in LOCAL:
        raise MissingEnvironment("TEST_ADMIN_DATABASE_URL phải trỏ tới localhost")
    cli = BACKEND.parent / "node_modules" / ".bin" / "supabase.cmd"
    if not cli.is_file():
        raise MissingEnvironment("Thiếu Supabase CLI local ở node_modules/.bin")
    try:
        result = subprocess.run(
            [str(cli), "status", "-o", "env"],
            cwd=BACKEND,
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        status = status_env(result.stdout)
        require_local(status.get("API_URL", ""), "Supabase API_URL")
        require_local(status.get("DB_URL", ""), "Supabase DB_URL")
        try:
            local_db = make_url(status["DB_URL"])
        except ArgumentError:
            raise MissingEnvironment("Supabase DB_URL không hợp lệ") from None
        if (local_db.port, local_db.database) != (requested_db.port, requested_db.database):
            raise MissingEnvironment("TEST_ADMIN_DATABASE_URL khác Supabase DB local")
        if not status.get("ANON_KEY") or not status.get("SERVICE_ROLE_KEY"):
            raise MissingEnvironment("Supabase local thiếu khóa dịch vụ")
        engine = create_engine(db_url, connect_args={"connect_timeout": 2})
        with engine.connect() as connection:
            tables = connection.execute(
                text("SELECT to_regclass('public.users'), to_regclass('public.caregiver_links')")
            ).one()
            if None in tables:
                raise MissingEnvironment("DB local chưa có migration; chạy supabase db reset")
    except (OSError, subprocess.SubprocessError) as exc:
        raise MissingEnvironment(f"Không đọc được Supabase local ({type(exc).__name__})") from None
    except MissingEnvironment:
        raise
    except SQLAlchemyError as exc:
        raise MissingEnvironment(f"Không kết nối được DB local ({type(exc).__name__})") from None

    try:
        adb = require_tool("adb")
        flutter = require_tool("flutter")
        try:
            devices = subprocess.run(
                [adb, "devices"], capture_output=True, text=True, timeout=30, check=False
            )
        except (OSError, subprocess.SubprocessError):
            raise MissingEnvironment("Không đọc được adb devices") from None
        selected = select_android_device(devices.stdout, device)
        return status, engine, selected, adb, flutter
    except MissingEnvironment:
        engine.dispose()
        raise


def request_json(url: str, token: str) -> tuple[int, dict]:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


class Relay:
    """Proxy only to the local backend; retain request evidence in memory."""

    def __init__(self, upstream: int, scenario: str):
        self.records: list[dict] = []
        self.upstream = upstream
        self.scenario = scenario
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.lock = threading.Lock()

    def start(self) -> int:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                self.forward()

            def do_POST(self):
                self.forward()

            def forward(self):
                size = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(size) if size else None
                target = f"http://127.0.0.1:{owner.upstream}{self.path}"
                headers = {
                    key: value
                    for key, value in self.headers.items()
                    if key.lower() in {"authorization", "content-type"}
                }
                if (
                    owner.scenario == "unauthorized"
                    and self.command == "POST"
                    and self.path == "/family/link-requests"
                ):
                    headers["Authorization"] = "Bearer invalid-local-session"
                upstream_request = urllib.request.Request(
                    target, data=body, headers=headers, method=self.command
                )
                try:
                    with urllib.request.urlopen(upstream_request, timeout=30) as response:
                        status, payload = response.status, response.read()
                        response_type = response.headers.get("Content-Type", "application/json")
                except urllib.error.HTTPError as exc:
                    status, payload = exc.code, exc.read()
                    response_type = exc.headers.get("Content-Type", "application/json")
                is_redeem = self.command == "POST" and self.path == "/family/link-requests"
                if is_redeem:
                    sent = json.loads(body or b"{}")
                    with owner.lock:
                        owner.records.append(
                            {
                                "status": status,
                                "uuid": sent.get("client_uuid"),
                                "code": sent.get("code"),
                                "response": json.loads(payload),
                            }
                        )
                        ordinal = len(owner.records)
                    if owner.scenario == "response_lost" and ordinal == 1:
                        self.close_connection = True
                        return
                    if owner.scenario == "double_tap":
                        time.sleep(0.4)
                self.send_response(status)
                self.send_header("Content-Type", response_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", free_port()), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self.server.server_port

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        if self.thread:
            self.thread.join(timeout=5)


def create_fixture(engine, secret: str, backend_port: int, scenario: str):
    import jwt
    from sqlalchemy import text

    patient, family = uuid4(), uuid4()
    with engine.begin() as connection:
        for user in (patient, family):
            connection.execute(
                text(
                    "INSERT INTO users (id, email, full_name, date_of_birth, gender, system_role) "
                    "VALUES (:id, :email, 'E2E User', '1990-01-01', 'nam', 'user')"
                ),
                {"id": user, "email": f"{user}@test.local"},
            )
        for user, purposes in (
            (patient, ("health_data", "family_sharing", "terms")),
            (family, ("health_data", "terms")),
        ):
            for purpose in purposes:
                connection.execute(
                    text(
                        "INSERT INTO user_consents (user_id, consent_type, document_version) "
                        "VALUES (:u, CAST(:p AS consent_type), '2026-10-v1')"
                    ),
                    {"u": user, "p": purpose},
                )
    expires = datetime.now(UTC) + timedelta(hours=2)

    def token(user):
        return jwt.encode(
            {
                "sub": str(user),
                "aud": "drugtime",
                "typ": "mobile",
                "iat": int(datetime.now(UTC).timestamp()),
                "exp": int(expires.timestamp()),
            },
            secret,
            algorithm="HS256",
        )

    patient_token, family_token = token(patient), token(family)
    status, code = request_json(f"http://127.0.0.1:{backend_port}/family/link-code", patient_token)
    if status != 200 or set(code) != {"code", "qr_payload"}:
        raise AssertionError("Patient GET /family/link-code không trả mã theo contract")
    fixture = {
        "session": {
            "user_id": str(family),
            "access_token": family_token,
            "token_type": "bearer",
            "session_type": "mobile",
            "expires_at": expires.isoformat(),
            "expires_in": 7200,
            "profile_complete": True,
        },
        "code": code["code"],
        "qr_payload": code["qr_payload"],
        "scenario": scenario,
    }
    return patient, family, fixture


def adb_call(adb: str, device: str, *args: str, input_text: str | None = None):
    return subprocess.run(
        [adb, "-s", device, *args],
        input=input_text,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )


def install_fixture(adb: str, device: str, host: Path) -> None:
    remote = "/data/local/tmp/drugtime_redeem_fixture.json"
    adb_call(adb, device, "push", str(host), remote)
    adb_call(adb, device, "shell", "run-as", APP_ID, "mkdir", "-p", "files")
    adb_call(adb, device, "shell", "run-as", APP_ID, "cp", remote, DEVICE_FIXTURE)
    adb_call(adb, device, "shell", "rm", remote)


def remove_fixture(adb: str, device: str) -> None:
    # Flutter integration_test may uninstall its debug package on completion.
    package = subprocess.run(
        [adb, "-s", device, "shell", "pm", "path", APP_ID],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if package.returncode == 0 and package.stdout.strip():
        adb_call(adb, device, "shell", "run-as", APP_ID, "rm", "-f", DEVICE_FIXTURE)
    elif package.returncode not in (0, 1):
        raise subprocess.CalledProcessError(package.returncode, package.args, package.stdout, package.stderr)
    adb_call(adb, device, "shell", "rm", "-f", "/data/local/tmp/drugtime_redeem_fixture.json")


def prepare_isolated_mobile() -> Path:
    """Build a separate Android package so a real phone keeps its installed app."""
    source = ROOT / "frontend" / "mobile"
    target = ROOT / "work" / "3.15-redeem-flow-tests" / "scratch" / "mobile_isolated"
    target.mkdir(parents=True, exist_ok=True)
    for name in ("lib", "assets", "integration_test"):
        shutil.copytree(source / name, target / name, dirs_exist_ok=True)
    for name in ("pubspec.yaml", "pubspec.lock", "analysis_options.yaml", ".metadata"):
        shutil.copy2(source / name, target / name)
    android_source = source / "android"
    android_target = target / "android"
    shutil.copytree(android_source / "gradle", android_target / "gradle", dirs_exist_ok=True)
    for name in (
        "build.gradle.kts", "gradle.properties", "gradlew", "gradlew.bat",
        "local.properties", "settings.gradle.kts",
    ):
        shutil.copy2(android_source / name, android_target / name)
    shutil.copytree(android_source / "app" / "src", android_target / "app" / "src", dirs_exist_ok=True)
    build = android_target / "app" / "build.gradle.kts"
    content = (android_source / "app" / "build.gradle.kts").read_text(encoding="utf-8")
    original = f'applicationId = "{APP_ID}"'
    if content.count(original) != 1:
        raise MissingEnvironment("Không tìm thấy applicationId gốc để cô lập gói test")
    build.write_text(content.replace(original, f'applicationId = "{ISOLATED_APP_ID}"'), encoding="utf-8")
    helper = target / "integration_test" / "redeem_e2e" / "redeem_helpers.dart"
    content = helper.read_text(encoding="utf-8")
    original = f"/data/data/{APP_ID}/files"
    if content.count(original) != 1:
        raise MissingEnvironment("Không tìm thấy đường dẫn fixture gốc để cô lập gói test")
    helper.write_text(content.replace(original, f"/data/data/{ISOLATED_APP_ID}/files"), encoding="utf-8")
    return target


def backup_session(adb: str, device: str) -> bool:
    """Keep any pre-existing login only inside the app's private directory."""
    original = "shared_prefs/FlutterSecureStorage.xml"
    backup = "files/redeem_e2e_session_backup.xml"
    stale_backup = subprocess.run(
        [adb, "-s", device, "shell", "run-as", APP_ID, "ls", backup],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    ).returncode == 0
    if stale_backup:
        raise AssertionError("Có backup phiên E2E cũ; cần kiểm tra trước khi ghi đè")
    present = subprocess.run(
        [adb, "-s", device, "shell", "run-as", APP_ID, "ls", original],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    ).returncode == 0
    if present:
        adb_call(adb, device, "shell", "run-as", APP_ID, "cp", original, backup)
    return present


def restore_session(adb: str, device: str, had_session_file: bool) -> None:
    original = "shared_prefs/FlutterSecureStorage.xml"
    backup = "files/redeem_e2e_session_backup.xml"
    if had_session_file:
        adb_call(adb, device, "shell", "run-as", APP_ID, "cp", backup, original)
        adb_call(adb, device, "shell", "run-as", APP_ID, "rm", "-f", backup)
    else:
        package = subprocess.run(
            [adb, "-s", device, "shell", "pm", "path", APP_ID],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if package.returncode == 0 and package.stdout.strip():
            adb_call(adb, device, "shell", "run-as", APP_ID, "rm", "-f", original)
        elif package.returncode not in (0, 1):
            raise subprocess.CalledProcessError(package.returncode, package.args, package.stdout, package.stderr)


def verify_db(engine, patient, family, records: list[dict], scenario: str, code: str):
    from sqlalchemy import text

    with engine.connect() as connection:  # fresh connection after HTTP commit
        links = connection.execute(
            text(
                "SELECT id, status::text, sharing_enabled, client_uuid::text, linked_at "
                "FROM caregiver_links WHERE patient_user_id=:p AND family_user_id=:f"
            ),
            {"p": patient, "f": family},
        ).all()
        if scenario in {"qr_invalid", "unauthorized", "qr_valid"}:
            expected = 1 if scenario == "qr_valid" else 0
        else:
            expected = 1
        assert len(links) == expected, "Số caregiver_links không đúng"
        if not expected:
            if scenario == "unauthorized":
                assert [item["status"] for item in records] == [401]
            else:
                assert not records, "Đã có POST redeem ngoài mong đợi"
            return
        link = links[0]
        assert link.status == "pending" and link.sharing_enabled is False
        assert records and all(item["code"] == code for item in records), "Mã POST không khớp fixture"
        assert all(item["response"].get("id") == str(link.id) for item in records), "ID response không khớp DB"
        assert all(item["response"].get("status") == "pending" for item in records), "Trạng thái response không phải pending"
        assert all(
            datetime.fromisoformat(item["response"]["requested_at"]) == link.linked_at
            for item in records
        )
        assert link.client_uuid == records[0]["uuid"]
        assert len({item["uuid"] for item in records}) == 1
        assert len(records) == (2 if scenario == "response_lost" else 1)
        assert [item["status"] for item in records] == (
            [201, 200] if scenario == "response_lost" else [201]
        )


def main(argv: list[str] | None = None) -> int:
    global MOBILE, APP_ID
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", action="append", choices=sorted(PHASES))
    parser.add_argument("--device")
    parser.add_argument("--preflight", action="store_true")
    args = parser.parse_args(argv)
    try:
        status, engine, device, adb, flutter = preflight(args.device)
    except MissingEnvironment as exc:
        print(f"LỖI MÔI TRƯỜNG: {exc}", file=sys.stderr)
        return 2
    if args.preflight:
        engine.dispose()
        print(f"Preflight OK: Supabase/DB local và Android {device} sẵn sàng")
        return 0
    if not device.startswith("emulator-"):
        try:
            MOBILE = prepare_isolated_mobile()
            APP_ID = ISOLATED_APP_ID
        except (MissingEnvironment, OSError) as exc:
            engine.dispose()
            print(f"LỖI MÔI TRƯỜNG: {exc}", file=sys.stderr)
            return 2

    from psycopg import sql
    from sqlalchemy import text

    failures = []
    temporary = Path(tempfile.mkdtemp(prefix="drugtime-redeem-"))
    secret = secrets.token_hex(32)
    api_password = os.getenv("TEST_API_ROLE_PASSWORD", "postgres")
    api_url = engine.url.set(username="api_role", password=api_password)
    backend_port = free_port()
    backend_env = {
        **os.environ,
        "PYTHONPATH": str(BACKEND / "src"),
        "DATABASE_URL": api_url.render_as_string(hide_password=False),
        "SESSION_JWT_SECRET": secret,
        "SUPABASE_URL": status["API_URL"],
        "SUPABASE_ANON_KEY": status["ANON_KEY"],
        "SUPABASE_SERVICE_KEY": status["SERVICE_ROLE_KEY"],
        "SUPABASE_JWKS_URL": "",
        "SUPABASE_JWT_SECRET": secrets.token_hex(32),
        "CORS_ALLOWED_ORIGINS": "",
    }
    known = (secret, api_password, status["ANON_KEY"], status["SERVICE_ROLE_KEY"])
    backend = None
    try:
        with engine.begin() as connection:
            driver = connection.connection.driver_connection
            with driver.cursor() as cursor:
                cursor.execute(
                    sql.SQL("ALTER ROLE api_role PASSWORD {}").format(sql.Literal(api_password))
                )
        with (temporary / "backend.log").open("w", encoding="utf-8") as output:
            backend = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "drugtime.main:app", "--host", "127.0.0.1", "--port", str(backend_port)],
                cwd=BACKEND,
                env=backend_env,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
            for _ in range(120):
                if backend.poll() is not None:
                    raise AssertionError("Backend dừng lúc khởi động")
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{backend_port}/docs", timeout=1) as response:
                        if response.status == 200:
                            break
                except OSError:
                    time.sleep(0.25)
            else:
                raise AssertionError("Backend không sẵn sàng trong 30 giây")

            phases = args.phase or list(PHASES)
            for scenario in phases:
                relay = Relay(backend_port, scenario)
                host_fixture = temporary / "fixture.json"
                reverse_port: int | None = None
                session_backup: bool | None = None
                try:
                    relay_port = relay.start()
                    physical = not device.startswith("emulator-")
                    if physical:
                        adb_call(
                            adb, device, "reverse", f"tcp:{relay_port}", f"tcp:{relay_port}"
                        )
                        reverse_port = relay_port
                    device_host = "127.0.0.1" if physical else "10.0.2.2"
                    device_url = f"http://{device_host}:{relay_port}"
                    patient, family, fixture = create_fixture(engine, secret, backend_port, scenario)
                    host_fixture.write_text(json.dumps(fixture), encoding="utf-8")
                    # Build/install before placing the private fixture. Flutter test upgrades
                    # the same debug package without clearing its app data.
                    build = subprocess.run(
                        [flutter, "build", "apk", "--debug", f"--dart-define=DRUGTIME_API_BASE_URL={device_url}"],
                        cwd=MOBILE, capture_output=True, text=True, timeout=900, check=False
                    )
                    if build.returncode:
                        raise AssertionError(
                            "Flutter debug build fail: " + redact((build.stdout + build.stderr)[-1600:], known)
                        )
                    adb_call(adb, device, "install", "-r", str(MOBILE / "build/app/outputs/flutter-apk/app-debug.apk"))
                    if PHASES[scenario] == "manual_code_test.dart":
                        session_backup = backup_session(adb, device)
                    install_fixture(adb, device, host_fixture)
                    run = subprocess.run(
                        [flutter, "test", f"integration_test/redeem_e2e/{PHASES[scenario]}", "-d", device,
                         f"--dart-define=DRUGTIME_API_BASE_URL={device_url}"],
                        cwd=MOBILE, capture_output=True, text=True, timeout=900, check=False
                    )
                    if run.returncode:
                        raise AssertionError(
                            "Flutter integration_test fail: " + redact((run.stdout + run.stderr)[-1600:], known)
                        )
                    verify_db(engine, patient, family, relay.records, scenario, fixture["code"])
                    if scenario == "direct_pending":
                        status_code, body = request_json(
                            f"http://127.0.0.1:{backend_port}/patients/{patient}/schedules",
                            fixture["session"]["access_token"],
                        )
                        assert (status_code, body) == (403, {"detail": "khong_co_quyen"})
                        with engine.connect() as connection:
                            assert connection.execute(
                                text("SELECT count(*) FROM access_logs WHERE patient_user_id=:p AND actor_user_id=:f AND result='denied'"),
                                {"p": patient, "f": family},
                            ).scalar_one() >= 1
                    print(f"PASS {scenario}: UI/API/DB", flush=True)
                except (AssertionError, OSError, subprocess.SubprocessError) as exc:
                    failures.append(scenario)
                    location = traceback.extract_tb(exc.__traceback__)[-1]
                    detail = redact(str(exc), known)
                    print(f"FAIL {scenario} ({type(exc).__name__} at {location.name}:{location.lineno}): {detail}", flush=True)
                finally:
                    relay.stop()
                    host_fixture.unlink(missing_ok=True)
                    try:
                        remove_fixture(adb, device)
                    except (OSError, subprocess.SubprocessError) as exc:
                        failures.append(f"cleanup:{scenario}")
                        location = traceback.extract_tb(exc.__traceback__)[-1]
                        print(f"FAIL cleanup {scenario} ({type(exc).__name__} at {location.name}:{location.lineno})", flush=True)
                    if session_backup is not None:
                        try:
                            restore_session(adb, device, session_backup)
                        except (OSError, subprocess.SubprocessError) as exc:
                            failures.append(f"restore:{scenario}")
                            location = traceback.extract_tb(exc.__traceback__)[-1]
                            print(f"FAIL restore session {scenario} ({type(exc).__name__} at {location.name}:{location.lineno})", flush=True)
                    if reverse_port is not None:
                        try:
                            adb_call(adb, device, "reverse", "--remove", f"tcp:{reverse_port}")
                        except (OSError, subprocess.SubprocessError):
                            failures.append(f"reverse:{scenario}")
                            print(f"FAIL reverse cleanup {scenario}", flush=True)
    finally:
        if backend and backend.poll() is None:
            backend.terminate()
            try:
                backend.wait(timeout=10)
            except subprocess.TimeoutExpired:
                backend.kill()
                backend.wait(timeout=10)
        engine.dispose()
        shutil.rmtree(temporary, ignore_errors=True)
        if temporary.exists():
            failures.append("cleanup:host")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
