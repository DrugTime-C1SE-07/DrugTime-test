"""Fail-closed local configuration, ownership, deadlines and sanitized output."""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[3]
WORK = ROOT / "work/2.22-sync-job-e2e"
EVIDENCE = WORK / "evidence"


class PrerequisiteError(RuntimeError):
    """Missing environment or ownership proof; runner exit 2."""


class BoundaryUnverified(AssertionError):
    """A crash precondition was blocked; nonzero, but no claim about crash safety."""


def local_url(value, schemes):
    parsed = urlsplit(value)
    if parsed.scheme not in schemes or parsed.hostname not in {"127.0.0.1", "::1"}:
        raise PrerequisiteError("Only literal loopback endpoints are allowed")
    if not parsed.port:
        raise PrerequisiteError("Endpoint must include a port")
    return value


class Redactor:
    runtime_secrets = set()

    def __init__(self, secrets=()):
        self.secrets = sorted((str(s) for s in secrets if s), key=len, reverse=True)

    def __call__(self, value):
        value = str(value)
        for secret in sorted(set(self.secrets) | self.runtime_secrets, key=len, reverse=True):
            value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"(\w[\w+.-]*://)[^\s/@]+:[^\s/@]*@", r"\1[REDACTED]@", value)
        value = re.sub(r"(?i)(Bearer\s+)[\w.+/-]+", r"\1[REDACTED]", value)
        value = re.sub(r"eyJ[\w-]+\.[\w-]+\.[\w-]+", "[REDACTED]", value)
        return re.sub(
            r"(?i)((?:password|token|secret|api[_-]?key)\s*[=:]\s*)[^\s,;]+",
            r"\1[REDACTED]", value,
        )


class SanitizedStream:
    """Buffer complete lines so secrets split across write() calls cannot leak."""

    encoding = "utf-8"

    def __init__(self, screen, sink, redact):
        self.screen, self.sink, self.redact = screen, sink, redact
        self.pending = ""

    def write(self, value):
        self.pending += value
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            clean = self.redact(line + "\n")
            self.screen.write(clean)
            self.sink.write(clean)
        self.flush()
        return len(value)

    def flush(self):
        self.screen.flush()
        self.sink.flush()

    def finish(self):
        if self.pending:
            self.write("\n")

    def isatty(self):
        return False


@contextmanager
def evidence_output(path, redact):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as sink:
        stdout = SanitizedStream(sys.stdout, sink, redact)
        stderr = SanitizedStream(sys.stderr, sink, redact)
        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                yield
        finally:
            stdout.finish()
            stderr.finish()


def save_json(path, value, redact):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(redact(json.dumps(value, ensure_ascii=False, indent=2, default=str)),
                    encoding="utf-8")


class Deadline:
    def __init__(self, seconds, *, parent=None, clock=time.monotonic):
        self.clock = clock
        self.end = min(clock() + seconds, parent.end if parent else float("inf"))

    def remaining(self, maximum=None):
        remaining = self.end - self.clock()
        if remaining <= 0:
            raise TimeoutError("Catalog E2E deadline exceeded")
        return min(remaining, maximum) if maximum else remaining


def isolated_env(*, db, broker, dav, empty_file, run_id, secrets):
    local_url(db, {"postgresql+psycopg"})
    local_url(broker, {"amqp"})
    local_url(dav, {"http"})
    # Deliberate allowlist. Never inherit settings, proxy URLs, PYTHONPATH or credentials.
    env = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL")
           if k in os.environ}
    env.update({
        "PYTHONPATH": str(ROOT / "backend/src"), "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "DRUGTIME_ENV_FILE": str(empty_file), "DATABASE_URL": db,
        "CELERY_BROKER_URL": broker, "CATALOG_DAV_DRUGBANK_URL": dav,
        "TIMEZONE": "Asia/Ho_Chi_Minh", "TICK_INTERVAL_SECONDS": "2",
        "NOTIFICATION_CHANNEL": "mock", "SUPABASE_URL": "http://127.0.0.1:1",
        "SUPABASE_ANON_KEY": "", "SUPABASE_SERVICE_KEY": "", "SUPABASE_JWKS_URL": "",
        "SUPABASE_JWT_SECRET": "", "LLM_API_URL": "", "LLM_API_KEY": "",
        "FCM_PROJECT_ID": "", "SESSION_JWT_SECRET": secrets,
        "CATALOG_E2E_RUN_ID": run_id, "TZ": "Asia/Ho_Chi_Minh",
    })
    return env


def verify_owner(manifest, *, db_name, marker, labels):
    run_id = manifest["run_id"]
    if not re.fullmatch(r"[a-f0-9]{12}", run_id):
        raise PrerequisiteError("Invalid ownership run ID")
    if db_name != f"drugtime_catalog_e2e_{run_id}" or marker != run_id:
        raise PrerequisiteError("Database ownership proof mismatch")
    if labels.get("com.docker.compose.project") != manifest["project_name"]:
        raise PrerequisiteError("Compose project ownership mismatch")
    if labels.get("drugtime.catalog-e2e") != run_id:
        raise PrerequisiteError("Resource run ownership mismatch")


def docker_endpoint_is_local(endpoint):
    # Unix socket / named pipe only; localhost TCP can forward to a remote daemon.
    if not endpoint.startswith(("unix:///", "npipe:////./pipe/")):
        raise PrerequisiteError("Docker endpoint must be a local Unix socket/named pipe")


def preflight():
    if sys.platform != "linux" or sys.version_info < (3, 12):
        raise PrerequisiteError("Requires Linux/WSL2 and Python >=3.12 (prefork)")
    for package in ("celery", "kombu", "amqp", "sqlalchemy", "psycopg", "httpx", "pytest",
                    "dotenv", "pydantic_settings"):
        if importlib.util.find_spec(package) is None:
            raise PrerequisiteError(f"Missing Linux dependency: {package}")
    if not shutil.which("docker"):
        raise PrerequisiteError("Missing local Docker Engine/Compose")
    if os.environ.get("DOCKER_HOST") or os.environ.get("DOCKER_CONTEXT"):
        raise PrerequisiteError("Unset DOCKER_HOST/DOCKER_CONTEXT before running")
    try:
        result = subprocess.run(["docker", "context", "inspect"], capture_output=True,
                                text=True, timeout=5, check=True)
        context = json.loads(result.stdout)[0]
        endpoint = context["Endpoints"]["docker"]["Host"]
        docker_endpoint_is_local(endpoint)
        subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                       capture_output=True, timeout=5, check=True)
        subprocess.run(["docker", "compose", "version"], capture_output=True,
                       timeout=5, check=True)
        return endpoint
    except (subprocess.SubprocessError, KeyError, ValueError) as exc:
        raise PrerequisiteError(f"Local Docker preflight failed: {type(exc).__name__}") from None
