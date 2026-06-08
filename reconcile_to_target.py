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
  - SAFETY GUARD (2026-06-08): a held ticker that IS still in the target basket but
    whose target_qty floored to 0 — its share price exceeds the per-name budget
    (capital*weight), or it couldn't be priced (no_quote) — is KEPT, not sold.
    Selling it would exit a name we want to hold purely because of capital
    granularity (e.g. a ~600k share when the per-name budget is ~333k). Only names
    that genuinely LEFT the basket are sold to zero; basket members are never
    floor-sold.
  - MINIMAL-CHURN BAND (2026-06-08, --rebalance-band, default 25%): a held basket
    member whose position is within the band of its intended KRW allocation
    (capital*weight) is left alone — no share-by-share re-truing. Drift is measured
    in KRW, not rounded shares. Genuine adds/drops always execute; only small
    weight-drift trades are suppressed. So a routine reshuffle trades just what
    actually changed (e.g. SELL the dropped name + BUY the new one). Set 0 for a
    full per-share rebalance.

Every target name gets an order attempt — so even a locked-limit-up (점상한가)
name that can't fill still gets a resting BUY (it just abandons after the peg
loop). That's the "we at least tried to buy them" behaviour.

Patient execution via peg_executor.execute_batch: SELLs run first (free cash),
then BUYs; orders rest at the touch, re-peg only when the touch moves against
us, and abandon unfilled qty after the peg loop — never chasing the spread.

