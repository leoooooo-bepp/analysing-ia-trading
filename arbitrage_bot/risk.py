"""Gestion du risque : taille des trades, kill switch journalier, disjoncteur sur échecs."""
from __future__ import annotations

import logging
import time
from datetime import date

from .config import RiskConfig
from .strategies import Opportunity

log = logging.getLogger(__name__)


class RiskManager:
    def __init__(self, cfg: RiskConfig):
        self.cfg = cfg
        self.equity = cfg.starting_equity
        self.day = date.today()
        self.day_start_equity = self.equity
        self.consecutive_failures = 0
        self.busy: set[tuple[str, str]] = set()   # carnets (exchange, symbole) occupés
        self.open_trades = 0
        self.halted_reason: str | None = None
        self.halted_until = 0.0

    # --- sizing -----------------------------------------------------------------
    def trade_budget(self) -> float:
        base = self.equity if self.cfg.compounding else self.cfg.starting_equity
        return min(base * self.cfg.trade_fraction, self.cfg.max_trade_notional)

    # --- gating -----------------------------------------------------------------
    def _roll_day(self) -> None:
        if date.today() != self.day:
            self.day, self.day_start_equity = date.today(), self.equity
            if self.halted_reason == "daily_loss":
                self.halted_reason = None
        if self.halted_reason == "failures" and time.monotonic() >= self.halted_until:
            self.halted_reason, self.consecutive_failures = None, 0

    @property
    def daily_pnl(self) -> float:
        return self.equity - self.day_start_equity

    def can_trade(self, opp: Opportunity) -> bool:
        self._roll_day()
        if self.halted_reason:
            return False
        if self.open_trades >= self.cfg.max_concurrent_trades:
            return False
        if opp.notional < self.cfg.min_trade_notional:
            return False
        return not any(k in self.busy for k in opp.key)

    def select(self, opps: list[Opportunity]) -> list[Opportunity]:
        """Garde les meilleures opportunités sans conflit de carnet, par profit décroissant."""
        chosen, used = [], set()
        for opp in sorted(opps, key=lambda o: o.expected_profit, reverse=True):
            if self.open_trades + len(chosen) >= self.cfg.max_concurrent_trades:
                break
            if not self.can_trade(opp) or any(k in used for k in opp.key):
                continue
            chosen.append(opp)
            used.update(opp.key)
        return chosen

    # --- bookkeeping ------------------------------------------------------------
    def open(self, opp: Opportunity) -> None:
        self.busy.update(opp.key)
        self.open_trades += 1

    def park(self, opp: Opportunity) -> None:
        """Position longue durée (cash-and-carry) : l'exécution est finie, on libère le slot et les carnets."""
        self.busy.difference_update(opp.key)
        self.open_trades -= 1

    def close(self, opp: Opportunity, pnl: float, ok: bool) -> None:
        self.busy.difference_update(opp.key)
        self.open_trades -= 1
        self.equity += pnl
        self.consecutive_failures = 0 if ok else self.consecutive_failures + 1
        if self.daily_pnl <= -self.cfg.daily_loss_limit * self.day_start_equity:
            self.halted_reason = "daily_loss"
            log.error("KILL SWITCH : perte journalière %.2f atteinte, trading suspendu", self.daily_pnl)
        elif self.consecutive_failures >= self.cfg.max_consecutive_failures:
            self.halted_reason = "failures"
            self.halted_until = time.monotonic() + self.cfg.failure_cooldown
            log.error("DISJONCTEUR : %d échecs consécutifs, pause de %.0f s",
                      self.consecutive_failures, self.cfg.failure_cooldown)
