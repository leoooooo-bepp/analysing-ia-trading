"""Chargement de la configuration YAML en dataclasses typées."""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml


@dataclass
class ExchangeConfig:
    name: str
    taker_fee: float = 0.001
    maker_fee: float = 0.001
    enabled: bool = True
    options: dict = field(default_factory=dict)


@dataclass
class CrossExchangeConfig:
    enabled: bool = True
    symbols: list[str] = field(default_factory=list)
    min_edge: float = 0.0008          # marge nette minimale après frais (0.08 %)
    latency_buffer: float = 0.0002    # coussin pour le glissement entre détection et exécution


@dataclass
class TriangularConfig:
    enabled: bool = True
    exchanges: list[str] = field(default_factory=list)
    base_asset: str = "USDT"
    universe: list[str] = field(default_factory=list)   # actifs autorisés (vide = tous)
    concurrent: bool = True           # 3 jambes simultanées sur inventaire (sinon séquentiel)
    min_edge: float = 0.0006
    max_cycles: int = 400


@dataclass
class FundingConfig:
    enabled: bool = True
    exchanges: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    min_annualized: float = 0.25      # 25 %/an minimum pour entrer
    exit_annualized: float = 0.05     # on sort si le carry retombe sous 5 %/an
    leverage: float = 3.0             # levier sur la jambe perp
    max_positions: int = 3            # positions de carry ouvertes simultanément
    expected_hold_periods: int = 9    # périodes de funding (8 h) pour amortir les frais


@dataclass
class RiskConfig:
    starting_equity: float = 10_000.0
    trade_fraction: float = 0.30          # fraction du capital engagée par trade
    max_trade_notional: float = 50_000.0
    min_trade_notional: float = 20.0
    max_concurrent_trades: int = 6
    daily_loss_limit: float = 0.05        # kill switch : -5 % sur la journée
    max_consecutive_failures: int = 5
    failure_cooldown: float = 60.0        # pause (s) après déclenchement du disjoncteur
    compounding: bool = True              # la taille suit le capital courant
    max_leg_imbalance: float = 0.02       # écart de remplissage toléré entre jambes


@dataclass
class EngineConfig:
    mode: str = "paper"                   # paper | live | demo
    tick_interval: float = 0.25
    use_websocket: bool = True
    orderbook_depth: int = 20
    paper_slippage: float = 0.0002
    log_level: str = "INFO"


@dataclass
class BotConfig:
    exchanges: list[ExchangeConfig]
    cross_exchange: CrossExchangeConfig
    triangular: TriangularConfig
    funding: FundingConfig
    risk: RiskConfig
    engine: EngineConfig

    def exchange(self, name: str) -> ExchangeConfig:
        return next(e for e in self.exchanges if e.name == name)

    @property
    def enabled_exchanges(self) -> list[ExchangeConfig]:
        return [e for e in self.exchanges if e.enabled]


def _build(cls, data: dict | None):
    data = data or {}
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"Clés inconnues pour {cls.__name__}: {sorted(unknown)}")
    return cls(**data)


def load_config(path: str | Path) -> BotConfig:
    raw = yaml.safe_load(Path(path).read_text())
    return BotConfig(
        exchanges=[_build(ExchangeConfig, e) for e in raw.get("exchanges", [])],
        cross_exchange=_build(CrossExchangeConfig, raw.get("cross_exchange")),
        triangular=_build(TriangularConfig, raw.get("triangular")),
        funding=_build(FundingConfig, raw.get("funding")),
        risk=_build(RiskConfig, raw.get("risk")),
        engine=_build(EngineConfig, raw.get("engine")),
    )
