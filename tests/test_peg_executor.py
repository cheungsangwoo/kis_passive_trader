"""Tests for the peg-to-best executor using MockBroker."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from kis_passive_trader.mock_broker import MockBroker
from kis_passive_trader.peg_executor import (
    OrderRequest,
    _should_repeg,
    execute_order,
    execute_batch,
)


# ── _should_repeg unit tests ────────────────────────────────────────────────

class TestShouldRepeg:
    def test_buy_bid_moves_up_triggers_repeg(self):
        assert _should_repeg("BUY", current_peg=100, new_peg=101) is True

    def test_buy_bid_unchanged_no_repeg(self):
        assert _should_repeg("BUY", current_peg=100, new_peg=100) is False

    def test_buy_bid_moves_down_no_repeg(self):
        # Bid moved down — our resting bid is now ABOVE the touch. No re-peg.
        assert _should_repeg("BUY", current_peg=100, new_peg=99) is False

    def test_sell_ask_moves_down_triggers_repeg(self):
        assert _should_repeg("SELL", current_peg=200, new_peg=199) is True

    def test_sell_ask_unchanged_no_repeg(self):
        assert _should_repeg("SELL", current_peg=200, new_peg=200) is False

    def test_sell_ask_moves_up_no_repeg(self):
        assert _should_repeg("SELL", current_peg=200, new_peg=201) is False


# ── Time & sleep helpers ────────────────────────────────────────────────────

class FakeClock:
    """Advances a mock 'now' by a fixed delta per sleep()."""
    def __init__(self, start: datetime | None = None):
        self.now = start or datetime(2026, 4, 16, 10, 0, 0)

    def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)

    def tick(self) -> datetime:
        return self.now


# ── execute_order scenarios ─────────────────────────────────────────────────

@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def broker() -> MockBroker:
    b = MockBroker()
    b.authenticate()
    return b


def test_zero_qty_returns_immediately(broker, clock):
    req = OrderRequest("005930", "삼성전자", "BUY", qty=0)
    r = execute_order(broker, req, sleep_fn=clock.sleep, now_fn=clock.tick)
    assert r.filled_qty == 0
    assert r.abandoned_qty == 0
    assert "zero_qty_skipped" in r.notes


def test_no_quote_abandons(broker, clock):
    """If the orderbook has no bid/ask at all, abandon immediately."""
    broker.set_orderbook("005930", best_bid=0, best_ask=0)
    req = OrderRequest("005930", "삼성전자", "BUY", qty=10)
    r = execute_order(broker, req, sleep_fn=clock.sleep, now_fn=clock.tick)
    assert r.filled_qty == 0
    assert r.abandoned_qty == 10
    assert any("no_quote" in n for n in r.notes)


def test_order_cap_rejects_oversize_order(broker, clock):
    broker.set_orderbook("005930", best_bid=100_000, best_ask=100_100)
    req = OrderRequest("005930", "삼성전자", "BUY", qty=100, ref_price=100_000)
    r = execute_order(broker, req, max_order_krw=5_000_000,
                      sleep_fn=clock.sleep, now_fn=clock.tick)
    # 100 shares × 100,000 = 10M > 5M cap
    assert r.filled_qty == 0
    assert r.abandoned_qty == 100
    assert any("rejected_order_size" in n for n in r.notes)


def test_price_deviation_abort(broker, clock):
    """If market moved >15% from the payload ref_price, don't trade."""
    broker.set_orderbook("005930", best_bid=80_000, best_ask=80_100)
    req = OrderRequest("005930", "삼성전자", "BUY", qty=10, ref_price=100_000)
    r = execute_order(broker, req, sleep_fn=clock.sleep, now_fn=clock.tick,
                      max_order_krw=100_000_000)
    assert r.filled_qty == 0
    assert r.abandoned_qty == 10
    assert any("rejected_price_deviation" in n for n in r.notes)


def test_happy_path_full_fill_first_iteration(broker, clock):
    """Fill immediately at best bid — no re-peg needed."""
    broker.set_orderbook("005930", best_bid=63_000, best_ask=63_100)
    req = OrderRequest("005930", "삼성전자", "BUY", qty=10, ref_price=63_000)

    # Pre-arrange: after the first submission, simulate a full fill
    original_submit = broker.submit_limit_order
    def submit_and_fill(ticker, side, qty, price):
        ok, oid = original_submit(ticker, side, qty, price)
        if ok:
            broker.simulate_fill(oid, qty)   # immediately fully filled
        return ok, oid
    broker.submit_limit_order = submit_and_fill

    r = execute_order(broker, req, max_order_krw=100_000_000,
                      sleep_fn=clock.sleep, now_fn=clock.tick)
    assert r.filled_qty == 10
    assert r.abandoned_qty == 0
    assert r.fully_filled
    assert len(r.peg_prices) == 1
    assert r.peg_prices[0] == 63_000
    assert len(broker.cancel_history) == 0


