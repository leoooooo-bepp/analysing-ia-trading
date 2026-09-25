"""Exécution des opportunités : ordres IOC simultanés, couverture des jambes orphelines."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from .config import BotConfig
from .exchanges import Fill, Venue
from .strategies import Leg, Opportunity

log = logging.getLogger(__name__)

HEDGE_AGGRESSION = 0.005  # prix limite à 0,5 % au-delà du meilleur prix pour garantir la couverture


@dataclass
class Result:
    opp: Opportunity
    pnl: float
    ok: bool
    fills: list[tuple[Leg, Fill]]


class Executor:
    def __init__(self, cfg: BotConfig, venues: dict[str, Venue]):
        self.cfg, self.venues = cfg, venues

    async def _send(self, leg: Leg, amount: float | None = None, price: float | None = None) -> Fill:
        try:
            return await self.venues[leg.exchange].order(
                leg.symbol, leg.side, amount if amount is not None else leg.amount,
                price if price is not None else leg.price, leg.market_type)
        except Exception as exc:
            log.warning("Ordre rejeté %s %s %s: %s", leg.exchange, leg.side, leg.symbol, exc)
            return Fill(0.0, 0.0, 0.0)

    async def _aggressive(self, exchange: str, symbol: str, side: str, amount: float, market_type: str) -> Fill:
        venue = self.venues[exchange]
        book = venue.books.get(symbol) or await venue.order_book(symbol)
        best = book["asks"][0][0] if side == "buy" else book["bids"][0][0]
        price = best * (1 + HEDGE_AGGRESSION if side == "buy" else 1 - HEDGE_AGGRESSION)
        return await self._send(Leg(exchange, symbol, side, amount, price, market_type))

    @staticmethod
    def _quote_pnl(fills: list[tuple[Leg, Fill]]) -> float:
        pnl = 0.0
        for leg, f in fills:
            pnl += (f.notional if leg.side == "sell" else -f.notional) - f.fee_quote
        return pnl

    async def execute(self, opp: Opportunity) -> Result:
        if opp.strategy == "triangular":
            return await self._triangular(opp)
        return await self._paired(opp)

    async def _paired(self, opp: Opportunity) -> Result:
        """Deux jambes (achat/vente) envoyées en parallèle. Si l'une remplit plus que l'autre,
        on complète la jambe manquante : d'abord au prix d'équilibre (trade à PnL nul),
        puis en agressif si le marché s'est vraiment enfui."""
        buy, sell = sorted(opp.legs, key=lambda l: l.side)  # 'buy' < 'sell'
        fb, fs = await asyncio.gather(self._send(buy), self._send(sell))
        fills = [(buy, fb), (sell, fs)]
        ok = True  # échec = seulement si on a dû couvrir en agressif (perte subie)
        diff = fb.filled - fs.filled
        tolerance = self.cfg.risk.max_leg_imbalance * max(fb.filled, fs.filled, 1e-12)
        if abs(diff) > tolerance:
            fee_b = self.cfg.exchange(buy.exchange).taker_fee
            fee_s = self.cfg.exchange(sell.exchange).taker_fee
            if diff > 0:   # l'achat est passé, pas la vente : on vend le reste au prix d'équilibre
                missing = Leg(sell.exchange, sell.symbol, "sell", diff,
                              fb.avg_price * (1 + fee_b) / (1 - fee_s), sell.market_type)
            else:          # la vente est passée, pas l'achat
                missing = Leg(buy.exchange, buy.symbol, "buy", -diff,
                              fs.avg_price * (1 - fee_s) / (1 + fee_b), buy.market_type)
            f = await self._send(missing)
            fills.append((missing, f))
            left = missing.amount - f.filled
            if left > tolerance:
                hf = await self._aggressive(missing.exchange, missing.symbol, missing.side, left,
                                            missing.market_type)
                fills.append((Leg(missing.exchange, missing.symbol, missing.side, left, hf.avg_price,
                                  missing.market_type), hf))
                ok = False
                log.warning("Jambe orpheline couverte en agressif (%s %.6f %s)", missing.side, left, missing.symbol)
        return Result(opp, self._quote_pnl(fills), ok, fills)

    async def _triangular(self, opp: Opportunity) -> Result:
        if self.cfg.triangular.concurrent:
            return await self._triangular_concurrent(opp)
        return await self._triangular_sequential(opp)

    async def _triangular_concurrent(self, opp: Opportunity) -> Result:
        """Les 3 jambes partent en même temps, chacune financée par l'inventaire déjà détenu :
        aucune jambe n'attend la précédente, la latence n'est payée qu'une fois.
        Un remplissage partiel laisse simplement un petit écart d'inventaire, valorisé au prix moyen."""
        ex = opp.legs[0].exchange
        fee = self.cfg.exchange(ex).taker_fee
        results = await asyncio.gather(*(self._send(l) for l in opp.legs))
        fills = list(zip(opp.legs, results))
        delta: dict[str, float] = {}
        for leg, f in fills:
            base, quote = leg.symbol.split("/")
            if leg.side == "buy":
                delta[base] = delta.get(base, 0.0) + f.filled * (1 - fee)
                delta[quote] = delta.get(quote, 0.0) - f.notional
            else:
                delta[base] = delta.get(base, 0.0) - f.filled
                delta[quote] = delta.get(quote, 0.0) + f.notional * (1 - fee)
        pnl = sum(q * self._mid(ex, a) for a, q in delta.items())
        return Result(opp, pnl, all(f.filled > 0 for _, f in fills), fills)

    def _mid(self, exchange: str, asset: str) -> float:
        base_asset = self.cfg.triangular.base_asset
        if asset == base_asset:
            return 1.0
        book = self.venues[exchange].books.get(f"{asset}/{base_asset}")
        if not book or not book["bids"] or not book["asks"]:
            return 0.0
        return (book["bids"][0][0] + book["asks"][0][0]) / 2

    async def _triangular_sequential(self, opp: Opportunity) -> Result:
        """Jambes séquentielles : la quantité de chaque jambe dépend du remplissage précédent.
        Si le cycle casse, on revend l'actif intermédiaire vers l'actif de départ."""
        base_asset = self.cfg.triangular.base_asset
        fee = self.cfg.exchange(opp.legs[0].exchange).taker_fee
        fills: list[tuple[Leg, Fill]] = []
        amount = opp.notional   # en devise source de la jambe courante
        spent = None
        holding = base_asset
        for leg in opp.legs:
            qty = amount / leg.price if leg.side == "buy" else amount
            f = await self._send(leg, amount=qty)
            fills.append((leg, f))
            if f.filled <= 0:
                break
            if spent is None:
                spent = f.notional if leg.side == "buy" else f.filled
            base, quote = leg.symbol.split("/")
            amount = f.filled * (1 - fee) if leg.side == "buy" else f.notional * (1 - fee)
            holding = base if leg.side == "buy" else quote
        if holding != base_asset and amount > 0:
            amount = await self._unwind(opp.legs[0].exchange, holding, amount, base_asset, fee, fills)
        if spent is None:
            return Result(opp, 0.0, False, fills)
        ok = len(fills) == 3 and all(f.filled > 0 for _, f in fills)
        return Result(opp, amount - spent, ok, fills)

    async def _unwind(self, exchange: str, asset: str, amount: float, base_asset: str, fee: float,
                      fills: list) -> float:
        symbol = f"{asset}/{base_asset}"
        log.warning("Cycle triangulaire interrompu : liquidation de %.6f %s", amount, asset)
        f = await self._aggressive(exchange, symbol, "sell", amount, "spot")
        fills.append((Leg(exchange, symbol, "sell", amount, f.avg_price), f))
        return f.notional * (1 - fee)