Usage (LIVE = real money; ALWAYS test --mock first):
    python reconcile_to_target.py --portfolio basket.json --capital auto --mock
    python reconcile_to_target.py --portfolio basket.json --capital auto              # LIVE
    python reconcile_to_target.py --portfolio basket.json --capital auto --deploy-frac 0.98
    python reconcile_to_target.py --portfolio basket.json --capital 10000000          # fixed sum

  --capital auto sizes to the account's live NAV (rebalance to actual value); a fixed
  number is for when you're deliberately adding/withdrawing cash. --deploy-frac keeps a
  cash buffer (e.g. 0.98) so a near-100% deploy never starves the executor.

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
                           buy_band: float, luk_band: float, aggressive_sells: bool,
                           rebalance_band: float = 0.0):
    """Returns (orders, plan_rows). plan_rows is a printable diff incl. holds.

    Per-order give-up band: BUY on the high-momentum LUK leg uses `luk_band`
    (those names are often locked at the limit — a wider band or it never fills),
    other BUYs use `buy_band`. SELLs use no band when `aggressive_sells` (you want
    to be OUT of a dropped name — chase the ask to fill), else `buy_band`.

    `rebalance_band` (>0 enables) is the minimal-churn tolerance: a HELD basket member
    whose position is within this fraction of its intended KRW allocation (capital*weight)
    is left alone rather than re-trued share-by-share. Genuine adds (new names) and genuine
    drops (names that left the basket) always execute; only small weight-drift re-truing is
    suppressed. Default 0.0 here (off) — the CLI sets it to 0.25."""
    holdings = broker.get_holdings()  # {ticker: {qty, name}}
    logger.info("Fetched %d current holdings from KIS.", len(holdings))

    # Target quantities, sized at the live price.
    target_qty: dict[str, int] = {}
    target_name: dict[str, str] = {}
    target_ref: dict[str, int] = {}
    price_by: dict[str, int] = {}        # live price per target name (for the tolerance band)
    tgt_value: dict[str, float] = {}     # intended KRW allocation = capital * weight
    no_quote: list[str] = []
    for p in portfolio.positions:
        if p.side != "BUY" or not p.weight:
            continue
        tk = p.stock_code
        target_name[tk] = p.stock_name or tk
        target_ref[tk] = int(p.reference_close or 0)
        tgt_value[tk] = capital * float(p.weight)
        try:
            price = broker.get_price(tk)
        except Exception as e:  # noqa: BLE001
            logger.warning("price fetch failed for %s: %s", tk, e)
            price = 0
        price_by[tk] = price
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
        # SAFETY GUARD: never SELL a name that is STILL in the target basket but whose
        # target floored to 0 (priced above the per-name budget) or couldn't be priced.
        # Genuinely-dropped names (NOT in target_name) are unaffected and still sell to 0.
        if tk in target_name and tgt == 0 and held > 0:
            reason = "no_quote — kept (not floor-sold)" if tk in no_quote \
                else "floors <1 share at this capital — kept (not floor-sold)"
            plan_rows.append(("HOLD", tk, name, held, tgt, 0, reason))
            continue
        # TOLERANCE BAND: skip resizing a HELD basket member that is still within `rebalance_band`
        # of its intended KRW allocation. Drift is measured in KRW vs capital*weight (NOT the
        # rounded share target), so whole-share lumpiness on low-priced / low-count names doesn't
        # read as a huge %. Genuine ADDS (held==0) and genuine DROPS (not in target_name) are NOT
        # banded — they always execute. Material drift (> band) still rebalances to the share target.
        if rebalance_band > 0 and tk in target_name and held > 0 and tgt > 0:
            price = price_by.get(tk, 0)
            tv = tgt_value.get(tk, 0.0)
            if price > 0 and tv > 0 and abs(held * price - tv) / tv <= rebalance_band:
                plan_rows.append(("HOLD", tk, name, held, tgt, 0,
                                  "within %.0f%% band — kept" % (rebalance_band * 100)))
                continue
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
    p.add_argument("--capital", required=True,
                   help="Total capital in whole KRW, or 'auto' to size to the account's live NAV "
                        "(총평가/순자산 read from KIS). For a REBALANCE use 'auto' — a fixed number "
                        "over-deploys after a drawdown (BUYs reject for lack of cash) and "
                        "under-deploys after a rally (idle cash).")
    p.add_argument("--deploy-frac", type=float, default=1.0,
                   help="Fraction of capital to actually deploy — a cash buffer. e.g. 0.98 keeps "
                        "~2%% cash so a near-100%% deploy never starves the executor's cancel-"
                        "replace. Default 1.0. Applied to --capital (incl. the 'auto' NAV).")
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
    p.add_argument("--rebalance-band", type=float, default=0.25,
                   help="Minimal-churn tolerance: leave a held basket name alone if its position "
                        "is within this fraction of its target KRW allocation (capital*weight). "
                        "Only genuine adds/drops + materially-drifted names trade. Default 0.25 "
                        "(25%%). Set 0 to re-true every name share-by-share (full rebalance).")
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

    # Resolve capital: 'auto' -> the account's live NAV (rebalance to actual value); else whole KRW.
    if str(args.capital).strip().lower() in ("auto", "nav"):
        try:
            acct = broker.get_account_value()
        except Exception as e:  # noqa: BLE001
            logger.error("--capital auto: could not read account NAV: %s", e)
            return 3
        base_capital = acct["nav"]
        logger.info("--capital auto: NAV=%s KRW (cash %s + stock %s)",
                    _fmt(acct["nav"]), _fmt(acct["cash"]), _fmt(acct["stock_value"]))
        if base_capital <= 0:
            logger.error("--capital auto: account NAV is %s — nothing to deploy.", _fmt(base_capital))
            return 3
    else:
        try:
            base_capital = int(str(args.capital).replace(",", ""))
        except ValueError:
            logger.error("--capital must be whole KRW or 'auto', got %r", args.capital)
            return 2
    if not (0 < args.deploy_frac <= 1.0):
        logger.error("--deploy-frac must be in (0, 1.0], got %s", args.deploy_frac)
        return 2
    capital = int(base_capital * args.deploy_frac)
    if args.deploy_frac != 1.0:
        logger.info("--deploy-frac %.3f -> deploying %s of %s KRW",
                    args.deploy_frac, _fmt(capital), _fmt(base_capital))

    orders, plan_rows = build_reconcile_orders(
        broker, portfolio, capital, src_map=src_map,
        buy_band=args.drift_band, luk_band=args.luk_band,
        aggressive_sells=args.aggressive_sells, rebalance_band=args.rebalance_band)
    print_plan(plan_rows, capital)

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
