import pytest

from matador.config import Config
from matador.edge import evaluate_market, kelly_stake, net_edge


def _cfg(**overrides) -> Config:
    kwargs = dict(bankroll=1000.0, min_liquidity=100.0, max_spread=0.05)
    kwargs.update(overrides)
    return Config(**kwargs)


def test_net_edge_formula():
    # (p - price) - fee*price*(1-price)
    assert net_edge(0.60, 0.50, 0.07) == pytest.approx(0.10 - 0.07 * 0.25)
    # fee is smaller on a favorite than at the 0.50 peak
    assert 0.07 * 0.80 * 0.20 < 0.07 * 0.50 * 0.50


def test_kelly_stake_caps_before_converting_to_contracts():
    # A large edge would exceed the max_stake_pct cap; the cap must apply BEFORE floor-to-contracts.
    stake, contracts = kelly_stake(0.50, 0.50, bankroll=1000.0, kelly_fraction=0.25, max_stake_pct=0.05)
    assert stake == pytest.approx(50.0)   # 0.05 * 1000 cap, not the larger raw Kelly stake
    assert contracts == 100               # floor(50 / 0.50)


def test_evaluate_market_picks_the_positive_side():
    cfg = _cfg()
    r = evaluate_market(0.60, 0.45, 0.55, cfg)  # Yes is +EV, No is -EV
    assert r is not None
    assert r.side == "yes"
    assert r.net_edge >= cfg.min_net_edge
    assert r.contracts >= 1


def test_evaluate_market_evaluates_the_no_side():
    cfg = _cfg()
    # p_model low -> backing No (buy No cheap) is the value side.
    r = evaluate_market(0.30, 0.72, 0.28, cfg)
    assert r is not None and r.side == "no"


def test_evaluate_market_abstains_without_edge():
    assert evaluate_market(0.50, 0.50, 0.50, _cfg()) is None


def test_evaluate_market_abstains_above_max_price():
    # Yes would be hugely +EV but its price is above max_price; No side is -EV -> abstain.
    assert evaluate_market(0.99, 0.96, 0.04, _cfg()) is None
