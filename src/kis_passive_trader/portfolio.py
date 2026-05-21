"""
F6 portfolio-export schema parsing + capital-weighted share allocation.

The F6 schema is the JSON shape exposed by backtest.co.kr's "Download portfolio
(JSON)" button. backtest.co.kr is treated purely as an *external* data source —
any JSON matching this schema works, regardless of where it came from.

F6 schema (top level):
    signal_id, signal_name, as_of, rebalance_cadence,
    transaction_cost_assumed_bps, positions[]

Position fields:
    stock_code, stock_name, weight, reference_close, reference_close_date,
    side   ("BUY" for F6 exports)

BUY positions carry `weight` (fraction of capital). SELL positions — only
present if the schema is extended by hand — must instead carry an explicit
`qty` / `shares`, since this tool does not derive sell sizes from a target.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


class PortfolioError(Exception):
    """Raised for missing files, invalid JSON, or schema violations."""


def _to_int(value, default: int = 0) -> int:
    """Coerce a value that may be an int, float, or comma-formatted string."""
    if value is None or value == "":
        return default
    if isinstance(value, str):
        value = value.replace(",", "").strip()
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return default


def _to_float(value, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    if isinstance(value, str):
        value = value.replace(",", "").strip()
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


@dataclass
class Position:
    """A single portfolio position from an F6 export."""
    stock_code: str
    stock_name: str
    side: str                    # "BUY" or "SELL"
    weight: float = 0.0          # BUY: fraction of capital (0.10 = 10%)
    qty: int = 0                 # SELL: explicit share count
    reference_close: int = 0     # signal's reference close price (KRW)
    reference_close_date: str = ""


@dataclass
class Portfolio:
    """A parsed F6 portfolio export."""
    signal_id: str
    signal_name: str
    as_of: str
    rebalance_cadence: str
    transaction_cost_assumed_bps: float
    positions: list[Position]

    def by_side(self, side: str) -> list[Position]:
        return [p for p in self.positions if p.side == side.upper()]


def load_portfolio(path: str | Path) -> Portfolio:
    """Load and validate an F6 portfolio JSON file."""
    p = Path(path)
    if not p.exists():
        raise PortfolioError(f"Portfolio file not found: {p}")
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise PortfolioError(f"Invalid JSON in {p}: {e}") from e

    if not isinstance(raw, dict):
        raise PortfolioError("Portfolio JSON must be a top-level object.")

    positions_raw = raw.get("positions")
    if not isinstance(positions_raw, list) or not positions_raw:
        raise PortfolioError("Portfolio JSON missing a non-empty 'positions' list.")

    positions: list[Position] = []
    for i, o in enumerate(positions_raw):
        if not isinstance(o, dict):
            raise PortfolioError(f"positions[{i}] is not an object.")
        code = str(o.get("stock_code", "")).strip()
        if not code:
            raise PortfolioError(f"positions[{i}] missing 'stock_code'.")
        # Korean tickers are 6-digit, zero-padded.
        code = code.zfill(6)
        side = str(o.get("side", "BUY")).strip().upper()
        if side not in ("BUY", "SELL"):
            raise PortfolioError(
                f"positions[{i}] ({code}) has invalid side '{side}' "
                "(expected BUY or SELL)."
            )
        weight = _to_float(o.get("weight"))
        # SELL positions take an explicit qty/shares; BUY positions don't.
        qty = _to_int(o.get("qty", o.get("shares", 0)))
        if side == "SELL" and qty <= 0:
            raise PortfolioError(
                f"positions[{i}] ({code}) is a SELL but has no positive "
                "'qty'/'shares'. SELL sizes are not derived from weight."
            )
        positions.append(Position(
            stock_code=code,
            stock_name=str(o.get("stock_name", "")),
            side=side,
            weight=weight,
            qty=qty,
            reference_close=_to_int(o.get("reference_close")),
            reference_close_date=str(o.get("reference_close_date", "")),
        ))

    return Portfolio(
        signal_id=str(raw.get("signal_id", "portfolio")).strip() or "portfolio",
        signal_name=str(raw.get("signal_name", "")),
        as_of=str(raw.get("as_of", "")).strip(),
        rebalance_cadence=str(raw.get("rebalance_cadence", "")),
        transaction_cost_assumed_bps=_to_float(raw.get("transaction_cost_assumed_bps")),
        positions=positions,
    )


def allocate_buy_quantities(
    buys: list[Position],
    capital: int,
    price_by_code: dict[str, int],
) -> dict[str, int]:
    """Floor + greedy-remainder share allocation for BUY positions.

    Matches the backtest.co.kr Portfolio-tab convention:

      1. Per stock, budget = capital * weight; floor qty = budget // price.
      2. remainder = capital - sum(floor_qty * price).
      3. Walk positions in weight-DESC order, adding one share wherever the
         remainder can still afford it, repeating until the remainder is
         smaller than the cheapest position's price.

    Positions with no live price (price <= 0) are allocated 0 shares — the
    caller should mark those `skipped_no_quote`.

    Returns a dict of stock_code -> integer share quantity.
    """
    qty: dict[str, int] = {}
    total_used = 0
    for p in buys:
        price = price_by_code.get(p.stock_code, 0)
        if price <= 0:
            qty[p.stock_code] = 0
            continue
        budget = capital * p.weight
        q = int(budget // price)
        qty[p.stock_code] = q
        total_used += q * price

    remainder = capital - total_used

    priced = [p for p in buys if price_by_code.get(p.stock_code, 0) > 0]
    if not priced:
        return qty

    order = sorted(priced, key=lambda p: p.weight, reverse=True)
    min_price = min(price_by_code[p.stock_code] for p in priced)

    # Each full pass deploys at least `min_price` of the remainder, so this
    # terminates. The `progressed` guard is belt-and-suspenders.
    while remainder >= min_price:
        progressed = False
        for p in order:
            price = price_by_code[p.stock_code]
            if remainder >= price:
                qty[p.stock_code] += 1
                remainder -= price
                progressed = True
        if not progressed:
            break

    return qty
