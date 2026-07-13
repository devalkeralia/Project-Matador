import pytest

from matador.clv import bootstrap_mean_ci, clv, net_pnl, summarize


def test_clv_sign():
    assert clv(0.50, 0.56) == pytest.approx(0.06)   # entered cheaper than the close -> beat it
    assert clv(0.60, 0.54) == pytest.approx(-0.06)  # close drifted against us


def test_net_pnl_win_and_loss_include_fee():
    # 100 contracts @ 0.50, fee_coeff 0.07 -> fee = 0.07*0.5*0.5*100 = 1.75
    assert net_pnl("win", 0.50, 100, 0.07) == pytest.approx(100 - 50 - 1.75)   # +48.25
    assert net_pnl("loss", 0.50, 100, 0.07) == pytest.approx(0 - 50 - 1.75)    # -51.75


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


def _bet(**o):
    f = dict(price=0.50, fill_price=None, closing_price=None, result=None,
             contracts_filled=None, event_ticker="E1", market_ticker="E1-A")
    f.update(o)
    return f


def test_summarize_hit_rate_pnl_clv_and_go_live_gate():
    bets = [
        # settled win with a fill + captured close
        _bet(fill_price=0.50, contracts_filled=100, closing_price=0.56, result="win", event_ticker="E1"),
        # settled loss
        _bet(fill_price=0.40, contracts_filled=50, closing_price=0.44, result="loss", event_ticker="E2"),
        # closing captured but no fill -> CLV uses the alert price (0.50), no P&L contribution
        _bet(price=0.50, closing_price=0.52, event_ticker="E3"),
        # nothing yet -> ignored by every metric
        _bet(event_ticker="E4"),
    ]
    s = summarize(bets, fee_coefficient=0.07, seed=0)
    assert s["n_opportunities"] == 4
    assert s["n_results"] == 2 and s["wins"] == 1 and s["hit_rate"] == pytest.approx(0.5)
    # P&L: win +48.25, loss: 0 - 20 - 0.07*0.4*0.6*50(=0.84) = -20.84  -> total 27.41
    assert s["total_pnl"] == pytest.approx(48.25 - 20.84)
    assert s["n_clv"] == 3                                  # three rows had a closing price
    assert s["mean_clv"] == pytest.approx((0.06 + 0.04 + 0.02) / 3)
    assert s["clv_ci"] is not None
    assert s["go_live"] is False                            # only 3 CLV obs, well under 200
