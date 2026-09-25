"""Moteur principal : flux de carnets, détection, sélection, exécution, suivi du PnL."""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import Counter, defaultdict

from .config import BotConfig
from .exchanges import CcxtVenue, SyntheticVenue, Venue
from .execution import Executor, Result
from .rebalancer import rebalance
from .risk import RiskManager
from .strategies import Leg, Opportunity, cross_exchange, funding, triangular

log = logging.getLogger(__name__)

STALE_AFTER = 2.0            # un carnet plus vieux que 2 s n'est pas utilisé
FUNDING_REFRESH = 60.0
REBALANCE_EVERY = 30.0
REPORT_EVERY = 10.0
STABLES = {"USDT", "USDC"}


class Engine:
    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self.mode = cfg.engine.mode
        self.risk = RiskManager(cfg.risk)
        self.venues: dict[str, Venue] = {}
        self.subscriptions: dict[str, set[str]] = defaultdict(set)
        self.cycles: dict[str, list] = {}
        self.funding_rates: dict[tuple[str, str], float] = {}
        self.funding_positions: dict[tuple[str, str], dict] = {}
        self.balances: dict[str, dict[str, float]] = {}
        self.pnl_by_strategy: Counter = Counter()
        self.trades_by_strategy: Counter = Counter()
        self.missed: Counter = Counter()
        self.tasks: set[asyncio.Task] = set()
        self.rng = random.Random(42)
        self.stop = asyncio.Event()

    # ------------------------------------------------------------------ setup
    def _all_symbols(self) -> set[str]:
        c = self.cfg
        syms = set(c.cross_exchange.symbols) | set(c.funding.symbols)
        if c.triangular.enabled and c.triangular.universe:
            base = c.triangular.base_asset
            u = c.triangular.universe
            syms |= {f"{a}/{base}" for a in u} | {f"{a}/{b}" for a in u for b in u if a != b}
        return syms

    async def setup(self) -> None:
        c = self.cfg
        wanted = self._all_symbols()
        for ex in c.enabled_exchanges:
            if self.mode == "demo":
                synth = wanted | {funding.perp_symbol(s) for s in c.funding.symbols}
                self.venues[ex.name] = SyntheticVenue(ex, c.engine, synth, seed=len(self.venues))
            else:
                self.venues[ex.name] = CcxtVenue(ex, c.engine)

        for name, venue in list(self.venues.items()):
            try:
                markets = await venue.load_markets()
            except Exception as exc:
                log.error("%s injoignable, plateforme ignorée : %s", name, exc)
                await venue.close()
                del self.venues[name]
                continue
            if c.cross_exchange.enabled:
                self.subscriptions[name] |= set(c.cross_exchange.symbols) & markets
            if c.triangular.enabled and name in c.triangular.exchanges:
                universe = set(c.triangular.universe) | {c.triangular.base_asset}
                pool = {s for s in markets if not c.triangular.universe or set(s.split("/")) <= universe}
                self.cycles[name] = triangular.build_cycles(pool, c.triangular.base_asset, c.triangular.max_cycles)
                self.subscriptions[name] |= {h.symbol for cyc in self.cycles[name] for h in cyc}
            if c.funding.enabled and name in c.funding.exchanges:
                for s in c.funding.symbols:
                    if s in markets:
                        self.subscriptions[name] |= {s, funding.perp_symbol(s)}
            log.info("%s : %d carnets suivis, %d cycles triangulaires",
                     name, len(self.subscriptions[name]), len(self.cycles.get(name, [])))
        if not self.venues:
            raise SystemExit("Aucune plateforme joignable : vérifie la connexion réseau (ou lance --mode demo).")

    async def _seed_paper_wallets(self) -> None:
        """Répartit le capital de départ : 50 % en stable, 50 % en actifs de base, à parts égales."""
        prices = self._prices()
        assets = {s.split(":")[0].split("/")[0] for subs in self.subscriptions.values() for s in subs} - STABLES
        assets = {a for a in assets if a in prices}
        per_venue = self.cfg.risk.starting_equity / len(self.venues)
        for venue in self.venues.values():
            venue.wallet.balances["USDT"] = per_venue / 2
            for a in assets:
                venue.wallet.balances[a] = per_venue / 2 / len(assets) / prices[a]

    def _prices(self) -> dict[str, float]:
        prices = {"USDT": 1.0, "USDC": 1.0}
        for venue in self.venues.values():
            for sym, book in venue.books.items():
                base, quote = sym.split(":")[0].split("/")
                if quote in STABLES and book["bids"] and book["asks"]:
                    prices.setdefault(base, (book["bids"][0][0] + book["asks"][0][0]) / 2)
        return prices

    # ------------------------------------------------------------------ data feeds
    async def _watch(self, venue: Venue, symbol: str) -> None:
        polling = not getattr(venue, "ws", False)
        while not self.stop.is_set():
            try:
                book = await venue.order_book(symbol)
                book["ts"] = time.monotonic()
            except Exception as exc:
                log.debug("%s %s carnet: %s", venue.name, symbol, exc)
                await asyncio.sleep(1.0)
            if polling:
                await asyncio.sleep(self.cfg.engine.tick_interval)

    def _fresh_books(self) -> dict[tuple[str, str], dict]:
        now = time.monotonic()
        return {(n, s): b for n, v in self.venues.items() for s, b in v.books.items()
                if now - b.get("ts", now) < STALE_AFTER}

    async def _refresh_balances(self) -> None:
        async def one(name, venue):
            try:
                self.balances[name] = await venue.balances()
            except Exception as exc:
                log.warning("%s soldes: %s", name, exc)
        await asyncio.gather(*(one(n, v) for n, v in self.venues.items()))

    async def _refresh_funding(self) -> None:
        pairs = [(ex, funding.perp_symbol(s)) for ex in self.cfg.funding.exchanges if ex in self.venues
                 for s in self.cfg.funding.symbols]
        rates = await asyncio.gather(*(self.venues[ex].funding_rate(sym) for ex, sym in pairs))
        self.funding_rates.update({p: r for p, r in zip(pairs, rates) if r is not None})
        await self._manage_funding_positions()

    async def _manage_funding_positions(self) -> None:
        now = time.time()
        for key, pos in list(self.funding_positions.items()):
            ex, spot = key
            rate = self.funding_rates.get((ex, funding.perp_symbol(spot)), 0.0)
            accrued = rate * pos["qty"] * pos["price"] * (now - pos["last"]) / (8 * 3600)
            pos["last"] = now
            self._book_pnl("funding", accrued)
            if funding.should_exit(rate, self.cfg):
                log.info("Sortie cash-and-carry %s %s (funding %.4f%%)", ex, spot, rate * 100)
                ex_ = Executor(self.cfg, self.venues)
                opp = pos["opp"]
                closing = tuple(Leg(l.exchange, l.symbol, "sell" if l.side == "buy" else "buy",
                                            l.amount, 0.0, l.market_type) for l in opp.legs)
                fills = [await ex_._aggressive(l.exchange, l.symbol, l.side, l.amount, l.market_type)
                         for l in closing]
                exit_pnl = Executor._quote_pnl(list(zip(closing, fills)))
                self._book_pnl("funding", exit_pnl + pos["entry_perp"] - pos["entry_spot"])
                del self.funding_positions[key]

    # ------------------------------------------------------------------ trading
    def _book_pnl(self, strategy: str, pnl: float) -> None:
        self.pnl_by_strategy[strategy] += pnl
        if strategy == "funding":
            self.risk.equity += pnl

    def scan(self) -> list[Opportunity]:
        books = self._fresh_books()
        budget = self.risk.trade_budget()
        return (
            cross_exchange.scan(self.cfg, books, self.balances, budget)
            + triangular.scan(self.cfg, books, self.cycles, self.balances, budget)
            + funding.scan(self.cfg, books, self.funding_rates, set(self.funding_positions), budget)
        )

    async def _run(self, opp: Opportunity) -> None:
        try:
            result: Result = await Executor(self.cfg, self.venues).execute(opp)
        except Exception:
            log.exception("Erreur d'exécution %s", opp.strategy)
            self.risk.close(opp, 0.0, False)
            return
        if not any(f.filled for _, f in result.fills):
            self.missed[opp.strategy] += 1   # opportunité disparue avant l'arrivée des ordres : sans coût
            self.risk.close(opp, 0.0, True)
            return
        self.trades_by_strategy[opp.strategy] += 1
        if opp.strategy == "funding" and result.ok:
            # la position reste ouverte : on ne libère pas les carnets, on encaisse les frais d'entrée
            spot = next(f for l, f in result.fills if l.side == "buy")
            perp = next(f for l, f in result.fills if l.side == "sell")
            self.funding_positions[(opp.legs[0].exchange, opp.legs[0].symbol)] = {
                "opp": opp, "qty": spot.filled, "price": spot.avg_price,
                "entry_spot": spot.notional, "entry_perp": perp.notional, "last": time.time()}
            self.risk.park(opp)
            # à l'entrée seuls les frais sont une perte : le spot acheté reste un actif
            self._book_pnl("funding", -sum(f.fee_quote for _, f in result.fills))
            log.info("Entrée cash-and-carry %s annualisé=%.1f%%", opp.legs[0].symbol,
                     opp.meta["annualized"] * 100)
            return
        self.pnl_by_strategy[opp.strategy] += result.pnl
        self.risk.close(opp, result.pnl, result.ok)
        log.info("%-14s edge=%.3f%% attendu=%.2f réalisé=%.2f %s", opp.strategy, opp.edge * 100,
                 opp.expected_profit, result.pnl, "OK" if result.ok else "ÉCHEC")

    def report(self) -> None:
        eq, start = self.risk.equity, self.cfg.risk.starting_equity
        detail = " | ".join(f"{s}: {self.trades_by_strategy[s]} trades ({self.missed[s]} ratés), "
                            f"{self.pnl_by_strategy[s]:+.2f}"
                            for s in ("cross_exchange", "triangular", "funding"))
        log.info("CAPITAL %.2f (%+.3f%%) | %s%s", eq, (eq / start - 1) * 100, detail,
                 f" | HALT: {self.risk.halted_reason}" if self.risk.halted_reason else "")

    # ------------------------------------------------------------------ main loop
    async def run(self, duration: float | None = None) -> None:
        await self.setup()
        for name, subs in self.subscriptions.items():
            for sym in subs:
                self.tasks.add(asyncio.create_task(self._watch(self.venues[name], sym)))
        if self.mode == "demo":
            SyntheticVenue.step_reference(self.rng)
        await asyncio.sleep(max(1.0, self.cfg.engine.tick_interval * 2))
        if self.mode in ("paper", "demo"):
            await self._seed_paper_wallets()
        self.risk.day_start_equity = self.risk.equity

        deadline = time.monotonic() + duration if duration else None
        last_funding = last_rebalance = last_report = 0.0
        try:
            while not self.stop.is_set() and (deadline is None or time.monotonic() < deadline):
                tick = time.monotonic()
                if self.mode == "demo":
                    SyntheticVenue.step_reference(self.rng)
                await self._refresh_balances()
                if tick - last_funding > FUNDING_REFRESH and self.cfg.funding.enabled:
                    await self._refresh_funding()
                    last_funding = tick
                for opp in self.risk.select(self.scan()):
                    self.risk.open(opp)
                    t = asyncio.create_task(self._run(opp))
                    self.tasks.add(t)
                    t.add_done_callback(self.tasks.discard)
                if tick - last_rebalance > REBALANCE_EVERY:
                    self._rebalance()
                    last_rebalance = tick
                if tick - last_report > REPORT_EVERY:
                    self.report()
                    last_report = tick
                await asyncio.sleep(max(0.0, self.cfg.engine.tick_interval - (time.monotonic() - tick)))
        finally:
            await self.shutdown()

    def _rebalance(self) -> None:
        assets = {s.split(":")[0].split("/")[0] for s in self.cfg.cross_exchange.symbols} | {"USDT"}
        costs = rebalance(self.venues, assets, live=self.mode == "live")
        prices = self._prices()
        cost = sum(q * prices.get(a, 0.0) for a, q in costs.items())
        if cost:
            self.pnl_by_strategy["cross_exchange"] -= cost
            self.risk.equity -= cost

    async def shutdown(self) -> None:
        self.stop.set()
        for t in list(self.tasks):
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await asyncio.gather(*(v.close() for v in self.venues.values()), return_exceptions=True)
        self.report()
