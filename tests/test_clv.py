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


def test_bca_adjusts_the_interval_on_right_skewed_clv_and_is_pinned():
    """The bias-correction + acceleration must actually RUN, and its numbers are pinned.

    Why pinned: replacing clv._NORM with a garbage stub (inv_cdf=-99, cdf=0.999) used to pass the
    whole suite, because every other call reaches the <4-cluster or degenerate fallback -- so the
    machinery that produces the interval authorizing real money was dead code under test. BCa exists
    precisely because CLV is right-skewed (a few big winners), which is what this fixture is.

    A golden value catches any sign/transcription change in z0 or the acceleration. It CANNOT catch a
    conceptual error shared by the implementation and this expectation -- for that, the guard is the
    'differs from the plain percentile' assertion plus the derivation in bootstrap_mean_ci's docstring.
    """
    big = [0.05, 0.10, 0.20, 0.35, 0.06, 0.12, 0.25, 0.40]   # one big winner per week, varying size
    vals, clusters = [], []
    for w, b in enumerate(big):
        for j in range(5):
            vals.append(0.002 + 0.001 * j)       # a mass of near-zero CLVs ...
            clusters.append(f"2026-W{w + 1:02d}")
        vals.append(b)                           # ... plus the skew
        clusters.append(f"2026-W{w + 1:02d}")

    bca = bootstrap_mean_ci(vals, clusters, n_boot=2000, seed=0)

    import matador.clv as clv_mod
    saved = clv_mod._BCA_MIN_CLUSTERS
    clv_mod._BCA_MIN_CLUSTERS = 99               # force the plain-percentile path, SAME rng stream
    try:
        plain = bootstrap_mean_ci(vals, clusters, n_boot=2000, seed=0)
    finally:
        clv_mod._BCA_MIN_CLUSTERS = saved

    assert bca != plain and bca[0] != plain[0] and bca[1] != plain[1]  # the correction moved BOTH bounds
    assert bca[0] == pytest.approx(0.022708, abs=1e-6)                 # golden: 8 clusters, seed 0, n_boot 2000
    assert bca[1] == pytest.approx(0.050625, abs=1e-6)


def _cfg():
    from matador.config import Config
    return Config(bankroll=1000.0, min_liquidity=10.0, max_spread=0.10)  # fee 0.07, min_effect 0.015, 12 ISO weeks, thin 50


def _bet(**o):
    f = dict(price=0.50, fill_price=None, closing_price=None, closing_source=None,
             sharp_close=None, sharp_source=None, result=None,
             contracts_filled=None, occurrence_datetime="2026-07-13T13:00:00Z",
             ts="2026-07-13T12:00:00Z", experience=100, staleness=3)
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


def test_summarize_consensus_does_not_gate_but_is_reported():
    # 240 CONSENSUS-sourced sharp rows with a strong CLV over 12 weeks must NOT go live (a soft-book
    # median isn't a sharp line); the identical sample sourced 'pinnacle' DOES.
    def sample(source):
        return [_bet_in_week(i % 12, price=0.70, closing_price=0.76, sharp_close=0.78, sharp_source=source,
                             closing_source="auto", fill_price=0.70, contracts_filled=1, result="win") for i in range(240)]
    con = summarize(sample("consensus"), _cfg(), seed=0)
    assert con["n_consensus"] == 240 and con["mean_consensus_clv"] is not None
    assert con["n_sharp"] == 0 and con["go_live"] is False        # consensus alone never satisfies the gate
    pin = summarize(sample("pinnacle"), _cfg(), seed=0)
    assert pin["n_sharp"] == 240 and pin["go_live"] is True


def test_summarize_segments_sharp_clv_by_staleness():
    # The pre-registered decay instrument: layoff segmentation must be on the SHARP (binding) track.
    # Fresh rows beat the sharp close (+8c before fee); the layoff rows lose to it (-8c).
    fresh = [_bet_in_week(i % 4, price=0.70, sharp_close=0.78, sharp_source="pinnacle",
                          closing_source="auto", staleness=2) for i in range(6)]
    stale = [_bet_in_week(i % 4, price=0.70, sharp_close=0.62, sharp_source="pinnacle",
                          closing_source="auto", staleness=95) for i in range(4)]
    s = summarize(fresh + stale, _cfg(), seed=0)
    seg = s["sharp_by_staleness"]
    assert seg["fresh(<14d)"]["n"] == 6 and seg["fresh(<14d)"]["mean_sharp_clv"] > 0
    assert seg["layoff(60d+)"]["n"] == 4 and seg["layoff(60d+)"]["mean_sharp_clv"] < 0
    assert "recent(14-29d)" not in seg and "layoff(30-59d)" not in seg   # no empty buckets emitted


def test_summarize_staleness_segmentation_handles_unknown():
    # pre-instrumentation rows (staleness NULL) must bucket as 'unknown', not crash or count as fresh
    bets = [_bet_in_week(0, price=0.70, sharp_close=0.78, sharp_source="pinnacle",
                         closing_source="auto", staleness=None)]
    seg = summarize(bets, _cfg(), seed=0)["sharp_by_staleness"]
    assert seg["unknown"]["n"] == 1 and "fresh(<14d)" not in seg


