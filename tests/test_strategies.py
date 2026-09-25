import asyncio

import pytest

from arbitrage_bot.config import (BotConfig, CrossExchangeConfig, EngineConfig, ExchangeConfig,
                                  FundingConfig, RiskConfig, TriangularConfig, load_config)
from arbitrage_bot.exchanges import Fill, Wallet, simulate_fill
from arbitrage_bot.orderbook import max_profitable_fill, vwap
from arbitrage_bot.risk import RiskManager
from arbitrage_bot.strategies import Leg, Opportunity, cross_exchange, funding, triangular


def make_cfg(**over) -> BotConfig:
    cfg = BotConfig(
        exchanges=[ExchangeConfig("a", taker_fee=0.001), ExchangeConfig("b", taker_fee=0.001)],
        cross_exchange=CrossExchangeConfig(symbols=["BTC/USDT"], min_edge=0.001, latency_buffer=0.0),
        triangular=TriangularConfig(exchanges=["a"], min_edge=0.001, concurrent=False),
        funding=FundingConfig(exchanges=["a"], symbols=["BTC/USDT"], min_annualized=0.2, leverage=3),
        risk=RiskConfig(),
        engine=EngineConfig(),
    )
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


def test_vwap_walks_levels():
    filled, avg = vwap([(100, 1), (101, 1)], 1.5)
    assert filled == 1.5
    assert avg == pytest.approx((100 + 50.5) / 1.5)


def test_max_profitable_fill_stops_when_marginal_edge_too_low():
    asks = [(100, 1), (100.5, 1), (102, 5)]
    bids = [(101, 1), (101, 1), (100, 5)]
    fill = max_profitable_fill(asks, bids, 0.001, 0.001, min_edge=0.0)
    # niveau 1 : 101*0.999/(100*1.001) > 1 ; niveau 2 : 101*0.999/(100.5*1.001) > 1 ; niveau 3 non
    assert fill.qty == pytest.approx(2)
    assert fill.profit > 0
    assert fill.worst_ask == 100.5


def test_max_profitable_fill_respects_budget():
    fill = max_profitable_fill([(100, 10)], [(110, 10)], 0, 0, 0, max_cost=250)
    assert fill.cost == pytest.approx(250)
    assert fill.qty == pytest.approx(2.5)


def test_cross_exchange_detects_spread_and_uses_inventory():
    cfg = make_cfg()
    books = {("a", "BTC/USDT"): {"bids": [(99, 5)], "asks": [(100, 5)]},
             ("b", "BTC/USDT"): {"bids": [(101, 5)], "asks": [(102, 5)]}}
    balances = {"a": {"USDT": 1_000}, "b": {"BTC": 1}}
    opps = cross_exchange.scan(cfg, books, balances, trade_budget=10_000)
    assert len(opps) == 1
    buy, sell = opps[0].legs
    assert (buy.exchange, buy.side, sell.exchange, sell.side) == ("a", "buy", "b", "sell")
    assert buy.amount == pytest.approx(1)  # limité par le BTC disponible sur b
    assert opps[0].expected_profit > 0


def test_cross_exchange_ignores_spread_eaten_by_fees():
    cfg = make_cfg()
    books = {("a", "BTC/USDT"): {"bids": [(99, 5)], "asks": [(100, 5)]},
             ("b", "BTC/USDT"): {"bids": [(100.15, 5)], "asks": [(102, 5)]}}
    balances = {"a": {"USDT": 1_000}, "b": {"BTC": 1}}
    assert cross_exchange.scan(cfg, books, balances, 10_000) == []


def test_triangular_cycle_found_and_profitable():
    symbols = {"BTC/USDT", "ETH/USDT", "ETH/BTC"}
    cycles = triangular.build_cycles(symbols, "USDT", 10)
    assert len(cycles) == 2
    # ETH/BTC sous-évalué : USDT -> BTC -> ETH -> USDT est rentable
    books = {("a", "BTC/USDT"): {"bids": [(60_000, 10)], "asks": [(60_001, 10)]},
             ("a", "ETH/BTC"): {"bids": [(0.0499, 100)], "asks": [(0.05, 100)]},
             ("a", "ETH/USDT"): {"bids": [(3_030, 100)], "asks": [(3_031, 100)]}}
    opps = triangular.scan(make_cfg(), books, {"a": cycles}, {"a": {"USDT": 5_000}}, 5_000)
    assert len(opps) == 1
    assert opps[0].meta["path"] == ["BTC", "ETH", "USDT"]
    assert opps[0].edge > 0.001


