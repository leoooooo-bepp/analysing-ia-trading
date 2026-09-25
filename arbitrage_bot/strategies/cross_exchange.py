"""Arbitrage spatial entre plateformes, sur inventaire pré-positionné.

On détient à la fois de la quote (USDT) et de la base (BTC…) sur chaque plateforme :
achat sur A et vente sur B partent en même temps, sans aucun transfert on-chain.
Le rééquilibrage de l'inventaire se fait à part (voir `rebalancer`).
"""
from __future__ import annotations

from itertools import permutations

from ..config import BotConfig
from ..orderbook import max_profitable_fill
from . import Leg, Opportunity

Books = dict[tuple[str, str], dict]
Balances = dict[str, dict[str, float]]


def scan(cfg: BotConfig, books: Books, balances: Balances, trade_budget: float) -> list[Opportunity]:
    ce = cfg.cross_exchange
    if not ce.enabled:
        return []
    fees = {e.name: e.taker_fee for e in cfg.enabled_exchanges}
    min_edge = ce.min_edge + ce.latency_buffer
    out: list[Opportunity] = []

    for symbol in ce.symbols:
        base, quote = symbol.split("/")
        venues = [ex for ex in fees if (ex, symbol) in books]
        for buy_ex, sell_ex in permutations(venues, 2):
            asks = books[(buy_ex, symbol)]["asks"]
            bids = books[(sell_ex, symbol)]["bids"]
            if not asks or not bids or bids[0][0] <= asks[0][0]:
                continue  # pas de croisement brut, inutile de calculer
            quote_free = balances.get(buy_ex, {}).get(quote, 0.0)
            base_free = balances.get(sell_ex, {}).get(base, 0.0)
            fill = max_profitable_fill(
                asks, bids, fees[buy_ex], fees[sell_ex], min_edge,
                max_qty=base_free,
                max_cost=min(quote_free, trade_budget),
            )
            if fill is None:
                continue
            out.append(Opportunity(
                strategy="cross_exchange",
                legs=(
                    Leg(buy_ex, symbol, "buy", fill.qty, fill.worst_ask),
                    Leg(sell_ex, symbol, "sell", fill.qty, fill.worst_bid),
                ),
                notional=fill.cost,
                expected_profit=fill.profit,
                edge=fill.edge,
            ))
    return out
