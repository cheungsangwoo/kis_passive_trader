"""
Resumable session state for patient_maker.

State lives in a single JSON file per (signal_id, as_of):

    .patient_maker_state_<signal_id>_<as_of>.json

It is written atomically (temp file + os.replace) after every meaningful
change so an interrupted session — crash, Ctrl-C, power loss — can be
resumed by re-running the same command. On resume we reconcile each
still-pending order against KIS before continuing.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

# KIS / KRX operate in Korea Standard Time, which is a fixed UTC+9 with no DST.
KST = timezone(timedelta(hours=9))

# Terminal statuses never change once set; `pending` is the only live status.
TERMINAL_STATUSES = frozenset({
    "filled",
    "partial_then_cancelled",
    "cancelled",
    "skipped_user_declined",
    "skipped_user_timeout",
    "skipped_no_capital",
    "skipped_no_quote",
})
ALL_STATUSES = TERMINAL_STATUSES | {"pending"}


class StateMismatchError(Exception):
    """Raised when an existing state file's metadata disagrees with the CLI."""


def now_kst_iso() -> str:
    """Current time as an ISO-8601 string in KST (e.g. 2026-05-25T09:15:00+09:00)."""
    return datetime.now(KST).isoformat(timespec="seconds")


def _sanitize(s: str) -> str:
    """Make a string safe for use in a filename."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", s or "")


def state_path(signal_id: str, as_of: str, directory: str | Path = ".") -> Path:
    """Deterministic state-file path for a (signal_id, as_of) pair."""
    name = f".patient_maker_state_{_sanitize(signal_id)}_{_sanitize(as_of)}.json"
    return Path(directory) / name


@dataclass
class PositionState:
    """Persisted per-position execution state."""
    stock_code: str
    stock_name: str
    target_qty: int
    side: str
    filled_qty: int = 0
    latest_order_id: str | None = None
    latest_order_price: int | None = None
    status: str = "pending"
    warning_decision: str | None = None   # accepted | declined | timed_out | None
    # Fills already counted on the *current* open order, so re-pegs (cancel +
    # replace) and repeated polls of a held order never double-count partials.
    # Reset to 0 each time a fresh order is placed.
    current_order_filled: int = 0
    history: list[dict] = field(default_factory=list)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def remaining_qty(self) -> int:
        return max(0, self.target_qty - self.filled_qty)

    def add_event(self, event: str, **fields) -> None:
        """Append a timestamped event to this position's audit history."""
        self.history.append({"event": event, "ts": now_kst_iso(), **fields})

    def to_dict(self) -> dict:
        return {
            "stock_code": self.stock_code,
            "stock_name": self.stock_name,
            "target_qty": self.target_qty,
            "filled_qty": self.filled_qty,
            "side": self.side,
            "latest_order_id": self.latest_order_id,
            "latest_order_price": self.latest_order_price,
            "status": self.status,
            "warning_decision": self.warning_decision,
            "current_order_filled": self.current_order_filled,
            "history": self.history,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PositionState":
        return cls(
            stock_code=str(d.get("stock_code", "")),
            stock_name=str(d.get("stock_name", "")),
            target_qty=int(d.get("target_qty", 0)),
            side=str(d.get("side", "BUY")),
            filled_qty=int(d.get("filled_qty", 0)),
            latest_order_id=d.get("latest_order_id"),
            latest_order_price=d.get("latest_order_price"),
            status=str(d.get("status", "pending")),
            warning_decision=d.get("warning_decision"),
            current_order_filled=int(d.get("current_order_filled", 0)),
            history=list(d.get("history", [])),
        )


@dataclass
class SessionState:
    """Top-level resumable state: metadata + per-position records."""
    session_metadata: dict
    positions: list[PositionState]

    def position(self, stock_code: str) -> PositionState | None:
        return next((p for p in self.positions if p.stock_code == stock_code), None)

    def pending(self) -> list[PositionState]:
        return [p for p in self.positions if p.status == "pending"]

    def all_terminal(self) -> bool:
        return all(p.is_terminal for p in self.positions)

    def to_dict(self) -> dict:
        return {
            "session_metadata": self.session_metadata,
            "positions": [p.to_dict() for p in self.positions],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SessionState":
        return cls(
            session_metadata=dict(d.get("session_metadata", {})),
            positions=[PositionState.from_dict(p) for p in d.get("positions", [])],
        )


def save_state_atomic(path: str | Path, state: SessionState) -> None:
    """Write state to disk atomically (temp file in the same dir + os.replace)."""
    path = Path(path)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(
        json.dumps(state.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, path)   # atomic on POSIX and Windows


def load_state(path: str | Path) -> SessionState | None:
    """Load state from disk, or None if the file does not exist."""
    path = Path(path)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return SessionState.from_dict(data)


def validate_resume(state: SessionState, signal_id: str, as_of: str, capital: int) -> None:
    """Ensure an existing state file matches the current CLI invocation.

    Raises StateMismatchError listing every field that disagrees.
    """
    meta = state.session_metadata
    mismatches = []
    if str(meta.get("signal_id")) != str(signal_id):
        mismatches.append(f"signal_id (state={meta.get('signal_id')!r} vs cli={signal_id!r})")
    if str(meta.get("as_of")) != str(as_of):
        mismatches.append(f"as_of (state={meta.get('as_of')!r} vs cli={as_of!r})")
    if int(meta.get("capital", 0)) != int(capital):
        mismatches.append(f"capital (state={meta.get('capital')} vs cli={capital})")
    if mismatches:
        raise StateMismatchError(
            "Existing state file does not match this command:\n  - "
            + "\n  - ".join(mismatches)
            + "\nUse --force-fresh to start over, or match the original arguments."
        )