def test_repeg_when_bid_moves_up(broker, clock):
    """First order doesn't fill; bid moves up; executor cancels + re-pegs."""
    broker.set_orderbook("005930", best_bid=63_000, best_ask=63_100)
    req = OrderRequest("005930", "삼성전자", "BUY", qty=10, ref_price=63_000)

    iteration = {"count": 0}
    original_submit = broker.submit_limit_order
    def submit_hook(ticker, side, qty, price):
        iteration["count"] += 1
        ok, oid = original_submit(ticker, side, qty, price)
        if iteration["count"] == 1:
            # First order: no fill this cycle, then market moves up
            pass
        elif iteration["count"] == 2 and ok:
            # Second order: immediate fill
            broker.simulate_fill(oid, qty)
        return ok, oid
    broker.submit_limit_order = submit_hook

    # Simulate the bid moving up BETWEEN iterations via sleep hook
    def advance_market(seconds):
        clock.sleep(seconds)
        if iteration["count"] == 1:
            broker.set_orderbook("005930", best_bid=63_100, best_ask=63_200)

    r = execute_order(broker, req, max_order_krw=100_000_000,
                      sleep_fn=advance_market, now_fn=clock.tick)
    assert r.filled_qty == 10
    assert r.abandoned_qty == 0
    assert len(r.peg_prices) == 2
    assert r.peg_prices == [63_000, 63_100]
    # First order should have been cancelled
    assert len(broker.cancel_history) >= 1


def test_no_repeg_when_bid_moves_down(broker, clock):
    """Bid moves DOWN — we're still at or above touch. Do not re-peg."""
    broker.set_orderbook("005930", best_bid=63_000, best_ask=63_100)
    req = OrderRequest("005930", "삼성전자", "BUY", qty=10, ref_price=63_000)

    iteration = {"count": 0}
    original_submit = broker.submit_limit_order
    def submit_hook(ticker, side, qty, price):
        iteration["count"] += 1
        ok, oid = original_submit(ticker, side, qty, price)
        if iteration["count"] == 2 and ok:
            broker.simulate_fill(oid, qty)
        return ok, oid
    broker.submit_limit_order = submit_hook

    def advance_market(seconds):
        clock.sleep(seconds)
        if iteration["count"] == 1:
            # Bid moves DOWN — no re-peg should happen
            broker.set_orderbook("005930", best_bid=62_900, best_ask=63_100)

    r = execute_order(broker, req, max_order_krw=100_000_000, max_iterations=3,
                      sleep_fn=advance_market, now_fn=clock.tick)
    # Only ONE submit should have happened because bid moved favourably.
    # A single cancel at the end (abandon-at-max-iters cleanup) is expected.
    assert iteration["count"] == 1
    assert len(broker.cancel_history) <= 1   # at most one cancel = final cleanup
    # Order abandoned at end because never filled
    assert r.filled_qty == 0
    assert r.abandoned_qty == 10
    assert any("max_iterations_abandoned" in n for n in r.notes)


def test_partial_fill_tracked_correctly(broker, clock):
    """Partial fill over multiple iterations — remaining should decrement."""
    broker.set_orderbook("005930", best_bid=63_000, best_ask=63_100)
    req = OrderRequest("005930", "삼성전자", "BUY", qty=10, ref_price=63_000)

    iteration = {"count": 0}
    original_submit = broker.submit_limit_order
    def submit_hook(ticker, side, qty, price):
        iteration["count"] += 1
        # qty should be 10 first call; after partial fills the executor may
        # re-submit remaining — but with unchanged bid we shouldn't re-peg.
        # So only ONE submit expected here.
        return original_submit(ticker, side, qty, price)
    broker.submit_limit_order = submit_hook

    # Each sleep, fill 3 more shares on the open order
    def advance_with_fill(seconds):
        clock.sleep(seconds)
        open_orders = [o for o in broker.orders.values() if o.open]
        for o in open_orders:
            broker.simulate_fill(o.order_id, 3)
            # If after 3 we're at 9, still open. After next iter, 12 -> clipped to 10, closed.

    r = execute_order(broker, req, max_order_krw=100_000_000, max_iterations=5,
                      sleep_fn=advance_with_fill, now_fn=clock.tick)
    assert iteration["count"] == 1
    assert r.filled_qty == 10
    assert r.abandoned_qty == 0
    assert r.fully_filled


