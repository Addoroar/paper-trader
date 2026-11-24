import copy

import pytest

from papertrader.config import ConfigError, load_config, parse_config


def test_example_config_is_valid(cfg):
    assert cfg.timeframe == "1h"
    assert "BTC/USDT" in cfg.universe.symbols()
    assert cfg.risk_for("benchmark").stop_loss_pct == 0
    assert cfg.risk_for("sma_cross").stop_loss_pct == 5


def _err(d) -> str:
    with pytest.raises(ConfigError) as e:
        parse_config(d, "config.yaml")
    return str(e.value)


def test_unknown_strategy(cfg_dict):
    d = copy.deepcopy(cfg_dict)
    d["accounts"][0]["strategy"] = "nope"
    msg = _err(d)
    assert "strategy 'nope' is unknown" in msg and "sma_crossover" in msg


def test_bad_strategy_params(cfg_dict):
    d = copy.deepcopy(cfg_dict)
    d["accounts"][0]["params"] = {"fast": 50, "slow": 30}
    assert "fast must be smaller than slow" in _err(d)
    d["accounts"][0]["params"] = {"fast": 5, "slow": 30, "bogus": 1}
    assert "bogus" in _err(d)


def test_allocation_exceeds_cash(cfg_dict):
    d = copy.deepcopy(cfg_dict)
    d["accounts"][0]["allocation"] = 99999
    assert "exceeds starting_cash" in _err(d)


def test_unknown_key_and_bad_values(cfg_dict):
    d = copy.deepcopy(cfg_dict)
    d["risk"]["stop_loss_pctt"] = 3
    d["timeframe"] = "2h"
    msg = _err(d)
    assert "risk.stop_loss_pctt: unknown option" in msg
    assert "timeframe" in msg


def test_crypto_symbol_format_and_selector(cfg_dict):
    d = copy.deepcopy(cfg_dict)
    d["universe"]["crypto"] = ["BTCUSDT"]
    assert "BASE/QUOTE" in _err(d)
    d = copy.deepcopy(cfg_dict)
    d["selection"]["method"] = "magic"
    assert "selection.method 'magic' is unknown" in _err(d)


def test_exclude_and_duplicate_accounts(cfg_dict):
    d = copy.deepcopy(cfg_dict)
    d["universe"]["exclude"] = ["TSLA"]
    assert "TSLA" not in parse_config(d).universe.symbols()
    d["accounts"][1]["name"] = d["accounts"][0]["name"]
    assert "unique" in _err(d)


def test_missing_file_and_yaml_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")
    p = tmp_path / "bad.yaml"
    p.write_text("starting_cash: [1, 2\n")
    with pytest.raises(ConfigError, match="YAML syntax error"):
        load_config(p)
