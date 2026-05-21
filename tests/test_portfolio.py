"""Tests for F6 portfolio parsing and capital allocation."""

from __future__ import annotations

import json

import pytest

from kis_passive_trader.portfolio import (
    Portfolio,
    Position,
    PortfolioError,
    allocate_buy_quantities,
    load_portfolio,
)


# ── load_portfolio ───────────────────────────────────────────────────────────

def _write(tmp_path, obj):
    p = tmp_path / "pf.json"
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return p


def test_load_valid_f6(tmp_path):
    p = _write(tmp_path, {
        "signal_id": "w1_luk_pead_alloc",
        "signal_name": "Limit-Up + Earnings",
        "as_of": "2026-05-22",
        "rebalance_cadence": "weekly",
        "transaction_cost_assumed_bps": 15,
        "positions": [
            {"stock_code": "5930", "stock_name": "삼성전자", "weight": 0.6,
             "reference_close": "71,500", "reference_close_date": "2026-05-22",
             "side": "BUY"},
            {"stock_code": "000660", "stock_name": "SK하이닉스", "weight": 0.4,
             "reference_close": 178000, "side": "buy"},
        ],
    })
    pf = load_portfolio(p)
    assert pf.signal_id == "w1_luk_pead_alloc"
    assert pf.as_of == "2026-05-22"
    assert pf.transaction_cost_assumed_bps == 15.0
    assert len(pf.positions) == 2
    # stock_code is zero-padded to 6 digits
    assert pf.positions[0].stock_code == "005930"
    # comma-formatted reference_close coerced to int
    assert pf.positions[0].reference_close == 71500
    # side normalized to upper
    assert pf.positions[1].side == "BUY"


def test_load_missing_file(tmp_path):
    with pytest.raises(PortfolioError, match="not found"):
        load_portfolio(tmp_path / "nope.json")


def test_load_bad_json(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(PortfolioError, match="Invalid JSON"):
        load_portfolio(p)


def test_load_empty_positions(tmp_path):
    p = _write(tmp_path, {"signal_id": "x", "positions": []})
    with pytest.raises(PortfolioError, match="non-empty 'positions'"):
        load_portfolio(p)


def test_load_invalid_side(tmp_path):
    p = _write(tmp_path, {"signal_id": "x", "positions": [
        {"stock_code": "005930", "side": "HOLD", "weight": 1.0},
    ]})
    with pytest.raises(PortfolioError, match="invalid side"):
        load_portfolio(p)


def test_load_sell_requires_qty(tmp_path):
    p = _write(tmp_path, {"signal_id": "x", "positions": [
        {"stock_code": "005930", "side": "SELL"},
    ]})
    with pytest.raises(PortfolioError, match="SELL"):
        load_portfolio(p)


def test_load_sell_with_qty(tmp_path):
    p = _write(tmp_path, {"signal_id": "x", "positions": [
        {"stock_code": "005930", "side": "SELL", "qty": 5},
    ]})
    pf = load_portfolio(p)
    assert pf.positions[0].qty == 5


def test_by_side():
    pf = Portfolio("s", "n", "d", "weekly", 0, [
        Position("005930", "A", "BUY", weight=0.5),
        Position("000660", "B", "SELL", qty=3),
    ])
    assert len(pf.by_side("BUY")) == 1
    assert len(pf.by_side("sell")) == 1


# ── allocate_buy_quantities ──────────────────────────────────────────────────

def test_allocate_floor_plus_greedy_remainder():
    buys = [
        Position("A", "A", "BUY", weight=0.5),
        Position("B", "B", "BUY", weight=0.5),
    ]
    prices = {"A": 30_000, "B": 40_000}
    qty = allocate_buy_quantities(buys, 1_000_000, prices)
    # floor: A=16 (480k), B=12 (480k); remainder 40k.
    # greedy weight-DESC (tie -> input order): A gets +1 (510k), remainder 10k
    # < cheapest (30k) -> stop.
    assert qty == {"A": 17, "B": 12}
    used = qty["A"] * prices["A"] + qty["B"] * prices["B"]
    assert used <= 1_000_000


def test_allocate_greedy_prefers_higher_weight():
    buys = [
        Position("LO", "lo", "BUY", weight=0.2),
        Position("HI", "hi", "BUY", weight=0.8),
    ]
    prices = {"LO": 10_000, "HI": 10_000}
    # capital 125,000: floor LO=2 (20k from 25k budget), HI=10 (100k).
    # used 120k, remainder 5k -> nobody affordable (min price 10k). no change.
    qty = allocate_buy_quantities(buys, 125_000, prices)
    assert qty == {"LO": 2, "HI": 10}

    # capital 135,000: remainder after floor = 135k - (2*10k + 10*10k)=15k.
    # greedy weight-DESC: HI first +1 (5k left), then LO not affordable.
    qty2 = allocate_buy_quantities(buys, 135_000, prices)
    assert qty2 == {"LO": 2, "HI": 11}


def test_allocate_no_quote_gets_zero():
    buys = [Position("A", "A", "BUY", weight=1.0)]
    qty = allocate_buy_quantities(buys, 1_000_000, {"A": 0})
    assert qty == {"A": 0}


def test_allocate_empty():
    assert allocate_buy_quantities([], 1_000_000, {}) == {}
