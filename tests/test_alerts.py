from matador.alerts import (
    _compact, format_abstain, format_alert, format_close, format_find, format_no_alert,
    format_recent, format_result, format_scan, format_stats,
)
from matador.engine import Diagnostics, MatchInfo, Opportunity
from matador.storage import connect, init_db, insert_opportunity, recent_opportunities


def _opp(**overrides) -> Opportunity:
    fields = dict(
        ts="2026-07-04T12:00:00+00:00", tour="ATP", event="Wimbledon", match="Aaa vs Bbb",
        market_ticker="KXATPMATCH-26JUL04AB-A", event_ticker="KXATPMATCH-26JUL04AB",
        market_player="Player Aaa", side="yes", price=0.54, p_model=0.60, net_edge=0.043,
        suggested_stake=46.0, contracts=85, liquidity=100.0, trigger_reason="prematch_value",
        occurrence_datetime="2026-07-04T13:00:00Z", flagged=False, experience=100, score_state=None,
    )
    fields.update(overrides)
    return Opportunity(**fields)


# ---- format_alert ----

def test_format_alert_renders_cents_percent_dollars_and_id():
    out = format_alert(_opp(), 1043, bankroll=2000.0)
    assert "🎾 VALUE ALERT — ATP · Wimbledon" in out
    assert "Aaa vs Bbb · pre-match" in out
    assert 'BUY YES "Player Aaa wins" @ 54¢' in out
    assert "depth ~$54" in out                       # liquidity 100 contracts * 0.54
    assert "Model 60.0% | Market 54¢ | Net edge +4.3% (after fee)" in out
    assert "Stake $46 → 85 contracts" in out
    assert "bankroll $2,000" in out
    assert "opp #1043" in out


def test_format_alert_omits_flag_line_unless_flagged():
    assert "⚠️" not in format_alert(_opp(flagged=False), 1, 2000.0)
    assert "⚠️ Large edge" in format_alert(_opp(flagged=True), 1, 2000.0)


def test_format_alert_no_side_keeps_market_player_as_yes_subject():
    # Buying No on a "Player Aaa wins" market backs the opponent; the quoted player is unchanged.
    assert 'BUY NO "Player Aaa wins"' in format_alert(_opp(side="no"), 1, 2000.0)


# ---- format_abstain ----

def test_format_abstain_covers_every_fixed_reason():
    fixed = [
        "empty_book", "unresolved_market", "unresolved_player", "no_series_for_tour",
        "no_edge", "one_sided_book", "spread_too_wide", "insufficient_liquidity", "stale_ratings",
    ]
    for reason in fixed:
        text = format_abstain(reason)
        assert text and not text.startswith("Abstained:")  # mapped, not the fallback


def test_format_abstain_matches_parameterized_reasons_by_prefix():
    assert "match history" in format_abstain("insufficient_history(3,40<20)")
    assert "format" in format_abstain("unknown_format(best_of=5)")
    assert "tour" in format_abstain("unknown_tour(xyz)")
    assert "went wrong" in format_abstain("error:ValueError")


def test_format_abstain_falls_back_on_unknown_reason():
    assert format_abstain("some_new_reason") == "Abstained: some_new_reason"


# ---- format_no_alert (priced-but-no-alert diagnostic) ----

def _diag(**o) -> Diagnostics:
    f = dict(match="Sinner vs Zverev", market_player="Jannik Sinner", opponent="Alexander Zverev",
             p_model=0.794, yes_price=0.81, no_price=0.20, yes_net_edge=-0.0268, no_net_edge=-0.0052,
             min_net_edge=0.03, depth=281427.0)
    f.update(o)
    return Diagnostics(**f)


def test_format_no_alert_walks_through_prices_model_and_edges():
    out = format_no_alert("no_edge", _diag())
    assert "Sinner vs Zverev · pre-match" in out
    assert "Jannik Sinner: 81¢" in out and "Alexander Zverev: 20¢" in out
    assert "~281k contracts" in out
    assert "My model: Jannik Sinner 79.4%" in out and "Alexander Zverev 20.6%" in out
    assert "net edge -2.7%" in out and "net edge -0.5%" in out   # per-side math shown
    assert "alert needs ≥ +3.0%" in out
    assert "No value." in out


def test_format_no_alert_gate_reasons_and_missing_price():
    assert "spread" in format_no_alert("spread_too_wide", _diag()).lower()
    assert "depth" in format_no_alert("insufficient_liquidity", _diag()).lower()
    out = format_no_alert("one_sided_book", _diag(no_price=None, no_net_edge=None))
    assert "no price on this side" in out and "one side of the order book is empty" in out.lower()


# ---- format_find ----

def _mi(**o) -> MatchInfo:
    f = dict(tour="ATP", player_a="Jannik Sinner", player_b="Alexander Zverev",
             event="Wimbledon Men Singles", is_final=True, modellable=True, strength=(2100.0, 1900.0))
    f.update(o)
    return MatchInfo(**f)


def test_format_find_ranks_modellable_first_and_tallies_others():
    matches = [
        _mi(player_a="Weak Player", player_b="Other Player", strength=(1600.0, 1500.0), is_final=False, event="ATP Gstaad"),
        _mi(),  # the final -- strongest, should rank #1
        _mi(player_a="Alpha Nadal", player_b="Beta Guerrero", modellable=False, strength=None, is_final=False, event="ATP Umag"),
    ]
    out = format_find(matches, top_n=5)
    assert "ATP — 3 open, 2 modellable" in out
    assert out.index("Jannik Sinner vs Alexander Zverev") < out.index("Weak Player vs Other Player")  # strongest first
    assert "· FINAL" in out
    assert "Not modellable (1):" in out and "• Alpha Nadal vs Beta Guerrero" in out  # one per line


