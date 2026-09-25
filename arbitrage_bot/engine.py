"""Moteur principal : flux de carnets, détection, sélection, exécution, suivi du PnL."""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import Counter, defaultdict
from datetime import date

from .config import BotConfig
from .exchanges import CcxtVenue, SyntheticVenue, Venue
from .execution import Executor, Result
from .notifier import TelegramNotifier, fmt_num, fmt_signed, format_recap, format_trade
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
        self.funding_pending: set[tuple[str, str]] = set()   # entrées en cours d'exécution
        self.balances: dict[str, dict[str, float]] = {}
        self.pnl_by_strategy: Counter = Counter()
        self.trades_by_strategy: Counter = Counter()
        self.missed: Counter = Counter()
        self.notifier = TelegramNotifier(cfg.telegram)
        self.trades_today, self.trades_day = 0, date.today()
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

    async def _wait_for_books(self, timeout: float = 45.0, target: float = 0.9) -> None:
        """Attend que la plupart des carnets aient reçu une première mise à jour
        (la connexion aux plateformes prend plusieurs secondes)."""
        total = sum(len(s) for s in self.subscriptions.values()) or 1
        start = time.monotonic()
        while True:
            ready = sum(1 for n, subs in self.subscriptions.items() for sym in subs if sym in self.venues[n].books)
            if ready / total >= target or time.monotonic() - start > timeout:
                log.info("Carnets reçus : %d/%d (%.0f s)", ready, total, time.monotonic() - start)
                return
            await asyncio.sleep(0.5)

    async def _seed_paper_wallets(self) -> None:
        """Répartit le capital de départ : 50 % en stable, 50 % en actifs de base, à parts égales."""
        prices = self._prices()
        per_venue = self.cfg.risk.starting_equity / len(self.venues)
        for name, venue in self.venues.items():
            assets = {s.split(":")[0].split("/")[0] for s in self.subscriptions[name]} - STABLES
            assets = {a for a in assets if a in prices}
            venue.wallet.balances["USDT"] = per_venue / 2 if assets else per_venue
            for a in assets:
                venue.wallet.balances[a] = per_venue / 2 / len(assets) / prices[a]
            log.info("%s : portefeuille simulé de %.0f USDT (%d actifs)", name, per_venue, len(assets))

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
            accrued = rate * pos["perp_qty"] * pos["price"] * (now - pos["last"]) / (8 * 3600)
            pos["last"] = now
            self._book_pnl("funding", accrued)
            if funding.should_exit(rate, self.cfg):
                log.info("Sortie cash-and-carry %s %s (funding %.4f%%)", ex, spot, rate * 100)
                ex_ = Executor(self.cfg, self.venues)
                opp = pos["opp"]
                closing = tuple(
                    Leg(l.exchange, l.symbol, "sell" if l.side == "buy" else "buy",
                        pos["spot_qty"] if l.side == "buy" else pos["perp_qty"], 0.0, l.market_type)
                    for l in opp.legs)
                fills = [await ex_._aggressive(l.exchange, l.symbol, l.side, l.amount, l.market_type)
                         for l in closing if l.amount > 0]
                closing = tuple(l for l in closing if l.amount > 0)
                exit_pnl = Executor._quote_pnl(list(zip(closing, fills)))
                realized = exit_pnl + pos["entry_perp"] - pos["entry_spot"]
                self._book_pnl("funding", realized)
                del self.funding_positions[key]
                self.notifier.notify(
                    f"🔚 <b>Sortie cash-and-carry</b> — {spot} sur {ex}\n"
                    f"Funding retombé à {funding.annualize(rate) * 100:.1f} %/an\n"
                    f"Résultat de la sortie : <b>{fmt_signed(realized)} USDT</b>\n"
                    f"━━━━━━━━━━\n{format_recap(self.recap())}")

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
            + funding.scan(self.cfg, books, self.funding_rates,
                           set(self.funding_positions) | self.funding_pending, budget)
        )

    async def _run(self, opp: Opportunity) -> None:
        try:
            await self._execute(opp)
        finally:
            self.funding_pending.discard((opp.legs[0].exchange, opp.legs[0].symbol))

    async def _execute(self, opp: Opportunity) -> None:
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
        if date.today() != self.trades_day:
            self.trades_today, self.trades_day = 0, date.today()
        self.trades_today += 1
        if opp.strategy == "funding":
            # la position reste ouverte (y compris quand une jambe a été complétée en urgence) :
            # on la suit jusqu'à la sortie ; à l'entrée seuls les frais sont une perte
            buys = [f for l, f in result.fills if l.side == "buy"]
            sells = [f for l, f in result.fills if l.side == "sell"]
            self.funding_positions[(opp.legs[0].exchange, opp.legs[0].symbol)] = {
                "opp": opp, "spot_qty": sum(f.filled for f in buys), "perp_qty": sum(f.filled for f in sells),
                "price": opp.legs[0].price, "entry_spot": sum(f.notional for f in buys),
                "entry_perp": sum(f.notional for f in sells), "last": time.time()}
            self.risk.park(opp)
            fees = -sum(f.fee_quote for _, f in result.fills)
            self._book_pnl("funding", fees)
            log.info("Entrée cash-and-carry %s annualisé=%.1f%%", opp.legs[0].symbol,
                     opp.meta["annualized"] * 100)
            if self.notifier.should_notify_trade(fees, result.ok):
                self.notifier.notify(format_trade(
                    opp.strategy, result.fills, opp.expected_profit, fees, opp.edge, opp.notional, result.ok,
                    self.recap(), entry=True,
                    extra=f"📌 Position ouverte · rendement visé {opp.meta['annualized'] * 100:.1f} %/an "
                          f"(le funding est versé toutes les 8 h)"))
            return
        self.pnl_by_strategy[opp.strategy] += result.pnl
        halted_before = self.risk.halted_reason
        self.risk.close(opp, result.pnl, result.ok)
        if self.notifier.should_notify_trade(result.pnl, result.ok):
            self.notifier.notify(format_trade(opp.strategy, result.fills, opp.expected_profit, result.pnl,
                                              opp.edge, opp.notional, result.ok, self.recap()))
        if self.risk.halted_reason and self.risk.halted_reason != halted_before:
            self._notify_halt()
        log.info("%-14s edge=%.3f%% attendu=%.2f réalisé=%.2f %s", opp.strategy, opp.edge * 100,
                 opp.expected_profit, result.pnl, "OK" if result.ok else "ÉCHEC")

    def recap(self) -> dict:
        eq = self.risk.equity
        return {
            "equity": eq,
            "return_pct": (eq / self.cfg.risk.starting_equity - 1) * 100,
            "daily_pnl": self.risk.daily_pnl,
            "trades_today": self.trades_today,
            "pnl_by_strategy": {s: self.pnl_by_strategy[s] for s in ("cross_exchange", "triangular", "funding")},
        }

    def _notify_halt(self) -> None:
        if self.risk.halted_reason == "daily_loss":
            text = (f"🛑 <b>KILL SWITCH</b> : perte du jour {fmt_signed(self.risk.daily_pnl)} USDT.\n"
                    f"Trading suspendu jusqu'à demain.")
        else:
            text = (f"⏸ <b>Disjoncteur</b> : {self.risk.consecutive_failures} échecs d'exécution consécutifs.\n"
                    f"Pause de {self.cfg.risk.failure_cooldown:.0f} s.")
        self.notifier.notify(f"{text}\n━━━━━━━━━━\n{format_recap(self.recap())}")

    def _best_edges(self) -> str:
        """Meilleurs écarts nets visibles sur le marché, même sous le seuil de déclenchement."""
        books = self._fresh_books()
        fees = {e.name: e.taker_fee for e in self.cfg.enabled_exchanges}
        best, where = -1.0, ""
        for symbol in self.cfg.cross_exchange.symbols:
            venues = [ex for ex in self.venues if (ex, symbol) in books]
            for a in venues:
                for b in venues:
                    asks, bids = books[(a, symbol)]["asks"], books[(b, symbol)]["bids"]
                    if a == b or not asks or not bids:
                        continue
                    edge = bids[0][0] * (1 - fees[b]) / (asks[0][0] * (1 + fees[a])) - 1
                    if edge > best:
                        best, where = edge, f"{symbol} {a}→{b}"
        parts = []
        if where:
            need = self.cfg.cross_exchange.min_edge + self.cfg.cross_exchange.latency_buffer
            parts.append(f"meilleur écart inter {best * 100:+.3f}% ({where}, seuil {need * 100:.3f}%)")
        if self.funding_rates:
            (ex, sym), rate = max(self.funding_rates.items(), key=lambda kv: kv[1])
            parts.append(f"meilleur funding {funding.annualize(rate) * 100:.1f}%/an ({sym.split(':')[0]} {ex}, "
                         f"seuil {self.cfg.funding.min_annualized * 100:.0f}%)")
        return " | ".join(parts)

    def report(self) -> None:
        eq, start = self.risk.equity, self.cfg.risk.starting_equity
        detail = " | ".join(f"{s}: {self.trades_by_strategy[s]} trades ({self.missed[s]} ratés), "
                            f"{self.pnl_by_strategy[s]:+.2f}"
                            for s in ("cross_exchange", "triangular", "funding"))
        log.info("CAPITAL %.2f (%+.3f%%) | %s%s", eq, (eq / start - 1) * 100, detail,
                 f" | HALT: {self.risk.halted_reason}" if self.risk.halted_reason else "")
        diag = self._best_edges()
        if diag:
            log.info("MARCHÉ  %s", diag)

    # ------------------------------------------------------------------ main loop
    async def run(self, duration: float | None = None) -> None:
        await self.setup()
        for name, subs in self.subscriptions.items():
            for sym in subs:
                self.tasks.add(asyncio.create_task(self._watch(self.venues[name], sym)))
        if self.mode == "demo":
            SyntheticVenue.step_reference(self.rng)
        await self._wait_for_books()
        if self.mode in ("paper", "demo"):
            await self._seed_paper_wallets()
        self.risk.day_start_equity = self.risk.equity
        self.notifier.start()
        strategies = [n for n, c in (("inter-plateformes", self.cfg.cross_exchange), ("triangulaire", self.cfg.triangular),
                                     ("funding", self.cfg.funding)) if c.enabled]
        self.notifier.notify(f"🚀 <b>Robot démarré</b> (mode {self.mode})\n"
                             f"Plateformes : {', '.join(self.venues)}\n"
                             f"Stratégies : {', '.join(strategies)}\n"
                             f"Capital : {fmt_num(self.risk.equity)} USDT")

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
                    if opp.strategy == "funding":
                        self.funding_pending.add((opp.legs[0].exchange, opp.legs[0].symbol))
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
        self.notifier.notify(f"⏹ <b>Robot arrêté</b>\n{format_recap(self.recap())}")
        await self.notifier.close()
