"""
patient_maker — patient, limit-only portfolio execution via KIS Open API.

Reads an F6 portfolio JSON, sizes each BUY by `capital * weight` (floor +
greedy remainder), and rests limit orders at the current best bid (BUY) or
best ask (SELL). It **never crosses the spread**: orders sit *at* the touch
and are only re-pegged when the touch moves *against* us. It reprices on a
fixed cadence until every order fills, the user interrupts with Ctrl-C, or
the market closes (15:20 KST).

Execution flow:
    1. Plan      — fetch quotes, size every position, flag price drift.
    2. Confirm   — one overall y/N (no timeout).
    3. Drift     — per-position y/N (5-min timeout) on any position whose
                   live bid is > threshold x the signal's reference close.
    4. Place     — rest one limit order per non-skipped position.
    5. Reprice   — every --reprice-interval seconds: detect fills, and
                   re-peg any order whose touch has moved against us.
    6. Exit      — all filled / Ctrl-C / market close. Cancel outstanding,
                   write final state, print summary.

State is persisted (resumably) to .patient_maker_state_<signal_id>_<as_of>.json.
Re-running the same command reconciles open orders with KIS and continues.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass
from datetime import datetime

from dotenv import load_dotenv

from kis_passive_trader.broker_base import BrokerAPI, Orderbook
from kis_passive_trader.peg_executor import _should_repeg
from kis_passive_trader.portfolio import (
    Portfolio,
    Position,
    PortfolioError,
    allocate_buy_quantities,
    load_portfolio,
)
from kis_passive_trader.prompt import TimedInput
from kis_passive_trader.state import (
    KST,
    PositionState,
    SessionState,
    StateMismatchError,
    load_state,
    now_kst_iso,
    save_state_atomic,
    state_path,
    validate_resume,
)

logger = logging.getLogger("patient_maker")

DRIFT_PROMPT_TIMEOUT = 300.0   # 5 minutes, per the brief
MARKET_CLOSE_HOUR = 15
MARKET_CLOSE_MINUTE = 20


# ──────────────────────────────────────────────────────────────────────────
# Small helpers
# ──────────────────────────────────────────────────────────────────────────

def _affirmative(answer: str | None) -> bool:
    return answer is not None and answer.strip().lower() in ("y", "yes")


def peg_price(ob: Orderbook, side: str) -> int:
    """Our resting price for a side: best bid for BUY, best ask for SELL."""
    return ob.best_bid if side.upper() == "BUY" else ob.best_ask


def _now_kst() -> datetime:
    return datetime.now(KST)


def _market_close(now: datetime) -> datetime:
    return now.replace(
        hour=MARKET_CLOSE_HOUR, minute=MARKET_CLOSE_MINUTE, second=0, microsecond=0
    )


def is_after_market_close(now: datetime) -> bool:
    return now >= _market_close(now)


def seconds_until_wake(now: datetime, interval: float) -> float:
    """Sleep until the sooner of the next reprice and market close."""
    to_close = (_market_close(now) - now).total_seconds()
    return max(0.0, min(float(interval), to_close))


# ──────────────────────────────────────────────────────────────────────────
# Planning
# ──────────────────────────────────────────────────────────────────────────

@dataclass
class PlanItem:
    position: Position
    peg_price: int            # plan-time top-of-book on our side (0 = no quote)
    target_qty: int
    budget: int               # capital * weight for BUY; 0 for SELL
    drift_pct: float | None   # (peg/ref - 1)*100 for BUY w/ reference, else None
    warn: bool                # True if peg > threshold * reference_close (BUY)
    status: str               # pending | skipped_no_quote | skipped_no_capital
    reason: str = ""


def build_plan(
    portfolio: Portfolio,
    broker: BrokerAPI,
    capital: int,
    *,
    side_filter: str = "both",
    threshold: float = 1.05,
) -> list[PlanItem]:
    """Quote every in-scope position, size it, and flag drift."""
    positions = [
        p for p in portfolio.positions
        if side_filter == "both" or p.side.lower() == side_filter
    ]

    peg: dict[str, int] = {}
    for p in positions:
        try:
            ob = broker.get_orderbook(p.stock_code)
            peg[p.stock_code] = peg_price(ob, p.side)
        except Exception as e:   # noqa: BLE001 — any broker error == no quote
            logger.warning("orderbook fetch failed for %s: %s", p.stock_code, e)
            peg[p.stock_code] = 0

    buys = [p for p in positions if p.side == "BUY" and peg[p.stock_code] > 0]
    alloc = allocate_buy_quantities(buys, capital, peg)

    items: list[PlanItem] = []
    for p in positions:
        price = peg[p.stock_code]
        drift_pct: float | None = None
        warn = False
        if p.side == "BUY" and p.reference_close > 0 and price > 0:
            drift_pct = (price / p.reference_close - 1) * 100
            warn = price > threshold * p.reference_close

        if price <= 0:
            items.append(PlanItem(p, 0, 0, 0, None, False,
                                  "skipped_no_quote", "no top-of-book quote"))
            continue

        if p.side == "BUY":
            budget = int(capital * p.weight)
            qty = alloc.get(p.stock_code, 0)
            if qty <= 0:
                items.append(PlanItem(p, price, 0, budget, drift_pct, warn,
                                      "skipped_no_capital", "budget < 1 share"))
                continue
        else:  # SELL — explicit qty
            budget = 0
            qty = p.qty

        items.append(PlanItem(p, price, qty, budget, drift_pct, warn, "pending"))

    return items


def plan_to_state(
    items: list[PlanItem],
    portfolio: Portfolio,
    capital: int,
    *,
    reprice_interval: int,
    threshold: float,
) -> SessionState:
    """Materialize a confirmed plan into persisted session state."""
    positions: list[PositionState] = []
    for it in items:
        ps = PositionState(
            stock_code=it.position.stock_code,
            stock_name=it.position.stock_name,
            target_qty=it.target_qty,
            side=it.position.side,
            status=it.status,
        )
        if it.status.startswith("skipped"):
            ps.add_event("skipped", status=it.status, reason=it.reason)
        positions.append(ps)

    meta = {
        "signal_id": portfolio.signal_id,
        "signal_name": portfolio.signal_name,
        "as_of": portfolio.as_of,
        "capital": int(capital),
        "started_at": now_kst_iso(),
        "reprice_interval_seconds": int(reprice_interval),
        "threshold": float(threshold),
    }
    return SessionState(meta, positions)


# ──────────────────────────────────────────────────────────────────────────
# Drift confirmation
# ──────────────────────────────────────────────────────────────────────────

def apply_drift_decisions(
    items: list[PlanItem],
    state: SessionState,
    timed_input: TimedInput,
    *,
    threshold: float,
    timeout: float = DRIFT_PROMPT_TIMEOUT,
    state_file=None,
) -> None:
    """Prompt (y/N, timed) on each pending position whose price has drifted.

    A position is "locked in" by a single accept — the reprice loop never
    re-prompts even if the bid runs further.
    """
    for it in items:
        ps = state.position(it.position.stock_code)
        if ps is None or ps.status != "pending" or not it.warn:
            continue

        ref = it.position.reference_close
        drift = it.drift_pct if it.drift_pct is not None else 0.0
        msg = (
            f"\n  WARNING: {it.position.stock_code} {it.position.stock_name}\n"
            f"    Reference close ({it.position.reference_close_date}): {ref:,} KRW\n"
            f"    Current best bid:             {it.peg_price:,} KRW ({drift:+.1f}%)\n"
            f"    Threshold ({threshold:g}x): exceeded\n"
            f"    Target quantity: {it.target_qty:,} shares "
            f"(~{it.peg_price * it.target_qty:,} KRW)\n"
            f"  Proceed with purchase? (y/N) [{int(timeout/60)} min timeout -> skip]: "
        )
        answer = timed_input.prompt(msg, timeout)

        if answer is None:
            ps.status = "skipped_user_timeout"
            ps.warning_decision = "timed_out"
            ps.add_event("drift_timeout", drift_pct=round(drift, 2))
            print(f"  -> timed out, skipping {it.position.stock_code}")
        elif _affirmative(answer):
            ps.warning_decision = "accepted"
            ps.add_event("drift_accepted", drift_pct=round(drift, 2))
            print(f"  -> accepted {it.position.stock_code}")
        else:
            ps.status = "skipped_user_declined"
            ps.warning_decision = "declined"
            ps.add_event("drift_declined", drift_pct=round(drift, 2))
            print(f"  -> declined, skipping {it.position.stock_code}")

        if state_file is not None:
            save_state_atomic(state_file, state)


# ──────────────────────────────────────────────────────────────────────────
# Order placement + reprice
# ──────────────────────────────────────────────────────────────────────────

def _reconcile(broker: BrokerAPI, pos: PositionState) -> None:
    """Poll the current order and fold any new fills into `pos.filled_qty`.

    Clears `latest_order_id` if the order is no longer open.
    """
    if not pos.latest_order_id:
        return
    try:
        st = broker.get_order_status(pos.stock_code, pos.latest_order_id)
    except Exception as e:   # noqa: BLE001
        logger.warning("status check failed for %s: %s", pos.stock_code, e)
        return
    delta = max(0, st.filled_qty - pos.current_order_filled)
    if delta > 0:
        pos.filled_qty += delta
        pos.current_order_filled = st.filled_qty
        pos.add_event("fill", qty=delta, cumulative=pos.filled_qty,
                      order_id=pos.latest_order_id)
        logger.info("%s fill +%d (%d/%d)", pos.stock_code, delta,
                    pos.filled_qty, pos.target_qty)
    if not st.is_open:
        pos.add_event("order_closed", order_id=pos.latest_order_id,
                      filled=st.filled_qty)
        pos.latest_order_id = None
        pos.current_order_filled = 0


def _place(broker: BrokerAPI, pos: PositionState) -> None:
    """Rest a fresh limit order for `pos`'s remaining quantity at the touch."""
    try:
        ob = broker.get_orderbook(pos.stock_code)
    except Exception as e:   # noqa: BLE001
        logger.error("orderbook fetch failed for %s: %s", pos.stock_code, e)
        return
    price = peg_price(ob, pos.side)
    if price <= 0:
        pos.add_event("no_quote")
        logger.warning("no quote for %s — will retry", pos.stock_code)
        return
    ok, ref = broker.submit_limit_order(
        pos.stock_code, pos.side, pos.remaining_qty, price
    )
    if ok:
        pos.latest_order_id = ref
        pos.latest_order_price = price
        pos.current_order_filled = 0
        pos.add_event("placed", qty=pos.remaining_qty, price=price, order_id=ref)
        logger.info("%s %s %d @ %d (order %s)", pos.stock_code, pos.side,
                    pos.remaining_qty, price, ref)
    else:
        pos.add_event("order_error", message=ref)
        logger.error("order submit failed for %s: %s", pos.stock_code, ref)


