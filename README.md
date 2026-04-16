# kis_passive_trader

**Passive limit-order execution for KIS (한국투자증권) that pegs to the best bid / best ask, iteratively, to save on bid-ask spread.**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)

> [!IMPORTANT]
> This tool places **real trades** in your **own brokerage account** using **your own API keys**. Nothing about your credentials, balance, or trades is sent to any third party. Read [`DISCLAIMER.md`](./DISCLAIMER.md) before using.

---

## What this does

1. Reads a portfolio from a local JSON file (or fetches it from `backtest.co.kr` if you have an account).
2. Queries the KIS orderbook for each stock.
3. For each BUY: places a limit order at the **best bid** (joins the bid queue).
   For each SELL: places a limit at the **best ask**.
4. Waits. If the order doesn't fill and the best bid/ask has moved against us, **cancels and re-places** at the new level.
5. Repeats up to `--max-iterations` times (default 30), then **abandons any unfilled quantity** and moves on to the next stock.

This is a **passive execution strategy** — you save the bid-ask spread, at the cost of occasionally not filling if the market runs away. In practice, for liquid KRX names you usually fill within a few iterations.

### Why "abandon" instead of chasing the price?

Chasing (re-pegging *away* from the touch) defeats the whole point. If you want to fill no matter what, use your broker's market order feature directly. This tool is for users who'd rather save 20-30bps on the spread and accept that some orders won't fill in a given session.

---

## Install

```bash
# Requires Python 3.10+
pip install git+https://github.com/cheungsangwoo/kis_passive_trader.git
```

Or for local development:

```bash
git clone https://github.com/cheungsangwoo/kis_passive_trader.git
cd kis_passive_trader
pip install -e ".[dev]"
```

---

## Setup

### 1. Get KIS API credentials

