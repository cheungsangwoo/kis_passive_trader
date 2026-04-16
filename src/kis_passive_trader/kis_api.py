"""
KIS (한국투자증권) REST API wrapper.

Docs: https://apiportal.koreainvestment.com
Live base:  https://openapi.koreainvestment.com:9443
Paper base: https://openapivts.koreainvestment.com:29443

Each method maps to a single KIS REST endpoint. The TR IDs below are taken
from KIS's official docs. Paper-trading TR IDs begin with 'V'; live begin
with 'T'.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta

import requests

from kis_passive_trader.broker_base import BrokerAPI, Orderbook, OrderStatus


# ── TR IDs ─────────────────────────────────────────────────────────────
# Read-only market data TR IDs are the same in paper and live.
TR_PRICE = "FHKST01010100"          # 현재가 (inquire-price)
TR_ORDERBOOK = "FHKST01010200"      # 호가/예상체결 (inquire-asking-price-exp-ccn)

# Account-dependent TR IDs differ between paper (V*) and live (T*).
def _tr(tr_live: str, tr_paper: str, paper: bool) -> str:
    return tr_paper if paper else tr_live

TR_BALANCE      = ("TTTC8434R", "VTTC8434R")
TR_ORDER_BUY    = ("TTTC0802U", "VTTC0802U")
TR_ORDER_SELL   = ("TTTC0801U", "VTTC0801U")
TR_ORDER_CANCEL = ("TTTC0803U", "VTTC0803U")    # 정정·취소
TR_ORDER_DAILY  = ("TTTC8001R", "VTTC8001R")    # 일별주문체결조회


class KisAPI(BrokerAPI):
    """KIS OpenAPI client.

    Reads credentials from environment variables (or the .env file) at init:
        KIS_APP_KEY, KIS_APP_SECRET, KIS_ACCOUNT ("CANO-ACNT_PRDT_CD")
    """

    def __init__(self, paper: bool = True):
        self.paper = paper
        self.app_key = os.getenv("KIS_APP_KEY", "").strip()
        self.app_secret = os.getenv("KIS_APP_SECRET", "").strip()
        account = os.getenv("KIS_ACCOUNT", "").strip()

        if not (self.app_key and self.app_secret and account):
            raise RuntimeError(
                "KIS credentials missing. Set KIS_APP_KEY, KIS_APP_SECRET, "
                "and KIS_ACCOUNT in your .env file."
            )

        if "-" in account:
            self.cano, self.acnt_prdt = account.split("-", 1)
        else:
            self.cano, self.acnt_prdt = account[:8], "01"

        self.base = (
            "https://openapivts.koreainvestment.com:29443" if paper
            else "https://openapi.koreainvestment.com:9443"
        )
        self.token: str | None = None
        self.token_expires: datetime | None = None

    # ── Helpers ────────────────────────────────────────────────────────

    def _headers(self, tr_id: str) -> dict:
        return {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
        }

    def _acct_params(self) -> dict:
        return {"CANO": self.cano, "ACNT_PRDT_CD": self.acnt_prdt}

    # ── Auth ───────────────────────────────────────────────────────────

    def authenticate(self) -> None:
        """Acquire an OAuth access token (valid 24h)."""
        resp = requests.post(
            f"{self.base}/oauth2/tokenP",
            json={
                "grant_type": "client_credentials",
                "appkey": self.app_key,
                "appsecret": self.app_secret,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        self.token = data["access_token"]
        # KIS tokens last 24h; mark expiry 5min early for safety
        expires_in_s = int(data.get("expires_in", 86400))
        self.token_expires = datetime.now() + timedelta(seconds=expires_in_s - 300)

    def _ensure_token(self) -> None:
        if not self.token or (self.token_expires and datetime.now() >= self.token_expires):
            self.authenticate()

    # ── Market data ────────────────────────────────────────────────────

    def get_price(self, ticker: str) -> int:
        self._ensure_token()
        resp = requests.get(
            f"{self.base}/uapi/domestic-stock/v1/quotations/inquire-price",
            headers=self._headers(TR_PRICE),
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker},
            timeout=10,
        )
        resp.raise_for_status()
        return int(resp.json().get("output", {}).get("stck_prpr", 0))

    def get_orderbook(self, ticker: str) -> Orderbook:
        """Return top-of-book via KIS inquire-asking-price-exp-ccn.

        Response `output1` contains 10 levels of bid and ask. We only need
        level 1 (the touch). Keys in KIS docs:
            askp1        = best ask price
            askp_rsqn1   = best ask quantity (remaining)
            bidp1        = best bid price
            bidp_rsqn1   = best bid quantity (remaining)
        """
        self._ensure_token()
        resp = requests.get(
            f"{self.base}/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
            headers=self._headers(TR_ORDERBOOK),
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker},
            timeout=10,
        )
        resp.raise_for_status()
        out1 = resp.json().get("output1", {})

        def _int(key: str) -> int:
            v = out1.get(key, "")
            if isinstance(v, str):
                v = v.replace(",", "").strip()
            try:
                return int(v) if v else 0
            except (ValueError, TypeError):
                return 0

        return Orderbook(
            ticker=ticker,
            best_bid=_int("bidp1"),
            best_bid_qty=_int("bidp_rsqn1"),
            best_ask=_int("askp1"),
            best_ask_qty=_int("askp_rsqn1"),
        )

    # ── Order management ───────────────────────────────────────────────

    def submit_limit_order(
        self, ticker: str, side: str, qty: int, price: int
    ) -> tuple[bool, str]:
        self._ensure_token()
        side_upper = side.upper()
        if side_upper not in ("BUY", "SELL"):
            return False, f"Invalid side: {side}"

        tr_id = _tr(*TR_ORDER_BUY, self.paper) if side_upper == "BUY" else _tr(*TR_ORDER_SELL, self.paper)

        body = {
            **self._acct_params(),
            "PDNO": ticker,
            "ORD_DVSN": "00",        # 00 = 지정가 (limit)
            "ORD_QTY": str(qty),
            "ORD_UNPR": str(price),
        }
        resp = requests.post(
            f"{self.base}/uapi/domestic-stock/v1/trading/order-cash",
            headers=self._headers(tr_id),
            json=body,
            timeout=10,
        )
        data = resp.json()
        ok = data.get("rt_cd") == "0"
        if ok:
            order_id = data.get("output", {}).get("ODNO", "")
            # ORD_GNO_BRNO is the branch code, needed for cancel
            branch = data.get("output", {}).get("KRX_FWDG_ORD_ORGNO", "")
            return True, f"{branch}:{order_id}"
        return False, data.get("msg1", "") or data.get("msg_cd", "unknown_error")

    def cancel_order(self, ticker: str, order_id: str) -> tuple[bool, str]:
        """Cancel a still-open order.

        `order_id` is the 'branch:ODNO' composite returned by submit_limit_order.
        """
        self._ensure_token()
        if ":" in order_id:
            branch, odno = order_id.split(":", 1)
        else:
            # Best effort — some brokers may return just the ODNO
            branch, odno = "", order_id

        tr_id = _tr(*TR_ORDER_CANCEL, self.paper)
        body = {
            **self._acct_params(),
            "KRX_FWDG_ORD_ORGNO": branch,
            "ORGN_ODNO": odno,
            "ORD_DVSN": "00",
            "RVSE_CNCL_DVSN_CD": "02",   # 02 = 취소 (01 = 정정)
            "ORD_QTY": "0",              # 0 when QTY_ALL_ORD_YN is "Y"
            "ORD_UNPR": "0",
            "QTY_ALL_ORD_YN": "Y",       # cancel all remaining
        }
        resp = requests.post(
            f"{self.base}/uapi/domestic-stock/v1/trading/order-rvsecncl",
            headers=self._headers(tr_id),
            json=body,
            timeout=10,
        )
        data = resp.json()
        if data.get("rt_cd") == "0":
            return True, "cancelled"
        msg = data.get("msg1", "") or data.get("msg_cd", "")
        # Treat "already fully filled" as success — nothing to cancel
        if "체결" in msg or "no open" in msg.lower():
            return True, "already_closed"
        return False, msg

    def get_order_status(self, ticker: str, order_id: str) -> OrderStatus:
        """Look up fill status via the daily-ccld (체결 조회) endpoint.

        KIS doesn't have a cheap "status of order X" endpoint, so we pull the
        day's orders for this stock and filter client-side.
        """
        self._ensure_token()
        tr_id = _tr(*TR_ORDER_DAILY, self.paper)

        if ":" in order_id:
            _, odno = order_id.split(":", 1)
        else:
            odno = order_id

        today = datetime.now().strftime("%Y%m%d")
        resp = requests.get(
            f"{self.base}/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
            headers=self._headers(tr_id),
            params={
                **self._acct_params(),
                "INQR_STRT_DT": today,
                "INQR_END_DT": today,
                "SLL_BUY_DVSN_CD": "00",     # 00 = both
                "INQR_DVSN": "00",
                "PDNO": ticker,
                "CCLD_DVSN": "00",
                "ORD_GNO_BRNO": "",
                "ODNO": odno,
                "INQR_DVSN_3": "00",
                "INQR_DVSN_1": "",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
            timeout=10,
        )
        data = resp.json()
        rows = data.get("output1", []) or []

        # Rows for this ODNO (there may be multiple: order + fills)
        row = next((r for r in rows if r.get("odno") == odno), None)
        if not row:
            # Order not found — likely cancelled or never submitted
            return OrderStatus(order_id=order_id, filled_qty=0, total_qty=0, is_open=False)

        def _i(k: str) -> int:
            v = row.get(k, "")
            if isinstance(v, str):
                v = v.replace(",", "").strip()
            try:
                return int(v) if v else 0
            except (ValueError, TypeError):
                return 0

        total = _i("ord_qty")
        filled = _i("tot_ccld_qty")
        # rmn_qty = remaining unfilled qty. If 0 AND filled>=total, order done.
        remaining = _i("rmn_qty")
        is_open = remaining > 0

        return OrderStatus(
            order_id=order_id,
            filled_qty=filled,
            total_qty=total,
            is_open=is_open,
        )
