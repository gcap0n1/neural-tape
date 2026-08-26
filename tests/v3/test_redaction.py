"""Test per lex/v3/redaction.py (D0.1)."""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure nt_v3 package is registered by run_all.py loader.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "lex" / "v3"))

from redaction import Redactor  # type: ignore[import-not-found]


def test_aws_access_key():
    r = Redactor()
    out, ev = r.redact("connect using AKIAIOSFODNN7EXAMPLE as the key")
    assert "[REDACTED:aws-access-key-id]" in out
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert any(e.kind == "aws-access-key-id" for e in ev)


def test_jwt_token():
    r = Redactor()
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    out, ev = r.redact(f"Authorization: Bearer {jwt}")
    assert jwt not in out
    assert any(e.kind in ("jwt", "bearer-token") for e in ev)


def test_github_token():
    r = Redactor()
    tok = "ghp_" + "a" * 36
    out, ev = r.redact(f"git remote add origin https://{tok}@github.com/x/y.git")
    assert tok not in out
    assert any(e.kind == "github-token" for e in ev)


def test_private_key_pem():
    r = Redactor()
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIBAAKCAQEAxxxxxx\n"
        "-----END RSA PRIVATE KEY-----"
    )
    out, ev = r.redact(f"here is the key:\n{pem}\ndone")
    assert "MIIEpAIBAA" not in out
    assert any(e.kind == "private-key-pem" for e in ev)


def test_url_credentials():
    r = Redactor()
    out, ev = r.redact("postgres://user:secretpass123@db.example.com:5432/mydb")
    assert "secretpass123" not in out
    assert any(e.kind == "url-credentials" for e in ev)


def test_generic_assignment_env_block():
    r = Redactor()
    text = "API_KEY=ak_live_1234567890abcdefghijklm\nPASSWORD=thisismypassword12345"
    out, ev = r.redact(text)
    assert "ak_live_1234567890abcdefghijklm" not in out
    assert "thisismypassword12345" not in out
    # At least 2 generic-assignment or other redactions occurred.
    assert len(ev) >= 2


def test_no_false_positive_function_def():
    """Critical: legitimate code must not be mangled."""
    r = Redactor()
    code = (
        "def api_key_handler(request):\n"
        "    return JsonResponse({'token_count': 42})\n"
        "class SecretManager:\n"
        "    pass\n"
    )
    out, ev = r.redact(code)
    # Function defs and class names that don't contain an actual secret assignment
    # must NOT be redacted. The number regex in generic-assignment requires '=' or ':'
    # followed by a 16+ char opaque value, which the above lacks.
    assert "api_key_handler" in out
    assert "SecretManager" in out
    # Only the integer 42 could be matched? No — too short. Expect zero events.
    assert ev == [], f"unexpected redactions: {ev}"


def test_extra_pattern_custom():
    """User-supplied extra_patterns must work.

    Note: we use a string WITHOUT a generic-assignment form (no 'token=' / 'secret:'
    prefix) so the default patterns don't shadow the custom one. The custom pattern
    matches MYCUSTOM_<20 digits> anywhere via word boundaries.
    """
    r = Redactor(extra_patterns=[(r"\bMYCUSTOM_\d{20}\b", "custom")])
    out, ev = r.redact("value MYCUSTOM_12345678901234567890 end")
    assert "MYCUSTOM_12345678901234567890" not in out
    assert any(e.kind == "custom" for e in ev), f"got kinds={[e.kind for e in ev]}"


def test_summary_zero():
    r = Redactor()
    s = r.summary([])
    assert "clean" in s.lower()


def test_summary_with_redactions():
    # AWS pattern requires EXACTLY 16 uppercase alphanumerics after 'AKIA' (20 total).
    # AKIAIOSFODNN7EXAMPLE         → 20 chars ✓
    # AKIAEXAMPLE123456780         → 20 chars ✓
    r = Redactor()
    _, ev = r.redact("key AKIAIOSFODNN7EXAMPLE plus AKIAEXAMPLE123456780 done")
    s = r.summary(ev)
    assert "aws-access-key-id" in s, f"summary was: {s!r}"
    assert "2" in s, f"expected 2 redactions, summary={s!r}"


# --- Ports from ai-memory sanitize.rs (2026-08-21) --------------------------


def _kinds(out: str, ev) -> set:
    return {e.kind for e in ev}


def test_redacts_stripe_and_github_pat_and_google_keys():
    r = Redactor()
    # Secret-shaped fixtures are built at runtime (never as single source
    # literals) so repository scanners don't flag this file as a leak.
    stripe_live = "sk_live_" + "51HAnbGjXh2" + "kK1mNqV7dW3pRtY0"
    google_api = "AIza" + "SyAqwertyuiopasd" + "fghjklzxcvbnm123456"
    text = (
        f"{stripe_live}\n"
        "rk_test_abcDEF1234567890_ghiJKL\n"
        "github_pat_11ABCDEFG0abcdefghijklmnopqrstuv\n"
        f"{google_api}\n"
        "1//0gAbCdEfGhIjKlMnOpQrStUvWxYz123"
    )
    out, ev = r.redact(text)
    assert "sk_live_" not in out
    assert "rk_test_" not in out
    assert "github_pat_" not in out
    assert "AIza" not in out
    assert "1//" not in out
    kinds = _kinds(out, ev)
    assert {"stripe-key", "github-token", "google-api-key",
            "google-oauth-refresh"} <= kinds, f"kinds={kinds}"


def test_redacts_telegram_and_slack_app_and_meta():
    r = Redactor()
    telegram_token = "AAHdqTcvCH1vGWJ" + "xfSeofSAs0K28P1fJbXz"
    text = (
        f"bot 123456789:{telegram_token} end\n"
        "xapp-1-A0B1C2D3E4F5G6H7I8J9K0 end\n"
        "EAAZCZCkL0o1XABAsampletoken9876543210abcde end"
    )
    out, ev = r.redact(text)
    assert "AAHdqTcv" not in out
    assert "xapp-" not in out
    assert "EAAZCZC" not in out
    kinds = _kinds(out, ev)
    assert "telegram-bot-token" in kinds
    assert "slack-token" in kinds
    assert "meta-graph-token" in kinds


def test_redacts_env_assignments_and_credential_paths():
    r = Redactor()
    text = (
        "OPENAI_API_KEY=sk-notreallyakey12345678901234\n"
        "MY_APP_PRIVATE_KEY = whatever\n"
        "the key lives in /home/user/.ssh/id_rsa and /etc/.aws/credentials"
    )
    out, ev = r.redact(text)
    assert "OPENAI_API_KEY=" not in out
    assert "MY_APP_PRIVATE_KEY" not in out
    assert ".ssh/id_rsa" not in out
    assert ".aws/credentials" not in out
    kinds = _kinds(out, ev)
    assert "provider-env" in kinds
    assert "generic-env" in kinds
    assert "credential-path" in kinds


def test_allowlist_preserves_matching_span():
    r = Redactor(allowlist=["MY_PROJECT_TOKEN"])
    out, ev = r.redact("MY_PROJECT_TOKEN=keepme-visible-value")
    assert "MY_PROJECT_TOKEN=keepme-visible-value" in out
    assert ev == [], f"allowlisted span must not produce events: {ev}"
