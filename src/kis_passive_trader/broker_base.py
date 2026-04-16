"""
Abstract broker interface.

Any concrete broker implementation (KIS, Kiwoom, etc.) must provide these
methods so the peg executor can drive it. All state is per-session — the
broker object is instantiated once per `execute` run.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Orderbook:
    """Top-of-book snapshot for a single stock.

    Prices are in KRW (integer for Korean stocks). Quantities are share counts.
    """
    ticker: str
    best_bid: int
    best_bid_qty: int
    best_ask: int
    best_ask_qty: int

    @property
    def spread(self) -> int:
        """Absolute spread in KRW (ask - bid)."""
        return self.best_ask - self.best_bid

    @property
    def mid(self) -> float:
        """Mid price (simple average)."""
        return (self.best_bid + self.best_ask) / 2


@dataclass
class OrderStatus:
    """Current fill state of an order.

    `filled_qty` is cumulative shares filled so far (may be < order qty for
    partial fills). `is_open` is True while the order can still accept fills.
    """
    order_id: str
    filled_qty: int
    total_qty: int
    is_open: bool

    @property
    def remaining_qty(self) -> int:
        return max(0, self.total_qty - self.filled_qty)


class BrokerAPI(ABC):
    """Abstract broker. Implementations must be side-effect-free until
    `authenticate()` is called, and must not send credentials anywhere
    outside the broker's own API endpoints."""

    # ── Lifecycle ──

    @abstractmethod
    def authenticate(self) -> None:
        """Acquire any tokens/sessions needed for subsequent calls."""
        ...

    # ── Read-only ──

    @abstractmethod
    def get_orderbook(self, ticker: str) -> Orderbook:
        """Return the top-of-book snapshot for `ticker`."""
        ...

    @abstractmethod
    def get_price(self, ticker: str) -> int:
        """Return the current last-traded price (KRW integer)."""
        ...

    # ── Order management ──

    @abstractmethod
    def submit_limit_order(
        self, ticker: str, side: str, qty: int, price: int
    ) -> tuple[bool, str]:
        """Submit a limit order. `side` is 'BUY' or 'SELL'.

        Returns (success, order_id_or_error_message).
        """
        ...

    @abstractmethod
    def cancel_order(self, ticker: str, order_id: str) -> tuple[bool, str]:
        """Cancel a still-open order. Idempotent — a not-found order
        returns (True, 'already_closed').

        Returns (success, message).
        """
        ...

    @abstractmethod
    def get_order_status(self, ticker: str, order_id: str) -> OrderStatus:
        """Return the current fill status for an order."""
        ...