def test_summarize_sharp_only_rows_count_toward_coverage_not_missed():
    # a pinnacle ref with NO Kalshi close (thin book -> sharp_only) still counts as a closed, pinnacle-covered bet
    bets = [_bet_in_week(i % 12, price=0.70, closing_price=None, sharp_close=0.78, sharp_source="pinnacle",
                         closing_source="sharp_only:auto") for i in range(3)]
    s = summarize(bets, _cfg(), seed=0)
    assert s["n_sharp"] == 3 and s["sharp_coverage"] == pytest.approx(1.0)   # 3 pinnacle / 3 closed
    assert s["n_clv"] == 0                                                    # no Kalshi close
    assert s["captures"]["sharp_only"] == 3 and s["captures"]["missed"] == 0  # sharp_only is not a miss


def test_summarize_tallies_capture_health():
    bets = [
        _bet(closing_source="auto", closing_price=0.52),
        _bet(closing_source="manual", closing_price=0.53),
        _bet(closing_source="missed:late[auto]"),      # missed -> no closing_price
        _bet(closing_source="missed:no_two_sided_book[manual]"),
        _bet(),                                          # never attempted -> uncounted
    ]
    caps = summarize(bets, _cfg(), seed=0)["captures"]
    assert caps == {"auto": 1, "manual": 1, "sharp_only": 0, "missed": 2}


def test_summarize_clv_entry_is_the_alert_price_not_the_fill():
    # CLV entry must be the OBJECTIVE logged alert price, never the (subjective) recorded fill.
    bets = [_bet(price=0.50, fill_price=0.55, contracts_filled=100, closing_price=0.56, result="win")]
    s = summarize(bets, _cfg(), seed=0)
    assert s["mean_gross_clv"] == pytest.approx(0.06)  # 0.56 - 0.50 (alert), not 0.56 - 0.55 (fill)


# ---- the go-live gate at its boundaries (the one boolean the whole run exists to produce) ----

_FEE = 0.07


def _gate_sample(*, n_sharp=210, n_weeks=12, net=0.04, n_kalshi_only=30, fills=True, entry=0.50):
    """A near-miss go-live sample where the two CLV tracks DIVERGE (n_sharp != n_clv).

    Divergence is the point: in every earlier gate test the tracks were perfectly aliased, so a gate
    that counted the CIRCULAR Kalshi-close rows instead of the Pinnacle rows was indistinguishable
    from the real one. `net` is the per-bet SHARP net CLV, so sharp_close is derived by adding back
    the entry-fee drag the metric subtracts.
    """
    from datetime import date, timedelta
    drag = _FEE * entry * (1.0 - entry)
    monday = date(2026, 1, 5)
    weeks = [(monday + timedelta(days=7 * i)).isoformat() for i in range(n_weeks)]
    bets = []
    for i in range(n_sharp):
        jitter = net + ((i % 5) - 2) * 0.004        # varied CLVs -> the bootstrap does real work
        close = entry + jitter + drag
        # A winning majority of recorded fills: at 50c the round-up fee makes a 50/50 book ROI-negative,
        # and roi >= 0 is a hard co-gate, so an even split would fail the base case for the wrong reason.
        fill = dict(fill_price=entry, contracts_filled=10, result="loss" if i % 5 == 0 else "win") if fills and i < 20 else {}
        bets.append(_bet(price=entry, sharp_close=close, sharp_source="pinnacle",
                         closing_price=close, closing_source="auto",
                         occurrence_datetime=f"{weeks[i % n_weeks]}T13:00:00Z", **fill))
    for i in range(n_kalshi_only):                  # Kalshi-closed, NO sharp ref -> inflates n_clv only
        bets.append(_bet(price=entry, closing_price=entry + 0.10, closing_source="auto",
                         occurrence_datetime=f"{weeks[i % n_weeks]}T13:00:00Z"))
    return bets


def test_go_live_true_on_a_qualifying_sample():
    """The anchor. Without a sample that genuinely PASSES, every False assertion below is vacuous."""
    s = summarize(_gate_sample(), _cfg(), seed=0)
    assert s["go_live"] is True
    assert s["n_sharp"] == 210 and s["n_clv"] == 240      # the tracks DIVERGE
    assert s["n_sharp_clusters"] == 12 and s["roi"] > 0


@pytest.mark.parametrize("label,kwargs,expect", [
    # 199 Pinnacle bets, but 229 Kalshi-closed rows: fails ONLY on the sharp floor. Kills both
    # "delete the >=200 floor" and "count the circular Kalshi rows instead".
    ("one bet short of the sharp floor", dict(n_sharp=199), dict(n_sharp=199, n_clv=229)),
    # Positive sharp CLV but under the 1.5c effect bar -- the CI lower bound stays ABOVE zero, so a
    # gate weakened to "> 0" would pass this. This is the lucky-looking near miss the bar exists for.
    ("positive but under the effect bar", dict(net=0.008), dict(n_sharp=210)),
    ("one week short of the cluster floor", dict(n_weeks=11), dict(n_sharp_clusters=11)),
    ("no recorded fills -> roi is None", dict(fills=False), dict(roi=None)),
])
def test_go_live_false_at_each_boundary(label, kwargs, expect):
    s = summarize(_gate_sample(**kwargs), _cfg(), seed=0)
    assert s["go_live"] is False, label
    for key, want in expect.items():
        assert s[key] == want, f"{label}: {key}"
    if "net" in kwargs:                    # the near-miss is genuinely positive, just not big enough
        assert 0.0 < s["sharp_ci"][0] < _cfg().min_effect_size