def place_initial_orders(broker: BrokerAPI, state: SessionState, state_file=None) -> None:
    """Place one limit order per pending position (the brief's placement phase)."""
    for pos in state.pending():
        _place(broker, pos)
        if state_file is not None:
            save_state_atomic(state_file, state)


def _cancel_and_clear(broker: BrokerAPI, pos: PositionState) -> None:
    """Capture late fills, cancel the open order, capture fills again, clear it."""
    if not pos.latest_order_id:
        return
    _reconcile(broker, pos)
    if not pos.latest_order_id:
        return
    try:
        ok, msg = broker.cancel_order(pos.stock_code, pos.latest_order_id)
        pos.add_event("cancel", order_id=pos.latest_order_id, ok=ok, message=msg)
    except Exception as e:   # noqa: BLE001
        logger.warning("cancel failed for %s: %s", pos.stock_code, e)
    _reconcile(broker, pos)
    pos.latest_order_id = None
    pos.current_order_filled = 0


def reprice_once(broker: BrokerAPI, state: SessionState) -> bool:
    """One reprice pass over all pending positions. Returns True if all terminal."""
    for pos in state.pending():
        # 1. Fold in any fills on the current order.
        _reconcile(broker, pos)
        if pos.remaining_qty <= 0:
            pos.status = "filled"
            pos.add_event("status", status="filled")
            continue

        # 2. No live order (never placed, or exchange-closed with remainder) → place.
        if pos.latest_order_id is None:
            _place(broker, pos)
            continue

        # 3. Order still open: re-peg only if the touch moved against us.
        try:
            ob = broker.get_orderbook(pos.stock_code)
        except Exception as e:   # noqa: BLE001
            logger.warning("orderbook fetch failed for %s: %s", pos.stock_code, e)
            continue
        new_peg = peg_price(ob, pos.side)
        if new_peg <= 0:
            continue   # transient missing quote — hold our resting order
        if _should_repeg(pos.side, pos.latest_order_price, new_peg):
            _cancel_and_clear(broker, pos)
            if pos.remaining_qty <= 0:
                pos.status = "filled"
                pos.add_event("status", status="filled")
            else:
                _place(broker, pos)
        # else: price unchanged or moved in our favour → hold.

    return state.all_terminal()


