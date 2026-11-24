import subprocess
import sys

import yaml

from papertrader.cli import main


def test_init_status_backtest_report_reset(tmp_path, cfg_dict, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["init", "--path", "c.yaml"]) == 0
    assert main(["init", "--path", "c.yaml"]) == 1  # refuses to overwrite
    d = cfg_dict
    d["universe"] = {"stocks": ["SPY"], "crypto": ["BTC/USDT"]}
    d["selection"]["lookback_bars"] = 10
    d["data"]["history_bars"] = 40
    (tmp_path / "c.yaml").write_text(yaml.safe_dump(d))
    assert main(["status", "-c", "c.yaml"]) == 0
    assert "no state yet" in capsys.readouterr().out
    assert main(["backtest", "-c", "c.yaml", "--start", "2025-01-01", "--end", "2025-01-04"]) == 0
    out = capsys.readouterr().out
    assert "TOTAL" in out and "universe buy & hold" in out
    assert main(["status", "-c", "c.yaml", "--backtest"]) == 0
    assert "backtest stopped" in capsys.readouterr().out
    assert main(["report", "-c", "c.yaml", "--backtest", "--export", "csv", "--out", "rep"]) == 0
    assert (tmp_path / "rep" / "backtest" / "trades.csv").exists()
    assert main(["reset", "-c", "c.yaml", "--backtest", "--yes"]) == 0
    assert not (tmp_path / d["storage"]["backtest_db_path"]).exists()


def test_invalid_config_exit_code(tmp_path, capsys):
    p = tmp_path / "bad.yaml"
    p.write_text("starting_cash: -5\nuniverse: {stocks: [A]}\naccounts: []\n")
    assert main(["status", "-c", str(p)]) == 2
    err = capsys.readouterr().err
    assert "starting_cash" in err and "accounts" in err


def test_module_entrypoint_help():
    r = subprocess.run([sys.executable, "-m", "papertrader", "--help"], capture_output=True, text=True)
    assert r.returncode == 0 and "backtest" in r.stdout
