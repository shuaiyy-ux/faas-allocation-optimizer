"""Shared per-session state for the FaaS allocator.

The previous single-process version used module-level globals
(`last_weekly`, `last_allocate`) shared across every connected user —
a race condition under multi-user demo deployment. This module replaces
that with `SessionStore`: a `Dict[client_id, SessionState]` keyed by the
UUID the frontend already mints per browser tab and sends on every
request via the `X-Session-Id` header.

Each `SessionState` carries:
- `fleet_df`: a private deep-copy of the immutable baseline fleet
  (mutated locally by `confirm_allocation` and `reset_fleet`).
- `constraints`: in-memory dict `{name: source_code}` replacing the
  previous file-based `app/constraints/<name>.py` storage. Approved
  constraints are executed at solve time inside the session.
- `last_weekly` / `last_allocate`: the session's own allocation cache.
- `last_touched`: monotonic timestamp used by the TTL reaper.

A `last_watchlist` singleton survives at module level because watchlist
alerts are a fleet-wide signal (not user-specific) and persist across
restarts via `last_watchlist.json` per the existing UX contract.
"""

import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd


# ── Watchlist singleton (unchanged from prior version) ──────────────

_WATCHLIST_PATH = Path(__file__).resolve().parent / "last_watchlist.json"


def _load_watchlist_from_disk() -> Optional[Dict[str, Any]]:
    try:
        with _WATCHLIST_PATH.open("r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _save_watchlist_to_disk(value: Optional[Dict[str, Any]]) -> None:
    try:
        if value is None:
            _WATCHLIST_PATH.unlink(missing_ok=True)
        else:
            with _WATCHLIST_PATH.open("w") as f:
                json.dump(value, f)
    except OSError as exc:
        print("[state] failed to persist watchlist: {}".format(exc))


_WATCHLIST_SEED = Path(__file__).resolve().parent / "demo_seed" / "watchlist.json"


def _load_demo_seed() -> Optional[Dict[str, Any]]:
    """Public demo only: a hand-written watchlist so visitors see alerts
    without spending an AI action. Refresh replaces it with an agent run."""
    if os.environ.get("DEMO_MODE") != "1":
        return None
    try:
        with _WATCHLIST_SEED.open("r") as f:
            seed = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    seed.pop("_seed", None)
    seed["refreshed_at"] = datetime.now().isoformat()
    return seed


last_watchlist: Optional[Dict[str, Any]] = _load_watchlist_from_disk() or _load_demo_seed()


def set_watchlist(result: Optional[Dict[str, Any]]) -> None:
    global last_watchlist
    last_watchlist = result
    _save_watchlist_to_disk(result)


# ── Deprecated module-level cache (Phase 4 will delete) ─────────────
# MCP tools that haven't yet migrated to per-session storage still write
# here; readers will be migrated in Phase 4 (mcp_server.py callers will
# resolve their session via the URL-routed path middleware).

last_weekly: Optional[Dict[str, Any]] = None
last_allocate: Optional[Dict[str, Any]] = None


def set_weekly(result: Optional[Dict[str, Any]]) -> None:
    global last_weekly
    last_weekly = result


def set_allocate(result: Optional[Dict[str, Any]]) -> None:
    global last_allocate
    last_allocate = result


def cached() -> Optional[Dict[str, Any]]:
    """Deprecated: use `SessionState.cached()`. Returns the most recent
    write to the legacy globals (weekly preferred)."""
    return last_weekly or last_allocate


def clear() -> None:
    """Deprecated: per-session clears happen on the SessionState itself."""
    global last_weekly, last_allocate
    last_weekly = None
    last_allocate = None


# ── Per-session state ───────────────────────────────────────────────


@dataclass
class SessionState:
    """Mutable state belonging to one browser tab / API caller."""

    client_id: str
    fleet_df: pd.DataFrame
    constraints: Dict[str, str] = field(default_factory=dict)
    last_weekly: Optional[Dict[str, Any]] = None
    last_allocate: Optional[Dict[str, Any]] = None
    last_touched: float = field(default_factory=time.time)
    # Chat budget per browser session. Each /api/chat POST increments this;
    # once it hits CHAT_LIMIT_PER_SESSION the server returns 429 instead of
    # spawning another Claude CLI subprocess.
    chat_count: int = 0

    def touch(self) -> None:
        self.last_touched = time.time()

    def cached(self) -> Optional[Dict[str, Any]]:
        """Most recent allocation result for this session, weekly preferred."""
        return self.last_weekly or self.last_allocate


class SessionStore:
    """Thread-safe registry of `SessionState`s keyed by `client_id`."""

    def __init__(
        self,
        baseline_fleet_df: pd.DataFrame,
        max_sessions: int = 50,
        ttl_seconds: int = 1800,
    ) -> None:
        self._lock = threading.Lock()
        self._sessions: Dict[str, SessionState] = {}
        self._baseline_fleet_df = baseline_fleet_df
        self.max_sessions = max_sessions
        self.ttl_seconds = ttl_seconds

    def _new_session_locked(self, client_id: str) -> SessionState:
        return SessionState(
            client_id=client_id,
            fleet_df=self._baseline_fleet_df.copy(deep=True),
        )

    def _evict_oldest_locked(self) -> Optional[str]:
        if not self._sessions:
            return None
        oldest_id = min(self._sessions, key=lambda k: self._sessions[k].last_touched)
        del self._sessions[oldest_id]
        return oldest_id

    def get_or_create(self, client_id: str) -> SessionState:
        with self._lock:
            session = self._sessions.get(client_id)
            if session is None:
                if len(self._sessions) >= self.max_sessions:
                    self._evict_oldest_locked()
                session = self._new_session_locked(client_id)
                self._sessions[client_id] = session
            session.touch()
            return session

    def get(self, client_id: str) -> Optional[SessionState]:
        with self._lock:
            session = self._sessions.get(client_id)
            if session is not None:
                session.touch()
            return session

    def drop(self, client_id: str) -> bool:
        with self._lock:
            return self._sessions.pop(client_id, None) is not None

    def reap(self) -> int:
        """Evict sessions idle longer than `ttl_seconds`. Returns count evicted."""
        cutoff = time.time() - self.ttl_seconds
        with self._lock:
            stale_ids = [k for k, s in self._sessions.items() if s.last_touched < cutoff]
            for sid in stale_ids:
                del self._sessions[sid]
            return len(stale_ids)

    def reset_session_fleet(self, session: SessionState) -> None:
        """Restore a session's fleet_df from the immutable baseline."""
        session.fleet_df = self._baseline_fleet_df.copy(deep=True)
        session.last_weekly = None
        session.last_allocate = None
        session.touch()

    def snapshot(self) -> List[Dict[str, Any]]:
        """Diagnostic view of currently held sessions."""
        with self._lock:
            return [
                {
                    "client_id": s.client_id,
                    "last_touched": s.last_touched,
                    "has_weekly": s.last_weekly is not None,
                    "has_allocate": s.last_allocate is not None,
                    "n_constraints": len(s.constraints),
                }
                for s in self._sessions.values()
            ]
