"""Test per lex/v3/cost.py (D0.5)."""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "lex" / "v3"))

from cost import CostBudget, CostPolicy  # type: ignore[ignore]


def _policy(calls=100, tokens=100000, notify_hours=24) -> tuple[CostPolicy, Path]:
    d = Path(tempfile.mkdtemp(prefix="nt-v3-cost-"))
    p = CostPolicy(
        budget=CostBudget(daily_limit_calls=calls, daily_limit_tokens=tokens),
        state_dir=d,
        fallback_notify_interval_hours=notify_hours,
    )
    return p, d


def test_can_call_initially():
    p, _ = _policy()
    ok, reason = p.can_call()
    assert ok is True
    assert reason == "ok"


def test_limit_calls_reached():
    p, _ = _policy(calls=2, tokens=100000)
    p.record_call(100)
    p.record_call(200)
    ok, reason = p.can_call()
    assert ok is False
    assert "call limit" in reason


def test_limit_tokens_reached():
    p, _ = _policy(calls=100, tokens=300)
    p.record_call(200)
    p.record_call(150)
    ok, reason = p.can_call()
    assert ok is False
    assert "token" in reason.lower()


def test_state_persists_across_instances():
    # Limit 2 calls: after 2 records we are AT the limit, so can_call() must be False.
    p1, d = _policy(calls=2, tokens=100000)
    p1.record_call(500)
    p1.record_call(500)

    # New instance, same dir → must see prior usage (2 calls already).
    p2 = CostPolicy(
        budget=CostBudget(daily_limit_calls=2, daily_limit_tokens=100000),
        state_dir=d,
    )
    ok, reason = p2.can_call()
    assert ok is False, f"expected limit reached, got ok={ok} reason={reason!r}"
    assert "call limit" in reason


def test_status_reports_counters():
    p, _ = _policy(calls=10, tokens=100000)
    p.record_call(123)
    p.record_call(77)
    s = p.status()
    assert s["calls_today"] == 2
    assert s["tokens_today"] == 200
    assert s["calls_limit"] == 10
    assert s["tokens_limit"] == 100000
    assert s["resets_at"] > time.time()


def test_corrupted_state_recovers():
    p, d = _policy()
    # Corrupt the state file.
    (d / "cost-state.json").write_text("not json at all {{{", encoding="utf-8")
    # Must not crash; fresh state takes over.
    ok, _ = p.can_call()
    assert ok is True


def test_fallback_notify_throttled():
    """First call returns True, second within interval returns False."""
    p, _ = _policy(notify_hours=24)
    assert p.should_notify_fallback() is True
    assert p.should_notify_fallback() is False


def test_fallback_notify_after_interval():
    p, d = _policy(notify_hours=1)
    assert p.should_notify_fallback() is True
    # Manually backdate last_fallback_notify to simulate interval elapsed.
    state = json.loads((d / "cost-state.json").read_text())
    state["last_fallback_notify"] = time.time() - 7200  # 2h ago
    (d / "cost-state.json").write_text(json.dumps(state))
    assert p.should_notify_fallback() is True


def test_zero_budget_is_unlimited():
    p, _ = _policy(calls=0, tokens=0)
    for _ in range(5):
        p.record_call(100_000)
    ok, reason = p.can_call()
    assert ok is True
    assert reason == "ok"
    s = p.status()
    assert s["calls_today"] == 5
    assert s["tokens_today"] == 500_000


def test_unlimited_tokens_still_honors_call_cap():
    p, _ = _policy(calls=1, tokens=0)
    p.record_call(9_999_999)
    ok, reason = p.can_call()
    assert ok is False
    assert "call limit" in reason


def test_midnight_reset():
    """If date in state != today, counters reset."""
    p, d = _policy(calls=5, tokens=100)
    p.record_call(10)
    p.record_call(10)
    # Forge yesterday's date.
    state = json.loads((d / "cost-state.json").read_text())
    yesterday = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400 * 2))
    state["date"] = yesterday
    (d / "cost-state.json").write_text(json.dumps(state))
    s = p.status()
    assert s["calls_today"] == 0
    assert s["tokens_today"] == 0
