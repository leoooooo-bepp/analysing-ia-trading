"""Notifications Telegram : un message par trade exécuté + récapitulatif du compte.

Les messages passent par une file d'attente envoyée en tâche de fond : l'exécution des
ordres n'attend jamais Telegram. Telegram limite à ~1 message/s par conversation ; quand
plusieurs trades tombent en même temps, ils sont regroupés dans un même message.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.error
import urllib.request
from html import escape
from typing import Callable

from .config import TelegramConfig

log = logging.getLogger(__name__)

API_URL = "https://api.telegram.org/bot{token}/sendMessage"
MAX_LEN = 4000           # limite Telegram : 4096 caractères par message
MAX_QUEUE = 200          # au-delà, les plus anciens messages sont abandonnés
SEPARATOR = "\n\n· · · · · · · · · ·\n\n"

STRATEGY_LABELS = {
    "cross_exchange": "Arbitrage inter-plateformes",
    "triangular": "Arbitrage triangulaire",
    "funding": "Cash-and-carry funding",
}
SHORT_LABELS = {"cross_exchange": "inter", "triangular": "triangulaire", "funding": "funding"}


def fmt_num(x: float, decimals: int = 2) -> str:
    """12345.678 -> '12 345.68' (espace comme séparateur de milliers)."""
    return f"{x:,.{decimals}f}".replace(",", " ")


def fmt_price(x: float) -> str:
    return fmt_num(x, 2 if x >= 100 else 4 if x >= 1 else 8)


def fmt_signed(x: float) -> str:
    return ("+" if x >= 0 else "−") + fmt_num(abs(x))


def fmt_pct(x: float) -> str:
    return ("+" if x >= 0 else "−") + f"{abs(x):.2f} %"


def format_recap(recap: dict) -> str:
    n = recap["trades_today"]
    per_strategy = " · ".join(f"{SHORT_LABELS[s]} {fmt_signed(p)}" for s, p in recap["pnl_by_strategy"].items())
    return (
        f"📊 Capital <b>{fmt_num(recap['equity'])} USDT</b> ({fmt_pct(recap['return_pct'])})\n"
        f"Aujourd'hui : {fmt_signed(recap['daily_pnl'])} USDT · {n} trade{'s' if n > 1 else ''}\n"
        f"Par stratégie : {per_strategy}"
    )


def format_trade(strategy: str, fills: list[tuple], expected: float, pnl: float, edge: float,
                 notional: float, ok: bool, recap: dict, extra: str = "", entry: bool = False) -> str:
    """`fills` : liste de (Leg, Fill). Seules les jambes réellement remplies sont affichées."""
    symbols = sorted({leg.symbol.split(":")[0] for leg, _ in fills})
    lines = [f"{'✅' if ok else '⚠️'} <b>{STRATEGY_LABELS.get(strategy, strategy)}</b> — {escape(', '.join(symbols))}"]
    for leg, f in fills:
        if f.filled <= 0:
            continue
        side = "🟢 Achat" if leg.side == "buy" else "🔴 Vente"
        kind = " (perp)" if leg.market_type == "swap" else ""
        lines.append(f"{side} {escape(leg.exchange)} {fmt_num(f.filled, 6)} {escape(leg.symbol.split(':')[0])}"
                     f"{kind} @ {fmt_price(f.avg_price)}")
    lines.append(f"Engagé : {fmt_num(notional)} USDT · Écart : {edge * 100:.3f} %")
    if entry:  # ouverture de position : seul le coût d'entrée est réalisé
        lines.append(f"Frais d'entrée : <b>{fmt_signed(pnl)} USDT</b> · gain attendu sur la durée {fmt_signed(expected)}")
    else:
        lines.append(f"Profit : <b>{fmt_signed(pnl)} USDT</b> (attendu {fmt_signed(expected)})")
    if not ok:
        lines.append("⚠️ Une jambe a dû être couverte en urgence")
    if extra:
        lines.append(extra)
    return "\n".join(lines) + "\n━━━━━━━━━━\n" + format_recap(recap)


def _http_send(token: str, chat_id: str, text: str) -> None:
    body = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                       "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(API_URL.format(token=token), data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()


class TelegramNotifier:
    def __init__(self, cfg: TelegramConfig, sender: Callable[[str], None] | None = None):
        self.cfg = cfg
        token, chat_id = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
        if sender is None and cfg.enabled and not (token and chat_id):
            log.warning("Telegram activé mais TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID absents du .env : désactivé")
        self.active = cfg.enabled and (sender is not None or bool(token and chat_id))
        self._send = sender or (lambda text: _http_send(token, chat_id, text))
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.dropped = 0
        self._worker: asyncio.Task | None = None

    # ------------------------------------------------------------------ API publique
    def notify(self, text: str) -> None:
        if not self.active:
            return
        if self.queue.qsize() >= MAX_QUEUE:
            self.queue.get_nowait()
            self.dropped += 1
        self.queue.put_nowait(text)

    def should_notify_trade(self, pnl: float, ok: bool) -> bool:
        return self.active and self.cfg.notify_trades and (not ok or abs(pnl) >= self.cfg.min_abs_pnl)

    def start(self) -> None:
        if self.active and self._worker is None:
            self._worker = asyncio.create_task(self._run())

    async def close(self, timeout: float = 10.0) -> None:
        """Vide la file (message d'arrêt compris) avant de rendre la main."""
        if self._worker is None:
            return
        try:
            await asyncio.wait_for(self.queue.join(), timeout)
        except asyncio.TimeoutError:
            log.warning("Telegram : %d messages non envoyés à l'arrêt", self.queue.qsize())
        self._worker.cancel()
        await asyncio.gather(self._worker, return_exceptions=True)

    # ------------------------------------------------------------------ envoi
    def _next_batch(self, first: str) -> tuple[str, int]:
        """Regroupe les messages en attente dans un seul envoi, sans dépasser la limite Telegram."""
        parts, taken = [first], 1
        while not self.queue.empty():
            nxt = self.queue._queue[0]  # type: ignore[attr-defined]  # aperçu sans retirer
            if sum(map(len, parts)) + len(SEPARATOR) * len(parts) + len(nxt) > MAX_LEN:
                break
            parts.append(self.queue.get_nowait())
            taken += 1
        if self.dropped:
            parts.append(f"<i>({self.dropped} messages omis : trop de trades à la fois)</i>")
            self.dropped = 0
        return SEPARATOR.join(parts), taken

    async def _run(self) -> None:
        while True:
            first = await self.queue.get()
            text, taken = self._next_batch(first)
            for attempt in range(3):
                try:
                    await asyncio.to_thread(self._send, text)
                    break
                except urllib.error.HTTPError as exc:
                    wait = 5.0
                    if exc.code == 429:  # trop de messages : Telegram indique combien attendre
                        try:
                            wait = float(json.loads(exc.read())["parameters"]["retry_after"])
                        except Exception:
                            pass
                    log.warning("Telegram HTTP %s, nouvel essai dans %.0f s", exc.code, wait)
                    await asyncio.sleep(wait)
                except Exception as exc:
                    log.warning("Telegram injoignable (%s), nouvel essai", exc)
                    await asyncio.sleep(2.0 * (attempt + 1))
            for _ in range(taken):
                self.queue.task_done()
            await asyncio.sleep(self.cfg.send_interval)
