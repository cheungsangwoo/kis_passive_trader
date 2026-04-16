"""
Command-line interface for kis_passive_trader.

Subcommands:
    fetch     Download portfolio payload from backtest.co.kr
    preview   Show order list without any broker interaction
    execute   Authenticate, preview, confirm, execute (peg-to-best)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from kis_passive_trader.payload import (
    PayloadError,
    fetch_from_server,
    load_from_file,
    payload_to_orders,
    save_payload,
)
from kis_passive_trader.peg_executor import (
    OrderRequest,
    OrderResult,
    execute_batch,
)


DEFAULT_PAYLOAD_PATH = Path("portfolio.json")

DISCLAIMER = """
═══════════════════════════════════════════════════════════════════════
  ⚠️  법적 고지 / Legal Disclaimer

  이 프로그램은 투자 정보 조회·주문 집행 보조 도구입니다.
  투자일임업 또는 투자자문업 서비스가 아닙니다.
  모든 주문은 사용자가 직접 확인하고 실행합니다.
  투자 판단 및 그에 따른 손익은 전적으로 사용자에게 귀속됩니다.

  This software is an informational and execution-helper tool, not
  investment management or advisory service. All orders require your
  explicit confirmation before submission. All investment decisions
  and outcomes are your own responsibility.

  See DISCLAIMER.md for the full notice.
