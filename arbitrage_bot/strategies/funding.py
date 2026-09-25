"""Cash-and-carry sur le funding rate : long spot + short perpétuel (delta neutre).

C'est la source de rendement la plus régulière de l'arbitrage crypto : quand le marché
est haussier, les longs paient les shorts toutes les 8 h. Le levier sur la jambe perp
réduit le capital immobilisé et augmente donc le rendement du capital.
"""
from __future__ import annotations

from ..config import BotConfig
from . import Leg, Opportunity

PERIODS_PER_DAY = 3  # funding toutes les 8 h
ENTRY_TOLERANCE = 0.0005  # le carry n'est pas une course : on traverse un peu le carnet pour être rempli


def perp_symbol(spot_symbol: str) -> str:
    base, quote = spot_symbol.split("/")
    return f"{base}/{quote}:{quote}"


def annualize(rate_per_period: float) -> float:
    return rate_per_period * PERIODS_PER_DAY * 365


def evaluate(spot_ask: float, perp_bid: float, funding_rate: float, fee: float,
             leverage: float, hold_periods: int) -> tuple[float, float]:
    """Retourne (rendement attendu sur le capital pour la période de détention, rendement annualisé)."""
    basis = perp_bid / spot_ask - 1            # contango capturé à l'entrée
    gross = funding_rate * hold_periods + basis - 4 * fee   # 2 jambes x entrée + sortie
    capital_per_notional = 1 + 1 / leverage    # spot payé cash + marge du perp
    ret = gross / capital_per_notional
    days = hold_periods / PERIODS_PER_DAY
    return ret, ret * 365 / days


def scan(cfg: BotConfig, books: dict, funding_rates: dict, open_keys: set,
         trade_budget: float) -> list[Opportunity]:
    fc = cfg.funding
    if not fc.enabled or len(open_keys) >= fc.max_positions:
        return []
    out = []
    for ex in fc.exchanges:
        fee = cfg.exchange(ex).taker_fee
        for symbol in fc.symbols:
            perp = perp_symbol(symbol)
            rate = funding_rates.get((ex, perp))
            spot_book, perp_book = books.get((ex, symbol)), books.get((ex, perp))
            if rate is None or not spot_book or not perp_book or (ex, symbol) in open_keys:
                continue
            if not spot_book["asks"] or not perp_book["bids"]:
                continue
            spot_ask = spot_book["asks"][0][0] * (1 + ENTRY_TOLERANCE)
            perp_bid = perp_book["bids"][0][0] * (1 - ENTRY_TOLERANCE)
            ret, annual = evaluate(spot_ask, perp_bid, rate, fee, fc.leverage, fc.expected_hold_periods)
            if annual < fc.min_annualized:
                continue
            notional = trade_budget / (1 + 1 / fc.leverage)
            qty = notional / spot_ask
            out.append(Opportunity(
                strategy="funding",
                legs=(
                    Leg(ex, symbol, "buy", qty, spot_ask),
                    Leg(ex, perp, "sell", qty, perp_bid, market_type="swap"),
                ),
                notional=trade_budget,
                expected_profit=trade_budget * ret,
                edge=ret,
                meta={"annualized": annual, "funding_rate": rate, "leverage": fc.leverage},
            ))
    return out


def should_exit(funding_rate: float, cfg: BotConfig) -> bool:
    return annualize(funding_rate) < cfg.funding.exit_annualized
