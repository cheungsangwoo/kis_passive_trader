#!/usr/bin/env python3
"""
Reconcile-to-target: make your ACTUAL KIS holdings match a freshly-downloaded
target portfolio JSON, by SELL/BUY-ing the difference with patient (no-spread-
cross) limit orders.

Full-account match (the account is assumed dedicated to the strategy):
  - For each ticker, target_qty = floor(capital * weight / current_price).
  - delta = target_qty - held_qty.
      delta > 0  -> BUY  delta      (rest at best bid)
      delta < 0  -> SELL -delta     (rest at best ask)
      delta == 0 -> hold (no order)
  - A held ticker NOT in the target has target_qty 0 -> SELL all of it.

Every target name gets an order attempt — so even a locked-limit-up (점상한가)
name that can't fill still gets a resting BUY (it just abandons after the peg
loop). That's the "we at least tried to buy them" behaviour.

Patient execution via peg_executor.execute_batch: SELLs run first (free cash),
then BUYs; orders rest at the touch, re-peg only when the touch moves against
us, and abandon unfilled qty after the peg loop — never chasing the spread.

Usage (LIVE = real money; ALWAYS test --mock first):
    python reconcile_to_target.py --portfolio basket.json --capital 10000000 --mock
    python reconcile_to_target.py --portfolio basket.json --capital 10000000        # LIVE

This places REAL orders on your own account with your own keys. Read DISCLAIMER.md.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from dotenv import load_dotenv

from kis_passive_trader.kis_api import KisAPI
from kis_passive_trader.peg_executor import OrderRequest, execute_batch
from kis_passive_trader.portfolio import load_portfolio, PortfolioError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("reconcile")


def _fmt(n: int) -> str:
    return f"{n:,}"


def build_reconcile_orders(broker, portfolio, capital: int, *, src_map: dict,
                           buy_band: float, luk_band: float, aggressive_sells: bool):
    """Returns (orders, plan_rows). plan_rows is a printable diff incl. holds.

    Per-order give-up band: BUY on the high-momentum LUK leg uses `luk_band`
    (those names are often locked at the limit — a wider band or it never fills),
    other BUYs use `buy_band`. SELLs use no band when `aggressive_sells` (you want
    to be OUT of a dropped name — chase the ask to fill), else `buy_band`."""
    holdings = broker.get_holdings()  # {ticker: {qty, name}}
    logger.info("Fetched %d current holdings from KIS.", len(holdings))

    # Target quantities, sized at the live price.
    target_qty: dict[str, int] = {}
    target_name: dict[str, str] = {}
    target_ref: dict[str, int] = {}
    no_quote: list[str] = []
    for p in portfolio.positions:
        if p.side != "BUY" or not p.weight:
            continue
        tk = p.stock_code
        target_name[tk] = p.stock_name or tk
        target_ref[tk] = int(p.reference_close or 0)
        try:
            price = broker.get_price(tk)
        except Exception as e:  # noqa: BLE001
            logger.warning("price fetch failed for %s: %s", tk, e)
            price = 0
        if price <= 0:
            no_quote.append(tk)
            target_qty[tk] = 0
            continue
        target_qty[tk] = int((capital * float(p.weight)) // price)

    all_tickers = sorted(set(target_qty) | set(holdings))
    orders: list[OrderRequest] = []
    plan_rows = []
    for tk in all_tickers:
        held = holdings.get(tk, {}).get("qty", 0)
        tgt = target_qty.get(tk, 0)
        name = target_name.get(tk) or holdings.get(tk, {}).get("name", "") or tk
        delta = tgt - held
        if delta > 0:
            action, qty = "BUY", delta
        elif delta < 0:
            action, qty = "SELL", -delta
        else:
            action, qty = "HOLD", 0
        src = src_map.get(tk)
        if action == "SELL":
            band = None if aggressive_sells else buy_band
        else:
            band = luk_band if src == "luk_04" else buy_band
        note = "no_quote" if tk in no_quote else (
            ("LUK %.0f%%" % (band * 100)) if (action == "BUY" and src == "luk_04")
            else (("band %.0f%%" % (band * 100)) if band is not None else "aggr"))
        plan_rows.append((action, tk, name, held, tgt, qty, note))
        if action in ("BUY", "SELL") and qty > 0:
            orders.append(OrderRequest(ticker=tk, stock_name=name, side=action,
                                       qty=qty, ref_price=target_ref.get(tk, 0),
                                       drift_band=band))
    return orders, plan_rows


def print_plan(plan_rows, capital: int) -> None:
    print("\n" + "=" * 78)
    print(f"  RECONCILE-TO-TARGET PLAN   capital={_fmt(capital)} KRW")
    print("=" * 78)
    print(f"  {'Action':<5} {'Ticker':<8} {'Name':<16} {'Held':>7} {'Target':>7} {'Order':>7}  Note")
    print("  " + "-" * 74)
    n_buy = n_sell = n_hold = 0
    for action, tk, name, held, tgt, qty, note in plan_rows:
        if action == "BUY":
            n_buy += 1
        elif action == "SELL":
            n_sell += 1
        else:
            n_hold += 1
        if action == "HOLD" and not note:
            continue  # don't clutter with unchanged rows
        print(f"  {action:<5} {tk:<8} {name[:16]:<16} {_fmt(held):>7} "
              f"{_fmt(tgt):>7} {_fmt(qty):>7}  {note}")
    print("  " + "-" * 74)
    print(f"  BUY {n_buy}   SELL {n_sell}   HOLD/unchanged {n_hold}")
    print("=" * 78)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--portfolio", required=True, help="Target portfolio JSON (F6 export).")
    p.add_argument("--capital", required=True, type=int, help="Total capital to deploy, whole KRW.")
    p.add_argument("--mock", action="store_true", help="KIS paper server (모의투자). Test here first.")
    p.add_argument("--max-order-krw", type=int, default=5_000_000,
                   help="Per-order size cap (peg_executor). Default 5,000,000.")
    p.add_argument("--max-session-min", type=int, default=120,
                   help="Total execution session cap, minutes. Default 120 (run toward close).")
    p.add_argument("--poll-seconds", type=float, default=8.0)
    p.add_argument("--drift-band", type=float, default=0.07,
                   help="BUY give-up band vs reference_close — stop chasing past this. Default 0.07 (7%%).")
    p.add_argument("--luk-band", type=float, default=0.12,
                   help="BUY band for the LUK (limit-up) leg — wider, those names gap. Default 0.12 (12%%).")
    p.add_argument("--max-iter", type=int, default=120,
                   help="Max re-peg iterations per order (persistence to fill). Default 120.")
    p.add_argument("--aggressive-sells", action="store_true",
                   help="SELLs ignore the band (chase the ask to fully exit dropped names).")
    p.add_argument("--yes", action="store_true", help="Skip the confirm prompt (non-interactive).")
    p.add_argument("--dry-run", action="store_true", help="Print the plan and exit; place NO orders.")
    args = p.parse_args()

    load_dotenv()
    try:
        portfolio = load_portfolio(args.portfolio)
        raw = json.loads(open(args.portfolio, encoding="utf-8").read())
        src_map = {p.get("stock_code"): p.get("combo_source")
                   for p in raw.get("positions", [])}
    except (PortfolioError, ValueError, OSError) as e:
        logger.error("Could not load portfolio: %s", e)
        return 2

    broker = KisAPI(paper=args.mock)
    mode = "모의투자 (paper)" if args.mock else "⚠ 실전투자 (LIVE — real money)"
    print(f"Connected to KIS — {mode}")
    try:
        broker.authenticate()
    except Exception as e:  # noqa: BLE001
        logger.error("KIS auth failed: %s", e)
        return 3

    orders, plan_rows = build_reconcile_orders(
        broker, portfolio, args.capital, src_map=src_map,
        buy_band=args.drift_band, luk_band=args.luk_band,
        aggressive_sells=args.aggressive_sells)
    print_plan(plan_rows, args.capital)

    if args.dry_run:
        print("\nDRY-RUN — no orders placed.")
        return 0
    if not orders:
        print("\nAlready matched — nothing to do.")
        return 0

    if not args.yes:
        if not args.mock:
            print("\n⚠  This will place REAL orders on your LIVE account.")
        try:
            ans = input(f"Proceed with {len(orders)} order(s) (SELLs first, then BUYs)? (y/N): ")
        except (KeyboardInterrupt, EOFError):
            print("\nAborted.")
            return 130
        if ans.strip().lower() != "y":
            print("Aborted.")
            return 0

    try:
        results = execute_batch(
            broker, orders,
            max_order_krw=args.max_order_krw,
            max_session_seconds=args.max_session_min * 60,
            poll_seconds=args.poll_seconds,
            max_iterations=args.max_iter,
            drift_band=args.drift_band,
        )
    except KeyboardInterrupt:
        print("\n⏹ Ctrl-C — execution interrupted. Any order still resting was NOT "
              "auto-cancelled; check your KIS account / app and cancel manually.")
        return 130

    print("\n" + "=" * 78)
    print("  RESULTS")
    print("=" * 78)
    for r in results:
        mark = "✓" if r.fully_filled else ("·" if r.filled_qty == 0 else "~")
        note = f"  {'; '.join(r.notes)}" if r.notes else ""
        print(f"  {mark} {r.request.side:<4} {r.request.ticker:<8} "
              f"{r.request.stock_name[:16]:<16} filled {_fmt(r.filled_qty)}/"
              f"{_fmt(r.request.qty)}{note}")
    filled = sum(r.filled_qty for r in results)
    asked = sum(r.request.qty for r in results)
    print("  " + "-" * 74)
    print(f"  filled {_fmt(filled)} / {_fmt(asked)} shares across {len(results)} orders")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