def test_format_find_none_modellable():
    out = format_find([_mi(tour="WTA", modellable=False, strength=None, is_final=False, event="WTA Athens")], top_n=5)
    assert "WTA — 1 open, 0 modellable" in out
    assert "Model can price: none right now" in out


def test_compact_counts():
    assert _compact(281427.0) == "281k"
    assert _compact(5300.0) == "5.3k"
    assert _compact(900.0) == "900"


# ---- format_recent (real sqlite3.Row) ----

def _log(conn, **overrides):
    fields = dict(
        ts="2026-07-04T12:00:00Z", tour="ATP", event="Wimbledon", match="Aaa vs Bbb",
        market_ticker="T-A", event_ticker="T", market_player="Player Aaa", side="yes",
        price=0.54, p_model=0.60, net_edge=0.043, suggested_stake=46.0, contracts=85,
        liquidity=100.0, trigger_reason="prematch_value", flagged=0,
    )
    fields.update(overrides)
    return insert_opportunity(conn, **fields)


def test_format_recent_empty():
    assert format_recent([]) == "No opportunities logged yet."


def test_format_recent_lists_rows_newest_first_with_flag():
    conn = connect(":memory:")
    init_db(conn)
    _log(conn, market_ticker="T-A")
    _log(conn, market_ticker="T-B", side="no", flagged=1)
    out = format_recent(recent_opportunities(conn, limit=20))
    conn.close()
    assert "Recent opportunities (2):" in out
    assert "#2  ATP  BUY NO \"Player Aaa wins\" @ 54¢  (+4.3%, 85c) ⚠️" in out
    assert out.index("#2") < out.index("#1")           # newest first
    assert "#1" in out and "⚠️" in out.split("#1")[0]  # flag only on the flagged (newest) row


# ---- format_scan ----

def test_format_scan_no_alerts_shows_tally():
    out = format_scan([], {"no_edge": 3, "unresolved_market": 1}, bankroll=2000.0)
    assert "No value alerts. Skipped 4 market(s):" in out
    assert "no_edge: 3" in out and "unresolved_market: 1" in out


def test_format_scan_renders_alert_blocks_and_tally():
    alerts = [(_opp(), 1), (_opp(market_ticker="T-B", side="no"), 2)]
    out = format_scan(alerts, {"no_edge": 5}, bankroll=2000.0)
    assert 'BUY YES "Player Aaa wins"' in out
    assert 'BUY NO "Player Aaa wins"' in out
    assert "2 alert(s) · 5 skipped (no_edge: 5)" in out


# ---- format_result / format_close / format_stats ----

def test_format_result():
    out = format_result({"id": 5, "market_player": "Jannik Sinner", "side": "yes"}, "win", 0.54, 85, 48.25)
    assert "Recorded opp #5" in out and "WIN" in out and "54¢" in out and "$+48.25" in out


def test_format_close_ok_and_fail():
    ok = format_close({"opp_id": 5, "ok": True, "side": "yes", "market_player": "Jannik Sinner",
                       "closing_price": 0.58, "entry_price": 0.54})
    assert "Closing line opp #5" in ok and "58¢" in ok and "CLV +4¢" in ok
    assert "No opportunity #9" in format_close({"opp_id": 9, "ok": False, "reason": "no_such_opp"})
    assert "missed" in format_close({"opp_id": 3, "ok": False, "reason": "no_price"})
    assert "too late" in format_close({"opp_id": 4, "ok": False, "reason": "too_late"}).lower()
    assert "isn't active" in format_close({"opp_id": 6, "ok": False, "reason": "not_active", "status": "settled"})


def _summary(**o) -> dict:
    s = dict(n_opportunities=0, n_results=0, wins=0, hit_rate=None, total_pnl=0.0, staked=0.0, roi=None,
             n_clv=0, n_clusters=0, mean_clv=None, mean_gross_clv=None, clv_ci=None,
             min_effect_size=0.005, min_clusters=30, go_live=False, buckets={},
             captures={"auto": 0, "manual": 0, "missed": 0})
    s.update(o)
    return s


def test_format_stats_empty_and_populated():
    empty = format_stats(_summary())
    assert "none yet" in empty and "No closing lines captured yet" in empty
    out = format_stats(_summary(n_opportunities=3, n_results=2, wins=1, hit_rate=0.5, total_pnl=12.5,
                                staked=100.0, roi=0.125, n_clv=2, n_clusters=2, mean_clv=0.02,
                                mean_gross_clv=0.03, clv_ci=(-0.01, 0.05),
                                buckets={"mid(50-200)": {"n": 2, "mean_clv": 0.02}},
                                captures={"auto": 5, "manual": 1, "missed": 2}))
    assert "1W/1L" in out and "hit rate 50%" in out
    assert "Captures: 5 auto / 1 manual / 2 missed" in out
    assert "Mean net CLV +2.0% (gross +3.0%) · 95% CI [-1.0%, +5.0%]" in out
    assert "by experience: mid(50-200) +2.0% (n=2)" in out
    assert "not yet" in out and "2/200" in out and "2/30" in out
