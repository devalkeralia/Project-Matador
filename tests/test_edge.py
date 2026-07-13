import pytest

from matador.config import Config
from matador.edge import evaluate_market, kalshi_fee, kelly_stake, net_edge


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


def test_kalshi_fee_rounds_up_to_the_cent_per_order():
    assert kalshi_fee(0.50, 100, 0.07) == pytest.approx(1.75)   # 0.07*100*0.25 = 1.75 exactly
    assert kalshi_fee(0.50, 3, 0.07) == pytest.approx(0.06)     # 0.0525 -> rounds UP to 0.06
    assert kalshi_fee(0.90, 10, 0.07) == pytest.approx(0.07)    # 0.063 -> up to 0.07 (favorite)


def test_contracts_floor_no_off_by_one_at_cent_prices():
    # $50 at 0.05 must floor to 1000 contracts, not 999 (plain // under-counts on float repr).
    _, contracts = kelly_stake(1.0, 0.05, bankroll=1000.0, kelly_fraction=0.25, max_stake_pct=0.05)
    assert contracts == 1000


def test_min_price_floor_blocks_a_longshot():
    # Model loves a 6c longshot, but price < min_price (0.10) -> no bet (unreliable Elo tail).
    assert evaluate_market(0.30, 0.06, 0.94, _cfg(min_price=0.10)) is None


def test_kelly_fraction_override_haircuts_the_stake():
    cfg = _cfg()
    full = evaluate_market(0.60, 0.45, 0.55, cfg)
    half = evaluate_market(0.60, 0.45, 0.55, cfg, kelly_fraction=cfg.kelly_fraction * 0.5)
    assert half.stake < full.stake  # a smaller Kelly fraction sizes down
