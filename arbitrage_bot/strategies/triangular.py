"""Arbitrage triangulaire intra-plateforme : USDT -> X -> Y -> USDT.

Aucun risque de transfert, tout se passe sur un seul compte. On évalue chaque cycle
à plusieurs tailles en parcourant la profondeur réelle de chaque carnet.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..config import BotConfig
from . import Leg, Opportunity

SIZE_STEPS = (1.0, 0.5, 0.25, 0.1)


@dataclass(frozen=True)
class Hop:
    symbol: str
    side: str      # buy: on dépense la quote pour obtenir la base ; sell: l'inverse
    src: str
    dst: str


def _hop(src: str, dst: str, symbols: set[str]) -> Hop | None:
    if f"{dst}/{src}" in symbols:
        return Hop(f"{dst}/{src}", "buy", src, dst)
    if f"{src}/{dst}" in symbols:
        return Hop(f"{src}/{dst}", "sell", src, dst)
    return None


def build_cycles(symbols: set[str], base_asset: str, max_cycles: int) -> list[tuple[Hop, Hop, Hop]]:
    assets = {a for s in symbols for a in s.split("/")} - {base_asset}
    linked = sorted(a for a in assets if _hop(base_asset, a, symbols))
    cycles = []
    for x in linked:
        for y in linked:
            if x == y:
                continue
            h1, h2, h3 = _hop(base_asset, x, symbols), _hop(x, y, symbols), _hop(y, base_asset, symbols)
            if h1 and h2 and h3:
                cycles.append((h1, h2, h3))
                if len(cycles) >= max_cycles:
                    return cycles
    return cycles


def _convert(book: dict, hop: Hop, amount_in: float, fee: float) -> tuple[float, float, float] | None:
    """Convertit `amount_in` (en devise src) à travers le carnet.
    Retourne (montant reçu net de frais, quantité de base de l'ordre, prix limite)."""
    if hop.side == "buy":
        spend, base, worst = amount_in, 0.0, 0.0
        for price, size in book["asks"]:
            take = min(size, spend / price)
            base += take
            spend -= take * price
            worst = price
            if spend <= 1e-12:
                break
        if spend > 1e-9:
            return None  # profondeur insuffisante
        return base * (1 - fee), base, worst
    left, quote, worst = amount_in, 0.0, 0.0
    for price, size in book["bids"]:
        take = min(size, left)
        quote += take * price
        left -= take
        worst = price
        if left <= 1e-12:
            break
    if left > 1e-9:
        return None
    return quote * (1 - fee), amount_in, worst


def _funded(legs: list[Leg], bal: dict[str, float]) -> bool:
    """En mode simultané, chaque jambe doit être financée par l'inventaire déjà détenu."""
    for leg in legs:
        base, quote = leg.symbol.split("/")
        need, asset = (leg.amount * leg.price, quote) if leg.side == "buy" else (leg.amount, base)
        if bal.get(asset, 0.0) < need:
            return False
    return True


def scan(cfg: BotConfig, books: dict, cycles_by_exchange: dict[str, list], balances: dict,
         trade_budget: float) -> list[Opportunity]:
    tri = cfg.triangular
    if not tri.enabled:
        return []
    out: list[Opportunity] = []
    for ex, cycles in cycles_by_exchange.items():
        fee = cfg.exchange(ex).taker_fee
        budget = min(trade_budget, balances.get(ex, {}).get(tri.base_asset, 0.0))
        if budget <= 0:
            continue
        for cycle in cycles:
            cycle_books = [books.get((ex, h.symbol)) for h in cycle]
            if not all(b and b["bids"] and b["asks"] for b in cycle_books):
                continue
            best: Opportunity | None = None
            for step in SIZE_STEPS:
                start = budget * step
                amount, legs = start, []
                for hop, book in zip(cycle, cycle_books):
                    res = _convert(book, hop, amount, fee)
                    if res is None:
                        break
                    amount, qty, px = res
                    legs.append(Leg(ex, hop.symbol, hop.side, qty, px))
                else:
                    if tri.concurrent and not _funded(legs, balances.get(ex, {})):
                        continue
                    edge = amount / start - 1
                    if edge >= tri.min_edge and (best is None or amount - start > best.expected_profit):
                        best = Opportunity("triangular", tuple(legs), start, amount - start, edge,
                                           meta={"path": [h.dst for h in cycle]})
            if best:
                out.append(best)
    return out
