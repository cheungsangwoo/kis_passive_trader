"""
MockBroker — a minimal in-process broker for testing the peg executor.

The mock maintains per-ticker orderbook and order state. Tests can script
price movements and partial fills by driving the mock between calls. It
is NOT a real simulator and makes no attempt to model exchange microstructure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from kis_passive_trader.broker_base import BrokerAPI, Orderbook, OrderStatus


@dataclass
class _OpenOrder:
    order_id: str
    ticker: str
    side: str
    qty: int
    price: int
    filled: int = 0
    open: bool = True


@dataclass
class MockBroker(BrokerAPI):
    """In-memory broker for unit tests.

    Usage:
        mb = MockBroker()
        mb.set_orderbook("005930", best_bid=63000, best_ask=63100)
        mb.authenticate()
        ok, oid = mb.submit_limit_order("005930", "BUY", 10, 63000)
        mb.simulate_fill(oid, 5)   # partial fill 5 shares
        mb.set_orderbook("005930", best_bid=63100, best_ask=63200)  # market moves up
    """
    orderbooks: dict[str, Orderbook] = field(default_factory=dict)
    orders: dict[str, _OpenOrder] = field(default_factory=dict)
    _seq: int = 0
    authenticated: bool = False
    submit_history: list[tuple[str, str, int, int]] = field(default_factory=list)
    cancel_history: list[str] = field(default_factory=list)

    # ── Test helpers (not part of BrokerAPI) ──

    def set_orderbook(
        self, ticker: str,
        best_bid: int = 0, best_bid_qty: int = 1000,
        best_ask: int = 0, best_ask_qty: int = 1000,
    ) -> None:
        self.orderbooks[ticker] = Orderbook(
            ticker=ticker,
            best_bid=best_bid, best_bid_qty=best_bid_qty,
            best_ask=best_ask, best_ask_qty=best_ask_qty,
        )

    def simulate_fill(self, order_id: str, delta: int, close: bool = False) -> None:
        """Add `delta` shares to the filled qty of an order.

        If `close` is True OR the order reaches its total qty, marks it closed.
        """
        o = self.orders.get(order_id)
        if o is None:
            raise KeyError(f"No such order: {order_id}")
        o.filled = min(o.qty, o.filled + delta)
        if close or o.filled >= o.qty:
            o.open = False

    # ── BrokerAPI implementation ──

    def authenticate(self) -> None:
        self.authenticated = True

    def get_orderbook(self, ticker: str) -> Orderbook:
        if ticker not in self.orderbooks:
            return Orderbook(ticker, 0, 0, 0, 0)
        return self.orderbooks[ticker]

    def get_price(self, ticker: str) -> int:
        ob = self.orderbooks.get(ticker)
        if not ob:
            return 0
        return (ob.best_bid + ob.best_ask) // 2

    def submit_limit_order(
        self, ticker: str, side: str, qty: int, price: int
    ) -> tuple[bool, str]:
        if not self.authenticated:
            return False, "not_authenticated"
        if qty <= 0 or price <= 0:
            return False, "invalid_args"
        self._seq += 1
        order_id = f"MOCK-{self._seq:06d}"
        self.orders[order_id] = _OpenOrder(
            order_id=order_id, ticker=ticker, side=side.upper(),
            qty=qty, price=price,
        )
        self.submit_history.append((ticker, side.upper(), qty, price))
        return True, order_id

    def cancel_order(self, ticker: str, order_id: str) -> tuple[bool, str]:
        o = self.orders.get(order_id)
        if o is None:
            return True, "not_found"
        if not o.open:
            return True, "already_closed"
        o.open = False
        self.cancel_history.append(order_id)
        return True, "cancelled"

    def get_order_status(self, ticker: str, order_id: str) -> OrderStatus:
        o = self.orders.get(order_id)
        if o is None:
            return OrderStatus(order_id=order_id, filled_qty=0, total_qty=0, is_open=False)
        return OrderStatus(
            order_id=order_id, filled_qty=o.filled,
            total_qty=o.qty, is_open=o.open,
        )
