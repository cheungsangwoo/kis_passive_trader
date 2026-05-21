"""Tests for the patient_maker orchestrator using MockBroker (no live KIS)."""

from __future__ import annotations

from datetime import datetime

from kis_passive_trader.mock_broker import MockBroker
from kis_passive_trader.portfolio import Portfolio, Position
from kis_passive_trader.state import KST, PositionState, SessionState
from kis_passive_trader import patient_maker as pm


# ── helpers ──────────────────────────────────────────────────────────────────

def _broker(books: dict[str, tuple[int, int]]) -> MockBroker:
    b = MockBroker()
    b.authenticate()
    for code, (bid, ask) in books.items():
        b.set_orderbook(code, best_bid=bid, best_ask=ask)
    return b


def _state(*positions: PositionState, capital: int = 10_000_000) -> SessionState:
    return SessionState({"signal_id": "t", "as_of": "2026-05-22", "capital": capital},
                        list(positions))


class FakePrompt:
    """Duck-typed stand-in for TimedInput.prompt returning scripted answers."""
    def __init__(self, answers):
        self._answers = list(answers)
        self.messages = []

    def prompt(self, message, timeout):
        self.messages.append(message)
        return self._answers.pop(0)


# ── time helpers ─────────────────────────────────────────────────────────────

def test_market_close_helpers():
    before = datetime(2026, 5, 22, 10, 0, tzinfo=KST)
    after = datetime(2026, 5, 22, 15, 25, tzinfo=KST)
    assert not pm.is_after_market_close(before)
    assert pm.is_after_market_close(after)
    # wake is capped at the close time
    assert pm.seconds_until_wake(before, 300) == 300
    near = datetime(2026, 5, 22, 15, 18, tzinfo=KST)
    assert pm.seconds_until_wake(near, 300) == 120   # only 2 min to close
    assert pm.seconds_until_wake(after, 300) == 0


# ── build_plan ───────────────────────────────────────────────────────────────

def test_build_plan_allocates_and_flags_drift():
    pf = Portfolio("sig", "Sig", "2026-05-22", "weekly", 15, [
        Position("005930", "삼성전자", "BUY", weight=0.5, reference_close=70_000),
        Position("000660", "SK하이닉스", "BUY", weight=0.5, reference_close=100_000),
        # stale ref -> current bid will exceed 1.05x -> drift warning
        Position("035420", "NAVER", "BUY", weight=0.0, reference_close=10_000),
    ])
    b = _broker({"005930": (70_000, 70_100),
                 "000660": (100_000, 100_500),
                 "035420": (20_000, 20_100)})
    items = {it.position.stock_code: it for it in
             pm.build_plan(pf, b, 1_000_000, threshold=1.05)}

    assert items["005930"].status == "pending"
    assert items["005930"].target_qty == 7   # 500k/70k = 7 (floor), no remainder add fits
    assert not items["005930"].warn

    # NAVER: weight 0 -> no_capital, but drift flag still computed for display
    assert items["035420"].status == "skipped_no_capital"
    assert items["035420"].warn is True
    assert items["035420"].drift_pct and items["035420"].drift_pct > 5


def test_build_plan_no_quote_skips():
    pf = Portfolio("sig", "Sig", "2026-05-22", "weekly", 15, [
        Position("005930", "삼성전자", "BUY", weight=1.0, reference_close=70_000),
    ])
    b = _broker({"005930": (0, 0)})
    items = pm.build_plan(pf, b, 1_000_000)
    assert items[0].status == "skipped_no_quote"
    assert items[0].target_qty == 0


# ── apply_drift_decisions ────────────────────────────────────────────────────

def _drift_item(code, warn=True):
    pos = Position(code, code, "BUY", weight=0.1, reference_close=10_000,
                   reference_close_date="2026-05-01")
    return pm.PlanItem(pos, peg_price=12_000, target_qty=5, budget=120_000,
                       drift_pct=20.0, warn=warn, status="pending")


def test_drift_accept_keeps_pending():
    item = _drift_item("005930")
    state = _state(PositionState("005930", "005930", 5, "BUY"))
    pm.apply_drift_decisions([item], state, FakePrompt(["y"]), threshold=1.05)
    pos = state.position("005930")
    assert pos.status == "pending"
    assert pos.warning_decision == "accepted"


def test_drift_decline_skips():
    item = _drift_item("005930")
    state = _state(PositionState("005930", "005930", 5, "BUY"))
    pm.apply_drift_decisions([item], state, FakePrompt(["n"]), threshold=1.05)
    pos = state.position("005930")
    assert pos.status == "skipped_user_declined"
    assert pos.warning_decision == "declined"


def test_drift_timeout_skips():
    item = _drift_item("005930")
    state = _state(PositionState("005930", "005930", 5, "BUY"))
    pm.apply_drift_decisions([item], state, FakePrompt([None]), threshold=1.05)
    pos = state.position("005930")
    assert pos.status == "skipped_user_timeout"
    assert pos.warning_decision == "timed_out"


