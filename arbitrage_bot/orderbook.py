"""Calculs sur carnets d'ordres : VWAP en profondeur et taille optimale d'arbitrage."""
from __future__ import annotations

from dataclasses import dataclass

Level = tuple[float, float]  # (prix, quantité)


@dataclass(frozen=True)
class ArbFill:
    qty: float          # quantité de base achetée puis revendue
    cost: float         # quote dépensée à l'achat, frais inclus
    proceeds: float     # quote reçue à la vente, frais déduits
    worst_ask: float    # prix limite à placer côté achat
    worst_bid: float    # prix limite à placer côté vente

    @property
    def profit(self) -> float:
        return self.proceeds - self.cost

    @property
    def edge(self) -> float:
        return self.profit / self.cost if self.cost else 0.0


def vwap(levels: list[Level], qty: float) -> tuple[float, float]:
    """Remplit `qty` en parcourant le carnet. Retourne (quantité remplie, prix moyen)."""
    filled = notional = 0.0
    for price, size in levels:
        take = min(size, qty - filled)
        filled += take
        notional += take * price
        if filled >= qty:
            break
    return filled, (notional / filled if filled else 0.0)


def max_profitable_fill(
    asks: list[Level],
    bids: list[Level],
    fee_buy: float,
    fee_sell: float,
    min_edge: float,
    max_qty: float = float("inf"),
    max_cost: float = float("inf"),
) -> ArbFill | None:
    """Parcourt simultanément les asks (achat) et les bids (vente) niveau par niveau
    et s'arrête dès que la marge *marginale* nette de frais passe sous `min_edge`.

    On capture ainsi toute la profondeur rentable, pas seulement le meilleur niveau.
    """
    i = j = 0
    ask_left = asks[0][1] if asks else 0.0
    bid_left = bids[0][1] if bids else 0.0
    qty = cost = proceeds = 0.0
    worst_ask = worst_bid = 0.0

    while i < len(asks) and j < len(bids) and qty < max_qty and cost < max_cost:
        ask_px, bid_px = asks[i][0], bids[j][0]
        unit_cost = ask_px * (1 + fee_buy)
        unit_proceeds = bid_px * (1 - fee_sell)
        if unit_proceeds / unit_cost - 1 < min_edge:
            break

        chunk = min(ask_left, bid_left, max_qty - qty, (max_cost - cost) / unit_cost)
        if chunk <= 0:
            break
        qty += chunk
        cost += chunk * unit_cost
        proceeds += chunk * unit_proceeds
        worst_ask, worst_bid = ask_px, bid_px

        ask_left -= chunk
        bid_left -= chunk
        if ask_left <= 1e-15:
            i += 1
            ask_left = asks[i][1] if i < len(asks) else 0.0
        if bid_left <= 1e-15:
            j += 1
            bid_left = bids[j][1] if j < len(bids) else 0.0

    if qty <= 0:
        return None
    return ArbFill(qty, cost, proceeds, worst_ask, worst_bid)
