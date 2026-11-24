"""The main loop: selector -> strategy -> risk -> broker, for every account, every tick.

One tick:

1. Heartbeat; fetch closed bars (``<= now``) for the universe plus held symbols.
2. For each account, find bars newer than its per-symbol cursor.

   * **Missed bars** (all but the newest, e.g. after downtime): replayed only
     for stop-loss / take-profit checks against each bar's high/low. No new
     entries are made on stale prices. Cursors advance bar by bar.
   * **Newest bar** (a "decision round"): risk exits, (re)selection when due,
     daily-loss check, then the strategy runs on selected + held symbols and
     the risk layer sizes / blocks the result before the broker executes it.

3. Cursors, the round counter and an equity snapshot are committed together.

Every decision (including HOLDs and skipped trades) is logged with a
human-readable reason. Replaying a tick after a crash is safe: order ids are
deterministic, and a symbol that already has an order for the current bar is
not re-evaluated, so nothing executes twice.
"""

from __future__ import annotations

import json
import signal
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd

from papertrader import logging_setup as ev
from papertrader.broker.paper import PaperBroker, round_qty
from papertrader.config import Config
from papertrader.data.base import DataFeed
from papertrader.engine.clock import WallClock
from papertrader.logging_setup import EventLogger
from papertrader.models import Action, OrderStatus, Position, Side, TickContext
from papertrader.risk.manager import RiskManager
from papertrader.selection import SELECTORS, SelectionResult, Selector
from papertrader.storage import repo
from papertrader.storage.db import Database
from papertrader.strategies import STRATEGIES, Strategy


def utc_day(ts: int) -> str:
    """ISO date (UTC) of ``ts``."""
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def fmt_ts(ts: int) -> str:
    """Readable UTC timestamp."""
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def fmt_qty(symbol: str, qty: float) -> str:
    """'12 shares' or '0.0153 BTC/USDT'."""
    if "/" in symbol:
        return f"{qty:g} {symbol.split('/')[0]}"
    return f"{qty:g} share{'s' if qty != 1 else ''}"


@dataclass
class AccountRuntime:
    """Per-account live objects."""

    name: str
    strategy: Strategy
    risk: RiskManager
    selector: Selector
    shadow: Selector | None