Register at [KIS Developers](https://apiportal.koreainvestment.com) → create an app → note your **App Key**, **App Secret**, and **account number** (CANO-ACNT_PRDT_CD, e.g. `12345678-01`).

**Strongly recommended:** create *two* apps — one for paper trading (모의투자) and one for live — so you can test safely.

### 2. Configure

```bash
cp .env.example .env
# Edit .env — paste your KIS_APP_KEY, KIS_APP_SECRET, KIS_ACCOUNT
```

### 3. Get a portfolio to execute

Option A — fetch from `backtest.co.kr`:

```bash
# Copy your JWT from backtest.co.kr: DevTools → Application → Cookies → jwt
# Paste into .env as KRXDATA_TOKEN, then:
kis-passive-trader fetch
```

Option B — provide a JSON file directly:

```json
{
  "version": 1,
  "strategy": "My Portfolio",
  "orders": [
    { "action": "BUY",  "ticker": "005930", "stock_name": "삼성전자", "shares": 10 },
    { "action": "BUY",  "ticker": "000660", "stock_name": "SK하이닉스", "shares": 5 },
    { "action": "SELL", "ticker": "035420", "stock_name": "NAVER",    "shares": 2 }
  ]
}
```

Save as `orders.json` and use `--orders-file orders.json`.

---

## Usage

```bash
# Always preview first — no broker interaction
kis-passive-trader preview

# Paper trade (모의투자) — safe, no real money
kis-passive-trader execute --paper

# Live (실전) — requires typing '동의' to confirm
kis-passive-trader execute

# Options
kis-passive-trader execute --max-iterations 30 --poll-seconds 8
```

### What you'll see

```
═══════════════════════════════════════════════════════════════════════
  ⚠️  법적 고지 / Legal Disclaimer
  ...
═══════════════════════════════════════════════════════════════════════

  Strategy: BT Value (2026-04-19)
  3 orders: 2 BUY, 1 SELL

  Action  Ticker    Name                Shares       Peg ref price
  ──────────────────────────────────────────────────────────────────
  BUY     005930    삼성전자                 10       ₩63,200
  BUY     000660    SK하이닉스                5       ₩142,500
  SELL    035420    NAVER                     2       ₩210,000

  Broker: KIS (모의투자)
  Max iterations per order: 30  |  Poll: 8s  |  Per-order cap: ₩5,000,000

  위 주문을 집행하려면 '동의' 를 입력하세요.
  입력: 동의

  [SELL 035420 NAVER 2주]
    iter 1  peg=₩210,500 (ask)  submitted ORD-12345
    iter 2  still open, ask unchanged, wait...
    iter 3  FILLED  2/2 shares @ ₩210,500

  [BUY 005930 삼성전자 10주]
    iter 1  peg=₩63,200 (bid)  submitted ORD-12346
    iter 2  bid moved up to ₩63,300 — cancel, re-peg
    iter 3  peg=₩63,300 (bid)  submitted ORD-12347
    iter 4  FILLED  10/10 shares @ ₩63,300

  [BUY 000660 SK하이닉스 5주]
    iter 1-30  still open at ₩142,500 (bid), never hit
    MAX ITERATIONS reached — abandoning 5 unfilled shares
    order ORD-12348 cancelled

  ═══════════════════════════════════════════════════════════════════════
  Session summary
    Requested: 17 shares across 3 stocks
    Filled:    12 shares
    Abandoned: 5 shares  (SK하이닉스)
    Duration:  6m 42s
  ═══════════════════════════════════════════════════════════════════════
```

---

## How "peg-to-best" works (technical)

For each order (after sells, then buys):

```
remaining = total_quantity
iter = 0

while remaining > 0 and iter < max_iterations:
    best_bid, best_ask = broker.get_orderbook(ticker)
    peg = best_bid if BUY else best_ask

    # If no open order, or peg moved against us, cancel & re-place
    if no open order:
        open_order = broker.submit_limit(ticker, side, remaining, peg)
    elif (BUY and peg > current_peg) or (SELL and peg < current_peg):
        broker.cancel(open_order)
        filled_before_cancel = broker.fill_qty(open_order)
        remaining -= filled_before_cancel
        open_order = broker.submit_limit(ticker, side, remaining, peg)

    sleep(poll_seconds)

    filled = broker.fill_qty(open_order)
    remaining -= filled
    if order closed: open_order = None

    iter += 1

# max iterations: cancel and abandon
if open_order:
    broker.cancel(open_order)
```

**Key properties:**

- Only re-pegs when the touch moves *against* us. A stale bid does not trigger a re-peg.
- Partial fills are tracked: `remaining = total - filled_so_far`.
- Hard stop at `max_iterations`. No aggressive fallback.
- Sells execute before buys (freeing up cash).

---

## Safety gates

Before any live order is submitted, the tool checks:

1. **`--paper` flag is explicit.** Live trading requires omitting the flag *and* typing `동의` at the prompt.
2. **Per-order KRW cap** (default ₩5,000,000, configurable via `MAX_ORDER_KRW` env var). A single order exceeding this is refused.
3. **Session time limit** (default 30 minutes, via `MAX_SESSION_MINUTES`). After this, remaining orders are cancelled.
4. **Price sanity check.** If the current best bid/ask deviates >15% from the snapshot price in the payload, the order is skipped with a warning.

---

## Testing

```bash
pytest
```

Tests use a mock broker that simulates an orderbook, partial fills, and moving prices. No real API calls.

---

## License

MIT — see [`LICENSE`](./LICENSE).

## Disclaimer

See [`DISCLAIMER.md`](./DISCLAIMER.md) for the full legal notice in English and Korean.

**This software is not investment advice. Collab Technologies Inc. is not a licensed investment advisor. Past performance does not guarantee future results. Use at your own risk.**

---

## Links

- KIS Open API portal: https://apiportal.koreainvestment.com
- Parent service: https://backtest.co.kr
- Support: webmaster@collab-tech.co.kr
