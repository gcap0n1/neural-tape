"""cost — cost & fallback policy for LLM calls (D0.5).

DeepSeek is paid. A budget of 0 means unlimited (still recorded for status).
When a positive cap is set, this module enforces:
    1. Daily call/token caps. Once hit, can_call() returns (False, reason) without
       touching the network.
    2. State persists across runs in tape/v3/.state/cost-state.json (resets at
       local midnight).
    3. Fallback notifications are throttled (once per N hours) to avoid spam when
       the LLM endpoint is down.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("neural-tape-v3")


@dataclass
class CostBudget:
    daily_limit_calls: int  # 0 = unlimited
    daily_limit_tokens: int  # 0 = unlimited


class CostPolicy:
    """Track LLM call usage and enforce daily caps with persistent state."""

    def __init__(self, budget: CostBudget, state_dir: Path,
                 fallback_notify_interval_hours: int = 24):
        self.budget = budget
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.state_dir / "cost-state.json"
        self.notify_interval_s = fallback_notify_interval_hours * 3600

    # ---- public API -----------------------------------------------------

    def can_call(self) -> tuple[bool, str]:
        """Check if a new LLM call is allowed today. Returns (allowed, reason)."""
        state = self._load()
        today = self._today()
        if state["date"] != today:
            # New day → reset counters automatically.
            state = self._fresh_state(today)
            self._save(state)

        if self.budget.daily_limit_calls > 0 and state["calls"] >= self.budget.daily_limit_calls:
            return (False, f"daily call limit reached ({state['calls']}/{self.budget.daily_limit_calls})")
        if self.budget.daily_limit_tokens > 0 and state["tokens"] >= self.budget.daily_limit_tokens:
            return (False, f"daily token limit reached ({state['tokens']}/{self.budget.daily_limit_tokens})")
        return (True, "ok")

    def record_call(self, tokens_used: int) -> None:
        """Record a successful (or attempted) call. Tokens capped at >= 0."""
        if tokens_used < 0:
            raise ValueError("tokens_used must be >= 0")
        state = self._load()
        state["calls"] += 1
        state["tokens"] += int(tokens_used)
        self._save(state)
        log.debug("cost recorded: calls=%d tokens=%d (today)",
                  state["calls"], state["tokens"])

    def should_notify_fallback(self) -> bool:
        """True if we should emit a fallback notification now (throttled)."""
        state = self._load()
        now = time.time()
        if now - state.get("last_fallback_notify", 0) >= self.notify_interval_s:
            state["last_fallback_notify"] = now
            self._save(state)
            return True
        return False

    def status(self) -> dict:
        """Snapshot of current usage. Includes resets_at (epoch of next local midnight)."""
        state = self._load()
        if state["date"] != self._today():
            state = self._fresh_state(self._today())
            self._save(state)
        return {
            "date": state["date"],
            "calls_today": state["calls"],
            "tokens_today": state["tokens"],
            "calls_limit": self.budget.daily_limit_calls,
            "tokens_limit": self.budget.daily_limit_tokens,
            "resets_at": self._next_midnight_epoch(),
        }

    # ---- internals ------------------------------------------------------

    def _load(self) -> dict:
        if not self.state_path.exists():
            return self._fresh_state(self._today())
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            # Validate shape; reset if corrupted.
            for key in ("date", "calls", "tokens"):
                if key not in data:
                    return self._fresh_state(self._today())
            return data
        except (json.JSONDecodeError, OSError) as e:
            log.warning("cost-state.json unreadable (%s); resetting", e)
            return self._fresh_state(self._today())

    def _save(self, state: dict) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.state_path)

    @staticmethod
    def _fresh_state(date_str: str) -> dict:
        return {"date": date_str, "calls": 0, "tokens": 0, "last_fallback_notify": 0.0}

    @staticmethod
    def _today() -> str:
        return time.strftime("%Y-%m-%d", time.localtime())

    @staticmethod
    def _next_midnight_epoch() -> float:
        lt = time.localtime()
        # Seconds from now to next local midnight.
        elapsed_today = lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec
        return time.time() + (86400 - elapsed_today)
