"""Point d'entrée : python -m arbitrage_bot --config config/aggressive.yaml --mode demo"""
from __future__ import annotations

import argparse
import asyncio
import logging

from .config import load_config
from .engine import Engine


def main() -> None:
    p = argparse.ArgumentParser(description="Robot d'arbitrage crypto agressif")
    p.add_argument("--config", default="config/aggressive.yaml")
    p.add_argument("--mode", choices=["demo", "paper", "live"], help="écrase engine.mode du YAML")
    p.add_argument("--duration", type=float, help="durée d'exécution en secondes (défaut : infini)")
    args = p.parse_args()

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