def test_drift_no_warn_no_prompt():
    item = _drift_item("005930", warn=False)
    state = _state(PositionState("005930", "005930", 5, "BUY"))
    fp = FakePrompt([])   # would IndexError if prompted
    pm.apply_drift_decisions([item], state, fp, threshold=1.05)
    assert state.position("005930").status == "pending"
    assert fp.messages == []


# ── placement + reprice ──────────────────────────────────────────────────────

def test_place_and_full_fill():
    b = _broker({"005930": (70_000, 70_100)})
    state = _state(PositionState("005930", "삼성", 10, "BUY"))
    pm.place_initial_orders(b, state)
    pos = state.position("005930")
    assert pos.latest_order_id is not None
    assert pos.latest_order_price == 70_000
    assert b.submit_history[0] == ("005930", "BUY", 10, 70_000)

    b.simulate_fill(pos.latest_order_id, 10)
    assert pm.reprice_once(b, state) is True
    assert pos.status == "filled"
    assert pos.filled_qty == 10


def test_reprice_repegs_on_adverse_move():
    b = _broker({"005930": (70_000, 70_100)})
    state = _state(PositionState("005930", "삼성", 10, "BUY"))
    pm.place_initial_orders(b, state)
    # bid moves UP -> against a resting BUY -> cancel + re-peg
    b.set_orderbook("005930", best_bid=70_100, best_ask=70_200)
    pm.reprice_once(b, state)
    pos = state.position("005930")
    assert len(b.cancel_history) == 1
    assert pos.latest_order_price == 70_100


def test_reprice_holds_on_favourable_move():
    b = _broker({"005930": (70_000, 70_100)})
    state = _state(PositionState("005930", "삼성", 10, "BUY"))
    pm.place_initial_orders(b, state)
    # bid moves DOWN -> our resting bid is now above the touch -> hold
    b.set_orderbook("005930", best_bid=69_900, best_ask=70_100)
    pm.reprice_once(b, state)
    pos = state.position("005930")
    assert b.cancel_history == []
    assert pos.latest_order_price == 70_000


def test_partial_fill_tracked_across_holds():
    b = _broker({"005930": (70_000, 70_100)})
    state = _state(PositionState("005930", "삼성", 10, "BUY"))
    pm.place_initial_orders(b, state)
    pos = state.position("005930")

    b.simulate_fill(pos.latest_order_id, 4)
    assert pm.reprice_once(b, state) is False
    assert pos.filled_qty == 4 and pos.status == "pending"
    assert pos.current_order_filled == 4   # no double-count next cycle

    b.simulate_fill(pos.latest_order_id, 6)   # closes the order
    assert pm.reprice_once(b, state) is True
    assert pos.filled_qty == 10 and pos.status == "filled"


def test_cancel_all_partial_then_cancelled():
    b = _broker({"005930": (70_000, 70_100)})
    state = _state(PositionState("005930", "삼성", 10, "BUY"))
    pm.place_initial_orders(b, state)
    pos = state.position("005930")
    b.simulate_fill(pos.latest_order_id, 3)
    pm.cancel_all(b, state)
    assert pos.filled_qty == 3
    assert pos.status == "partial_then_cancelled"
    assert pos.latest_order_id is None


def test_reconcile_on_resume_picks_up_fill():
    b = _broker({"005930": (70_000, 70_100)})
    state = _state(PositionState("005930", "삼성", 10, "BUY"))
    pm.place_initial_orders(b, state)
    pos = state.position("005930")
    # fill happened "while away"
    b.simulate_fill(pos.latest_order_id, 10)
    pm.reconcile_on_resume(b, state)
    assert pos.filled_qty == 10
    assert pos.status == "filled"


# ── run loop ─────────────────────────────────────────────────────────────────

def test_run_exits_all_filled(tmp_path):
    b = _broker({"005930": (70_000, 70_100)})
    state = _state(PositionState("005930", "삼성", 10, "BUY"))
    pm.place_initial_orders(b, state)
    b.simulate_fill(state.position("005930").latest_order_id, 10)
    reason = pm.run(b, state, reprice_interval=300,
                    state_file=tmp_path / "s.json",
                    sleep_fn=lambda s: None,
                    now_fn=lambda: datetime(2026, 5, 22, 10, 0, tzinfo=KST))
    assert reason == "all_filled"


def test_run_exits_market_close(tmp_path):
    b = _broker({"005930": (70_000, 70_100)})
    state = _state(PositionState("005930", "삼성", 10, "BUY"))
    pm.place_initial_orders(b, state)   # never fills
    reason = pm.run(b, state, reprice_interval=300,
                    state_file=tmp_path / "s.json",
                    sleep_fn=lambda s: None,
                    now_fn=lambda: datetime(2026, 5, 22, 15, 25, tzinfo=KST))
    assert reason == "market_close"
    pos = state.position("005930")
    assert pos.status == "cancelled"
    assert len(b.cancel_history) == 1
