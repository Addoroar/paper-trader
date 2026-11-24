"""The risk layer sits between strategies and the broker and can override both.

* **Sizing**: a BUY is sized to ``min(signal.size_pct, max_position_pct)`` of
  account equity, minus what is already held, and capped by available cash.
* **Exposure**: no new symbol once ``max_open_positions`` are open.
* **Stops**: stop-loss / take-profit relative to average cost, checked
  against each bar's low/high (so intrabar breaches are caught, including
  bars replayed after downtime). If a bar *opens* beyond the level (a gap)
  the exit fills at the open, not at the better stop price. If both levels
  are touched in the same bar the stop-loss is assumed to hit first
  (conservative).
* **Daily loss cap**: once equity is down ``max_daily_loss_pct`` from the
  start of the UTC day, new entries are blocked until the next day. Exits are
  always allowed.
"""

from __future__ import annotations

from dataclasses import dataclass

from papertrader.broker.paper import PaperBroker, round_qty
from papertrader.config import RiskConfig
from papertrader.models import ExitDecision, Position, Side, Signal


@dataclass
class SizingDecision:
    """Result of sizing a BUY: a quantity, or zero with the reason."""

    qty: float
    reason: str
    target_pct: float = 0.0


class RiskManager:
    """Applies one account's :class:`~papertrader.config.RiskConfig`."""

    def __init__(self, cfg: RiskConfig, broker: PaperBroker) -> None:
        self.cfg = cfg
        self.broker = broker

    def check_exit(self, pos: Position, bar_open: float, bar_high: float, bar_low: float) -> ExitDecision | None:
        """Stop-loss / take-profit check for one bar after the entry bar."""
        sl, tp = self.cfg.stop_loss_pct, self.cfg.take_profit_pct
        if sl > 0:
            stop = pos.avg_cost * (1 - sl / 100)
            if bar_open <= stop:
                return ExitDecision("stop_loss", bar_open,
                                    f"stop-loss: gapped open {bar_open:.4f} <= stop {stop:.4f} "
                                    f"({sl:g}% below avg cost {pos.avg_cost:.4f}); exit at open")
            if bar_low <= stop:
                return ExitDecision("stop_loss", stop,
                                    f"stop-loss: low {bar_low:.4f} hit stop {stop:.4f} "
                                    f"({sl:g}% below avg cost {pos.avg_cost:.4f})")
        if tp > 0:
            target = pos.avg_cost * (1 + tp / 100)
            if bar_open >= target:
                return ExitDecision("take_profit", bar_open,
                                    f"take-profit: gapped open {bar_open:.4f} >= target {target:.4f}; exit at open")
            if bar_high >= target:
                return ExitDecision("take_profit", target,
                                    f"take-profit: high {bar_high:.4f} reached target {target:.4f} "
                                    f"({tp:g}% above avg cost {pos.avg_cost:.4f})")
        return None

    def daily_loss_breached(self, start_equity: float, equity: float) -> bool:
        """True if today's loss exceeds the cap."""
        cap = self.cfg.max_daily_loss_pct
        return cap > 0 and start_equity > 0 and (equity - start_equity) / start_equity * 100 <= -cap

    def size_buy(
        self,
        symbol: str,
        signal: Signal,
        price: float,
        equity: float,
        cash: float,
        position: Position | None,
        open_positions: int,
    ) -> SizingDecision:
        """Turn a BUY signal into a quantity (or a zero with the reason it was blocked)."""
        if position is None and open_positions >= self.cfg.max_open_positions:
            return SizingDecision(0, f"max_open_positions ({self.cfg.max_open_positions}) reached")
        cap = self.cfg.max_position_pct / 100
        target_pct = min(signal.size_pct, cap) if signal.size_pct else cap
        held_value = position.qty * price if position else 0.0
        want_value = equity * target_pct - held_value
        if want_value <= max(1.0, equity * 0.005):
            return SizingDecision(0, f"already at target size ({held_value / equity * 100:.1f}% of equity)", target_pct)
        unit = self.broker.fill_price(Side.BUY, price) * (1 + self.broker.fee_rate)
        qty = round_qty(symbol, want_value / unit)
        affordable = self.broker.max_affordable_qty(symbol, cash, price)
        note = f"sized to {target_pct * 100:.0f}% of equity"
        if qty > affordable:
            qty = affordable
            note = f"wanted {target_pct * 100:.0f}% of equity, capped by cash {cash:.2f}"
        if qty <= 0:
            return SizingDecision(0, f"insufficient cash ({cash:.2f}) for even 1 unit at {price:.4f}", target_pct)
        return SizingDecision(qty, note, target_pct)
