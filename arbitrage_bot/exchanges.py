"""Accès aux plateformes : ccxt (réel ou papier) et marché synthétique pour la démo."""
from __future__ import annotations

import asyncio
import logging
import os
import random
from collections import defaultdict
from dataclasses import dataclass

from .config import EngineConfig, ExchangeConfig

log = logging.getLogger(__name__)


@dataclass
class Fill:
    filled: float
    avg_price: float
    fee_quote: float

    @property
    def notional(self) -> float:
        return self.filled * self.avg_price


class Wallet:
    """Soldes simulés (modes paper et demo)."""

    def __init__(self):
        self.balances: dict[str, float] = defaultdict(float)
        self.positions: dict[str, float] = defaultdict(float)   # perps : quantité signée

    def apply(self, symbol: str, side: str, fill: Fill, market_type: str) -> None:
        base, quote = symbol.split(":")[0].split("/")
        sign = 1 if side == "buy" else -1
        if market_type == "swap":
            self.positions[symbol] += sign * fill.filled
            self.balances[quote] -= fill.fee_quote
            return
        self.balances[base] += sign * fill.filled
        self.balances[quote] -= sign * fill.notional + fill.fee_quote


def simulate_fill(book: dict, side: str, amount: float, limit: float, fee: float, slippage: float) -> Fill:
    """Remplissage IOC : on ne prend que les niveaux à un prix égal ou meilleur que la limite."""
    levels = book["asks"] if side == "buy" else book["bids"]
    filled = notional = 0.0
    for price, size in levels:
        if (side == "buy" and price > limit) or (side == "sell" and price < limit):
            break
        take = min(size, amount - filled)
        filled += take
        notional += take * price
        if filled >= amount:
            break
    if not filled:
        return Fill(0.0, 0.0, 0.0)
    avg = notional / filled * (1 + slippage if side == "buy" else 1 - slippage)
    return Fill(filled, avg, filled * avg * fee)


class Venue:
    """Interface commune à toutes les plateformes."""

    name: str
    cfg: ExchangeConfig
    wallet: Wallet | None = None
    books: dict[str, dict]

    async def load_markets(self) -> set[str]: ...
    async def order_book(self, symbol: str) -> dict: ...
    async def funding_rate(self, symbol: str) -> float | None: ...
    async def balances(self) -> dict[str, float]: ...
    async def order(self, symbol: str, side: str, amount: float, price: float, market_type: str = "spot") -> Fill: ...
    async def close(self) -> None: ...


class CcxtVenue(Venue):
    def __init__(self, cfg: ExchangeConfig, engine: EngineConfig):
        self.cfg, self.name, self.engine = cfg, cfg.name, engine
        self.live = engine.mode == "live"
        self.books = {}
        module = None
        if engine.use_websocket:
            try:
                import ccxt.pro as module  # type: ignore
            except ImportError:
                module = None
        if module is None:
            import ccxt.async_support as module  # type: ignore
        prefix = cfg.name.upper()
        params = {"enableRateLimit": True, **cfg.options}
        if self.live:
            params.update(apiKey=os.getenv(f"{prefix}_API_KEY"), secret=os.getenv(f"{prefix}_API_SECRET"),
                          password=os.getenv(f"{prefix}_API_PASSWORD"))
        self.client = getattr(module, cfg.name)(params)
        self.ws = engine.use_websocket and hasattr(self.client, "watch_order_book")
        self.wallet = None if self.live else Wallet()

    async def load_markets(self) -> set[str]:
        markets = await self.client.load_markets()
        return {s for s, m in markets.items() if m.get("active", True) and m.get("spot")}

    async def order_book(self, symbol: str) -> dict:
        depth = self.engine.orderbook_depth
        if self.ws:
            ob = await self.client.watch_order_book(symbol, depth)
        else:
            ob = await self.client.fetch_order_book(symbol, depth)
        book = {"bids": [tuple(l[:2]) for l in ob["bids"][:depth]],
                "asks": [tuple(l[:2]) for l in ob["asks"][:depth]]}
        self.books[symbol] = book
        return book

    async def funding_rate(self, symbol: str) -> float | None:
        try:
            return (await self.client.fetch_funding_rate(symbol)).get("fundingRate")
        except Exception as exc:  # plateforme sans perp pour ce symbole
            log.debug("%s funding %s: %s", self.name, symbol, exc)
            return None

    async def balances(self) -> dict[str, float]:
        if self.wallet is not None:
            return dict(self.wallet.balances)
        bal = await self.client.fetch_balance()
        return {k: float(v) for k, v in bal.get("free", {}).items() if v}

    async def order(self, symbol, side, amount, price, market_type="spot") -> Fill:
        if not self.live:
            book = self.books.get(symbol) or await self.order_book(symbol)
            fill = simulate_fill(book, side, amount, price, self.cfg.taker_fee, self.engine.paper_slippage)
            self.wallet.apply(symbol, side, fill, market_type)
            return fill
        c = self.client
        amount_p = float(c.amount_to_precision(symbol, amount))
        price_p = float(c.price_to_precision(symbol, price))
        if amount_p <= 0:
            return Fill(0.0, 0.0, 0.0)
        o = await c.create_order(symbol, "limit", side, amount_p, price_p, {"timeInForce": "IOC"})
        if o.get("filled") is None or o.get("status") not in ("closed", "canceled", "expired"):
            o = await c.fetch_order(o["id"], symbol)
        filled = float(o.get("filled") or 0.0)
        avg = float(o.get("average") or o.get("price") or price_p)
        fee = o.get("fee") or {}
        quote = symbol.split(":")[0].split("/")[1]
        fee_quote = float(fee["cost"]) if fee.get("currency") == quote and fee.get("cost") is not None \
            else filled * avg * self.cfg.taker_fee
        return Fill(filled, avg, fee_quote)

    async def close(self) -> None:
        await self.client.close()


