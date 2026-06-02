"""
KIS (한국투자증권) REST API wrapper.

Docs: https://apiportal.koreainvestment.com
Live base:  https://openapi.koreainvestment.com:9443
Mock base:  https://openapivts.koreainvestment.com:29443   (모의투자 / paper)

Each method maps to a single KIS REST endpoint. The TR IDs below are taken
from KIS's official docs. Mock/paper TR IDs begin with 'V'; live begin with 'T'.

Resilience features (additive — the BrokerAPI method contract is unchanged):
  - KIS_BASE_URL env override (else derived from the `paper` flag).
  - Account from KIS_ACCOUNT ("CANO-PRDT") or KIS_ACCOUNT_NO + KIS_PRODUCT_CODE.
  - Access-token cache in .kis_token.json (gitignored), keyed by app key + base.
  - _request_with_retry: exponential backoff on transient network errors,
    HTTP 429/5xx, and KIS per-second rate-limit responses.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

from kis_passive_trader.broker_base import BrokerAPI, Orderbook, OrderStatus

logger = logging.getLogger("patient_maker.kis")

LIVE_BASE = "https://openapi.koreainvestment.com:9443"
MOCK_BASE = "https://openapivts.koreainvestment.com:29443"
TOKEN_CACHE_FILE = ".kis_token.json"

# KIS returns this message code (HTTP 200, rt_cd != "0") when the per-second
# transaction limit is exceeded.
RATE_LIMIT_MSG_CD = "EGW00201"


# ── TR IDs ─────────────────────────────────────────────────────────────
# Read-only market-data TR IDs are the same in paper and live.
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


def _int(value, default: int = 0) -> int:
    if isinstance(value, str):
        value = value.replace(",", "").strip()
    try:
        return int(value) if value not in ("", None) else default
    except (ValueError, TypeError):
        return default


class KisAPI(BrokerAPI):
    """KIS OpenAPI client.

    Reads credentials from environment variables (or the .env file) at init:
        KIS_APP_KEY, KIS_APP_SECRET
        KIS_ACCOUNT ("CANO-ACNT_PRDT_CD")  — or —
        KIS_ACCOUNT_NO + KIS_PRODUCT_CODE
        KIS_BASE_URL (optional override)
    """

    def __init__(self, paper: bool = True, *, max_retries: int = 4,
                 backoff_base: float = 0.5, sleep_fn=time.sleep):
        self.paper = paper
        self.app_key = os.getenv("KIS_APP_KEY", "").strip()
        self.app_secret = os.getenv("KIS_APP_SECRET", "").strip()
        account = os.getenv("KIS_ACCOUNT", "").strip()
        if not account:
            no = os.getenv("KIS_ACCOUNT_NO", "").strip()
            prdt = os.getenv("KIS_PRODUCT_CODE", "01").strip() or "01"
            account = f"{no}-{prdt}" if no else ""

        if not (self.app_key and self.app_secret and account):
            raise RuntimeError(
                "KIS credentials missing. Set KIS_APP_KEY, KIS_APP_SECRET, and "
                "either KIS_ACCOUNT or KIS_ACCOUNT_NO (+ KIS_PRODUCT_CODE) in .env."
            )

        if "-" in account:
            self.cano, self.acnt_prdt = account.split("-", 1)
        else:
            self.cano, self.acnt_prdt = account[:8], "01"

        self.base = os.getenv("KIS_BASE_URL", "").strip() or (
            MOCK_BASE if paper else LIVE_BASE
        )
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self._sleep = sleep_fn
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

    def _request_with_retry(self, method: str, url: str, **kwargs) -> requests.Response:
        """HTTP with exponential backoff on transient failures + KIS rate limits."""
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = requests.request(method, url, timeout=10, **kwargs)
            except requests.RequestException as e:
                last_exc = e
                if attempt >= self.max_retries:
                    raise
                wait = self.backoff_base * (2 ** attempt)
                logger.warning("network error (%s); retry %d/%d in %.1fs",
                               e, attempt + 1, self.max_retries, wait)
                self._sleep(wait)
                continue

            # Retry on transient HTTP statuses.
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                wait = self.backoff_base * (2 ** attempt)
                logger.warning("HTTP %d; retry %d/%d in %.1fs", resp.status_code,
                               attempt + 1, self.max_retries, wait)
                self._sleep(wait)
                continue

            # Retry on KIS per-second rate-limit (HTTP 200, msg_cd EGW00201).
            if attempt < self.max_retries and self._is_rate_limited(resp):
                wait = self.backoff_base * (2 ** attempt)
                logger.warning("KIS rate limit; retry %d/%d in %.1fs",
                               attempt + 1, self.max_retries, wait)
                self._sleep(wait)
                continue

            return resp

        if last_exc:
            raise last_exc
        return resp   # pragma: no cover

    @staticmethod
    def _is_rate_limited(resp: requests.Response) -> bool:
        try:
            data = resp.json()
        except ValueError:
            return False
        return data.get("rt_cd") not in ("0", None) and (
            data.get("msg_cd") == RATE_LIMIT_MSG_CD
            or "초당 거래" in str(data.get("msg1", ""))
        )

    # ── Auth + token cache ─────────────────────────────────────────────

    def _key_fingerprint(self) -> str:
        """Short, non-reversible tag so a cached token is only reused for the
        same app key + base (never stores the raw key)."""
        digest = hashlib.sha256(f"{self.app_key}|{self.base}".encode()).hexdigest()
        return digest[:16]

    def _load_token_from_cache(self) -> bool:
        path = Path(TOKEN_CACHE_FILE)
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return False
        if data.get("fingerprint") != self._key_fingerprint():
            return False
        try:
            expires = datetime.fromisoformat(data["expires_at"])
        except (KeyError, ValueError):
            return False
        if datetime.now() >= expires:
            return False
        self.token = data.get("access_token")
        self.token_expires = expires
        return bool(self.token)

    def _save_token_to_cache(self) -> None:
        path = Path(TOKEN_CACHE_FILE)
        payload = {
            "fingerprint": self._key_fingerprint(),
            "access_token": self.token,
            "expires_at": self.token_expires.isoformat() if self.token_expires else "",
        }
        try:
            tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as e:
            logger.warning("could not cache KIS token: %s", e)

    def authenticate(self) -> None:
        """Acquire an OAuth access token (valid ~24h), reusing a cached one."""
        if self._load_token_from_cache():
            logger.debug("reusing cached KIS token (expires %s)", self.token_expires)
            return
        resp = self._request_with_retry(
            "POST", f"{self.base}/oauth2/tokenP",
            json={
                "grant_type": "client_credentials",
                "appkey": self.app_key,
                "appsecret": self.app_secret,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        self.token = data["access_token"]
        # KIS tokens last ~24h; mark expiry 5 min early for safety.
        expires_in_s = int(data.get("expires_in", 86400))
        self.token_expires = datetime.now() + timedelta(seconds=expires_in_s - 300)
        self._save_token_to_cache()

    def _ensure_token(self) -> None:
        if not self.token or (self.token_expires and datetime.now() >= self.token_expires):
            self.authenticate()

    # ── Market data ────────────────────────────────────────────────────

    def get_price(self, ticker: str) -> int:
        self._ensure_token()
        resp = self._request_with_retry(
            "GET",
            f"{self.base}/uapi/domestic-stock/v1/quotations/inquire-price",
            headers=self._headers(TR_PRICE),
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker},
        )
        resp.raise_for_status()
        return _int(resp.json().get("output", {}).get("stck_prpr", 0))

    def get_orderbook(self, ticker: str) -> Orderbook:
        """Return top-of-book via KIS inquire-asking-price-exp-ccn.

        Response `output1` contains 10 levels of bid and ask. We only need
        level 1 (the touch):
            askp1 / askp_rsqn1 = best ask price / remaining qty
            bidp1 / bidp_rsqn1 = best bid price / remaining qty
        """
        self._ensure_token()
        resp = self._request_with_retry(
            "GET",
            f"{self.base}/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
            headers=self._headers(TR_ORDERBOOK),
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker},
        )
        resp.raise_for_status()
        out1 = resp.json().get("output1", {})
        return Orderbook(
            ticker=ticker,
            best_bid=_int(out1.get("bidp1")),
            best_bid_qty=_int(out1.get("bidp_rsqn1")),
            best_ask=_int(out1.get("askp1")),
            best_ask_qty=_int(out1.get("askp_rsqn1")),
        )

    # ── Order management ───────────────────────────────────────────────

    def submit_limit_order(
        self, ticker: str, side: str, qty: int, price: int
    ) -> tuple[bool, str]:
        self._ensure_token()
        side_upper = side.upper()
        if side_upper not in ("BUY", "SELL"):
            return False, f"Invalid side: {side}"

        tr_id = (_tr(*TR_ORDER_BUY, self.paper) if side_upper == "BUY"
                 else _tr(*TR_ORDER_SELL, self.paper))

        body = {
            **self._acct_params(),
            "PDNO": ticker,
            "ORD_DVSN": "00",        # 00 = 지정가 (limit)
            "ORD_QTY": str(qty),
            "ORD_UNPR": str(price),
        }
        resp = self._request_with_retry(
            "POST",
            f"{self.base}/uapi/domestic-stock/v1/trading/order-cash",
            headers=self._headers(tr_id),
            json=body,
        )
        data = resp.json()
        if data.get("rt_cd") == "0":
            order_id = data.get("output", {}).get("ODNO", "")
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
        resp = self._request_with_retry(
            "POST",
            f"{self.base}/uapi/domestic-stock/v1/trading/order-rvsecncl",
            headers=self._headers(tr_id),
            json=body,
        )
        data = resp.json()
        if data.get("rt_cd") == "0":
            return True, "cancelled"
        msg = data.get("msg1", "") or data.get("msg_cd", "")
        # Treat "already fully filled / nothing to cancel" as success.
        if "체결" in msg or "no open" in msg.lower():
            return True, "already_closed"
        return False, msg

    def get_order_status(self, ticker: str, order_id: str) -> OrderStatus:
        """Look up fill status via the daily-ccld (체결 조회) endpoint.

        KIS has no cheap "status of order X" endpoint, so we pull the day's
        orders for this stock and filter client-side.
        """
        self._ensure_token()
        tr_id = _tr(*TR_ORDER_DAILY, self.paper)
        odno = order_id.split(":", 1)[1] if ":" in order_id else order_id

        today = datetime.now().strftime("%Y%m%d")
        resp = self._request_with_retry(
            "GET",
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
        )
        data = resp.json()
        rows = data.get("output1", []) or []
        row = next((r for r in rows if r.get("odno") == odno), None)
        if not row:
            # Not found — likely cancelled or never submitted.
            return OrderStatus(order_id=order_id, filled_qty=0, total_qty=0, is_open=False)

        total = _int(row.get("ord_qty"))
        filled = _int(row.get("tot_ccld_qty"))
        remaining = _int(row.get("rmn_qty"))
        return OrderStatus(
            order_id=order_id,
            filled_qty=filled,
            total_qty=total,
            is_open=remaining > 0,
        )

    def get_holdings(self) -> dict[str, dict]:
        """Current equity holdings: {ticker: {"qty": int, "name": str}}.

        Queries inquire-balance (체결기준 잔고). Paginates CTX_AREA_*100 if the
        account holds more than one page. Only non-zero holding_qty rows returned.
        Used by the reconcile-to-target flow to diff actual vs target.
        """
        self._ensure_token()
        tr_id = _tr(*TR_BALANCE, self.paper)
        holdings: dict[str, dict] = {}
        fk, nk = "", ""
        for _ in range(20):  # hard page cap
            resp = self._request_with_retry(
                "GET",
                f"{self.base}/uapi/domestic-stock/v1/trading/inquire-balance",
                headers=self._headers(tr_id),
                params={
                    **self._acct_params(),
                    "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02",
                    "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N",
                    "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00",
                    "CTX_AREA_FK100": fk, "CTX_AREA_NK100": nk,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            for r in (data.get("output1", []) or []):
                ticker = str(r.get("pdno", "")).strip()
                qty = _int(r.get("hldg_qty"))
                if ticker and qty > 0:
                    holdings[ticker] = {"qty": qty,
                                        "name": str(r.get("prdt_name", "")).strip()}
            # tr_cont 'F'/'M' = more pages; else done.
            if str(data.get("tr_cont", "")).strip() in ("F", "M"):
                fk = str(data.get("ctx_area_fk100", "")).strip()
                nk = str(data.get("ctx_area_nk100", "")).strip()
            else:
                break
        return holdings