def test_triangular_concurrent_requires_inventory():
    cfg = make_cfg()
    cfg.triangular.concurrent = True
    cycles = triangular.build_cycles({"BTC/USDT", "ETH/USDT", "ETH/BTC"}, "USDT", 10)
    books = {("a", "BTC/USDT"): {"bids": [(60_000, 10)], "asks": [(60_001, 10)]},
             ("a", "ETH/BTC"): {"bids": [(0.0499, 100)], "asks": [(0.05, 100)]},
             ("a", "ETH/USDT"): {"bids": [(3_030, 100)], "asks": [(3_031, 100)]}}
    assert triangular.scan(cfg, books, {"a": cycles}, {"a": {"USDT": 5_000}}, 5_000) == []
    rich = {"a": {"USDT": 5_000, "BTC": 1, "ETH": 10}}
    assert len(triangular.scan(cfg, books, {"a": cycles}, rich, 5_000)) == 1


def test_funding_evaluate_and_scan():
    ret, annual = funding.evaluate(100, 100, 0.0005, 0.0005, leverage=3, hold_periods=9)
    # 9 x 0,05 % - 4 x 0,05 % = 0,25 % sur 1,333 de capital par unité de notionnel
    assert ret == pytest.approx(0.0025 / (4 / 3))
    assert annual == pytest.approx(ret * 365 / 3)
    books = {("a", "BTC/USDT"): {"bids": [(99.9, 5)], "asks": [(100, 5)]},
             ("a", "BTC/USDT:USDT"): {"bids": [(100.05, 5)], "asks": [(100.1, 5)]}}
    opps = funding.scan(make_cfg(), books, {("a", "BTC/USDT:USDT"): 0.001}, set(), 1_000)
    assert len(opps) == 1 and opps[0].legs[1].market_type == "swap"
    assert funding.scan(make_cfg(), books, {("a", "BTC/USDT:USDT"): 0.00001}, set(), 1_000) == []


def test_risk_kill_switch_and_selection():
    rm = RiskManager(RiskConfig(starting_equity=1_000, daily_loss_limit=0.05, max_concurrent_trades=2))
    legs = lambda ex: (Leg(ex, "BTC/USDT", "buy", 1, 1),)
    o1 = Opportunity("x", legs("a"), 100, 5, 0.05)
    o2 = Opportunity("x", legs("a"), 100, 3, 0.03)   # même carnet que o1 -> exclu
    o3 = Opportunity("x", legs("b"), 100, 1, 0.01)
    assert rm.select([o2, o3, o1]) == [o1, o3]
    rm.open(o1)
    rm.close(o1, -60, ok=True)
    assert rm.halted_reason == "daily_loss"
    assert not rm.can_trade(o3)


def test_simulated_fill_respects_limit_and_wallet():
    book = {"asks": [(100, 1), (105, 1)], "bids": [(99, 1)]}
    f = simulate_fill(book, "buy", 2, limit=101, fee=0.001, slippage=0)
    assert f.filled == 1 and f.avg_price == 100
    w = Wallet()
    w.balances["USDT"] = 1_000
    w.apply("BTC/USDT", "buy", f, "spot")
    assert w.balances["BTC"] == 1
    assert w.balances["USDT"] == pytest.approx(1_000 - 100 - 0.1)


def test_paired_execution_completes_orphan_leg():
    from arbitrage_bot.execution import Executor

    class FakeVenue:
        def __init__(self, fills):
            self.fills, self.books = list(fills), {"BTC/USDT": {"bids": [(100, 9)], "asks": [(100, 9)]}}

        async def order(self, symbol, side, amount, price, market_type="spot"):
            return self.fills.pop(0)

    cfg = make_cfg()
    venues = {"a": FakeVenue([Fill(1, 100, 0.1)]),
              "b": FakeVenue([Fill(0, 0, 0), Fill(1, 101, 0.1)])}   # vente ratée puis complétée
    opp = Opportunity("cross_exchange", (Leg("a", "BTC/USDT", "buy", 1, 100),
                                          Leg("b", "BTC/USDT", "sell", 1, 101)), 100, 0.8, 0.008)
    res = asyncio.run(Executor(cfg, venues).execute(opp))
    assert res.ok
    assert res.pnl == pytest.approx(101 - 100 - 0.2)


def test_shipped_config_loads():
    cfg = load_config("config/aggressive.yaml")
    assert cfg.engine.mode == "paper"
    assert cfg.risk.daily_loss_limit > 0


def test_funding_scan_respects_max_positions():
    cfg = make_cfg()
    cfg.funding.symbols = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
    cfg.funding.max_positions = 2
    books, rates = {}, {}
    for i, sym in enumerate(cfg.funding.symbols):
        books[("a", sym)] = {"bids": [(99.9, 5)], "asks": [(100, 5)]}
        books[("a", sym + ":USDT")] = {"bids": [(100.05, 5)], "asks": [(100.1, 5)]}
        rates[("a", sym + ":USDT")] = 0.001 * (i + 1)
    opps = funding.scan(cfg, books, rates, set(), 1_000)
    assert [o.legs[0].symbol for o in opps] == ["SOL/USDT", "ETH/USDT"]   # les meilleurs d'abord
    assert len(funding.scan(cfg, books, rates, {("a", "SOL/USDT")}, 1_000)) == 1
