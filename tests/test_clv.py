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
    f = dict(price=0.50, fill_price=None, closing_price=None, closing_source=None,
             sharp_close=None, sharp_source=None, result=None,
             contracts_filled=None, occurrence_datetime="2026-07-13T13:00:00Z",
             ts="2026-07-13T12:00:00Z", experience=100)
    f.update(o)
    return f


def test_summarize_hit_rate_pnl_net_clv_and_gate():
    # occurrence dates are a WEEK apart so they land in 3 distinct ISO-week clusters
    bets = [
        _bet(price=0.50, fill_price=0.50, contracts_filled=100, closing_price=0.56, result="win", occurrence_datetime="2026-07-01T13:00Z"),
        _bet(price=0.40, fill_price=0.40, contracts_filled=50, closing_price=0.44, result="loss", occurrence_datetime="2026-07-08T13:00Z"),
        _bet(price=0.50, closing_price=0.52, occurrence_datetime="2026-07-15T13:00Z"),   # closing but no fill -> CLV only
        _bet(result="void", closing_price=0.60, occurrence_datetime="2026-07-16T13:00Z"),  # void -> excluded everywhere
        _bet(),  # nothing recorded -> ignored
    ]
    s = summarize(bets, _cfg(), seed=0)
    assert s["n_opportunities"] == 5
    assert s["n_results"] == 2 and s["wins"] == 1 and s["hit_rate"] == pytest.approx(0.5)  # void NOT counted
    assert s["total_pnl"] == pytest.approx(48.25 - 20.84)   # win +48.25, loss -(20 + 0.84 fee)
    # entry = alert price; NET clv = gross - fee*entry*(1-entry); void row excluded
    assert s["n_clv"] == 3 and s["n_clusters"] == 3          # 3 distinct ISO weeks
    assert s["mean_gross_clv"] == pytest.approx((0.06 + 0.04 + 0.02) / 3)
    assert s["mean_clv"] == pytest.approx(((0.06 - 0.0175) + (0.04 - 0.0168) + (0.02 - 0.0175)) / 3)
    assert s["clv_ci"] is not None and s["go_live"] is False   # 3 bets/3 weeks, well under 200/12
    assert s["buckets"]["mid(50-200)"]["n"] == 3               # experience 100 -> mid bucket


def _bet_in_week(week_idx, **o):
    from datetime import date
    monday = date.fromisocalendar(2026, week_idx + 1, 1).isoformat()  # Monday of a distinct ISO week
    return _bet(occurrence_datetime=f"{monday}T12:00:00Z", **o)


def test_summarize_go_live_true_path_and_cogates():
    # TRUE go-live path: 240 bets / 12 ISO weeks, SHARP close 0.78 vs entry 0.70 (sharp net ~+6.5c >
    # 1.5c bar), Kalshi close too, wins recorded (ROI>0), full sharp coverage, 0 missed -> MET.
    # Then flip each binding co-gate and confirm it blocks. The BINDING metric is the SHARP track.
    def sample(result="win", sharp=True, extra_missed=0, extra_kalshi_only=0):
        bets = [_bet_in_week(i % 12, price=0.70, closing_price=0.76,
                             sharp_close=(0.78 if sharp else None), sharp_source=("pinnacle" if sharp else None),
                             closing_source="auto", fill_price=0.70, contracts_filled=1, result=result)
                for i in range(240)]
        bets += [_bet_in_week(0, closing_source="missed:late[auto]") for _ in range(extra_missed)]
        # Kalshi-close-only bets: a valid Kalshi CLV but NO sharp ref -> dilute sharp coverage.
        bets += [_bet_in_week(i % 12, price=0.70, closing_price=0.76, closing_source="auto",
                              fill_price=0.70, contracts_filled=1, result="win") for i in range(extra_kalshi_only)]
        return bets

    met = summarize(sample(), _cfg(), seed=0)
    assert met["n_sharp"] == 240 and met["n_sharp_clusters"] == 12
    assert met["sharp_coverage"] == pytest.approx(1.0) and met["sharp_ci"][0] > _cfg().min_effect_size
    assert met["sharp_sources"] == {"pinnacle": 240, "consensus": 0}
    assert met["roi"] > 0 and met["go_live"] is True                       # all co-gates clear

    assert summarize(sample(sharp=False), _cfg(), seed=0)["go_live"] is False   # NO sharp ref -> gate can't pass
    assert summarize(sample(result="loss"), _cfg(), seed=0)["go_live"] is False # realized net-ROI < 0
    assert summarize(sample(extra_missed=130), _cfg(), seed=0)["go_live"] is False  # missed-rate co-gate (35% > 30%)
    low = summarize(sample(extra_kalshi_only=300), _cfg(), seed=0)          # 240 sharp / 540 closed = 44% < 50%
    assert low["n_sharp"] == 240 and low["sharp_coverage"] < _cfg().min_sharp_coverage and low["go_live"] is False


def test_summarize_tallies_capture_health():
    bets = [
        _bet(closing_source="auto", closing_price=0.52),
        _bet(closing_source="manual", closing_price=0.53),
        _bet(closing_source="missed:late[auto]"),      # missed -> no closing_price
        _bet(closing_source="missed:no_two_sided_book[manual]"),
        _bet(),                                          # never attempted -> uncounted
    ]
    caps = summarize(bets, _cfg(), seed=0)["captures"]
    assert caps == {"auto": 1, "manual": 1, "missed": 2}


def test_summarize_clv_entry_is_the_alert_price_not_the_fill():
    # CLV entry must be the OBJECTIVE logged alert price, never the (subjective) recorded fill.
    bets = [_bet(price=0.50, fill_price=0.55, contracts_filled=100, closing_price=0.56, result="win")]
    s = summarize(bets, _cfg(), seed=0)
    assert s["mean_gross_clv"] == pytest.approx(0.06)  # 0.56 - 0.50 (alert), not 0.56 - 0.55 (fill)
