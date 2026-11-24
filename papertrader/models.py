"""Core value types shared across the bot.

Market history is passed around as a ``pandas.DataFrame`` indexed by the bar's
*open* time (UTC epoch seconds, ``int``) with columns
``open, high, low, close, volume``. A bar is only ever visible once it has
closed, which is what keeps the bot free of lookahead bias.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

BAR_COLUMNS = ["open", "high", "low", "close", "volume"]

TIMEFRAME_SECONDS: dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "1d": 86400,
}


class Action(StrEnum):
    """What a strategy wants to do with a symbol."""

    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class Side(StrEnum):
    """Order side. Long-only: BUY opens/increases, SELL reduces/closes."""

    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(StrEnum):
    """Lifecycle of a simulated order."""

    PENDING = "PENDING"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class Signal:
    """A strategy's decision for one symbol on one bar.

    Attributes:
        action: BUY, SELL or HOLD.
        reason: human-readable explanation (goes straight into the decision log).
        size_pct: for BUY, desired position size as a fraction of account equity
            (``None`` = as large as the risk layer allows); for SELL, the fraction
            of the current position to sell (``None`` = all of it).
        strength: optional conviction in [-1, 1]; used by the ``signal_strength``
            selector to rank symbols.
    """

    action: Action
    reason: str
    size_pct: float | None = None
    strength: float = 0.0

    @classmethod
    def hold(cls, reason: str, strength: float = 0.0) -> "Signal":
        """Shorthand for a HOLD signal."""
        return cls(Action.HOLD, reason, strength=strength)


@dataclass
class Position:
    """An open long position in one account."""

    account: str
    symbol: str
    qty: float
    avg_cost: float  # per unit, *including* buy fees
    opened_ts: int  # bar open time of the entry fill
    last_bar_ts: int = 0  # bar on which the position was last increased

    def value(self, price: float) -> float:
        """Market value at ``price``."""
        return self.qty * price

    def unrealized_pnl(self, price: float) -> float:
        """Unrealized P&L at ``price`` (fees on entry already in avg_cost)."""
        return (price - self.avg_cost) * self.qty


@dataclass
class Order:
    """A simulated order as stored in the ``orders`` table."""

    order_id: str
    account: str
    symbol: str
    side: Side
    qty: float
    ref_price: float
    bar_ts: int
    kind: str
    status: OrderStatus
    reason: str
    created_ts: int = 0
    reject_reason: str | None = None


@dataclass
class Fill:
    """An executed simulated order."""

    order_id: str
    account: str
    symbol: str
    side: Side
    qty: float
    price: float
    fee: float
    realized_pnl: float
    bar_ts: int
    ts: int


@dataclass
class ExitDecision:
    """A risk-layer forced exit (stop-loss or take-profit)."""

    kind: str  # "stop_loss" | "take_profit"
    price: float
    reason: str


@dataclass
class TickContext:
    """Information about the current tick passed to selectors."""

    account: str
    step: int
    now: int
    seed: int
    extra: dict = field(default_factory=dict)


def is_crypto(symbol: str) -> bool:
    """Crypto pairs are written ``BASE/QUOTE``; everything else is a stock/ETF."""
    return "/" in symbol