class Engine:
    """Runs accounts against a data feed. Used identically by live and backtest modes.

    Args:
        cfg: validated configuration.
        db: state database (live or backtest).
        feed: market data feed.
        mode: ``"live"`` or ``"backtest"`` (affects heartbeat bookkeeping only).
        clock: returns the current time in epoch seconds.
    """

    def __init__(self, cfg: Config, db: Database, feed: DataFeed, *, mode: str = "live", clock=None) -> None:
        self.cfg = cfg
        self.db = db
        self.feed = feed
        self.mode = mode
        self.clock = clock or WallClock()
        self.events = EventLogger(db, self.clock)
        self.broker = PaperBroker(db, self.events, cfg.fees_pct, cfg.slippage_pct)
        self.symbols = cfg.universe.symbols()
        self.stop_event = threading.Event()
        self.bars: dict[str, pd.DataFrame] = {}
        self._price_cache: dict[str, float] | None = None
        feed.on_event = self._feed_event
        self.accounts: list[AccountRuntime] = []
        sel_cls = SELECTORS.get(cfg.selection.method)
        s = cfg.selection
        with db.tx():
            for a in cfg.accounts:
                existing = db.one("SELECT strategy, params_json FROM accounts WHERE name=?", (a.name,))
                repo.ensure_account(db, a.name, a.strategy, a.params, a.allocation)
                if existing and (existing["strategy"] != a.strategy
                                 or json.loads(existing["params_json"]) != a.params):
                    db.execute("UPDATE accounts SET strategy=?, params_json=? WHERE name=?",
                               (a.strategy, json.dumps(a.params, sort_keys=True), a.name))
                    self.events.log(ev.STARTUP, f"account {a.name}: strategy/params changed in config "
                                    f"({existing['strategy']} -> {a.strategy}); keeping cash and positions",
                                    level="WARNING", account=a.name)
                strat = STRATEGIES.get(a.strategy)(a.params)
                shadow = None
                if s.shadow_random_baseline and s.method != "random_baseline":
                    shadow = SELECTORS.get("random_baseline")(s.lookback_bars, s.top_n, s.min_avg_volume, strat)
                self.accounts.append(AccountRuntime(
                    a.name, strat, RiskManager(cfg.risk_for(a.name), self.broker),
                    sel_cls(s.lookback_bars, s.top_n, s.min_avg_volume, strat), shadow))

    # ------------------------------------------------------------------ events
    def _feed_event(self, type_: str, message: str, level: str = "WARNING", symbol: str | None = None, **kw) -> None:
        self.events.log(type_, message, level=level, symbol=symbol, **kw)

    def log(self, type_: str, message: str, **kw) -> bool:
        """Shorthand for ``self.events.log``."""
        return self.events.log(type_, message, **kw)

    # ------------------------------------------------------------------ lifecycle
    def startup(self) -> None:
        """Mark the bot running; detect an unclean previous shutdown and recover."""
        from papertrader.engine.recovery import reconcile, resolve_pending_orders

        now_wall = time.time()
        prev = self.db.one("SELECT * FROM bot_status WHERE id=1")
        unclean = prev is not None and prev["state"] in ("running", "recovering")
        with self.db.tx():
            self.db.execute(
                "INSERT INTO bot_status(id, state, mode, pid, started_at, heartbeat_at, restarts) VALUES(1,?,?,?,?,?,0)"
                " ON CONFLICT(id) DO UPDATE SET state=excluded.state, mode=excluded.mode, pid=excluded.pid,"
                " started_at=excluded.started_at, heartbeat_at=excluded.heartbeat_at,"
                " restarts=restarts + ?",
                ("recovering" if unclean else "running", self.mode, _pid(), now_wall, now_wall, 1 if unclean else 0),
            )
            self.log(ev.STARTUP, f"papertrader starting ({self.mode} mode, {len(self.accounts)} accounts, "
                     f"{len(self.symbols)} symbols, timeframe {self.cfg.timeframe})",
                     mode=self.mode, accounts=[a.name for a in self.accounts])
        if unclean:
            hb = prev["heartbeat_at"] or prev["started_at"] or now_wall
            self.log(ev.RECOVERY,
                     f"recovered from interruption: previous run (pid {prev['pid']}) did not shut down cleanly; "
                     f"last heartbeat {now_wall - hb:.0f}s ago, last processed tick "
                     f"{fmt_ts(prev['last_tick_ts']) if prev['last_tick_ts'] else 'none'}",
                     level="WARNING", last_heartbeat=hb, downtime_seconds=round(now_wall - hb, 1),
                     last_tick_ts=prev["last_tick_ts"])
        resolve_pending_orders(self)
        reconcile(self)
        with self.db.tx():
            self.db.execute("UPDATE bot_status SET state='running' WHERE id=1")

    def shutdown(self, reason: str = "requested") -> None:
        """Mark a clean shutdown."""
        with self.db.tx():
            self.db.execute("UPDATE bot_status SET state='stopped', clean_shutdown_at=?, heartbeat_at=? WHERE id=1",
                            (time.time(), time.time()))
            self.log(ev.SHUTDOWN, f"clean shutdown ({reason})")

    def heartbeat(self, now: int) -> None:
        """Record liveness."""
        self.db.execute("UPDATE bot_status SET heartbeat_at=?, last_tick_ts=?, pid=? WHERE id=1",
                        (time.time(), now, _pid()))

    def run_forever(self) -> None:
        """Live loop: tick every ``poll_interval_seconds`` until SIGINT/SIGTERM."""

        def handle(signum, _frame):
            name = signal.Signals(signum).name
            if self.stop_event.is_set():
                raise KeyboardInterrupt
            self.events.logger.warning(f"received {name}; finishing current tick then shutting down "
                                       "(send again to force)")
            self.stop_event.set()

        signal.signal(signal.SIGINT, handle)
        signal.signal(signal.SIGTERM, handle)
        self.startup()
        try:
            while not self.stop_event.is_set():
                t0 = time.monotonic()
                try:
                    self.tick()
                except Exception as e:  # noqa: BLE001 - keep the bot alive; the error is recorded
                    self.log(ev.ERROR, f"tick failed: {type(e).__name__}: {e}", level="ERROR",
                             traceback=traceback.format_exc()[-4000:])
                self.stop_event.wait(max(0.0, self.cfg.poll_interval_seconds - (time.monotonic() - t0)))
        finally:
            self.shutdown("signal" if self.stop_event.is_set() else "exception")

    # ------------------------------------------------------------------ the tick
    def fetch_all(self, now: int) -> dict[str, pd.DataFrame]:
        """Closed bars for the universe plus every held symbol."""
        held = {r["symbol"] for r in self.db.query("SELECT DISTINCT symbol FROM positions")}
        need = list(dict.fromkeys([*self.symbols, *sorted(held)]))
        limit = max(self.cfg.data.history_bars, self.cfg.selection.lookback_bars + 1)
        out: dict[str, pd.DataFrame] = {}
        for sym in need:
            try:
                df = self.feed.get_bars(sym, self.cfg.timeframe, now, limit)
            except Exception as e:  # noqa: BLE001
                self.log(ev.DATA_ERROR, f"no data for {sym}: {type(e).__name__}: {e}", level="ERROR", symbol=sym)
                continue
            if not df.empty:
                out[sym] = df
        return out

    def tick(self) -> None:
        """Process everything that is new as of ``clock()``."""
        now = self.clock()
        live = self.mode == "live"
        if live:
            self.log(ev.TICK_STARTED, f"tick at {fmt_ts(now)}", level="DEBUG")
        with self.db.tx():
            self.heartbeat(now)
        self.bars = self.fetch_all(now)
        self._price_cache = None
        self._maybe_daily_summary(now)
        rounds = 0
        for acct in self.accounts:
            rounds += self.process_account(acct, now)
        with self.db.tx():
            self._update_benchmark(now)
            self.db.set_meta("last_tick_ts", now)
            self.heartbeat(now)
        if live:
            self.log(ev.TICK_FINISHED, f"tick done: {rounds} account decision round(s), "
                     f"{len(self.bars)}/{len(self.symbols)} symbols with data", level="DEBUG")

    # ------------------------------------------------------------------ per account
    def _prices(self) -> dict[str, float]:
        """Latest close per symbol for this tick (cached; reset when bars are refetched)."""
        if self._price_cache is None:
            self._price_cache = {s: float(df["close"].iat[-1]) for s, df in self.bars.items() if not df.empty}
        return self._price_cache

    def equity(self, account: str, prices: dict[str, float] | None = None) -> tuple[float, float, dict]:
        """(equity, positions value, prices used) for ``account`` at the latest known prices."""
        prices = prices or self._prices()
        cash = repo.get_cash(self.db, account)
        used = {}
        value = 0.0
        for sym, pos in repo.get_positions(self.db, account).items():
            px = prices.get(sym, pos.avg_cost)
            used[sym] = px
            value += pos.qty * px
        return cash + value, value, used

    def process_account(self, acct: AccountRuntime, now: int) -> int:
        """Run one account for this tick. Returns 1 if a decision round happened."""
        name = acct.name
        positions = repo.get_positions(self.db, name)
        relevant = list(dict.fromkeys([*self.symbols, *positions]))
        new_bars: dict[str, pd.DataFrame] = {}
        for sym in relevant:
            df = self.bars.get(sym)
            if df is None or df.empty:
                continue
            cur = repo.get_cursor(self.db, name, sym)
            new = df.iloc[-1:] if cur == 0 else df.iloc[int(df.index.searchsorted(cur, side="right")) :]
            if len(new):
                new_bars[sym] = new
        if not new_bars:
            return 0

        # 1) gap replay: missed bars are only used for stop/take-profit checks
        self._replay_gap(acct, new_bars)

        # 2) decision round on the newest bar of each symbol
        latest = {sym: int(df.index[-1]) for sym, df in new_bars.items()}
        step = repo.get_step(self.db, name)
        selection, sel_reasons = self._selection(acct, step, now)
        halted = self._daily_check(acct, now)

        risk_exited: set[str] = set()
        for sym in sorted(latest):
            pos = repo.get_position(self.db, name, sym)
            if pos is None or latest[sym] <= pos.last_bar_ts:
                continue
            if self._existing_orders(name, sym, latest[sym]):
                risk_exited.add(sym)
                continue
            df = self.bars[sym]
            ex = acct.risk.check_exit(pos, float(df["open"].iat[-1]), float(df["high"].iat[-1]),
                                      float(df["low"].iat[-1]))
            if ex:
                self._risk_exit(acct, pos, latest[sym], ex, now)
                risk_exited.add(sym)

        held = repo.get_positions(self.db, name)
        rank = {s: i for i, s in enumerate(selection)}
        candidates = [s for s in latest if (s in held or s in rank) and s not in risk_exited]
        candidates.sort(key=lambda s: (0 if s in held else 1, rank.get(s, 999), s))
        for sym in candidates:
            self._decide(acct, sym, latest[sym], now, halted, sel_reasons.get(sym, ""))

        # 3) commit the round: cursors, counter, equity snapshot
        eq, posval, used = self.equity(name)
        with self.db.tx():
            for sym, ts in latest.items():
                repo.set_cursor(self.db, name, sym, ts)
            self.db.execute("UPDATE accounts SET step=step+1 WHERE name=?", (name,))
            self.db.execute(
                "INSERT OR REPLACE INTO equity_snapshots(account, ts, cash, positions_value, equity, prices_json)"
                " VALUES (?,?,?,?,?,?)",
                (name, now, repo.get_cash(self.db, name), posval, eq, json.dumps(used)),
            )
        from papertrader import faults

        faults.maybe_fault("after_round")
        return 1

    def _existing_orders(self, account: str, symbol: str, bar_ts: int) -> list:
        """Orders already created for this account/symbol/bar (a replayed tick)."""
        rows = self.db.query("SELECT order_id, status FROM orders WHERE account=? AND symbol=? AND bar_ts=?",
                             (account, symbol, bar_ts))
        for r in rows:
            if r["status"] == OrderStatus.PENDING:
                self.broker.fill_order(r["order_id"], self.clock())
        return rows

    def _replay_gap(self, acct: AccountRuntime, new_bars: dict[str, pd.DataFrame]) -> None:
        name = acct.name
        for sym, df in new_bars.items():
            if len(df) <= 1:
                continue
            missed = df.iloc[:-1]
            outcome = "no stop/take-profit triggered"
            for ts, bar in missed.iterrows():
                ts = int(ts)
                pos = repo.get_position(self.db, name, sym)
                if pos is not None and ts > pos.last_bar_ts and not self._existing_orders(name, sym, ts):
                    ex = acct.risk.check_exit(pos, float(bar["open"]), float(bar["high"]), float(bar["low"]))
                    if ex:
                        self._risk_exit(acct, pos, ts, ex, self.clock(), gap=True)
                        outcome = f"{ex.kind} executed on missed bar {fmt_ts(ts)} at {ex.price:.4f}"
                with self.db.tx():
                    repo.set_cursor(self.db, name, sym, ts)
            held_note = "" if repo.get_position(self.db, name, sym) or "executed" in outcome else " (not held)"
            self.log(ev.GAP_REPLAY,
                     f"caught up {len(missed)} missed bar(s) {fmt_ts(int(missed.index[0]))} .. "
                     f"{fmt_ts(int(missed.index[-1]))}{held_note}: {outcome}; no new entries on stale bars",
                     account=name, symbol=sym, bars=len(missed),
                     dedupe_key=f"gap:{name}:{sym}:{int(missed.index[0])}:{int(missed.index[-1])}")

    def _risk_exit(self, acct: AccountRuntime, pos: Position, bar_ts: int, ex, now: int, gap: bool = False) -> None:
        name, sym = acct.name, pos.symbol
        msg = f"{ex.reason}. SELL {fmt_qty(sym, pos.qty)} of {sym} at ~{ex.price:.4f}" + (
            " (during downtime gap)" if gap else "")
        self.log(ev.RISK_TRIGGERED, msg, level="WARNING", account=name, symbol=sym, rule=ex.kind,
                 bar_ts=bar_ts, dedupe_key=f"risk:{name}:{sym}:{bar_ts}")
        self.broker.execute(name, sym, Side.SELL, pos.qty, ex.price, bar_ts, kind=ex.kind, reason=msg, now=now)

    def _selection(self, acct: AccountRuntime, step: int, now: int) -> tuple[list[str], dict[str, str]]:
        """Current selected symbols (best first) and per-symbol reasons; re-selects when due."""
        name = acct.name
        key = f"selection:{name}"
        stored = self.db.get_meta(key)
        state = json.loads(stored) if stored else None
        due = state is None or step - state["step"] >= self.cfg.selection.rebalance_every
        if not due:
            return state["selected"], state["reasons"]
        histories = {s: df for s, df in self.bars.items() if s in self.symbols}
        ctx = TickContext(account=name, step=step, now=now, seed=self.cfg.selection.seed)
        res = acct.selector.select(histories, ctx)
        shadow = acct.shadow.select(histories, ctx) if acct.shadow else None
        reasons = {}
        for s in res.ranked:
            if s.selected:
                reasons[s.symbol] = f"Selected {s.symbol} ({res.method} rank #{s.rank}, {s.detail})"
        with self.db.tx():
            self._store_selection(name, step, now, "primary", res)
            if shadow:
                self._store_selection(name, step, now, "random_shadow", shadow)
            self.db.set_meta(key, json.dumps({"step": step, "ts": now, "selected": res.selected, "reasons": reasons}))
            top = ", ".join(f"#{s.rank} {s.symbol} ({s.detail})" for s in res.ranked[: max(5, self.cfg.selection.top_n)])
            filt = f"; filtered: {', '.join(f'{k} ({v})' for k, v in res.filtered.items())}" if res.filtered else ""
            self.log(ev.SYMBOLS_SELECTED,
                     f"selected {res.selected or 'nothing'} via {res.method}. Ranking: {top or 'n/a'}{filt}",
                     account=name, step=step, selected=res.selected,
                     scores={s.symbol: round(s.score, 6) for s in res.ranked},
                     shadow_random=shadow.selected if shadow else None,
                     dedupe_key=f"sel:{name}:{step}")
        return res.selected, reasons

    def _store_selection(self, account: str, step: int, now: int, kind: str, res: SelectionResult) -> None:
        for s in res.ranked:
            self.db.execute(
                "INSERT OR IGNORE INTO selections(account, step, ts, kind, method, symbol, rank, score, price, selected,"
                " reason) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (account, step, now, kind, res.method, s.symbol, s.rank, s.score, s.price, int(s.selected), s.detail),
            )
        for sym, why in res.filtered.items():
            px = float(self.bars[sym]["close"].iloc[-1]) if sym in self.bars else None
            self.db.execute(
                "INSERT OR IGNORE INTO selections(account, step, ts, kind, method, symbol, rank, score, price, selected,"
                " reason) VALUES (?,?,?,?,?,?,NULL,NULL,?,0,?)",
                (account, step, now, kind, res.method, sym, px, why),
            )

    def _daily_check(self, acct: AccountRuntime, now: int) -> bool:
        """Track start-of-day equity; True if new entries are halted today."""
        name, day = acct.name, utc_day(now)
        eq, _, _ = self.equity(name)
        with self.db.tx():
            self.db.execute("INSERT OR IGNORE INTO daily_state(account, day, start_equity, halted) VALUES (?,?,?,0)",
                            (name, day, eq))
            row = self.db.one("SELECT start_equity, halted FROM daily_state WHERE account=? AND day=?", (name, day))
            if row["halted"]:
                return True
            if acct.risk.daily_loss_breached(row["start_equity"], eq):
                self.db.execute("UPDATE daily_state SET halted=1 WHERE account=? AND day=?", (name, day))
                loss = (eq / row["start_equity"] - 1) * 100
                self.log(ev.RISK_TRIGGERED,
                         f"daily loss cap: equity {eq:.2f} is {loss:.2f}% vs start of day {row['start_equity']:.2f} "
                         f"(limit -{acct.risk.cfg.max_daily_loss_pct:g}%); new entries halted until next UTC day",
                         level="WARNING", account=name, rule="max_daily_loss", dedupe_key=f"dailycap:{name}:{day}")
                return True
        return False

    def _decide(self, acct: AccountRuntime, sym: str, bar_ts: int, now: int, halted: bool, sel_reason: str) -> None:
        name = acct.name
        if self._existing_orders(name, sym, bar_ts):
            return  # decided (and executed) before an interruption; never act twice on one bar
        hist = self.bars[sym]
        if int(hist.index[-1]) != bar_ts:
            hist = hist.iloc[: int(hist.index.searchsorted(bar_ts, side="right"))]
        pos = repo.get_position(self.db, name, sym)
        sig = acct.strategy.on_bar(hist, pos)
        price = float(hist["close"].iloc[-1])
        prefix = f"{sel_reason}. " if sel_reason else ("Held position. " if pos else "")
        dkey = f"decision:{name}:{sym}:{bar_ts}"

        if sig.action == Action.HOLD:
            self.log(ev.HOLD, f"{prefix}{sig.reason}, so HOLD.", account=name, symbol=sym, bar_ts=bar_ts,
                     price=price, dedupe_key=dkey)
            return

        if sig.action == Action.SELL:
            if pos is None:
                self.log(ev.TRADE_SKIPPED, f"{prefix}{sig.reason}, but no position to sell (long-only).",
                         account=name, symbol=sym, bar_ts=bar_ts, dedupe_key=dkey)
                return
            frac = sig.size_pct if sig.size_pct else 1.0
            qty = pos.qty if frac >= 1 else round_qty(sym, pos.qty * frac)
            if qty <= 0:
                self.log(ev.TRADE_SKIPPED, f"{prefix}{sig.reason}, but sell size rounds to zero.",
                         account=name, symbol=sym, bar_ts=bar_ts, dedupe_key=dkey)
                return
            pnl = (price - pos.avg_cost) * qty
            msg = (f"{prefix}{sig.reason}, so SELL {fmt_qty(sym, qty)} at {price:.4f} "
                   f"(avg cost {pos.avg_cost:.4f}, est. P&L {pnl:+.2f}).")
            self.log(ev.SIGNAL, msg, account=name, symbol=sym, action="SELL", qty=qty, price=price,
                     bar_ts=bar_ts, dedupe_key=dkey)
            self.broker.execute(name, sym, Side.SELL, qty, price, bar_ts, kind="signal_exit", reason=msg, now=now)
            return

        # BUY
        if halted:
            self.log(ev.TRADE_SKIPPED, f"{prefix}{sig.reason}, BUY blocked: daily loss cap reached.",
                     level="WARNING", account=name, symbol=sym, bar_ts=bar_ts, rule="max_daily_loss", dedupe_key=dkey)
            return
        eq, _, _ = self.equity(name)
        held = repo.get_positions(self.db, name)
        sizing = acct.risk.size_buy(sym, sig, price, eq, repo.get_cash(self.db, name), pos, len(held))
        if sizing.qty <= 0:
            self.log(ev.TRADE_SKIPPED, f"{prefix}{sig.reason}, BUY skipped by risk: {sizing.reason}.",
                     account=name, symbol=sym, bar_ts=bar_ts, dedupe_key=dkey)
            return
        msg = (f"{prefix}{sig.reason}, so BUY {fmt_qty(sym, sizing.qty)} at {price:.4f}. "
               f"{sizing.reason[0].upper()}{sizing.reason[1:]}.")
        self.log(ev.SIGNAL, msg, account=name, symbol=sym, action="BUY", qty=sizing.qty, price=price,
                 bar_ts=bar_ts, dedupe_key=dkey)
        self.broker.execute(name, sym, Side.BUY, sizing.qty, price, bar_ts, kind="entry", reason=msg, now=now)

    # ------------------------------------------------------------------ benchmark & summaries
    def _update_benchmark(self, now: int) -> None:
        prices = {s: p for s, p in self._prices().items() if s in self.symbols}
        if not prices:
            return
        base_raw = self.db.get_meta("benchmark_base")
        base = json.loads(base_raw) if base_raw else {}
        if not base:
            base = prices
            self.db.set_meta("benchmark_base", json.dumps(base))
        ratios = [prices[s] / base[s] for s in base if s in prices and base[s] > 0]
        if ratios:
            total = sum(a.allocation for a in self.cfg.accounts)
            self.db.execute("INSERT OR REPLACE INTO benchmark(ts, value) VALUES (?,?)",
                            (now, total * sum(ratios) / len(ratios)))

    def _maybe_daily_summary(self, now: int) -> None:
        day = utc_day(now)
        prev = self.db.get_meta("summary_day")
        if prev is None:
            with self.db.tx():
                self.db.set_meta("summary_day", day)
            return
        if prev >= day:
            return
        start = int(datetime.fromisoformat(prev).replace(tzinfo=timezone.utc).timestamp())
        end = start + 86400
        parts, data = [], {}
        for a in self.cfg.accounts:
            last = self.db.one("SELECT equity FROM equity_snapshots WHERE account=? AND ts<? ORDER BY ts DESC LIMIT 1",
                               (a.name, end))
            first = self.db.one("SELECT start_equity FROM daily_state WHERE account=? AND day=?", (a.name, prev))
            eq = last["equity"] if last else a.allocation
            st = first["start_equity"] if first else eq
            trades = self.db.scalar("SELECT count(*) FROM fills WHERE account=? AND ts>=? AND ts<?",
                                    (a.name, start, end), 0)
            realized = self.db.scalar("SELECT sum(realized_pnl) FROM fills WHERE account=? AND ts>=? AND ts<?",
                                      (a.name, start, end), 0.0)
            parts.append(f"{a.name}: equity {eq:.2f} ({eq - st:+.2f} today, {(eq / a.allocation - 1) * 100:+.2f}% total),"
                         f" {trades} trade(s), realized {realized:+.2f}")
            data[a.name] = {"equity": eq, "day_pnl": eq - st, "trades": trades, "realized": realized}
        notable = self.db.query(
            "SELECT type, count(*) n FROM events WHERE ts>=? AND ts<? AND type IN (?,?,?,?,?) GROUP BY type",
            (start, end, ev.RISK_TRIGGERED, ev.ORDER_REJECTED, ev.DATA_ERROR, ev.RECOVERY, ev.ERROR),
        )
        note = ", ".join(f"{r['n']} {r['type']}" for r in notable) or "none"
        with self.db.tx():
            self.log(ev.DAILY_SUMMARY, f"daily summary {prev}: " + "; ".join(parts) + f". Notable: {note}",
                     ts=end - 1, day=prev, accounts=data, notable={r["type"]: r["n"] for r in notable},
                     dedupe_key=f"summary:{prev}")
            self.db.set_meta("summary_day", day)


def _pid() -> int:
    import os

    return os.getpid()
