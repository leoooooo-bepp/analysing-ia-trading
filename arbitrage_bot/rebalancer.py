"""Rééquilibrage de l'inventaire entre plateformes.

L'arbitrage spatial déplace mécaniquement la base vers la plateforme la moins chère et la
quote vers la plus chère. Sans rééquilibrage, l'inventaire s'épuise et les opportunités
ne peuvent plus être prises. En mode paper/demo on simule un transfert instantané facturé ;
en live on se contente d'alerter (les clés API n'ont volontairement pas le droit de retrait).
"""
from __future__ import annotations

import logging

from .exchanges import Venue

log = logging.getLogger(__name__)

TRIGGER_SHARE = 0.25     # on rééquilibre si une plateforme détient < 25 % de sa part cible
TRANSFER_COST = 0.0005   # coût simulé d'un transfert (frais réseau + retrait), en fraction du montant


def rebalance(venues: dict[str, Venue], assets: set[str], live: bool) -> dict[str, float]:
    """Rééquilibre chaque actif à parts égales. Retourne la quantité perdue en frais par actif."""
    costs: dict[str, float] = {}
    for asset in assets:
        amounts = {n: v.wallet.balances.get(asset, 0.0) if v.wallet else 0.0 for n, v in venues.items()}
        total = sum(amounts.values())
        if total <= 0 or len(amounts) < 2:
            continue
        target = total / len(amounts)
        starved = [n for n, a in amounts.items() if a < target * TRIGGER_SHARE]
        if not starved:
            continue
        if live:
            log.warning("Inventaire %s déséquilibré sur %s — rééquilibrage manuel requis", asset, starved)
            continue
        moved = sum(target - a for a in amounts.values() if a < target)
        cost = moved * TRANSFER_COST
        for name in amounts:
            venues[name].wallet.balances[asset] = (total - cost) / len(amounts)
        costs[asset] = cost
        log.info("Rééquilibrage %s (%s à sec) : %.6f déplacés, coût %.6f", asset, starved, moved, cost)
    return costs