def cancel_all(broker: BrokerAPI, state: SessionState) -> None:
    """Cancel every outstanding order and finalize each pending position's status."""
    for pos in state.pending():
        _cancel_and_clear(broker, pos)
        if pos.remaining_qty <= 0 and pos.filled_qty > 0:
            pos.status = "filled"
        elif pos.filled_qty > 0:
            pos.status = "partial_then_cancelled"
        else:
            pos.status = "cancelled"
        pos.add_event("status", status=pos.status)


def reconcile_on_resume(broker: BrokerAPI, state: SessionState) -> None:
    """On resume, reconcile each pending order's fills against KIS."""
    for pos in state.pending():
        if not pos.latest_order_id:
            continue
        before = pos.filled_qty
        _reconcile(broker, pos)
        if pos.filled_qty > before:
            pos.add_event("reconciled_fill_from_kis", cumulative=pos.filled_qty)
            logger.info("reconciled %s: %d/%d filled while away",
                        pos.stock_code, pos.filled_qty, pos.target_qty)
        if pos.remaining_qty <= 0:
            pos.status = "filled"
            pos.add_event("status", status="filled")


def run(
    broker: BrokerAPI,
    state: SessionState,
    *,
    reprice_interval: int,
    state_file,
    sleep_fn=time.sleep,
    now_fn=_now_kst,
) -> str:
    """Reprice loop. Returns the exit reason: 'all_filled' or 'market_close'."""
    while True:
        if state.all_terminal():
            return "all_filled"
        if is_after_market_close(now_fn()):
            logger.info("Market close (15:20 KST) — cancelling outstanding orders.")
            cancel_all(broker, state)
            save_state_atomic(state_file, state)
            return "market_close"

        done = reprice_once(broker, state)
        save_state_atomic(state_file, state)
        if done:
            return "all_filled"

        secs = seconds_until_wake(now_fn(), reprice_interval)
        if secs > 0:
            logger.info("Repricing again in %ds (%d position(s) pending).",
                        int(secs), len(state.pending()))
            sleep_fn(secs)


