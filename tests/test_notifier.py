import asyncio

from arbitrage_bot.config import TelegramConfig
from arbitrage_bot.exchanges import Fill
from arbitrage_bot.notifier import MAX_LEN, TelegramNotifier, format_trade
from arbitrage_bot.strategies import Leg

RECAP = {"equity": 10_045.2, "return_pct": 0.452, "daily_pnl": 45.2, "trades_today": 37,
         "pnl_by_strategy": {"cross_exchange": 12.3, "triangular": 30.1, "funding": 2.8}}


def test_format_trade_contains_legs_profit_and_recap():
    fills = [(Leg("binance", "BTC/USDT", "buy", 0.01, 65_000), Fill(0.0123, 65_010.5, 0.6)),
             (Leg("okx", "BTC/USDT", "sell", 0.01, 65_100), Fill(0.0123, 65_120.1, 0.5)),
             (Leg("okx", "BTC/USDT", "sell", 0.01, 65_100), Fill(0.0, 0.0, 0.0))]  # jambe vide : masquée
    text = format_trade("cross_exchange", fills, expected=1.10, pnl=1.02, edge=0.00168,
                        notional=800.12, ok=True, recap=RECAP)
    assert "Arbitrage inter-plateformes" in text and "BTC/USDT" in text
    assert "🟢 Achat binance 0.012300 BTC/USDT @ 65 010.50" in text
    assert "🔴 Vente okx" in text
    assert text.count("Vente") == 1
    assert "+1.02 USDT" in text and "attendu +1.10" in text
    assert "10 045.20 USDT" in text and "37 trades" in text
    assert "couverte en urgence" not in text


def test_format_trade_flags_failure_and_perp():
    fills = [(Leg("bybit", "SOL/USDT:USDT", "sell", 1, 150, "swap"), Fill(1, 150, 0.1))]
    text = format_trade("funding", fills, 1.0, -0.4, 0.001, 150, ok=False, recap=RECAP)
    assert text.startswith("⚠️") and "(perp)" in text and "−0.40 USDT" in text
    assert "couverte en urgence" in text


def test_disabled_notifier_is_noop(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    n = TelegramNotifier(TelegramConfig(enabled=True))  # pas de jeton -> désactivé
    n.notify("x")
    assert not n.active and n.queue.empty()


def test_min_abs_pnl_filter_but_failures_always_sent():
    n = TelegramNotifier(TelegramConfig(enabled=True, min_abs_pnl=1.0), sender=lambda t: None)
    assert not n.should_notify_trade(0.3, ok=True)
    assert n.should_notify_trade(-2.0, ok=True)
    assert n.should_notify_trade(0.0, ok=False)


def test_burst_is_batched_and_flushed_on_close():
    sent: list[str] = []

    async def scenario():
        n = TelegramNotifier(TelegramConfig(enabled=True, send_interval=0), sender=sent.append)
        n.start()
        for i in range(50):
            n.notify(f"trade {i} " + "x" * 200)
        await n.close()

    asyncio.run(scenario())
    joined = "".join(sent)
    assert all(f"trade {i} " in joined for i in range(50))
    assert len(sent) < 50                       # regroupés
    assert all(len(m) <= MAX_LEN for m in sent)  # jamais au-delà de la limite Telegram
