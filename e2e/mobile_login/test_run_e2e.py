"""Unit test cho phần thuần của run_e2e.py (không cần Supabase, backend hay emulator)."""

import pytest
import run_e2e


def test_redact_hides_known_secrets_jwts_and_db_passwords():
    text = (
        "key=sb-secret-123 token=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl "
        "url=postgresql+psycopg://api_role:postgres@127.0.0.1:54322/postgres"
    )

    out = run_e2e.redact(text, ["sb-secret-123"])

    assert "sb-secret-123" not in out
    assert "eyJ" not in out
    assert ":postgres@" not in out
    assert "api_role:***@127.0.0.1" in out


def test_redact_ignores_empty_secret_values():
    assert run_e2e.redact("không có gì", ["", ""]) == "không có gì"


def test_parse_status_env_strips_quotes_and_ignores_other_lines():
    out = 'API_URL="http://127.0.0.1:54321"\nANON_KEY=abc\nStopped services: []\n'

    assert run_e2e.parse_status_env(out) == {
        "API_URL": "http://127.0.0.1:54321",
        "ANON_KEY": "abc",
    }


@pytest.mark.parametrize("url", ["http://127.0.0.1:54321", "http://localhost:54321"])
def test_require_local_accepts_local_urls(url):
    run_e2e.require_local(url, "API_URL")


@pytest.mark.parametrize(
    "url", ["https://abc.supabase.co", "http://10.0.0.5:54321", ""]
)
def test_require_local_rejects_non_local_urls(url):
    with pytest.raises(run_e2e.MissingEnvironment):
        run_e2e.require_local(url, "API_URL")


ADB = """List of devices attached
R58M12345\tdevice
emulator-5556\toffline
emulator-5554\tdevice

"""


def test_pick_device_prefers_running_emulator():
    assert run_e2e.pick_device(ADB) == "emulator-5554"


def test_pick_device_uses_requested_device_when_online():
    assert run_e2e.pick_device(ADB, "R58M12345") == "R58M12345"


@pytest.mark.parametrize(
    ("output", "wanted"),
    [
        ("List of devices attached\n\n", None),
        ("List of devices attached\nemulator-5554\toffline\n", None),
        ("List of devices attached\nR58M12345\tdevice\n", None),
        (ADB, "emulator-5556"),
    ],
    ids=[
        "khong-co-thiet-bi",
        "emulator-offline",
        "chi-co-may-that",
        "thiet-bi-yeu-cau-offline",
    ],
)
def test_pick_device_fails_without_usable_emulator(output, wanted):
    with pytest.raises(run_e2e.MissingEnvironment):
        run_e2e.pick_device(output, wanted)


def test_main_exits_2_when_environment_missing(monkeypatch, capsys):
    def missing():
        raise run_e2e.MissingEnvironment("không đọc được `supabase status`")

    monkeypatch.setattr(run_e2e, "supabase_status", missing)

    assert run_e2e.main([]) == 2
    assert "LỖI MÔI TRƯỜNG" in capsys.readouterr().err