# ──────────────────────────────────────────────────────────────────────────
# Console output
# ──────────────────────────────────────────────────────────────────────────

def print_plan(items: list[PlanItem], capital: int, portfolio: Portfolio) -> None:
    print("\n" + "=" * 76)
    print(f"  EXECUTION PLAN — {portfolio.signal_name or portfolio.signal_id}")
    print(f"  signal_id={portfolio.signal_id}  as_of={portfolio.as_of}  "
          f"cadence={portfolio.rebalance_cadence or '-'}")
    print(f"  Capital: {capital:,} KRW")
    print("=" * 76)
    print(f"  {'Side':<4} {'Code':<8} {'Name':<16} {'Qty':>6} {'Price':>11} "
          f"{'Budget':>13}  Note")
    print("  " + "-" * 72)
    deployed = 0
    for it in items:
        flag = "  ⚠ DRIFT" if it.warn else ""
        note = it.status if it.status != "pending" else flag.strip()
        price_s = f"{it.peg_price:,}" if it.peg_price else "-"
        budget_s = f"{it.budget:,}" if it.budget else "-"
        if it.status == "pending" and it.position.side == "BUY":
            deployed += it.peg_price * it.target_qty
        print(f"  {it.position.side:<4} {it.position.stock_code:<8} "
              f"{it.position.stock_name[:16]:<16} {it.target_qty:>6,} "
              f"{price_s:>11} {budget_s:>13}  {note}")
    print("  " + "-" * 72)
    residual = capital - deployed
    print(f"  Deployed (BUY): {deployed:,} KRW   |   Cash residual: {residual:,} KRW")
    n_warn = sum(1 for it in items if it.warn and it.status == "pending")
    if n_warn:
        print(f"  {n_warn} position(s) exceed the drift threshold and will prompt "
              f"individually (5-min timeout each).")
    print()


def print_summary(state: SessionState, exit_reason: str = "") -> None:
    print("\n" + "=" * 76)
    print("  SESSION SUMMARY" + (f"  ({exit_reason})" if exit_reason else ""))
    print("=" * 76)
    for pos in state.positions:
        mark = {"filled": "✓"}.get(pos.status, "~" if pos.filled_qty else "·")
        print(f"  {mark} {pos.side:<4} {pos.stock_code:<8} "
              f"{pos.stock_name[:16]:<16} {pos.filled_qty:>6,}/{pos.target_qty:<6,} "
              f"{pos.status}")
    total_t = sum(p.target_qty for p in state.positions)
    total_f = sum(p.filled_qty for p in state.positions)
    print("  " + "-" * 72)
    print(f"  Filled: {total_f:,}/{total_t:,} shares across "
          f"{len(state.positions)} position(s).")
    print("  Verify all fills in your KIS app. (체결 내역을 증권사 앱에서 확인하세요.)")
    print()