def test_max_iterations_abandons_remaining(broker, clock):
    """After max_iters, unfilled quantity is abandoned; open order cancelled."""
    broker.set_orderbook("005930", best_bid=63_000, best_ask=63_100)
    req = OrderRequest("005930", "삼성전자", "BUY", qty=10, ref_price=63_000)

    # Never any fills; bid never moves. Should submit once, never refill,
    # then cancel at max_iters.
    r = execute_order(broker, req, max_order_krw=100_000_000, max_iterations=3,
                      sleep_fn=clock.sleep, now_fn=clock.tick)
    assert r.iterations_used == 3
    assert r.filled_qty == 0
    assert r.abandoned_qty == 10
    assert any("max_iterations_abandoned" in n for n in r.notes)
    # Should have cancelled the open order
    assert len(broker.cancel_history) == 1


def test_sell_uses_best_ask(broker, clock):
    """SELL should peg to best_ask, not best_bid."""
    broker.set_orderbook("035420", best_bid=200_000, best_ask=200_500)
    req = OrderRequest("035420", "NAVER", "SELL", qty=2, ref_price=200_500)

    original_submit = broker.submit_limit_order
    def submit_and_fill(ticker, side, qty, price):
        ok, oid = original_submit(ticker, side, qty, price)
        if ok:
            broker.simulate_fill(oid, qty)
        return ok, oid
    broker.submit_limit_order = submit_and_fill

    r = execute_order(broker, req, max_order_krw=100_000_000,
                      sleep_fn=clock.sleep, now_fn=clock.tick)
    assert r.fully_filled
    assert r.peg_prices == [200_500]
    # Verify the submit went in at 200_500, not 200_000
    assert broker.submit_history[0] == ("035420", "SELL", 2, 200_500)


# ── execute_batch scenarios ─────────────────────────────────────────────────

def test_batch_sorts_sells_before_buys(broker, clock):
    broker.set_orderbook("A", best_bid=1000, best_ask=1010)
    broker.set_orderbook("B", best_bid=2000, best_ask=2010)

    original_submit = broker.submit_limit_order
    def submit_and_fill(ticker, side, qty, price):
        ok, oid = original_submit(ticker, side, qty, price)
        if ok:
            broker.simulate_fill(oid, qty)
        return ok, oid
    broker.submit_limit_order = submit_and_fill

    orders = [
        OrderRequest("A", "Aco", "BUY", qty=1, ref_price=1000),
        OrderRequest("B", "Bco", "SELL", qty=1, ref_price=2010),
    ]

    results = execute_batch(broker, orders, max_order_krw=100_000_000,
                             sleep_fn=clock.sleep, now_fn=clock.tick,
                             inter_order_sleep=0)
    # Both should be fully filled
    assert all(r.fully_filled for r in results)
    # First submit in history should be the SELL (B)
    assert broker.submit_history[0][0] == "B"
    assert broker.submit_history[0][1] == "SELL"
    assert broker.submit_history[1][0] == "A"
    assert broker.submit_history[1][1] == "BUY"


def test_batch_session_timeout(broker, clock):
    """Orders not started by deadline should be marked abandoned."""
    broker.set_orderbook("A", best_bid=1000, best_ask=1010)
    broker.set_orderbook("B", best_bid=2000, best_ask=2010)

    orders = [
        OrderRequest("A", "Aco", "BUY", qty=1, ref_price=1000),
        OrderRequest("B", "Bco", "BUY", qty=1, ref_price=2000),
    ]

    # Custom sleep that advances the clock by a LOT so the batch deadline trips
    def big_sleep(seconds):
        clock.sleep(seconds * 600)   # each 'sleep' = 10 min of real time

    results = execute_batch(
        broker, orders,
        max_order_krw=100_000_000,
        max_session_seconds=60,        # only 60 seconds
        max_iterations=1,
        sleep_fn=big_sleep,
        now_fn=clock.tick,
        inter_order_sleep=0,
    )
    # At least one should be session_timeout
    timeouts = [r for r in results if any("session_timeout" in n for n in r.notes)]
    assert len(timeouts) >= 1
