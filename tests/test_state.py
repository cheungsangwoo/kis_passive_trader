"""Tests for resumable session state."""

from __future__ import annotations

import pytest

from kis_passive_trader.state import (
    PositionState,
    SessionState,
    StateMismatchError,
    load_state,
    now_kst_iso,
    save_state_atomic,
    state_path,
    validate_resume,
)


def test_state_path_sanitized(tmp_path):
    p = state_path("w1/luk:pead", "2026-05-22", directory=tmp_path)
    assert p.name == ".patient_maker_state_w1_luk_pead_2026-05-22.json"


def test_now_kst_iso_has_kst_offset():
    assert now_kst_iso().endswith("+09:00")


def test_position_roundtrip():
    ps = PositionState("005930", "삼성전자", 10, "BUY",
                       filled_qty=3, latest_order_id="b:1", latest_order_price=70000,
                       status="pending", warning_decision="accepted",
                       current_order_filled=3)
    ps.add_event("placed", qty=10, price=70000)
    d = ps.to_dict()
    back = PositionState.from_dict(d)
    assert back == ps
    assert back.current_order_filled == 3
    assert back.history[0]["event"] == "placed"


def test_position_remaining_and_terminal():
    ps = PositionState("005930", "삼성", 10, "BUY", filled_qty=4)
    assert ps.remaining_qty == 6
    assert not ps.is_terminal
    ps.status = "filled"
    assert ps.is_terminal
    assert ps.remaining_qty == 6


def test_session_save_load_roundtrip(tmp_path):
    state = SessionState(
        {"signal_id": "x", "as_of": "2026-05-22", "capital": 1_000_000},
        [PositionState("005930", "삼성", 10, "BUY")],
    )
    p = tmp_path / ".patient_maker_state_x.json"
    save_state_atomic(p, state)
    assert p.exists()
    loaded = load_state(p)
    assert loaded.session_metadata["capital"] == 1_000_000
    assert loaded.positions[0].stock_code == "005930"
    # No leftover temp files from the atomic write.
    assert list(tmp_path.glob("*.tmp.*")) == []


def test_load_missing_returns_none(tmp_path):
    assert load_state(tmp_path / "nope.json") is None


def test_session_helpers():
    state = SessionState({}, [
        PositionState("A", "A", 1, "BUY", status="pending"),
        PositionState("B", "B", 1, "BUY", status="filled"),
    ])
    assert [p.stock_code for p in state.pending()] == ["A"]
    assert state.position("B").status == "filled"
    assert not state.all_terminal()
    state.positions[0].status = "cancelled"
    assert state.all_terminal()


def test_validate_resume_ok():
    state = SessionState(
        {"signal_id": "x", "as_of": "2026-05-22", "capital": 1_000_000}, [])
    validate_resume(state, "x", "2026-05-22", 1_000_000)   # no raise


def test_validate_resume_mismatch():
    state = SessionState(
        {"signal_id": "x", "as_of": "2026-05-22", "capital": 1_000_000}, [])
    with pytest.raises(StateMismatchError, match="capital"):
        validate_resume(state, "x", "2026-05-22", 999)
    with pytest.raises(StateMismatchError, match="signal_id"):
        validate_resume(state, "y", "2026-05-22", 1_000_000)
