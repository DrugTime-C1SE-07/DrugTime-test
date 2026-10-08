"""E2E đăng nhập Mobile App: app Flutter thật trên Android emulator ↔ backend thật ↔ Supabase local.

Chạy từ gốc superproject bằng Python của backend (cần sqlalchemy, psycopg, uvicorn):

    backend/.venv/Scripts/python.exe tests/e2e/mobile_login/run_e2e.py [--phase 1|2|3 ...]
        [--device emulator-5554]

Điều kiện: `supabase start` (từ backend/), một Android emulator đang chạy, `flutter` và `adb` trong PATH.
Script khởi động backend ở cổng trống (127.0.0.1), chuẩn bị dữ liệu số thử `test_otp`, chạy ba pha
`flutter test integration_test/login_e2e/*` rồi đối chiếu DB sau mỗi pha, và luôn tắt backend khi
kết thúc. Chỉ chạy với Supabase LOCAL; không in key, token hay mật khẩu DB.

Exit: 0 = mọi pha pass; 1 = có pha fail; 2 = thiếu điều kiện môi trường.
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[3]
BACKEND = ROOT / "backend"
MOBILE = ROOT / "frontend" / "mobile"
E2E_DIR = "integration_test/login_e2e"
TEST_PHONE_DB = "+84900000001"
INJECTION_NAME = "'); DROP TABLE users;--"
LOCAL_HOSTS = {"127.0.0.1", "localhost"}
ADMIN_DB_URL = os.environ.get(
    "TEST_ADMIN_DATABASE_URL",
    "postgresql+psycopg://postgres:postgres@127.0.0.1:54322/postgres",
)
API_ROLE_PASSWORD = os.environ.get("TEST_API_ROLE_PASSWORD", "postgres")
PHASES = {
    1: "login_new_user_test.dart",
    2: "login_errors_test.dart",
    3: "profile_input_test.dart",
}

_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
_URL_PASSWORD_RE = re.compile(r"(://[^:/@\s]+:)[^@\s]+@")


class MissingEnvironment(Exception):
    """Thiếu điều kiện môi trường (exit 2)."""


def redact(text: str, secrets_: list[str] | tuple[str, ...] = ()) -> str:
    for value in secrets_:
        if value:
            text = text.replace(value, "***")
    text = _JWT_RE.sub("***", text)
    return _URL_PASSWORD_RE.sub(r"\1***@", text)


def parse_status_env(output: str) -> dict[str, str]:
    pairs = (line.split("=", 1) for line in output.splitlines() if "=" in line)
    return {k.strip(): v.strip().strip('"') for k, v in pairs}


def require_local(url: str, what: str) -> None:
    if urlsplit(url).hostname not in LOCAL_HOSTS:
        raise MissingEnvironment(
            f"{what} không phải localhost; E2E chỉ chạy với Supabase local."
        )


def pick_device(adb_output: str, wanted: str | None = None) -> str:
    devices = [
        line.split()[0]
        for line in adb_output.splitlines()[1:]
        if line.strip() and line.split()[-1] == "device"
    ]
    if wanted:
        if wanted not in devices:
            raise MissingEnvironment(f"Thiết bị {wanted} không ở trạng thái 'device'.")
        return wanted
    emulators = [d for d in devices if d.startswith("emulator-")]
    if not emulators:
        raise MissingEnvironment(
            "Không có Android emulator nào đang chạy (adb devices)."
        )
    return emulators[0]


def _which(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise MissingEnvironment(f"Không tìm thấy `{name}` trong PATH.")
    return found


def supabase_status() -> dict[str, str]:
    cli = shutil.which("supabase")
    cmd = [cli] if cli else [_which("npx"), "--no-install", "supabase"]
    try:
        out = subprocess.run(
            [*cmd, "status", "-o", "env"],
            cwd=BACKEND,
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise MissingEnvironment(
            f"Không đọc được `supabase status` ({type(exc).__name__}); chạy `supabase start`."
        )
    status = parse_status_env(out)
    require_local(status.get("API_URL", ""), "Supabase API_URL")
    return status


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Db:
    def __init__(self) -> None:
        from sqlalchemy import create_engine, text

        require_local(
            ADMIN_DB_URL.replace("postgresql+psycopg", "http"),
            "TEST_ADMIN_DATABASE_URL",
        )
        self._text = text
        self._engine = create_engine(ADMIN_DB_URL, isolation_level="AUTOCOMMIT")

    def run(self, sql: str, **params):
        with self._engine.connect() as c:
            return c.execute(self._text(sql), params)

    def set_api_role_password(self) -> None:
        self.run(f"ALTER ROLE api_role PASSWORD '{API_ROLE_PASSWORD}'")

    def reset_profile(self) -> None:
        self.run(
            "UPDATE users SET full_name = NULL, date_of_birth = NULL, gender = NULL "
            "WHERE phone_number = :p AND erased_at IS NULL",
            p=TEST_PHONE_DB,
        )

    def revoke_health_data(self) -> None:
        self.run(
            "UPDATE user_consents SET status = 'thu_hoi', revoked_at = now() "
            "WHERE consent_type = 'health_data' AND status = 'dong_y' "
            "AND user_id IN (SELECT id FROM users WHERE phone_number = :p)",
            p=TEST_PHONE_DB,
        )

    def phone_rows(self) -> list[dict]:
        rows = self.run(
            "SELECT id::text AS id, full_name, phone_number FROM users WHERE phone_number = :p",
            p=TEST_PHONE_DB,
        )
        return [dict(r._mapping) for r in rows]

    def users_table_exists(self) -> bool:
        return self.run("SELECT to_regclass('public.users') IS NOT NULL").scalar_one()

    def users_count(self) -> int:
        return self.run("SELECT count(*) FROM users").scalar_one()


class Backend:
    def __init__(self, status: dict[str, str], port: int, log_path: Path) -> None:
        self.port = port
        self.log_path = log_path
        self.secrets = [
            status.get("ANON_KEY", ""),
            status.get("SERVICE_ROLE_KEY", ""),
            API_ROLE_PASSWORD,
        ]
        session_secret = secrets.token_hex(32)
        self.secrets.append(session_secret)
        api_url = f"postgresql+psycopg://api_role:{API_ROLE_PASSWORD}@127.0.0.1:54322/postgres"
        self.env = {
            **os.environ,
            "PYTHONPATH": str(BACKEND / "src"),
            "DATABASE_URL": api_url,
            "SUPABASE_URL": status["API_URL"],
            "SUPABASE_ANON_KEY": status["ANON_KEY"],
            "SUPABASE_SERVICE_KEY": status["SERVICE_ROLE_KEY"],
            "SUPABASE_JWKS_URL": "",
            "SUPABASE_JWT_SECRET": secrets.token_hex(32),
            "SESSION_JWT_SECRET": session_secret,
            "CORS_ALLOWED_ORIGINS": "",
        }
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        log = self.log_path.open("w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "drugtime.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
            ],
            cwd=BACKEND,
            env=self.env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("Backend dừng ngay khi khởi động:\n" + self.tail())
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/docs", timeout=2
                ) as r:
                    if r.status == 200:
                        return
            except OSError:
                time.sleep(0.5)
        raise RuntimeError("Backend không trả lời sau 60 giây:\n" + self.tail())

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)

    def _lines(self) -> list[str]:
        try:
            return self.log_path.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
        except OSError:
            return []

    def line_count(self) -> int:
        return len(self._lines())

    def lines_since(self, mark: int) -> str:
        """Dòng log backend từ vị trí [mark] (đã che secret) — để biết mỗi pha gọi API nào."""
        return redact("\n".join(self._lines()[mark:]), self.secrets)

    def tail(self, lines: int = 40) -> str:
        try:
            content = self.log_path.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
        except OSError:
            return ""
        return redact("\n".join(content[-lines:]), self.secrets)


def run_flutter_phase(phase: int, device: str, port: int, secrets_: list[str]) -> bool:
    cmd = [
        _which("flutter"),
        "test",
        f"{E2E_DIR}/{PHASES[phase]}",
        "-d",
        device,
        f"--dart-define=DRUGTIME_API_BASE_URL=http://10.0.2.2:{port}",
    ]
    print(
        f"\n=== Pha {phase}: flutter test {E2E_DIR}/{PHASES[phase]} -d {device}",
        flush=True,
    )
    proc = subprocess.Popen(
        cmd,
        cwd=MOBILE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        print(redact(line.rstrip("\n"), secrets_), flush=True)
    return proc.wait() == 0


def check(condition: bool, message: str, failures: list[str]) -> None:
    print(("  OK   " if condition else "  FAIL ") + message, flush=True)
    if not condition:
        failures.append(message)


def main(argv: list[str] | None = None) -> int:
    # Output có tiếng Việt; console/pipe trên Windows mặc định cp1252.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", type=int, action="append", choices=sorted(PHASES))
    parser.add_argument("--device", default=os.environ.get("E2E_DEVICE"))
    args = parser.parse_args(argv)
    phases = args.phase or sorted(PHASES)

    try:
        status = supabase_status()
        adb = subprocess.run(
            [_which("adb"), "devices"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        device = pick_device(adb.stdout, args.device)
        _which("flutter")
        db = Db()
        db.set_api_role_password()
    except MissingEnvironment as exc:
        print(f"LỖI MÔI TRƯỜNG: {exc}", file=sys.stderr)
        return 2

    port = free_port()
    log_dir = Path(tempfile.mkdtemp(prefix="drugtime-e2e-"))
    backend = Backend(status, port, log_dir / "backend.log")
    failures: list[str] = []
    first_user_id: str | None = None
    try:
        backend.start()
        print(
            f"Backend: http://127.0.0.1:{port} (emulator: 10.0.2.2:{port}); thiết bị: {device}"
        )
        for phase in phases:
            if phase == 1:
                db.reset_profile()
                db.revoke_health_data()
            elif phase == 3:
                db.reset_profile()
            users_before = db.users_count()
            log_mark = backend.line_count()
            passed = run_flutter_phase(phase, device, port, backend.secrets)
            print(
                f"\n--- request backend trong pha {phase} ---\n"
                + backend.lines_since(log_mark)
            )
            if not passed:
                failures.append(f"Pha {phase}: flutter test fail")
                break
            rows = db.phone_rows()
            print(f"Đối chiếu DB sau pha {phase}:")
            check(
                len(rows) == 1,
                f"đúng một dòng users cho {TEST_PHONE_DB} (thấy {len(rows)})",
                failures,
            )
            if len(rows) != 1:
                break
            row = rows[0]
            if phase == 1:
                first_user_id = row["id"]
                check(
                    row["full_name"] == "Nguyễn Văn Ấn",
                    "full_name = 'Nguyễn Văn Ấn' (đúng dấu)",
                    failures,
                )
            elif phase == 2 and first_user_id:
                check(row["id"] == first_user_id, "user_id trùng pha 1", failures)
            elif phase == 3:
                check(
                    row["full_name"] == INJECTION_NAME,
                    "full_name lưu nguyên văn chuỗi injection",
                    failures,
                )
                check(
                    db.users_table_exists(), "bảng public.users vẫn tồn tại", failures
                )
                check(
                    db.users_count() >= users_before,
                    "số dòng users không giảm",
                    failures,
                )
            if failures:
                break
    except RuntimeError as exc:
        failures.append(redact(str(exc), backend.secrets))
        print(failures[-1], file=sys.stderr)
    finally:
        backend.stop()
        shutil.rmtree(log_dir, ignore_errors=True)

    if failures:
        print("\nE2E FAIL:\n- " + "\n- ".join(failures))
        return 1
    print(f"\nE2E PASS: pha {', '.join(map(str, phases))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
