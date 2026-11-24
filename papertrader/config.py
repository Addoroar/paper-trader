"""Configuration loading and validation.

Everything the bot does is driven by ``config.yaml``. The file is parsed with
PyYAML and validated with pydantic; any problem is reported as a
:class:`ConfigError` that lists every offending field with a readable message.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from papertrader.models import TIMEFRAME_SECONDS


class ConfigError(Exception):
    """Raised when the configuration file is missing or invalid."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UniverseConfig(_Strict):
    """The pool of symbols the bot may choose from."""

    stocks: list[str] = Field(default_factory=list)
    crypto: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)

    @field_validator("crypto")
    @classmethod
    def _crypto_pairs(cls, v: list[str]) -> list[str]:
        for s in v:
            if "/" not in s:
                raise ValueError(f"crypto symbol {s!r} must look like BASE/QUOTE, e.g. BTC/USDT")
        return v

    @field_validator("stocks")
    @classmethod
    def _stock_symbols(cls, v: list[str]) -> list[str]:
        for s in v:
            if "/" in s:
                raise ValueError(f"stock symbol {s!r} must not contain '/'; put crypto pairs under 'crypto'")
        return v

    def symbols(self) -> list[str]:
        """All tradable symbols after applying ``exclude`` (stable order, no duplicates)."""
        out: list[str] = []
        for s in [*self.stocks, *self.crypto]:
            if s not in self.exclude and s not in out:
                out.append(s)
        return out


class SelectionConfig(_Strict):
    """How each account picks symbols from the universe."""

    method: str = "momentum_rank"
    lookback_bars: int = Field(50, ge=2)
    top_n: int = Field(3, ge=1)
    rebalance_every: int = Field(24, ge=1)
    min_avg_volume: float = Field(100_000, ge=0)
    seed: int = 42
    shadow_random_baseline: bool = True


class RiskConfig(_Strict):
    """Risk limits. A value of 0 disables stop-loss / take-profit / daily cap."""

    max_position_pct: float = Field(25, gt=0, le=100)
    stop_loss_pct: float = Field(5, ge=0, lt=100)
    take_profit_pct: float = Field(10, ge=0)
    max_open_positions: int = Field(5, ge=1)
    max_daily_loss_pct: float = Field(3, ge=0, lt=100)


class RiskOverride(_Strict):
    """Per-account partial override of :class:`RiskConfig`."""

    max_position_pct: float | None = Field(None, gt=0, le=100)
    stop_loss_pct: float | None = Field(None, ge=0, lt=100)
    take_profit_pct: float | None = Field(None, ge=0)
    max_open_positions: int | None = Field(None, ge=1)
    max_daily_loss_pct: float | None = Field(None, ge=0, lt=100)


class AccountConfig(_Strict):
    """One isolated virtual account running one strategy."""

    name: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_\-]+$")
    strategy: str
    params: dict[str, Any] = Field(default_factory=dict)
    allocation: float = Field(gt=0)
    risk: RiskOverride | None = None


class LoggingConfig(_Strict):
    """Where and how much to log."""

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    dir: str = "./logs"
    max_bytes: int = Field(10_000_000, ge=10_000)
    backup_count: int = Field(5, ge=1)


class DataConfig(_Strict):
    """Market data source settings."""

    feed: Literal["auto", "synthetic"] = "auto"
    crypto_exchange: str = "binance"
    crypto_exchange_fallbacks: list[str] = Field(default_factory=lambda: ["kraken", "coinbase"])
    cache_dir: str = "./cache"
    history_bars: int = Field(250, ge=20)
    max_retries: int = Field(4, ge=0)
    retry_base_delay: float = Field(1.0, ge=0)


class StorageConfig(_Strict):
    """SQLite database locations (live and backtest are kept separate)."""

    db_path: str = "./state/papertrader.db"
    backtest_db_path: str = "./state/backtest.db"


class Config(_Strict):
    """Top-level configuration."""

    starting_cash: float = Field(gt=0)
    currency: str = "USD"
    timeframe: Literal["1m", "5m", "15m", "1h", "1d"] = "1h"
    poll_interval_seconds: float = Field(60, gt=0)
    heartbeat_stale_seconds: float = Field(300, gt=0)
    universe: UniverseConfig
    selection: SelectionConfig = Field(default_factory=SelectionConfig)
    accounts: list[AccountConfig] = Field(min_length=1)
    fees_pct: float = Field(0.1, ge=0, lt=100)
    slippage_pct: float = Field(0.05, ge=0, lt=100)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    data: DataConfig = Field(default_factory=DataConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)

    @model_validator(mode="after")
    def _cross_checks(self) -> "Config":
        from papertrader.selection import SELECTORS
        from papertrader.strategies import STRATEGIES

        if not self.universe.symbols():
            raise ValueError("universe is empty (after 'exclude'); list at least one stock or crypto symbol")
        names = [a.name for a in self.accounts]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"account names must be unique; duplicated: {sorted(dupes)}")
        total = sum(a.allocation for a in self.accounts)
        if total > self.starting_cash + 1e-9:
            raise ValueError(
                f"sum of account allocations ({total:g}) exceeds starting_cash ({self.starting_cash:g})"
            )
        if self.selection.method not in SELECTORS:
            raise ValueError(
                f"selection.method {self.selection.method!r} is unknown; available: {', '.join(SELECTORS.names())}"
            )
        for a in self.accounts:
            if a.strategy not in STRATEGIES:
                raise ValueError(
                    f"account {a.name!r}: strategy {a.strategy!r} is unknown; "
                    f"available: {', '.join(STRATEGIES.names())}"
                )
            try:
                STRATEGIES.get(a.strategy).validate_params(a.params)
            except ValidationError as e:
                msgs = "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors())
                raise ValueError(f"account {a.name!r}: invalid params for {a.strategy}: {msgs}") from None
            except ValueError as e:
                raise ValueError(f"account {a.name!r}: invalid params for {a.strategy}: {e}") from None
        return self

    @property
    def timeframe_seconds(self) -> int:
        """Bar length in seconds."""
        return TIMEFRAME_SECONDS[self.timeframe]

    def risk_for(self, account: str) -> RiskConfig:
        """Effective risk settings for ``account`` (global merged with overrides)."""
        acc = next(a for a in self.accounts if a.name == account)
        if acc.risk is None:
            return self.risk
        merged = self.risk.model_dump()
        merged.update({k: v for k, v in acc.risk.model_dump().items() if v is not None})
        return RiskConfig(**merged)


