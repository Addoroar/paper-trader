# papertrader

An autonomous **paper-trading bot**. It simulates trades with fake money against real market data. It
**never places real orders and never needs brokerage credentials**.

You give it starting cash, a universe of symbols and one or more strategies. It decides for itself which
symbols to trade and when, logs every decision with a human-readable reason, and survives crashes,
`kill -9` and power loss without losing or duplicating trades.

> **Disclaimer.** This is a simulation. Simulated fills are optimistic: every order fills completely
> at the bar's price plus a fixed slippage, with no partial fills, queue position, market impact,
> borrow costs or outages. Past performance (simulated or real) does not predict future results.
> Nothing here is investment advice.

---

## Contents
- [Quick start](#quick-start)
- [Commands](#commands)
- [How it decides](#how-it-decides)
- [Configuration reference](#configuration-reference)
- [Dashboard](#dashboard)
- [Logging](#logging)
- [Crash safety and recovery](#crash-safety-and-recovery)
- [Running in the background](#running-in-the-background)
- [Backtesting](#backtesting)
- [Adding a strategy](#adding-a-strategy)
- [Adding a selector](#adding-a-selector)
- [Project layout](#project-layout)
- [Defaults and design decisions](#defaults-and-design-decisions)
- [Tests](#tests)

## Quick start

Requires Python 3.14+.

```bash
python3.14 -m venv .venv          # or: uv venv --python 3.14 .venv
source .venv/bin/activate
pip install -e ".[dev]"           # or: pip install -r requirements.txt && pip install -e .
python -m papertrader init        # writes config.yaml (a commented copy of config.example.yaml)
python -m papertrader backtest --config config.yaml --start 2025-01-01 --end 2025-11-01
python -m papertrader run --config config.yaml        # live paper trading, Ctrl+C to stop
python -m papertrader dashboard                       # http://localhost:8501
```

No internet access, or Yahoo or the exchanges blocked? Add `--feed synthetic` (or set
`data.feed: synthetic`) to use the built-in deterministic offline price generator. This is handy for
trying everything out. Synthetic prices are random walks, so don't read anything into strategy
results on them.

## Commands

| Command | What it does |
|---|---|
| `python -m papertrader init [--path config.yaml] [--force]` | Write the example config |
| `python -m papertrader run --config config.yaml [--feed synthetic]` | Live paper trading. Resumes automatically from saved state |
| `python -m papertrader backtest --config config.yaml --start YYYY-MM-DD --end YYYY-MM-DD [--feed synthetic] [--resume] [-v]` | Same engine over history, into a separate DB |
| `python -m papertrader dashboard [--config config.yaml] [--port 8501] [--backtest]` | Web dashboard (Streamlit) |
| `python -m papertrader status [--backtest]` | Quick CLI summary: bot state, heartbeat, accounts, positions, recent warnings |
| `python -m papertrader report [--export html\|csv] [--out reports] [--backtest]` | Metrics table, plus an optional HTML report or CSV files |
| `python -m papertrader reset [--yes] [--backtest]` | Delete the state DB. Asks for confirmation unless `--yes`; refuses while the bot is running |

`--config` defaults to `config.yaml`. An installed `papertrader` console script works the same way.
Exit codes: `0` ok, `1` user aborted or nothing to do, `2` invalid config or another instance running.

## How it decides

Every tick (every `poll_interval_seconds`), for each account:

1. **Selector.** Scores every symbol in the universe with `selection.method`, drops symbols with
   too little history or too little liquidity, and keeps the top `top_n`. It re-selects every
   `rebalance_every` decision rounds.
2. **Strategy.** Runs on the selected symbols plus any symbols the account already holds (held
   positions are always managed, even after they drop out of the selection). It returns
   BUY / SELL / HOLD.
3. **Risk layer.** Applies stop-loss and take-profit (against each bar's high and low), sizes buys
   to `max_position_pct` of equity and the available cash, enforces `max_open_positions`, and halts
   new entries for the rest of the UTC day once `max_daily_loss_pct` is hit. It overrides the
   strategy when needed.
4. **Broker.** Fills the order at the bar close ± `slippage_pct`, charges `fees_pct`, and updates
   cash, positions, average cost and realized P&L in one transaction.

Every decision is recorded with a reason, including holds, skips, rejections and risk overrides:

```
Selected ETH/USDT (momentum rank #3, +0.0% over 20 bars). SMA3 crossed above SMA8 (SMA3 3798.51 vs SMA8 3798.09), so BUY 0.26269 ETH at 3801.0518. Sized to 25% of equity.
Selected QQQ (momentum rank #1, +0.1% over 20 bars). RSI5=78.3 within [40, 60], so HOLD.
stop-loss: low 17.8145 hit stop 17.9056 (5% below avg cost 18.8480). SELL 53.466 SOL of SOL/USDT at ~17.9056
Selected SPY (momentum rank #2, +5.0% over 50 bars). buy and hold: initial entry, BUY skipped by risk: max_open_positions (5) reached.
```

**No lookahead.** A bar becomes visible only after it has *closed*. Stock bars close at the bar end
or the 16:00 ET session close, whichever comes first. Strategies and selectors only ever receive
closed bars up to "now". `tests/test_lookahead.py` proves this: it runs two backtests whose data
is identical up to a date T and wildly different afterwards, then asserts that every order,
decision and selection before T is identical.

**No stale trading.** The newest bar is traded only if it closed within one bar length plus two
poll intervals of now. Older bars (the market is closed, data is delayed, or the bot was down) are
used only for stop-loss and take-profit checks.

## Configuration reference

Everything is driven by `config.yaml`, which is validated on startup. Errors list every bad field,
for example `accounts.0.params: invalid params for sma_crossover: fast must be smaller than slow`,
or `risk.stop_loss_pctt: unknown option (check spelling)`.

| Option | Default | Meaning |
|---|---|---|
| `starting_cash` | required | Total virtual cash. The sum of account `allocation`s must not exceed it |
| `currency` | `USD` | Display only |
| `timeframe` | `1h` | Bar size: `1m`, `5m`, `15m`, `1h`, `1d` |
| `poll_interval_seconds` | `60` | Live mode: seconds between ticks (each tick checks for newly closed bars) |
| `heartbeat_stale_seconds` | `300` | A heartbeat older than this means the bot is not running (status and dashboard) |
| `universe.stocks` | `[]` | Stock/ETF tickers (yfinance) |
| `universe.crypto` | `[]` | Crypto pairs as `BASE/QUOTE` (ccxt) |
| `universe.exclude` | `[]` | Symbols to remove from the universe |
| `selection.method` | `momentum_rank` | `momentum_rank` (trailing return), `signal_strength` (how strongly the account's own strategy wants to buy), `volatility_adjusted` (return / volatility), `random_baseline` (seeded random) |
| `selection.lookback_bars` | `50` | Bars used for scoring |
| `selection.top_n` | `3` | Trade at most N selected symbols at a time |
| `selection.rebalance_every` | `24` | Decision rounds between re-selections |
| `selection.min_avg_volume` | `100000` | Liquidity filter: average **dollar** volume per bar (volume × close), so stocks and crypto compare fairly |
| `selection.seed` | `42` | Seed for `random_baseline`. Backtests are deterministic |
| `selection.shadow_random_baseline` | `true` | At each rebalance, also record a seeded random pick so the Selection page can compare the selector against chance |
| `accounts[].name` | required | Unique account name (letters, digits, `_`, `-`) |
| `accounts[].strategy` | required | A registered strategy (see below) |
| `accounts[].params` | `{}` | Strategy parameters, validated per strategy (unknown keys are errors) |
| `accounts[].allocation` | required | This account's virtual cash |
| `accounts[].risk` | none | Optional per-account override of any `risk` field (e.g. disable stops for the benchmark) |
| `fees_pct` | `0.1` | Commission, % of notional, on every fill |
| `slippage_pct` | `0.05` | Adverse price move on every fill, % |
| `risk.max_position_pct` | `25` | Max size of one position, % of account equity |
| `risk.stop_loss_pct` | `5` | Exit when the price falls this % below average cost. `0` = off |
| `risk.take_profit_pct` | `10` | Exit when the price rises this % above average cost. `0` = off |
| `risk.max_open_positions` | `5` | Per account |
| `risk.max_daily_loss_pct` | `3` | Halt new entries for the rest of the UTC day once equity is down this much from the day's start. Exits still run. `0` = off |
| `logging.level` | `INFO` | `DEBUG` also writes tick started/finished and routine stale-bar notes |
| `logging.dir` | `./logs` | JSON-lines log directory |
| `logging.max_bytes` / `backup_count` | `10 MB` / `5` | Log rotation |
| `data.feed` | `auto` | `auto` = yfinance for stocks + ccxt for crypto; `synthetic` = offline generator |
| `data.crypto_exchange` | `binance` | ccxt exchange id (public endpoints only) |
| `data.crypto_exchange_fallbacks` | `[kraken, coinbase]` | Tried in order if the primary is unreachable or geo-blocked |
| `data.cache_dir` | `./cache` | Downloaded bars are cached in `bars.db` here |
| `data.history_bars` | `250` | Bars of history passed to strategies |
| `data.max_retries` / `retry_base_delay` | `4` / `1.0` | Retries with exponential backoff (1 s, 2 s, 4 s, 8 s, + jitter) |
| `storage.db_path` | `./state/papertrader.db` | Live state |
| `storage.backtest_db_path` | `./state/backtest.db` | Backtest state (never mixed with live) |

### Strategies

| Name | Params (defaults) | Logic |
|---|---|---|
| `sma_crossover` | `fast: 10, slow: 30, require_cross: true` | BUY when the fast SMA crosses above the slow one, SELL when it crosses below. With `require_cross: false` it also enters whenever fast > slow |
| `rsi_mean_reversion` | `period: 14, oversold: 30, overbought: 70` | BUY when RSI < oversold, SELL when RSI > overbought |
| `bollinger_bands` | `period: 20, num_std: 2, exit_at: middle` | BUY below the lower band, SELL at the middle (or `upper`) band |
| `momentum` | `lookback: 20, entry_pct: 2, exit_pct: 0` | BUY when the trailing return > entry_pct %, SELL when it is < exit_pct % |
| `buy_and_hold` | `size_pct: null` | Buys each symbol it is given once and never sells. Pair it with `risk: {stop_loss_pct: 0, take_profit_pct: 0}` for a pure benchmark |

There are two benchmarks. The `buy_and_hold` account buys whatever the selector picks and holds it.
The **universe benchmark** is an equal-weight buy-and-hold of the whole universe from the first
tick, tracked automatically. It's the "vs buy & hold" figure on the dashboard and in `status`.

## Dashboard

**Streamlit** was chosen because it runs with one command and needs no frontend build:
`python -m papertrader dashboard` (add `--backtest` to open on the backtest DB, or switch databases
in the sidebar). It opens the SQLite DB read-only, so it works while the bot is running or stopped,
and refreshes itself every 30 s.

| Tab | Shows |
|---|---|
| Overview | Total equity, P&L, return vs the universe buy & hold, bot status (running / stopped / recovering / not running) and last heartbeat, all accounts' return (or equity) on one chart, equity CSV download |
| Accounts | Cash, equity, return, max drawdown, Sharpe, win rate, profit factor, fees, and open positions with unrealized P&L |
| Trades | Sortable table filterable by account, symbol, side and date range, with realized P&L per closing trade and the reason for each trade, plus trades CSV download |
| Decision log | Searchable, filterable timeline of every event: selections, signals, holds, skips, rejections, risk overrides, gap replays, daily summaries |
| Selection | Which symbols were selected over time, the latest ranking with scores, and the selector's forward returns vs the random baseline and the universe |
| Health | Errors, data-fetch failures and retries, startups, shutdowns, recoveries, reconciliation results, last heartbeat |

## Logging

- `logs/papertrader.jsonl` (`logs/backtest.jsonl` for backtests): one JSON object per line, rotated
  by size. For example:
  `{"logged_at": 1795528743.1, "level": "INFO", "ts": 1795528740, "type": "order_filled", "account": "sma_cross", "symbol": "ETH/USDT", "message": "BUY 0.26269 ETH/USDT filled @ 3802.9523, fee 1.00", "data": {...}}`
- Readable lines on the console (stderr).
- Every event is also stored in the `events` table, which the dashboard queries.

Event types: `startup`, `shutdown`, `recovered_from_interruption`, `reconciliation`,
`tick_started`, `tick_finished` (DEBUG), `symbols_selected`, `signal`, `hold`, `trade_skipped`,
`order_placed`, `order_filled`, `order_rejected`, `order_cancelled`, `risk_triggered`, `gap_replay`,
`data_fetch_retry`, `data_fetch_error`, `daily_summary`, `error`.

A `daily_summary` is written when the UTC date rolls over. It covers equity, the day's P&L, total
return, trade count and realized P&L per account, plus counts of notable events (risk triggers,
rejections, data errors, recoveries).

## Crash safety and recovery

The bot is built so that `kill -9`, power loss or a network drop at any instant leaves consistent
state, and a restart continues exactly where it stopped.

- **SQLite in WAL mode with `synchronous=FULL`**, so committed transactions survive power loss.
- **Atomic state changes.** A fill writes the fill row, cash, position, average cost and order
  status in one transaction. The end of a decision round writes the per-symbol bar cursors, the
  round counter and the equity snapshot in another.
- **Deterministic, idempotent orders.** The order id is `sha1(account | symbol | bar time | side)`,
  inserted with `INSERT OR IGNORE`. A symbol that already has an order for the current bar is never
  re-evaluated, and decision log entries have dedupe keys. Replaying a tick therefore can't create
  a second trade or a duplicate log entry.
- **Two-step orders.** An order is committed as `PENDING` and the fill comes in a separate
  transaction. On restart, a pending order whose bar is still the newest is filled at exactly the
  price it would have had (the fill price depends only on the stored reference price). If newer
  bars exist, it is **cancelled as stale** rather than filled at an old price.
- **Resume from cursors.** The last fully processed bar is stored per account and symbol. After
  downtime, the missed bars are fetched and processed in order, for **stop-loss / take-profit only**
  (checked against each missed bar's high and low; a gap through the stop fills at the bar's open).
  New entries are only made on the current bar. Each catch-up is logged as a `gap_replay` event.
- **Heartbeat** every tick. At startup, a previous status of `running` or `recovering` means an
  unclean shutdown. The bot then logs `recovered_from_interruption` (with the downtime), resolves
  pending orders, and **reconciles** every account: it rebuilds cash and positions from the fill
  ledger, compares them with the stored values, and compares cash + positions with the last equity
  snapshot. The result is logged as `reconciliation` (ERROR level, and visible on the Health tab, if
  anything disagrees).
- **Single instance.** An exclusive `flock` on `<db>.lock` stops a second bot from running against
  the same state. The OS releases it when the process dies, so a stale lock never blocks a restart.
- **Graceful stop.** SIGINT/SIGTERM finish the current tick and record a clean shutdown. A second
  signal forces an exit.

`tests/test_crash_recovery.py` crashes the bot mid-tick (between order placement and fill, after a
fill, and after a round), including with a real `SIGKILL` in a subprocess. It then restarts and
asserts that the final orders, fills, cash and positions are **identical to an uninterrupted run**,
with no duplicates and a clean reconciliation. You can inject the same faults yourself:
`PAPERTRADER_FAULT=after_place:kill:3 python -m papertrader run ...` kills the process right after
its 3rd order is placed and before it fills. See `papertrader/faults.py`.

## Running in the background

Use **one** of these:

```bash
# 1. Simple: a supervisor loop that restarts on crashes (stops on clean exit or a config error)
nohup deploy/supervise.sh config.yaml > logs/supervisor.log 2>&1 &

# 2. systemd (Linux): edit User/paths in the unit first
sudo cp deploy/papertrader.service /etc/systemd/system/
sudo systemctl enable --now papertrader

# 3. Docker: bot (restart: unless-stopped) + dashboard on :8501
docker compose up -d
```

Then check on it with `python -m papertrader status`, the dashboard, or
`tail -f logs/papertrader.jsonl`.

## Backtesting

```bash
python -m papertrader backtest --config config.yaml --start 2025-01-01 --end 2025-11-01
python -m papertrader dashboard --backtest
python -m papertrader report --backtest --export html     # reports/backtest_report.html
```

The backtest runs the same `Engine` (selector, strategy, risk, broker), driven by a simulated clock
that steps through every bar close in the range. Bars are revealed only once closed. Results go to
`storage.backtest_db_path`, which is wiped at the start of each backtest. `--resume` continues an
interrupted backtest through the normal recovery path. Runs are deterministic: same config and
data, same trades. A 10-month hourly backtest over 11 symbols and 3 accounts takes about a minute.

Data limits of the free sources: Yahoo serves 1m bars for about the last 30 days, 5m/15m for about
60 days and 1h for about 730 days. Crypto history depends on the exchange. Downloaded bars are
cached in `cache/bars.db`.

## Adding a strategy

Create `papertrader/strategies/my_strategy.py`. It is registered automatically:

```python
import pandas as pd
from pydantic import Field

from papertrader.models import Action, Position, Signal
from papertrader.strategies.base import STRATEGIES, Strategy, StrategyParams


class Params(StrategyParams):
    lookback: int = Field(5, ge=1)


@STRATEGIES.register("three_up_days")
class ThreeUp(Strategy):
    """Buy after N consecutive higher closes, sell after the first lower close."""

    Params = Params

    @property
    def warmup_bars(self) -> int:
        return self.p.lookback + 1

    def on_bar(self, history: pd.DataFrame, position: Position | None) -> Signal:
        c = history["close"].to_numpy()          # only closed bars up to now
        if len(c) < self.warmup_bars:
            return Signal.hold("warming up")
        ups = (c[-self.p.lookback:] > c[-self.p.lookback - 1:-1]).all()
        if position is None and ups:
            return Signal(Action.BUY, f"{self.p.lookback} higher closes in a row", strength=0.5)
        if position is not None and c[-1] < c[-2]:
            return Signal(Action.SELL, "first lower close")
        return Signal.hold("no setup")
```

Then use it in `config.yaml` as `strategy: three_up_days`, `params: {lookback: 3}`. `Signal.size_pct`
optionally requests a smaller position (fraction of equity). The risk layer caps it at
`max_position_pct`.

## Adding a selector

Create `papertrader/selection/my_selector.py`:

```python
from papertrader.selection.base import SELECTORS, Selector


@SELECTORS.register("lowest_volatility")
class LowestVolatility(Selector):
    """Prefer calm symbols."""

    def score(self, symbol, window, history, ctx):
        rets = window["close"].pct_change().dropna()
        vol = float(rets.std())
        return -vol, f"volatility {vol * 100:.2f}%/bar"   # higher score = better
```

Then set `selection.method: lowest_volatility`. The base class applies the history and liquidity
filters, ranks the symbols, keeps the top N, and writes the reasons to the log.

## Project layout

```
papertrader/
  config.py        config schema, validation, example config
  models.py        Signal, Position, Order, Fill, enums
  data/            DataFeed interface; yfinance, ccxt, synthetic, historical (backtest) feeds;
                   SQLite cache; retry/backoff; NYSE market hours
  selection/       pluggable selectors (auto-registered)
  strategies/      Strategy base class + 5 strategies (auto-registered)
  broker/          PaperBroker (cash, positions, avg cost, P&L, fees, slippage)
  risk/            sizing, stops, exposure limits, daily loss cap
  engine/          main loop, recovery/reconciliation, instance lock, clocks, backtest runner
  storage/         SQLite wrapper + versioned migrations
  reporting/       metrics, HTML/CSV export
  dashboard/       Streamlit app
  logging_setup.py JSONL + console + events table
  faults.py        fault injection for crash tests
deploy/            systemd unit, supervisor script
tests/             pytest suite
```

## Defaults and design decisions

- **Fills** happen at the decision bar's close ± slippage. Stop and take-profit exits fill at the
  trigger level, or at the bar's open if the price gapped through it. If both are touched in one
  bar, the stop is assumed to hit first (conservative).
- **Average cost includes buy fees**, so realized + unrealized P&L adds up exactly to the change in
  equity. Stops and targets are measured from this average cost.
- **Quantities**: whole shares for stocks, 6 decimals for crypto. Long-only, no leverage, no
  shorting. Orders the account can't afford are rejected and logged.
- **Accounts are isolated.** Each has its own cash, positions and selector state. Changing an
  account's strategy or params in the config keeps its cash and positions (with a warning). Use
  `reset` for a fresh start.
- **Selection includes stocks outside market hours.** They can't trade until a fresh bar arrives,
  while held positions keep being managed.
- **Daily boundaries** (daily loss cap, daily summary) use UTC days.
- **Market hours**: a hard-coded NYSE holiday list for 2024–2027. Early-close days are treated as
  full days.
- **Sharpe** is annualised from the observed bar frequency with a zero risk-free rate. Win rate and
  profit factor are computed over closed (SELL) trades.
- **No secrets.** Only public, keyless data endpoints are used and nothing needs credentials.
  `config.yaml`, `state/`, `logs/`, `cache/` and `reports/` are git-ignored.

## Tests

```bash
pytest
```

The suite covers config validation; the broker (fees, slippage, average cost, insufficient funds,
no shorting, idempotent orders); risk rules; all selectors (including seeded randomness);
strategies and indicators; metrics and reports; feeds (synthetic determinism, retry/backoff, cache
fallback, market hours, close-time gating); the no-lookahead proof; deterministic backtests;
idempotent tick replay; gap replay with stop-loss on missed bars; stale-bar protection; the
instance lock; and crash recovery (in-process faults and a real `kill -9`, compared against an
uninterrupted run). No test touches the network.
