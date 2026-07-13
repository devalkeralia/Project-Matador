import pytest

from matador.config import Config, load_config


def base_kwargs(**overrides):
    kwargs = dict(bankroll=2000.0, min_liquidity=100.0, max_spread=0.05)
    kwargs.update(overrides)
    return kwargs


def test_valid_config_loads_with_defaults():
    cfg = Config(**base_kwargs())
    assert cfg.bankroll == 2000.0
    assert cfg.kelly_fraction == 0.25
    assert cfg.max_stake_pct == 0.05
    assert cfg.min_net_edge == 0.03
    assert cfg.min_matches == 20
    assert cfg.max_price == 0.95
    assert cfg.fee_coefficient == 0.07
    assert cfg.tours == ["ATP", "WTA"]
    assert cfg.series.atp == "KXATPMATCH"
    assert cfg.series.wta is None
    assert cfg.min_price == 0.10  # favorite floor on by default (blocks deep longshots)


def test_rejects_nonpositive_bankroll():
    with pytest.raises(ValueError):
        Config(**base_kwargs(bankroll=0))
    with pytest.raises(ValueError):
        Config(**base_kwargs(bankroll=-100))


def test_rejects_kelly_fraction_out_of_range():
    with pytest.raises(ValueError):
        Config(**base_kwargs(kelly_fraction=1.5))
    with pytest.raises(ValueError):
        Config(**base_kwargs(kelly_fraction=0))


def test_rejects_max_stake_pct_out_of_range():
    with pytest.raises(ValueError):
        Config(**base_kwargs(max_stake_pct=1.5))


def test_rejects_negative_min_net_edge():
    with pytest.raises(ValueError):
        Config(**base_kwargs(min_net_edge=-0.01))


def test_rejects_max_price_out_of_range():
    with pytest.raises(ValueError):
        Config(**base_kwargs(max_price=1.0))
    with pytest.raises(ValueError):
        Config(**base_kwargs(max_price=0.0))


def test_rejects_min_price_at_or_above_max_price():
    with pytest.raises(ValueError):
        Config(**base_kwargs(min_price=0.95, max_price=0.95))
    with pytest.raises(ValueError):
        Config(**base_kwargs(min_price=0.96, max_price=0.95))


def test_accepts_min_price_below_max_price():
    cfg = Config(**base_kwargs(min_price=0.5, max_price=0.95))
    assert cfg.min_price == 0.5


def test_rejects_negative_fee_coefficient():
    with pytest.raises(ValueError):
        Config(**base_kwargs(fee_coefficient=-0.01))


def test_rejects_negative_liquidity_or_spread():
    with pytest.raises(ValueError):
        Config(**base_kwargs(min_liquidity=-1))
    with pytest.raises(ValueError):
        Config(**base_kwargs(max_spread=-1))


def test_load_config_from_yaml(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        """
bankroll: 1500
min_liquidity: 50
max_spread: 0.04
series:
  atp: KXATPMATCH
  wta: null
"""
    )
    cfg = load_config(path)
    assert cfg.bankroll == 1500
    assert cfg.series.wta is None


def test_load_real_config_example():
    cfg = load_config("config.example.yaml")
    assert cfg.bankroll == 2000.0
    assert cfg.series.wta == "KXWTAMATCH"  # confirmed via scripts/probe.py


def test_config_has_default_artifact_and_db_paths():
    cfg = Config(**base_kwargs())
    assert cfg.model_path.endswith("model.json")
    assert cfg.db_path.endswith(".db")


def test_elo_config_rejects_pathological_hyperparams():
    from matador.config import EloConfig

    with pytest.raises(ValueError):
        EloConfig(surface_weight=1.5)
    with pytest.raises(ValueError):
        EloConfig(k_num=0)          # K numerator must be > 0
    with pytest.raises(ValueError):
        EloConfig(k_shift=0)        # n + k_shift is the divisor at n=0 -> would be 0
    with pytest.raises(ValueError):
        EloConfig(max_staleness_days=-1)