def _format_errors(err: ValidationError, source: str) -> str:
    lines = [f"Invalid configuration in {source}:"]
    for e in err.errors():
        loc = ".".join(str(p) for p in e["loc"]) or "(top level)"
        msg = e["msg"].removeprefix("Value error, ")
        if e["type"] == "extra_forbidden":
            msg = "unknown option (check spelling)"
        lines.append(f"  - {loc}: {msg}")
    return "\n".join(lines)


def parse_config(data: dict[str, Any], source: str = "<config>") -> Config:
    """Validate a config dict, raising :class:`ConfigError` with readable messages."""
    if not isinstance(data, dict):
        raise ConfigError(f"{source}: top level must be a mapping of options")
    try:
        return Config.model_validate(data)
    except ValidationError as e:
        raise ConfigError(_format_errors(e, source)) from None


def load_config(path: str | Path) -> Config:
    """Load and validate ``path``."""
    p = Path(path)
    if not p.exists():
        raise ConfigError(
            f"config file not found: {p}. Create one with: python -m papertrader init"
        )
    try:
        data = yaml.safe_load(p.read_text()) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{p}: YAML syntax error: {e}") from None
    return parse_config(data, str(p))


EXAMPLE_CONFIG = """\
# =====================================================================
# papertrader configuration
# ---------------------------------------------------------------------
# This is a SIMULATION. No real orders are ever placed and no brokerage
# credentials are needed. Market data comes from free, keyless sources:
# yfinance (stocks/ETFs) and ccxt public endpoints (crypto).
# =====================================================================

starting_cash: 10000          # total virtual cash; account allocations must not exceed it
currency: USD                 # display only
timeframe: 1h                 # bar size: 1m, 5m, 15m, 1h, 1d
poll_interval_seconds: 60     # live mode: seconds between ticks (checks for newly closed bars)
heartbeat_stale_seconds: 300  # a heartbeat older than this marks the bot as not running

universe:                     # the pool the bot may choose from
  stocks: [AAPL, MSFT, NVDA, AMZN, GOOGL, TSLA, SPY, QQQ]
  crypto: [BTC/USDT, ETH/USDT, SOL/USDT]
  exclude: []                 # e.g. [TSLA]

selection:                    # how each account picks symbols from the universe
  method: momentum_rank       # momentum_rank | signal_strength | volatility_adjusted | random_baseline
  lookback_bars: 50           # bars of history used for scoring
  top_n: 3                    # trade at most N selected symbols at a time (held symbols are always managed)
  rebalance_every: 24         # decision rounds between re-evaluating the selection
  min_avg_volume: 100000      # liquidity filter: average *dollar* volume per bar over lookback_bars
  seed: 42                    # seed for random_baseline (makes backtests deterministic)
  shadow_random_baseline: true  # also record a seeded random pick each rebalance, to compare against

accounts:                     # one isolated virtual account per strategy
  - name: sma_cross
    strategy: sma_crossover
    params: {fast: 10, slow: 30}
    allocation: 4000
  - name: rsi_reversion
    strategy: rsi_mean_reversion
    params: {period: 14, oversold: 30, overbought: 70}
    allocation: 4000
  - name: benchmark
    strategy: buy_and_hold      # buys the selected symbols once and holds them
    params: {}
    allocation: 2000
    risk: {stop_loss_pct: 0, take_profit_pct: 0, max_daily_loss_pct: 0}  # per-account override; 0 = disabled

fees_pct: 0.1                 # commission, % of notional, charged on every fill
slippage_pct: 0.05            # adverse price move applied to every fill, %

risk:
  max_position_pct: 25        # max size of one position, % of account equity
  stop_loss_pct: 5            # exit when price falls this % below average cost (0 = off)
  take_profit_pct: 10         # exit when price rises this % above average cost (0 = off)
  max_open_positions: 5       # per account
  max_daily_loss_pct: 3       # halt new entries for the rest of the UTC day if breached (0 = off)

logging:
  level: INFO                 # DEBUG | INFO | WARNING | ERROR
  dir: ./logs                 # JSON-lines log at <dir>/papertrader.jsonl (rotated)
  max_bytes: 10000000
  backup_count: 5

data:
  feed: auto                  # auto = yfinance + ccxt; synthetic = offline deterministic random walk (demo/tests)
  crypto_exchange: binance    # ccxt exchange id, public endpoints only
  crypto_exchange_fallbacks: [kraken, coinbase]  # tried in order if the primary is unreachable/geo-blocked
  cache_dir: ./cache          # downloaded bars are cached here (SQLite)
  history_bars: 250           # bars of history handed to strategies
  max_retries: 4              # data fetch retries (exponential backoff)
  retry_base_delay: 1.0       # seconds; doubles each retry

storage:
  db_path: ./state/papertrader.db          # live state (SQLite, WAL)
  backtest_db_path: ./state/backtest.db    # backtests never touch live state
"""
