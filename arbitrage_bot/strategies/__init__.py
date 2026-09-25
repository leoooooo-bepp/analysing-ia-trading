"""Stratégies d'arbitrage : chacune produit des `Opportunity` classées par profit attendu."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Leg:
    exchange: str
    symbol: str
    side: str            # buy | sell
    amount: float        # quantité en devise de base
    price: float         # prix limite (IOC)
    market_type: str = "spot"   # spot | swap


@dataclass(frozen=True)
class Opportunity:
    strategy: str
    legs: tuple[Leg, ...]
    notional: float          # capital engagé en devise de cotation
    expected_profit: float   # profit net attendu en devise de cotation
    edge: float              # rendement net du trade
    meta: dict = field(default_factory=dict, hash=False, compare=False)

    @property
    def key(self) -> tuple:
        """Identifie les ressources touchées pour éviter deux trades concurrents sur le même carnet."""
        return tuple(sorted({(l.exchange, l.symbol) for l in self.legs}))
