"""
Portfolio payload loading.

Two sources:
  1. A local JSON file (user-provided, for review & editing)
  2. Fetch from backtest.co.kr's /api/portfolio/export endpoint (HMAC-signed)

We do NOT verify the HMAC signature client-side — the signature's purpose
is server-side audit, and verifying it would require sharing the server's
JWT_SECRET which we deliberately do not do. Trust the HTTPS connection
plus your own Bearer token.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import requests

from kis_passive_trader.peg_executor import OrderRequest


class PayloadError(Exception):
    pass


def load_from_file(path: str | Path) -> dict:
    """Load a portfolio payload from a local JSON file."""
    p = Path(path)
    if not p.exists():
        raise PayloadError(f"Payload file not found: {p}")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise PayloadError(f"Invalid JSON in {p}: {e}") from e


def fetch_from_server(api_url: str | None = None, token: str | None = None) -> dict:
    """Fetch the signed portfolio payload from backtest.co.kr."""
    api_url = api_url or os.getenv("KRXDATA_API_URL", "https://backtest.co.kr")
    token = token or os.getenv("KRXDATA_TOKEN", "")
    if not token:
        raise PayloadError(
            "KRXDATA_TOKEN missing. Copy your JWT from backtest.co.kr "
            "(DevTools → Application → Cookies → 'jwt') and put it in .env."
        )
    url = f"{api_url.rstrip('/')}/api/portfolio/export"
    try:
        resp = requests.get(
            url, headers={"Authorization": f"Bearer {token}"}, timeout=15
        )
    except requests.RequestException as e:
        raise PayloadError(f"Network error fetching {url}: {e}") from e
    if resp.status_code == 401:
        raise PayloadError("Authentication failed — your token may have expired.")
    if resp.status_code != 200:
        raise PayloadError(f"Server returned {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def payload_to_orders(payload: dict) -> list[OrderRequest]:
    """Convert a payload dict into a list of OrderRequest objects."""
    orders_raw = payload.get("orders", [])
    if not isinstance(orders_raw, list):
        raise PayloadError("Payload missing 'orders' list")

    out: list[OrderRequest] = []
    for o in orders_raw:
        side = str(o.get("action", "")).upper()
        if side not in ("BUY", "SELL"):
            continue
        ticker = str(o.get("ticker", "")).strip().zfill(6)
        qty = int(o.get("shares", 0) or 0)
        ref_price = int(o.get("price", 0) or 0)
        if not ticker or qty <= 0:
            continue
        out.append(OrderRequest(
            ticker=ticker,
            stock_name=str(o.get("stock_name", "")),
            side=side,
            qty=qty,
            ref_price=ref_price,
        ))
    return out


def save_payload(payload: dict, path: str | Path) -> None:
    """Persist a fetched payload to disk for reuse / auditing."""
    Path(path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
