# kis_passive_trader

**Patient, limit-only portfolio execution via the KIS (한국투자증권) Open API.**
Reads a portfolio JSON file, rests limit orders at the current best bid (BUY)
or best ask (SELL), and reprices patiently until filled — **without ever
crossing the bid-ask spread**.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)

> [!IMPORTANT]
> This tool places **real trades** in your **own brokerage account** using
> **your own API keys**. Nothing about your credentials, balance, or trades is
> sent to anyone. Read [`DISCLAIMER.md`](./DISCLAIMER.md) before using.

---

## Author

Sangwoo "Paul" Cheung. This is a **personal project** — it is **not affiliated
with any commercial entity**, including backtest.co.kr. backtest.co.kr is
referenced only as one possible *external* source of the portfolio JSON files
this tool can read; any JSON matching the documented schema works just as well.

---

## What it does

- Reads a portfolio JSON file (the "F6" export schema documented below).
- Authenticates with the KIS Open Trading API using your credentials.
- Sizes each BUY position from `capital × weight` using a floor + greedy
  remainder (the same convention as the source platform's Portfolio tab).
- For each position, places a **limit order at the current best bid** (BUY) or
  **best ask** (SELL).
- Reprices on a fixed cadence (default every 5 minutes) to track top-of-book
  until filled — **never paying any premium** over the prevailing bid (or
  accepting anything below the prevailing ask).
- Warns and prompts you (with a 5-minute timeout) before buying any position
  whose live bid has risen more than 5% above the signal's reference close.
- Maintains a local state file for crash recovery / resumability.

## What it does NOT do

- **Cross the bid-ask spread.** No market orders, no aggressive fills. Orders
  rest *at* the touch and are only re-pegged when the touch moves *against* you.
- **Make decisions for you.** Every order is a passive limit order; if the
  market runs away, your order simply doesn't fill.
- **Guarantee fills.** Patient bidding may not transact if the market never
  returns to your price.
- **Provide investment advice.** This is execution infrastructure only.

---

## Setup

1. **Clone:**
   ```bash
   git clone https://github.com/cheungsangwoo/kis_passive_trader.git
   cd kis_passive_trader
   ```
2. **Python 3.10+.** Install dependencies:
   ```bash
   pip install -r requirements.txt
   # or, to get the console command + dev/test extras:
   pip install -e ".[dev]"
   ```
3. **Configure credentials:** copy `.env.example` to `.env` and fill in your KIS
   App Key, App Secret, and account number.
4. **Test against the KIS mock server first** (the `--mock` flag — see below).

## KIS account setup

- Open a Korea Investment & Securities (한국투자증권) account.
- Apply for the KIS Open Trading API at
  [apiportal.koreainvestment.com](https://apiportal.koreainvestment.com).
- Create **two** apps and generate App Key + App Secret for **both** the mock
  (모의투자) and live servers.
- **IMPORTANT:** real-server credentials trade **real money**. Always test
  against the mock server first.

---

## Usage

Download a portfolio JSON (e.g. from backtest.co.kr's "Download portfolio
(JSON)" button) or craft your own, then run:

```bash
# Always test on the mock server first:
python patient_maker.py --portfolio examples/sample_portfolio.json \
    --capital 10000000 --mock

# Live (real money) — omit --mock:
python patient_maker.py --portfolio my_portfolio.json --capital 10000000
```

If you installed the package (`pip install -e .`), the same thing is available
as the `patient-maker` console command.

### What happens

1. **Plan.** The tool quotes every position, computes share quantities, and
   prints an execution plan (per-stock target qty, price, budget; total
   deployed; cash residual).
2. **Confirm.** You confirm the whole plan once (`y/N`, no timeout).
3. **Drift check.** For any stock whose current bid is more than 5% above the
   signal's reference close, you're prompted individually (`y/N`, **5-minute
   timeout → skip**). Accepting "locks in" that position — the reprice loop
   will not prompt again even if the price runs further. (Power users wanting
   tighter ongoing control can fork and add re-warn logic; see *Open design
   notes* below.)
4. **Place + reprice.** The tool rests limit orders and reprices every
   `--reprice-interval` seconds until all orders fill, you press Ctrl-C, or the
   market closes (15:20 KST).

### Options

| Flag | Default | Meaning |
|------|---------|---------|
| `--portfolio` | (required) | Path to the F6 portfolio JSON file. |
| `--capital` | (required) | Total capital to deploy, in whole KRW. |
| `--reprice-interval` | `300` | Seconds between reprice passes (5 min). |
| `--side` | `both` | Process `buy`, `sell`, or `both`. |
| `--threshold` | `1.05` | Warn if current bid > threshold × reference close. |
| `--mock` | off | Use the KIS mock (paper / 모의투자) server. |
| `--force-fresh` | off | Ignore any existing state file and start over. |
| `-v` / `--verbose` | off | Debug logging. |

> This tool is intended for use **during the KRX trading session**. Limit orders
> placed outside session hours will be rejected by KIS.

---

## Portfolio JSON schema (F6)

```json
{
  "signal_id": "w1_luk_pead_alloc",
  "signal_name": "Limit-Up + Earnings (cash-overflow weekly)",
  "as_of": "2026-05-22",
  "rebalance_cadence": "weekly",
  "transaction_cost_assumed_bps": 15,
  "positions": [
    {
      "stock_code": "005930",
      "stock_name": "삼성전자",
      "weight": 0.4,
      "reference_close": 71500,
      "reference_close_date": "2026-05-22",
      "side": "BUY"
    }
  ]
}
```

**Top level:** `signal_id`, `signal_name`, `as_of`, `rebalance_cadence`,
`transaction_cost_assumed_bps`, `positions[]`.

**Each position:** `stock_code` (6-digit KRX code), `stock_name`, `side`
(`"BUY"` for F6 exports), `reference_close`, `reference_close_date`.

- **BUY** positions require `weight` (fraction of capital, e.g. `0.4` = 40%);
  the share quantity is derived from `capital × weight`.
- **SELL** positions require an explicit `qty` (or `shares`) — sell sizes are
  **not** derived from a target weight.

See [`examples/sample_portfolio.json`](./examples/sample_portfolio.json) for a
complete example. Its `035420` (NAVER) position uses a deliberately stale
`reference_close` so you can see the drift-warning flow.

---

## State file & resumability

The tool writes `.patient_maker_state_<signal_id>_<as_of>.json` (atomically)
after every meaningful change. If it crashes or you Ctrl-C, just **re-run the
same command** — it reconciles with KIS for any orders that filled while you
were away and resumes the reprice loop. Use `--force-fresh` to discard prior
state and start over.

Each position carries a status: `pending`, `filled`, `partial_then_cancelled`,
`cancelled`, `skipped_user_declined`, `skipped_user_timeout`,
`skipped_no_capital`, or `skipped_no_quote`, plus a timestamped event history.

---

## Safety

- **Never crosses the spread** → never pays the ask premium on a buy.
- **Warns + prompts** on any position whose price has drifted > 5% from the
  signal's reference close.
- **Won't fill if the market runs away** → your cash is preserved.
- **`--mock` first.** Test against the KIS mock (paper) server before trading
  real money.

---

## Testing

```bash
pip install -e ".[dev]"
pytest
```

Tests use an in-process mock broker that simulates an orderbook, partial fills,
and moving prices — no real API calls.

---

## Open design notes

- **Single drift decision per position.** Once you accept a drifted buy, the
  reprice loop won't prompt again even if the bid keeps rising. This is
  deliberate (you're trusted with one decision). To re-warn on each crossing
  during the reprice loop, fork and extend `reprice_once` in
  `src/kis_passive_trader/patient_maker.py`.
- **Legacy command.** An earlier, share-explicit interface remains available as
  `kis-passive-trader {preview,execute}` for an `orders[]`-style JSON. The
  `patient_maker` workflow above (weight-based, resumable, run-to-close) is the
  primary one.

---

## License

MIT — see [`LICENSE`](./LICENSE).

## Disclaimer

See [`DISCLAIMER.md`](./DISCLAIMER.md) for the full notice (English + Korean).

**This software is not investment advice. The author is an individual, not a
licensed investment advisor, and has no fiduciary relationship to users of this
tool. Past backtested performance does not guarantee future results. KIS API
credentials enable real trading on real-money accounts — handle with care. Not
affiliated with backtest.co.kr or any commercial entity. Use at your own risk.**

---

## Links

- KIS Open API portal: https://apiportal.koreainvestment.com
- A compatible portfolio JSON source (external): https://backtest.co.kr