# ──────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="patient_maker",
        description="Patient limit-only portfolio execution (peg to best "
                    "bid/ask, never cross the spread) via KIS Open API.",
    )
    p.add_argument("--portfolio", required=True,
                   help="Path to an F6 portfolio JSON file.")
    p.add_argument("--capital", required=True, type=int,
                   help="Total capital to deploy, in whole KRW.")
    p.add_argument("--reprice-interval", type=int, default=300,
                   help="Seconds between reprice passes (default: 300 = 5 min).")
    p.add_argument("--side", choices=["buy", "sell", "both"], default="both",
                   help="Which sides to process (default: both).")
    p.add_argument("--threshold", type=float, default=1.05,
                   help="Warn if current bid > threshold x reference close "
                        "(default: 1.05).")
    p.add_argument("--mock", action="store_true",
                   help="Use the KIS mock (paper/모의투자) server. STRONGLY "
                        "recommended for testing.")
    p.add_argument("--force-fresh", action="store_true",
                   help="Ignore any existing state file and start over.")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Enable debug logging.")
    return p


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    # ── Load portfolio ──
    try:
        portfolio = load_portfolio(args.portfolio)
    except PortfolioError as e:
        print(f"✗ {e}")
        return 1

    sp = state_path(portfolio.signal_id, portfolio.as_of)
    existing = None if args.force_fresh else load_state(sp)
    if args.force_fresh and load_state(sp) is not None:
        print(f"⚠ --force-fresh: ignoring existing state file {sp.name}")

    # ── Connect to KIS ──
    from kis_passive_trader.kis_api import KisAPI
    try:
        broker = KisAPI(paper=args.mock)
        broker.authenticate()
    except Exception as e:   # noqa: BLE001
        print(f"✗ KIS connection failed: {e}")
        return 1
    server = "MOCK (paper/모의투자)" if args.mock else "⚠ LIVE (real money)"
    print(f"Connected to KIS — {server}")

    # ── Resume, or plan a fresh session ──
    if existing is not None:
        try:
            validate_resume(existing, portfolio.signal_id, portfolio.as_of, args.capital)
        except StateMismatchError as e:
            print(f"✗ {e}")
            return 1
        state = existing
        print(f"↻ Resuming session for {portfolio.signal_id} @ {portfolio.as_of} "
              f"({len(state.pending())} pending).")
        reconcile_on_resume(broker, state)
        save_state_atomic(sp, state)
        if state.all_terminal():
            print("✓ Nothing left to do — all positions terminal.")
            print_summary(state, "resumed-complete")
            return 0
    else:
        items = build_plan(portfolio, broker, args.capital,
                           side_filter=args.side, threshold=args.threshold)
        print_plan(items, args.capital, portfolio)
        if not any(it.status == "pending" for it in items):
            print("✓ No actionable positions (nothing to do).")
            return 0
        try:
            ans = input("  Start execution? (y/N): ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n  Cancelled.")
            return 130
        if not _affirmative(ans):
            print("  Cancelled.")
            return 0

        state = plan_to_state(items, portfolio, args.capital,
                              reprice_interval=args.reprice_interval,
                              threshold=args.threshold)
        save_state_atomic(sp, state)

        # Per-position drift confirmations (5-min timeout each). Ctrl-C here is
        # BEFORE any order is placed, so just save the partial decisions and exit
        # cleanly (no ugly traceback, no orders to cancel).
        try:
            apply_drift_decisions(items, state, TimedInput(),
                                  threshold=args.threshold,
                                  timeout=DRIFT_PROMPT_TIMEOUT, state_file=sp)
        except KeyboardInterrupt:
            print("\n⏹ Ctrl-C during drift review — no orders were placed; exiting.")
            save_state_atomic(sp, state)
            return 130
        if state.all_terminal():
            print("✓ All positions skipped — nothing to execute.")
            print_summary(state)
            return 0

        print("\nPlacing limit orders at top-of-book...")
        place_initial_orders(broker, state, state_file=sp)

    # ── Reprice loop ──
    exit_reason = ""
    try:
        exit_reason = run(broker, state, reprice_interval=args.reprice_interval,
                          state_file=sp)
    except KeyboardInterrupt:
        print("\n⏹ Ctrl-C — cancelling outstanding orders...")
        cancel_all(broker, state)
        save_state_atomic(sp, state)
        exit_reason = "ctrl_c"

    print_summary(state, exit_reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
