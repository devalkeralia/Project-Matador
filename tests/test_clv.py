import pytest

from matador.clv import bootstrap_mean_ci, clv, net_pnl, summarize


def test_clv_sign():
    assert clv(0.50, 0.56) == pytest.approx(0.06)   # entered cheaper than the close -> beat it
    assert clv(0.60, 0.54) == pytest.approx(-0.06)  # close drifted against us


def test_net_pnl_win_and_loss_include_fee():
    # 100 contracts @ 0.50, fee_coeff 0.07 -> fee = 0.07*0.5*0.5*100 = 1.75
    assert net_pnl("win", 0.50, 100, 0.07) == pytest.approx(100 - 50 - 1.75)   # +48.25
    assert net_pnl("loss", 0.50, 100, 0.07) == pytest.approx(0 - 50 - 1.75)    # -51.75


def test_net_pnl_uses_exact_round_up_fee():
    # 3 contracts @ 0.50: fee 0.0525 rounds UP to 0.06 (not the 0.0525 linear approx)
    assert net_pnl("win", 0.50, 3, 0.07) == pytest.approx(3 - 1.5 - 0.06)


def test_bootstrap_ci_is_deterministic_and_ordered():
    vals = [0.02, 0.05, -0.01, 0.03, 0.04, 0.01]
    clusters = ["e1", "e1", "e2", "e2", "e3", "e3"]
    lo, hi = bootstrap_mean_ci(vals, clusters, n_boot=2000, seed=0)
    assert lo <= sum(vals) / len(vals) <= hi
    assert bootstrap_mean_ci(vals, clusters, n_boot=2000, seed=0) == (lo, hi)  # reproducible


def test_bootstrap_ci_positive_when_all_positive():
    vals = [0.05] * 40
    clusters = [f"e{i}" for i in range(40)]  # each its own cluster
    lo, _ = bootstrap_mean_ci(vals, clusters, n_boot=2000, seed=0)
    assert lo > 0  # a uniformly positive sample -> CI lower bound above zero


def test_bootstrap_ci_none_on_empty():
    assert bootstrap_mean_ci([], [], seed=0) is None


def _cfg():
    from matador.config import Config
    return Config(bankroll=1000.0, min_liquidity=10.0, max_spread=0.10)  # fee 0.07, min_effect 0.005, 30 clusters, thin 50


def _bet(**o):
    f = dict(price=0.50, fill_price=None, closing_price=None, result=None, contracts_filled=None,
             occurrence_datetime="2026-07-13T13:00:00Z", ts="2026-07-13T12:00:00Z", experience=100)
    f.update(o)
    return f


def test_summarize_hit_rate_pnl_net_clv_and_gate():
    bets = [
        _bet(price=0.50, fill_price=0.50, contracts_filled=100, closing_price=0.56, result="win", occurrence_datetime="2026-07-13T13:00Z"),
        _bet(price=0.40, fill_price=0.40, contracts_filled=50, closing_price=0.44, result="loss", occurrence_datetime="2026-07-14T13:00Z"),
        _bet(price=0.50, closing_price=0.52, occurrence_datetime="2026-07-15T13:00Z"),   # closing but no fill -> CLV only
        _bet(result="void", closing_price=0.60, occurrence_datetime="2026-07-16T13:00Z"),  # void -> excluded everywhere
        _bet(),  # nothing recorded -> ignored
    ]
    s = summarize(bets, _cfg(), seed=0)
    assert s["n_opportunities"] == 5
    assert s["n_results"] == 2 and s["wins"] == 1 and s["hit_rate"] == pytest.approx(0.5)  # void NOT counted
    assert s["total_pnl"] == pytest.approx(48.25 - 20.84)   # win +48.25, loss -(20 + 0.84 fee)
    # entry = alert price; NET clv = gross - fee*entry*(1-entry); void row excluded
    assert s["n_clv"] == 3 and s["n_clusters"] == 3
    assert s["mean_gross_clv"] == pytest.approx((0.06 + 0.04 + 0.02) / 3)
    assert s["mean_clv"] == pytest.approx(((0.06 - 0.0175) + (0.04 - 0.0168) + (0.02 - 0.0175)) / 3)
    assert s["clv_ci"] is not None and s["go_live"] is False   # 3 bets/3 days, well under 200/30
    assert s["buckets"]["mid(50-200)"]["n"] == 3               # experience 100 -> mid bucket


def test_summarize_clv_entry_is_the_alert_price_not_the_fill():
    # CLV entry must be the OBJECTIVE logged alert price, never the (subjective) recorded fill.
    bets = [_bet(price=0.50, fill_price=0.55, contracts_filled=100, closing_price=0.56, result="win")]
    s = summarize(bets, _cfg(), seed=0)
    assert s["mean_gross_clv"] == pytest.approx(0.06)  # 0.56 - 0.50 (alert), not 0.56 - 0.55 (fill)
