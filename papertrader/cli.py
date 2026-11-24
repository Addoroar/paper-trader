"""Command-line interface: ``python -m papertrader <command>``.

Commands: init, run, backtest, dashboard, status, report, reset.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from papertrader.config import EXAMPLE_CONFIG, Config, ConfigError, load_config

DEFAULT_CONFIG = "config.yaml"


def _cfg(args) -> Config:
    return load_config(args.config)


def _db_path(cfg: Config, backtest: bool) -> Path:
    return Path(cfg.storage.backtest_db_path if backtest else cfg.storage.db_path)


# ---------------------------------------------------------------- commands
def cmd_init(args) -> int:
    """Write an example config."""
    p = Path(args.path)
    if p.exists() and not args.force:
        print(f"{p} already exists (use --force to overwrite)")
        return 1
    p.write_text(EXAMPLE_CONFIG)
    print(f"wrote {p}. Edit it, then: python -m papertrader run --config {p}")
    return 0


def cmd_run(args) -> int:
    """Live paper trading; resumes automatically from the saved state."""
    from papertrader.data import build_feed
    from papertrader.engine import Engine
    from papertrader.engine.lock import InstanceLock, LockError
    from papertrader.logging_setup import setup_logging
    from papertrader.storage.db import Database

    cfg = _cfg(args)
    lg = cfg.logging
    setup_logging(lg.dir, lg.level, max_bytes=lg.max_bytes, backup_count=lg.backup_count)
    lock = InstanceLock(cfg.storage.db_path)
    try:
        lock.acquire()
    except LockError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    try:
        db = Database(cfg.storage.db_path)
        engine = Engine(cfg, db, build_feed(cfg, args.feed), mode="live")
        engine.run_forever()
        db.close()
    finally:
        lock.release()
    return 0


def cmd_backtest(args) -> int:
    """Fast-forward the same engine over historical data into a separate DB."""
    from papertrader.engine.backtest import parse_date, run_backtest
    from papertrader.engine.lock import InstanceLock, LockError
    from papertrader.logging_setup import setup_logging
    from papertrader.storage.db import Database

    cfg = _cfg(args)
    lg = cfg.logging
    setup_logging(lg.dir, lg.level, filename="backtest.jsonl", max_bytes=lg.max_bytes,
                  backup_count=lg.backup_count, console_level="ERROR" if not args.verbose else lg.level)
    start, end = parse_date(args.start), parse_date(args.end)
    if end <= start:
        print("error: --end must be after --start", file=sys.stderr)
        return 2
    feed = args.feed or cfg.data.feed
    print(f"Backtesting {len(cfg.accounts)} account(s) on {len(cfg.universe.symbols())} symbols, "
          f"{cfg.timeframe} bars, {args.start} -> {args.end}, data: {feed}")
    try:
        with InstanceLock(cfg.storage.backtest_db_path):
            r = run_backtest(cfg, start, end, feed=args.feed, resume=args.resume, progress=True)
    except LockError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    empty = [s for s, n in r["bars"].items() if n == 0]
    if len(empty) == len(r["bars"]):
        print("error: no market data could be loaded for any symbol (see logs/backtest.jsonl). If the data sources "
              "are unreachable from this machine, try --feed synthetic for an offline demo.", file=sys.stderr)
        return 1
    if empty:
        print(f"warning: no data for {', '.join(empty)} (see logs/backtest.jsonl)")
    print(f"Processed {r['ticks']} bars in {r['seconds']:.1f}s -> {r['db_path']}\n")
    db = Database(r["db_path"], readonly=True)
    _print_summary(db)
    _print_selection(db)
    print("\nInspect: python -m papertrader dashboard --backtest    "
          "|  python -m papertrader report --backtest --export html")
    return 0


def cmd_dashboard(args) -> int:
    """Launch the Streamlit dashboard."""
    app = Path(__file__).parent / "dashboard" / "app.py"
    cmd = [sys.executable, "-m", "streamlit", "run", str(app), "--server.port", str(args.port),
           "--server.headless", "true", "--browser.gatherUsageStats", "false", "--", "--config", args.config]
    if args.backtest:
        cmd.append("--backtest")
    print(f"Dashboard: http://localhost:{args.port}  (Ctrl+C to stop)")
    try:
        return subprocess.call(cmd)
    except KeyboardInterrupt:
        return 0


def cmd_status(args) -> int:
    """Quick summary of bot status and accounts."""
    from papertrader.storage.db import Database

    cfg = _cfg(args)
    path = _db_path(cfg, args.backtest)
    if not path.exists():
        print(f"no state yet at {path}. Start with: python -m papertrader run --config {args.config}")
        return 0
    db = Database(path, readonly=True)
    st = bot_state(db, cfg.heartbeat_stale_seconds)
    print(f"Bot: {st['label']}  |  last heartbeat: {st['heartbeat_age']}  |  restarts/recoveries: {st['restarts']}"
          f"  |  last tick: {st['last_tick']}")
    _print_summary(db)
    pos = db.query("SELECT account, symbol, qty, avg_cost FROM positions ORDER BY account, symbol")
    if pos:
        print("\nOpen positions:")
        for p in pos:
            print(f"  {p['account']:<16} {p['symbol']:<10} qty {p['qty']:<12g} avg cost {p['avg_cost']:.4f}")
    errs = db.query("SELECT ts, type, message FROM events WHERE level IN ('ERROR','WARNING') ORDER BY id DESC LIMIT 5")
    if errs:
        print("\nRecent warnings/errors:")
        for e in errs:
            print(f"  {_ts(e['ts'])} {e['type']}: {e['message'][:140]}")
    return 0


def cmd_report(args) -> int:
    """Print metrics; optionally export HTML or CSV."""
    from papertrader.reporting.export import export_csv, export_html
    from papertrader.storage.db import Database

    cfg = _cfg(args)
    path = _db_path(cfg, args.backtest)
    if not path.exists():
        print(f"no state at {path}")
        return 1
    db = Database(path, readonly=True)
    _print_summary(db)
    _print_selection(db)
    out = Path(args.out)
    if args.export == "html":
        p = export_html(db, out / ("backtest_report.html" if args.backtest else "report.html"),
                        title="papertrader backtest report" if args.backtest else "papertrader report")
        print(f"\nwrote {p}")
    elif args.export == "csv":
        for p in export_csv(db, out / ("backtest" if args.backtest else "live")):
            print(f"wrote {p}")
    return 0


def cmd_reset(args) -> int:
    """Delete the state database (live, or backtest with --backtest)."""
    from papertrader.engine.backtest import wipe_db
    from papertrader.engine.lock import InstanceLock

    cfg = _cfg(args)
    path = _db_path(cfg, args.backtest)
    if InstanceLock(path).is_locked():
        print(f"error: a papertrader instance is running against {path}; stop it first", file=sys.stderr)
        return 2
    if not args.yes:
        ans = input(f"Permanently delete all simulated state in {path}? Type 'yes' to confirm: ")
        if ans.strip().lower() != "yes":
            print("aborted")
            return 1
    wipe_db(path)
    print(f"deleted {path}")
    return 0


# ---------------------------------------------------------------- helpers
def _ts(ts) -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if ts else "-"


def bot_state(db, stale_seconds: float) -> dict:
    """Interpret ``bot_status``: running / stopped / recovering / crashed (stale heartbeat)."""
    r = db.one("SELECT * FROM bot_status WHERE id=1")
    if r is None:
        return {"label": "never started", "state": "never", "heartbeat_age": "-", "restarts": 0, "last_tick": "-",
                "heartbeat_at": None, "mode": "-"}
    age = time.time() - (r["heartbeat_at"] or 0)
    label = r["state"]
    if r["state"] in ("running", "recovering") and r["mode"] == "live" and age > stale_seconds:
        label = f"not running (stale heartbeat; last state '{r['state']}', will recover on restart)"
    elif r["mode"] == "backtest":
        label = f"backtest {r['state']}"
    return {"label": label, "state": r["state"], "heartbeat_age": f"{age:,.0f}s ago", "restarts": r["restarts"],
            "last_tick": _ts(r["last_tick_ts"]), "heartbeat_at": r["heartbeat_at"], "mode": r["mode"]}


def _print_summary(db) -> None:
    from papertrader.reporting.metrics import account_summary, benchmark_return_pct

    s = account_summary(db)
    if s.empty:
        print("no accounts yet")
        return
    print(f"{'account':<16}{'strategy':<20}{'equity':>11}{'return':>9}{'maxDD':>8}{'sharpe':>8}{'win%':>7}"
          f"{'PF':>6}{'closed':>7}{'fees':>8}{'pos':>5}")
    for r in s.itertuples():
        pf = "-" if r.profit_factor is None or r.profit_factor != r.profit_factor else (
            "inf" if r.profit_factor == float("inf") else f"{r.profit_factor:.2f}")
        win = "-" if r.win_rate_pct is None or r.win_rate_pct != r.win_rate_pct else f"{r.win_rate_pct:.0f}"
        print(f"{r.account:<16}{r.strategy:<20}{r.equity:>11,.2f}{r.return_pct:>+8.2f}%{r.max_drawdown_pct:>7.1f}%"
              f"{r.sharpe:>8.2f}{win:>7}{pf:>6}{r.trades:>7}{r.fees:>8.2f}{r.open_positions:>5}")
    print("(closed = round trips closed by a SELL; win%/PF are computed over those)")
    b = benchmark_return_pct(db)
    total_eq, total_alloc = s["equity"].sum(), s["allocation"].sum()
    print(f"{'TOTAL':<36}{total_eq:>11,.2f}{(total_eq / total_alloc - 1) * 100:>+8.2f}%"
          + (f"   (universe buy & hold: {b:+.2f}%)" if b is not None else ""))


def _print_selection(db) -> None:
    from papertrader.reporting.metrics import selection_performance

    sp = selection_performance(db)
    if sp.empty:
        return
    print("\nSelector vs random baseline (mean forward return of picks per rebalance period):")
    for acct, g in sp.groupby("account"):
        parts = [f"{r.kind}={r.avg_period_return_pct:+.3f}%" for r in g.itertuples()]
        print(f"  {acct:<16} " + "  ".join(parts) + f"  ({int(g['periods'].max())} periods)")


# ---------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="papertrader", description="Autonomous paper-trading bot (simulation only).")
    sub = p.add_subparsers(dest="command", required=True)

    def with_config(sp):
        sp.add_argument("--config", "-c", default=DEFAULT_CONFIG, help="path to config.yaml")
        return sp

    s = sub.add_parser("init", help="create an example config")
    s.add_argument("--path", default=DEFAULT_CONFIG)
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_init)

    s = with_config(sub.add_parser("run", help="live paper trading (resumes automatically)"))
    s.add_argument("--feed", choices=["auto", "synthetic"], help="override data.feed")
    s.set_defaults(func=cmd_run)

    s = with_config(sub.add_parser("backtest", help="run over historical data into a separate DB"))
    s.add_argument("--start", required=True, help="YYYY-MM-DD (UTC)")
    s.add_argument("--end", required=True, help="YYYY-MM-DD (UTC)")
    s.add_argument("--feed", choices=["auto", "synthetic"], help="override data.feed")
    s.add_argument("--resume", action="store_true", help="continue an interrupted backtest instead of restarting")
    s.add_argument("--verbose", "-v", action="store_true", help="print every event to the console")
    s.set_defaults(func=cmd_backtest)

    s = with_config(sub.add_parser("dashboard", help="launch the web dashboard (Streamlit)"))
    s.add_argument("--port", type=int, default=8501)
    s.add_argument("--backtest", action="store_true", help="open on the backtest DB")
    s.set_defaults(func=cmd_dashboard)

    s = with_config(sub.add_parser("status", help="quick CLI summary"))
    s.add_argument("--backtest", action="store_true")
    s.set_defaults(func=cmd_status)

    s = with_config(sub.add_parser("report", help="metrics; optional HTML/CSV export"))
    s.add_argument("--export", choices=["html", "csv"])
    s.add_argument("--out", default="reports")
    s.add_argument("--backtest", action="store_true")
    s.set_defaults(func=cmd_report)

    s = with_config(sub.add_parser("reset", help="wipe state (asks for confirmation)"))
    s.add_argument("--yes", action="store_true", help="confirm deletion")
    s.add_argument("--backtest", action="store_true", help="wipe the backtest DB instead of live state")
    s.set_defaults(func=cmd_reset)
    return p


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except ConfigError as e:
        print(str(e), file=sys.stderr)
        return 2