═══════════════════════════════════════════════════════════════════════
"""


def cmd_fetch(args: argparse.Namespace) -> int:
    try:
        payload = fetch_from_server(args.api_url, args.token)
    except PayloadError as e:
        print(f"✗ {e}")
        return 1
    save_payload(payload, args.output)
    n = len(payload.get("orders", []))
    print(f"✓ {payload.get('strategy', 'portfolio')}: {n} orders saved to {args.output}")
    return 0


def cmd_preview(args: argparse.Namespace) -> int:
    try:
        payload = load_from_file(args.payload)
    except PayloadError as e:
        print(f"✗ {e}")
        return 1
    orders = payload_to_orders(payload)
    _print_preview(payload, orders)
    return 0


def cmd_execute(args: argparse.Namespace) -> int:
    # Lazy import — broker requires KIS creds, no reason to load for fetch/preview
    from kis_passive_trader.kis_api import KisAPI

    try:
        payload = load_from_file(args.payload)
    except PayloadError as e:
        print(f"✗ {e}")
        return 1

    orders = payload_to_orders(payload)
    if not orders:
        print("✓ No actionable orders in payload (nothing to do).")
        return 0

    _print_preview(payload, orders)

    # ── Broker connect ──
    try:
        broker = KisAPI(paper=args.paper)
    except RuntimeError as e:
        print(f"✗ {e}")
        return 1

    mode_label = "모의투자 (paper)" if args.paper else "⚠ 실전투자 (LIVE)"
    print(f"\nBroker: KIS ({mode_label})")
    print(f"Config: max_iterations={args.max_iterations}, "
          f"poll_seconds={args.poll_seconds}, "
          f"max_order_krw=₩{args.max_order_krw:,}")

    try:
        broker.authenticate()
        print("  ✓ KIS authenticated")
    except Exception as e:
        print(f"  ✗ KIS authentication failed: {e}")
        return 1

    # ── Explicit confirmation gate ──
    print("\n위 주문을 패시브 지정가(peg-to-best)로 집행하려면 '동의' 를 입력하세요.")
    print("취소하려면 Enter를 누르세요.")
    try:
        ans = input("  입력: ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\n  취소되었습니다.")
        return 130
    if ans != "동의":
        print("  취소되었습니다.")
        return 0

    # ── Execute ──
    def on_progress(i: int, n: int, req: OrderRequest) -> None:
        print(f"\n[{i+1}/{n}] {req.side} {req.ticker} {req.stock_name} — {req.qty}주")

    results = execute_batch(
        broker, orders,
        max_iterations=args.max_iterations,
        poll_seconds=args.poll_seconds,
        max_order_krw=args.max_order_krw,
        max_session_seconds=args.max_session_minutes * 60,
        on_progress=on_progress,
    )

    _print_summary(results)
    return 0


# ── Helpers ──

def _print_preview(payload: dict, orders: list[OrderRequest]) -> None:
    print(DISCLAIMER)
    print(f"  Strategy:  {payload.get('strategy', '—')}")
    print(f"  Generated: {payload.get('generated_at', '—')}")
    print(f"  Orders:    {len(orders)} "
          f"({sum(1 for o in orders if o.side=='BUY')} BUY, "
          f"{sum(1 for o in orders if o.side=='SELL')} SELL)")
    print()
    print(f"  {'Action':<6}  {'Ticker':<8}  {'Name':<22}  {'Shares':>8}  {'Ref price':>12}")
    print("  " + "─" * 70)
    for o in orders:
        print(f"  {o.side:<6}  {o.ticker:<8}  {o.stock_name[:22]:<22}  "
              f"{o.qty:>8,}  ₩{o.ref_price:>10,}")
    print()


def _print_summary(results: list[OrderResult]) -> None:
    print("\n" + "═" * 72)
    print("  SESSION SUMMARY")
    print("═" * 72)
    total_req = sum(r.request.qty for r in results)
    total_filled = sum(r.filled_qty for r in results)
    total_abandoned = sum(r.abandoned_qty for r in results)
    total_iters = sum(r.iterations_used for r in results)
    total_time = sum(r.duration_seconds for r in results)

    for r in results:
        status_mark = "✓" if r.fully_filled else ("~" if r.filled_qty > 0 else "✗")
        print(f"  {status_mark} {r.request.side:<4} {r.request.ticker} "
              f"{r.request.stock_name[:18]:<18} "
              f"{r.filled_qty:>4}/{r.request.qty:<4}주 filled "
              f"| {r.iterations_used:>2} iters "
              f"| {r.duration_seconds:>5.0f}s")
        for note in r.notes:
            print(f"        ↳ {note}")

    print("  " + "─" * 70)
    print(f"  Filled:    {total_filled}/{total_req} shares")
    print(f"  Abandoned: {total_abandoned} shares")
    print(f"  Iterations: {total_iters}  |  Duration: {total_time:.0f}s")
    print()
    print("  실제 체결 여부는 증권사 앱에서 반드시 확인하세요.")
    print("  (Verify fills in the KIS mobile/HTS app.)")


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(
        prog="kis-passive-trader",
        description="Passive limit-order execution (peg to best bid/ask) for KIS.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_fetch = sub.add_parser("fetch", help="Download portfolio from backtest.co.kr")
    p_fetch.add_argument("--api-url", default=None, help="Base URL (default: from KRXDATA_API_URL)")
    p_fetch.add_argument("--token", default=None, help="Bearer token (default: from KRXDATA_TOKEN)")
    p_fetch.add_argument("--output", "-o", default=str(DEFAULT_PAYLOAD_PATH),
                         help=f"Save to path (default: {DEFAULT_PAYLOAD_PATH})")

    p_preview = sub.add_parser("preview", help="Show order list without broker interaction")
    p_preview.add_argument("--payload", "-p", default=str(DEFAULT_PAYLOAD_PATH),
                            help=f"Payload JSON path (default: {DEFAULT_PAYLOAD_PATH})")

    p_exec = sub.add_parser("execute", help="Execute orders with peg-to-best strategy")
    p_exec.add_argument("--payload", "-p", default=str(DEFAULT_PAYLOAD_PATH),
                        help=f"Payload JSON path (default: {DEFAULT_PAYLOAD_PATH})")
    p_exec.add_argument("--paper", action="store_true",
                        help="Use KIS paper trading (모의투자). STRONGLY recommended for first run.")
    p_exec.add_argument("--max-iterations", type=int, default=30,
                        help="Max re-peg attempts per order (default: 30)")
    p_exec.add_argument("--poll-seconds", type=float, default=8.0,
                        help="Seconds between order-status polls (default: 8.0)")
    p_exec.add_argument("--max-order-krw", type=int,
                        default=int(os.getenv("MAX_ORDER_KRW", "5000000")),
                        help="Refuse any single order larger than this KRW (default: 5,000,000)")
    p_exec.add_argument("--max-session-minutes", type=int,
                        default=int(os.getenv("MAX_SESSION_MINUTES", "30")),
                        help="Total session time limit in minutes (default: 30)")

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    if args.command == "fetch":
        return cmd_fetch(args)
    if args.command == "preview":
        return cmd_preview(args)
    if args.command == "execute":
        return cmd_execute(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