class SyntheticVenue(Venue):
    """Marché simulé hors-ligne : un prix de référence commun + des décalages propres à chaque
    plateforme qui créent régulièrement des dislocations exploitables."""

    _ref: dict[str, float] = {}
    _REF_PRICES = {"BTC": 65_000.0, "ETH": 3_200.0, "SOL": 150.0, "XRP": 0.6, "DOGE": 0.15,
                   "AVAX": 35.0, "LINK": 15.0, "BNB": 580.0}

    def __init__(self, cfg: ExchangeConfig, engine: EngineConfig, symbols: set[str], seed: int = 0):
        self.cfg, self.name, self.engine = cfg, cfg.name, engine
        self.symbols = symbols
        self.rng = random.Random(f"{cfg.name}-{seed}")
        self.wallet = Wallet()
        self.books = {}
        self.skew: dict[str, float] = defaultdict(float)

    @classmethod
    def step_reference(cls, rng: random.Random) -> None:
        for asset, px in cls._ref.items():
            cls._ref[asset] = px * (1 + rng.gauss(0, 0.0004))

    def _mid(self, symbol: str) -> float:
        base, quote = symbol.split(":")[0].split("/")

        def px(asset: str) -> float:
            if asset in ("USDT", "USDC", "FDUSD"):
                return 1.0
            default = self._REF_PRICES.get(asset, 1 + sum(map(ord, asset)) % 50)
            return self._ref.setdefault(asset, default)

        return px(base) / px(quote)

    async def load_markets(self) -> set[str]:
        return set(self.symbols)

    async def order_book(self, symbol: str) -> dict:
        # dérive du décalage local avec fort retour vers zéro : les autres arbitragistes
        # referment les écarts en permanence, seuls de rares chocs de liquidité persistent
        s = self.skew[symbol] * 0.6 + self.rng.gauss(0, 0.00025)
        if self.rng.random() < 0.004:
            s += self.rng.choice((-1, 1)) * self.rng.uniform(0.001, 0.004)
        self.skew[symbol] = s
        mid = self._mid(symbol) * (1 + s)
        spread = mid * 0.0001
        notional_per_level = 4_000.0
        bids, asks = [], []
        for k in range(self.engine.orderbook_depth):
            step = spread * (1 + k * 0.8)
            size = notional_per_level * self.rng.uniform(0.5, 2.0) * (1 + k * 0.3) / mid
            bids.append((mid - step, size))
            asks.append((mid + step, size))
        book = {"bids": bids, "asks": asks}
        self.books[symbol] = book
        await asyncio.sleep(0)
        return book

    async def funding_rate(self, symbol: str) -> float | None:
        return max(-0.0005, self.rng.gauss(0.0002, 0.00025))

    async def balances(self) -> dict[str, float]:
        return dict(self.wallet.balances)

    async def order(self, symbol, side, amount, price, market_type="spot") -> Fill:
        # latence : le carnet a bougé entre la détection et l'arrivée de l'ordre
        book = await self.order_book(symbol)
        fill = simulate_fill(book, side, amount, price, self.cfg.taker_fee, self.engine.paper_slippage)
        self.wallet.apply(symbol, side, fill, market_type)
        return fill

    async def close(self) -> None:
        return None
