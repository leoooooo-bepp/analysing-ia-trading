"""Point d'entrée : python -m arbitrage_bot --config config/aggressive.yaml --mode demo"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path

from .config import load_config
from .engine import Engine
from .notifier import _http_send


def load_dotenv(path: str = ".env") -> None:
    """Charge les lignes CLE=valeur du fichier .env sans écraser l'environnement existant."""
    env = Path(path)
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            if value.strip():
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def test_telegram() -> int:
    token, chat_id = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        print("TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID manquant dans le fichier .env")
        return 1
    try:
        _http_send(token, chat_id, "✅ <b>Test réussi</b> : le robot d'arbitrage peut t'envoyer des messages.")
    except Exception as exc:  # jeton invalide (401), chat_id inconnu (400), réseau…
        detail = exc.read().decode(errors="replace") if hasattr(exc, "read") else ""
        print(f"Échec de l'envoi : {exc} {detail}")
        return 1
    print("Message de test envoyé : vérifie ta conversation Telegram.")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Robot d'arbitrage crypto agressif")
    p.add_argument("--config", default="config/aggressive.yaml")
    p.add_argument("--mode", choices=["demo", "paper", "live"], help="écrase engine.mode du YAML")
    p.add_argument("--duration", type=float, help="durée d'exécution en secondes (défaut : infini)")
    p.add_argument("--test-telegram", action="store_true", help="envoie un message de test sur Telegram et quitte")
    args = p.parse_args()
    load_dotenv()
    if args.test_telegram:
        raise SystemExit(test_telegram())

    cfg = load_config(args.config)
    if args.mode:
        cfg.engine.mode = args.mode
    logging.basicConfig(level=cfg.engine.log_level, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")
    if cfg.engine.mode == "live":
        logging.warning("MODE LIVE : ordres réels avec les clés API du fichier .env")
    try:
        asyncio.run(Engine(cfg).run(args.duration))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
