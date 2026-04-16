"""
Passive peg-to-best execution algorithm.

For each order, the executor joins the appropriate side of the orderbook
(best bid for BUY, best ask for SELL) and waits. If the order doesn't
fill and the touch moves *against* us, we cancel and re-place at the
new level. If the touch stays still or moves *in our favour*, we hold.

After `max_iterations`, any unfilled quantity is abandoned — we do NOT
chase the price. Users who want guaranteed fills should use a market
order via their broker directly.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

from kis_passive_trader.broker_base import BrokerAPI

logger = logging.getLogger(__name__)


@dataclass
class OrderRequest:
    """A single order to execute."""
    ticker: str
    stock_name: str
    side: str          # "BUY" or "SELL"
    qty: int
    ref_price: int = 0   # Snapshot price at payload generation, for sanity checks


@dataclass
class OrderResult:
    """Outcome of a single execute_order call."""
    request: OrderRequest
    filled_qty: int
    abandoned_qty: int
    iterations_used: int
    duration_seconds: float
    peg_prices: list[int] = field(default_factory=list)   # Every peg used
    order_ids: list[str] = field(default_factory=list)    # Every order id (audit)
    notes: list[str] = field(default_factory=list)

    @property
    def fully_filled(self) -> bool:
        return self.filled_qty >= self.request.qty


def _should_repeg(side: str, current_peg: int, new_peg: int) -> bool:
    """True iff the touch moved *against* us since the last peg.

    BUY: we're on the bid. best_bid moved UP -> we're now behind the
         queue at our old price. Re-peg.
    SELL: we're on the ask. best_ask moved DOWN -> same logic.
    Moves in our favour (bid down for BUY, ask up for SELL) are ignored
    — our existing order is still at-or-better than touch.
    """
    if side == "BUY":
        return new_peg > current_peg
    return new_peg < current_peg


def execute_order(
    broker: BrokerAPI,
    request: OrderRequest,
    *,
    max_iterations: int = 30,
    poll_seconds: float = 8.0,
    max_order_krw: int = 5_000_000,
    price_deviation_abort: float = 0.15,
    sleep_fn=time.sleep,
    now_fn=datetime.now,
) -> OrderResult:
    """Execute a single order with the peg-to-best strategy.

    Returns an OrderResult with filled / abandoned quantities and an audit
    trail of peg prices, order ids, and any notes.
    """
    start = now_fn()
    result = OrderResult(
        request=request,
        filled_qty=0,
        abandoned_qty=0,
        iterations_used=0,
        duration_seconds=0.0,
    )

    if request.qty <= 0:
        result.notes.append("zero_qty_skipped")
        return result

    # ── Initial sanity: orderbook + price deviation + size cap ──
    try:
        initial_ob = broker.get_orderbook(request.ticker)
    except Exception as e:
        result.notes.append(f"orderbook_error_initial: {e}")
        result.abandoned_qty = request.qty
        return result

    initial_peg = initial_ob.best_bid if request.side == "BUY" else initial_ob.best_ask
    if initial_peg <= 0:
        result.notes.append(
            f"no_quote_available (bid={initial_ob.best_bid}, ask={initial_ob.best_ask})"
        )
        result.abandoned_qty = request.qty
        return result

    est_krw = initial_peg * request.qty
    if est_krw > max_order_krw:
        result.notes.append(
            f"rejected_order_size: est ₩{est_krw:,} > cap ₩{max_order_krw:,}"
        )
        result.abandoned_qty = request.qty
        return result

    if request.ref_price > 0:
        deviation = abs(initial_peg - request.ref_price) / request.ref_price
        if deviation > price_deviation_abort:
            result.notes.append(
                f"rejected_price_deviation: {deviation*100:.1f}% "
                f"(ref=₩{request.ref_price:,}, now=₩{initial_peg:,})"
            )
            result.abandoned_qty = request.qty
            return result

    # ── Main peg loop ──
    remaining = request.qty
    current_peg: int = 0
    current_order_id: str | None = None
    # Cumulative fill on the *current* open order only. Reset to 0 each
    # time we submit a fresh order. Lets us compute delta fills correctly.
    prior_fill_on_current_order = 0

    def _reconcile_current_order() -> None:
        """Poll the current order and update remaining / result.filled_qty
        with any newly-reported fills since the last check. Resets
        `current_order_id` to None if the order is closed."""
        nonlocal remaining, current_order_id, prior_fill_on_current_order
        if current_order_id is None:
            return
        try:
            status = broker.get_order_status(request.ticker, current_order_id)
        except Exception as e:
            result.notes.append(f"status_error: {e}")
            return
        delta = max(0, status.filled_qty - prior_fill_on_current_order)
        if delta > 0:
            result.filled_qty += delta
            remaining -= delta
            prior_fill_on_current_order = status.filled_qty
        if not status.is_open:
            current_order_id = None

    while remaining > 0 and result.iterations_used < max_iterations:
        # Fresh orderbook for this iteration
        try:
            ob = broker.get_orderbook(request.ticker)
        except Exception as e:
            result.notes.append(f"orderbook_error_iter_{result.iterations_used}: {e}")
            break
        peg = ob.best_bid if request.side == "BUY" else ob.best_ask
        if peg <= 0:
            result.notes.append(f"no_quote_iter_{result.iterations_used}")
            break

        need_replace = (
            current_order_id is None
            or _should_repeg(request.side, current_peg, peg)
        )

        if need_replace and current_order_id is not None:
            # Before cancelling, reconcile any fills so we don't double-count
            _reconcile_current_order()
            if current_order_id is not None:
                cancel_ok, cancel_msg = broker.cancel_order(request.ticker, current_order_id)
                if not cancel_ok:
                    result.notes.append(f"cancel_failed: {cancel_msg}")
                # Final reconcile in case a fill landed during cancellation
                _reconcile_current_order()
                current_order_id = None

        if remaining <= 0:
            break

        if need_replace:
            ok, order_ref = broker.submit_limit_order(
                request.ticker, request.side, remaining, peg
            )
            result.iterations_used += 1
            if not ok:
                result.notes.append(
                    f"submit_failed_iter_{result.iterations_used}: {order_ref}"
                )
                sleep_fn(poll_seconds)
                continue
            current_order_id = order_ref
            current_peg = peg
            prior_fill_on_current_order = 0
            result.peg_prices.append(peg)
            result.order_ids.append(order_ref)
        else:
            result.iterations_used += 1

        sleep_fn(poll_seconds)

        _reconcile_current_order()

    # ── End of loop: cancel any outstanding, record abandoned ──
    if current_order_id is not None:
        broker.cancel_order(request.ticker, current_order_id)
        _reconcile_current_order()
        if remaining > 0:
            result.notes.append(
                f"max_iterations_abandoned: {remaining}/{request.qty} unfilled"
            )

    result.abandoned_qty = remaining
    result.duration_seconds = (now_fn() - start).total_seconds()
    return result


def execute_batch(
    broker: BrokerAPI,
    orders: list[OrderRequest],
    *,
    max_iterations: int = 30,
    poll_seconds: float = 8.0,
    max_order_krw: int = 5_000_000,
    max_session_seconds: float = 30 * 60,
    inter_order_sleep: float = 0.5,
    sleep_fn=time.sleep,
    now_fn=datetime.now,
    on_progress=None,
) -> list[OrderResult]:
    """Execute a batch of orders: all SELLs first (free cash), then BUYs.

    Enforces a total session time limit — orders not started by the
    deadline are marked abandoned with a `session_timeout` note.
    """
    ordered = sorted(orders, key=lambda o: 0 if o.side == "SELL" else 1)
    session_start = now_fn()
    results: list[OrderResult] = []

    for i, req in enumerate(ordered):
        elapsed = (now_fn() - session_start).total_seconds()
        if elapsed > max_session_seconds:
            for remaining_req in ordered[i:]:
                r = OrderResult(
                    request=remaining_req,
                    filled_qty=0,
                    abandoned_qty=remaining_req.qty,
                    iterations_used=0,
                    duration_seconds=0.0,
                )
                r.notes.append("session_timeout")
                results.append(r)
            break

        if on_progress:
            on_progress(i, len(ordered), req)

        r = execute_order(
            broker, req,
            max_iterations=max_iterations,
            poll_seconds=poll_seconds,
            max_order_krw=max_order_krw,
            sleep_fn=sleep_fn,
            now_fn=now_fn,
        )
        results.append(r)

        if inter_order_sleep > 0 and i < len(ordered) - 1:
            sleep_fn(inter_order_sleep)

    return results
