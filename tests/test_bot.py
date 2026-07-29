import asyncio
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from matador.bot import (
    CAPTURE_LATE_GRACE,
    RESCHEDULE_EPSILON,
    auto_capture,
    build_application,
    capture_close,
    is_authorized,
    parse_check_args,
    parse_result_args,
    run_check,
    run_close,
    run_recent,
    run_result,
    run_scan,
    run_stats,
    schedule_pending_captures,
    scheduled_scan_job,
    _scheduled_scan_job,
    _sharp_entry_job,
    split_message,
)
from matador.config import Config
from matador.kalshi.client import KalshiClient
from matador.model.probability import WinProbability
from matador.sharp import SharpOddsClient
from matador.storage import connect, init_db, insert_opportunity, pending_captures, recent_opportunities


# ---- helpers copied from test_engine.py (no tests/__init__.py -> can't cross-import) ----

def make_cfg(**overrides) -> Config:
    kwargs = dict(bankroll=1000.0, min_liquidity=10.0, max_spread=0.10)
    kwargs.update(overrides)
    return Config(**kwargs)


def book(yes_levels, no_levels) -> dict:
    return {"orderbook_fp": {"yes_dollars": yes_levels, "no_dollars": no_levels}}


LIQUID_BOOK = book(yes_levels=[["0.45", "100"]], no_levels=[["0.50", "50"]])

_EVENT = "KXATPMATCH-26JUL04AB"
_EVENTS = {"events": [{"event_ticker": _EVENT, "title": "Aaa vs Bbb", "product_metadata": {"competition": "Wimbledon Men Singles"}}]}


def _mk(ticker, name):
    return {"ticker": ticker, "event_ticker": _EVENT, "status": "active", "yes_sub_title": name,
            "no_sub_title": name, "occurrence_datetime": "2026-07-04T13:00:00Z"}


_MARKETS = {_EVENT: [_mk(_EVENT + "-A", "Player Aaa"), _mk(_EVENT + "-B", "Player Bbb")]}


class OrientedModel:
    def __init__(self, a, b, p):
        self._a, self._b, self._p = a, b, p

    def predict(self, tour, name_a, name_b, *args, **kwargs):
        if name_a == self._a and name_b == self._b:
            return WinProbability(self._p, "ok")
        return WinProbability(None, "wrong_orientation")


def make_client(markets=None) -> KalshiClient:
    markets = markets if markets is not None else _MARKETS

    def handler(request):
        path = request.url.path
        if path.endswith("/events"):
            evs = _EVENTS if request.url.params.get("series_ticker") == "KXATPMATCH" else {"events": []}
            return httpx.Response(200, json=evs)
        if path.endswith("/orderbook"):
            return httpx.Response(200, json=LIQUID_BOOK)
        if path.endswith("/markets"):
            et = request.url.params.get("event_ticker")
            return httpx.Response(200, json={"markets": markets.get(et, [])})
        raise AssertionError(f"unexpected {request.url}")

    return KalshiClient(base_url="https://x/trade-api/v2", transport=httpx.MockTransport(handler))


# outright tournament-winner mock (Grand Slam final): H2H series empty, KXATP has a 2-active final
def make_slam_client() -> KalshiClient:
    event = {"event_ticker": "KXATP-FINAL", "title": "Final: Aaa vs Bbb",
             "product_metadata": {"competition": "Wimbledon Men Singles"}}

    def mkt(suffix, name):
        return {"ticker": f"KXATP-FINAL-{suffix}", "event_ticker": "KXATP-FINAL", "status": "active",
                "yes_sub_title": name, "no_sub_title": name, "occurrence_datetime": "2026-07-13T05:00:00Z"}

    markets = [mkt("A", "Player Aaa"), mkt("B", "Player Bbb")]

    def handler(request):
        path = request.url.path
        if path.endswith("/events"):
            evs = {"events": [event]} if request.url.params.get("series_ticker") == "KXATP" else {"events": []}
            return httpx.Response(200, json=evs)
        if path.endswith("/orderbook"):
            return httpx.Response(200, json=LIQUID_BOOK)
        if path.endswith("/markets"):
            et = request.url.params.get("event_ticker")
            return httpx.Response(200, json={"markets": markets if et == "KXATP-FINAL" else []})
        raise AssertionError(f"unexpected {request.url}")

    return KalshiClient(base_url="https://x/trade-api/v2", transport=httpx.MockTransport(handler))


def _db():
    conn = connect(":memory:")
    init_db(conn)
    return conn


# ---- parse_check_args ----

def test_parse_check_args_good_trailing_tour_and_garbage():
    assert parse_check_args("Dimitrov v Berrettini", "atp") == ("Dimitrov", "Berrettini", "atp")
    assert parse_check_args("Sinner vs. Medvedev wta", "atp") == ("Sinner", "Medvedev", "wta")
    assert parse_check_args("Swiatek vs Sabalenka", "wta") == ("Swiatek", "Sabalenka", "wta")
    assert parse_check_args("just one name", "atp") is None
    assert parse_check_args("", "atp") is None
    assert parse_check_args("foo atp", "atp") is None  # tour peeled, body has no 'v'


# ---- is_authorized ----

def test_is_authorized():
    assert is_authorized(123, 123) is True
    assert is_authorized(999, 123) is False


# ---- run_check ----

def test_run_check_alerts_and_logs_a_row():
    conn = _db()
    with make_client() as client:
        out = run_check(client, OrientedModel("Player Aaa", "Player Bbb", 0.60), make_cfg(), conn, "atp", "Aaa", "Bbb")
    assert 'BUY YES "Player Aaa wins"' in out and "opp #1" in out
    assert "/notes" in out  # footnote pointing to the how-to-read guide
    assert len(recent_opportunities(conn, 10)) == 1
    conn.close()


def test_run_check_dedup_shows_prior_id_and_no_second_row():
    conn = _db()
    model, cfg = OrientedModel("Player Aaa", "Player Bbb", 0.60), make_cfg()
    with make_client() as client:
        first = run_check(client, model, cfg, conn, "atp", "Aaa", "Bbb")
        second = run_check(client, model, cfg, conn, "atp", "Aaa", "Bbb")
    assert "opp #1" in first and "already logged" not in first
    assert "opp #1" in second and "already logged" in second
    assert len(recent_opportunities(conn, 10)) == 1  # deduped -- no second row
    conn.close()


class SymmetricModel:
    """Answers BOTH orientations consistently (p for (a,b), 1-p for (b,a)), unlike OrientedModel.
    Needed to reach the reversed-order anchor: /check Bbb Aaa resolves to Bbb's market, so the
    engine asks predict(Bbb, Aaa) -- which OrientedModel refuses, abstaining before the dedup."""

    def __init__(self, a, b, p):
        self._a, self._b, self._p = a, b, p

    def predict(self, tour, name_a, name_b, *args, **kwargs):
        if (name_a, name_b) == (self._a, self._b):
            return WinProbability(self._p, "ok")
        if (name_a, name_b) == (self._b, self._a):
            return WinProbability(1.0 - self._p, "ok")
        return WinProbability(None, "wrong_orientation")


def _step_scan(client, model, cfg, conn):
    return run_scan(client, model, cfg, conn, ["atp"])


def _step_check_ab(client, model, cfg, conn):
    return run_check(client, model, cfg, conn, "atp", "Aaa", "Bbb")


def _step_check_ba(client, model, cfg, conn):
    return run_check(client, model, cfg, conn, "atp", "Bbb", "Aaa")


_STEPS = {"scan": _step_scan, "check(A,B)": _step_check_ab, "check(B,A)": _step_check_ba}


@pytest.mark.parametrize("first", _STEPS)
@pytest.mark.parametrize("second", _STEPS)
def test_prior_id_survives_a_cross_anchor_dedup(first, second):
    """Every ordered pair of entry points must dedup to ONE row and still name the prior id.

    The three paths anchor differently on the same match -- scan sorts by ticker (-A/yes),
    resolve_match anchors on whichever player was TYPED first (so /check Bbb Aaa gives -B/no) --
    yet all three back Player Aaa, i.e. one economic position. log_opportunity dedups on that
    position, but the alert layer used to re-find the prior row by (market_ticker, side); across
    anchors that lookup missed and prior["id"] raised TypeError. Worst path: run_scan raising
    mid-sweep is swallowed by scheduled_scan_job, silently dropping the cycle's alerts and with
    them the systematic sampling that keeps owner timing out of the paper sample.

    The H2H-vs-outright double count (a Slam final listed under both series carries two different
    event_tickers) is a DOCUMENTED DEFERRAL, deliberately out of scope here -- see
    DESIGN-DECISIONS "Dedup identity". Don't "fix" it by widening this test's key.
    """
    model, cfg = SymmetricModel("Player Aaa", "Player Bbb", 0.60), make_cfg()
    conn = _db()
    with make_client() as client:
        out_first = _STEPS[first](client, model, cfg, conn)
        rows_first = recent_opportunities(conn, 10)
        out_second = _STEPS[second](client, model, cfg, conn)

    assert len(rows_first) == 1, f"{first} should log exactly one row"
    assert "opp #1" in out_first
    assert len(recent_opportunities(conn, 10)) == 1, f"{first} then {second} logged the position twice"
    assert "opp #1" in out_second, f"{second} did not name the prior id after {first}"
    conn.close()


def test_model_freshness_reports_every_tour_and_ages_off_the_oldest(tmp_path):
    """The heartbeat's freshness line is the ONLY routine signal that the weekly refresh has silently
    failed (the `rejected` warning goes to a log nobody reads). It must show EVERY tour and age off
    the OLDEST -- the first version used max(), which made a single-tour freeze invisible."""
    from matador.bot import STALE_DATA_WARN_DAYS, _model_freshness
    art = tmp_path / "model.json"
    art.write_text(json.dumps({"tours": {"atp": {"data_through": 20260726},
                                         "wta": {"data_through": 20260719}}}))
    both = _model_freshness(str(art), today=date(2026, 7, 28))
    assert "atp 2026-07-26 (2d)" in both and "wta 2026-07-19 (9d)" in both
    assert "STALE" not in both

    stale = _model_freshness(str(art), today=date(2026, 7, 26) + timedelta(days=STALE_DATA_WARN_DAYS + 1))
    assert "STALE" in stale and "refresh.log" in stale


def test_model_freshness_catches_a_SINGLE_tour_freeze(tmp_path):
    """The regression the review caught: ATP fresh, WTA frozen 88 days. With max() this reported
    '(1d)' and no warning, while every WTA alert priced off a frozen rating book for the rest of the
    run. Upstream broke per-directory before (the 2026-07 Git-LFS move), so this is the realistic
    failure, not a hypothetical."""
    from matador.bot import _model_freshness
    art = tmp_path / "model.json"
    art.write_text(json.dumps({"tours": {"atp": {"data_through": 20260727},
                                         "wta": {"data_through": 20260501}}}))
    out = _model_freshness(str(art), today=date(2026, 7, 28))
    assert "STALE" in out, f"a single-tour freeze must warn, got: {out}"
    assert "wta 2026-05-01" in out and "(88d)" in out   # and the frozen tour must be VISIBLE


def test_model_freshness_flags_a_missing_or_malformed_stamp(tmp_path):
    """A tour with no stamp, or a nonsense one, is itself suspicious -- name it and warn rather than
    filtering it out, which would hide the per-tour problem this line exists to surface."""
    from matador.bot import _model_freshness
    art = tmp_path / "model.json"
    art.write_text(json.dumps({"tours": {"atp": {"data_through": 20260727}, "wta": {}}}))
    out = _model_freshness(str(art), today=date(2026, 7, 28))
    assert "wta unknown" in out and "STALE" in out

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"tours": {"atp": {"data_through": 20261345}}}))
    out2 = _model_freshness(str(bad), today=date(2026, 7, 28))
    assert "bad-stamp" in out2 and "STALE" in out2


def test_model_freshness_never_breaks_the_heartbeat(tmp_path):
    """A missing or pre-`data_through` artifact must degrade to a note, not an exception -- the
    liveness DM is what tells 'running' from 'silently down', so it must always send."""
    from matador.bot import _model_freshness
    assert "unavailable" in _model_freshness(str(tmp_path / "nope.json"))
    old = tmp_path / "old.json"
    old.write_text(json.dumps({"tours": {"atp": {}}}))       # artifact built before the field existed
    assert "unknown" in _model_freshness(str(old))


def test_artifact_config_drift_is_detected_and_reported_not_fatal():
    """predict() uses the ARTIFACT's params (Model.from_artifact reads surface_weight / shrinkage_n0 /
    min_matches / initial_rating from the JSON), so a drifted config.yaml documents a model that is
    not running -- and the weekly cron rebuilds the artifact from whatever code is checked out, so
    drift can appear on a Monday untouched by anyone. Report it; never make it fatal, because
    refusing to start would crash-loop the container and stop sampling entirely."""
    from matador.bot import _heartbeat_text, artifact_config_drift

    class FakeModel:
        surface_weight, shrinkage_n0, initial, min_matches = 0.3, 0.0, 1500.0, 20

    cfg = make_cfg()
    assert artifact_config_drift(FakeModel(), cfg) == []      # coherent -> silent

    class Drifted(FakeModel):
        shrinkage_n0 = 10.0                                    # artifact built with the OLD value

    drift = artifact_config_drift(Drifted(), cfg)
    assert len(drift) == 1 and "shrinkage_n0" in drift[0]
    assert "artifact=10.0" in drift[0] and "config=0.0" in drift[0]

    conn = _db()
    assert "DRIFT" not in _heartbeat_text(conn, cfg, [])       # clean heartbeat stays clean
    assert "DRIFT" in _heartbeat_text(conn, cfg, drift)        # drifted heartbeat shouts
    conn.close()


def test_run_check_dry_renders_the_alert_but_logs_nothing():
    """/preview must be able to show a real qualifying alert -- the demo case -- while leaving the
    paper sample byte-for-byte untouched. A dry run that logged would inject an owner-TIMED
    opportunity into a sample whose whole point is unbiased scheduled sampling."""
    conn = _db()
    model, cfg = OrientedModel("Player Aaa", "Player Bbb", 0.60), make_cfg()
    with make_client() as client:
        out = run_check(client, model, cfg, conn, "atp", "Aaa", "Bbb", dry=True)
    assert 'BUY YES "Player Aaa wins"' in out      # the full alert still renders
    assert "PREVIEW" in out and "NOT logged" in out
    assert "opp #" not in out                       # no fake row id
    assert recent_opportunities(conn, 10) == []     # THE POINT: nothing written
    conn.close()


def test_dry_run_does_not_consume_the_dedup_slot():
    """A preview must not shadow a later real alert: if the dry run had written, the subsequent
    /check would dedup against it and report 'already logged', losing the genuine row."""
    conn = _db()
    model, cfg = OrientedModel("Player Aaa", "Player Bbb", 0.60), make_cfg()
    with make_client() as client:
        run_check(client, model, cfg, conn, "atp", "Aaa", "Bbb", dry=True)
        real = run_check(client, model, cfg, conn, "atp", "Aaa", "Bbb")
    assert "opp #1" in real and "already logged" not in real
    assert len(recent_opportunities(conn, 10)) == 1
    conn.close()


def test_run_check_abstain_logs_no_row():
    conn = _db()
    with make_client() as client:  # p 0.51 vs 0.50 ask -> negative net edge -> no_edge abstain
        out = run_check(client, OrientedModel("Player Aaa", "Player Bbb", 0.51), make_cfg(), conn, "atp", "Aaa", "Bbb")
    assert "No value" in out
    assert "My model: Player Aaa" in out and "Value check" in out  # rich priced-but-no-alert diagnostic
    assert recent_opportunities(conn, 10) == []
    conn.close()


def test_run_check_abstain_when_series_unconfigured():
    conn = _db()
    with make_client() as client:
        out = run_check(client, OrientedModel("a", "b", 0.6), make_cfg(), conn, "wta", "X", "Y")
    assert "No Kalshi series is configured" in out
    conn.close()


def test_run_check_warns_when_open_exposure_over_cap():
    conn = _db()
    cfg = make_cfg(max_open_exposure_pct=0.001)  # cap = $1 -> the logged stake exceeds it
    with make_client() as client:
        out = run_check(client, OrientedModel("Player Aaa", "Player Bbb", 0.60), cfg, conn, "atp", "Aaa", "Bbb")
    assert "VALUE ALERT" in out and "Open exposure" in out and "exceeds" in out
    conn.close()


def test_heartbeat_text_summarizes_state():
    import matador.bot as bot
    conn = _db()
    _capture_opp(conn)  # one logged opp, no closing line captured yet
    txt = bot._heartbeat_text(conn, make_cfg())
    assert "Matador OK" in txt and "1 opps" in txt and "1 pending" in txt
    conn.close()


# ---- run_scan ----

def test_run_scan_alerts_tallies_and_dedups():
    conn = _db()
    model, cfg = OrientedModel("Player Aaa", "Player Bbb", 0.60), make_cfg()
    with make_client() as client:
        out1 = run_scan(client, model, cfg, conn, ["atp"])
        run_scan(client, model, cfg, conn, ["atp"])          # second sweep dedups
    assert 'BUY YES "Player Aaa wins"' in out1 and "1 alert(s)" in out1
    assert len(recent_opportunities(conn, 10)) == 1
    conn.close()


def test_run_scan_includes_outright_final():
    conn = _db()
    with make_slam_client() as client:  # no H2H markets; a Grand Slam final only in the outright series
        out = run_scan(client, OrientedModel("Player Aaa", "Player Bbb", 0.60), make_cfg(), conn, ["atp"])
    assert 'BUY YES "Player Aaa wins"' in out and "1 alert(s)" in out
    assert len(recent_opportunities(conn, 10)) == 1
    conn.close()


def test_run_scan_tallies_missing_series():
    conn = _db()
    with make_client() as client:
        out = run_scan(client, OrientedModel("a", "b", 0.6), make_cfg(), conn, ["wta"])
    assert "No value alerts" in out and "no_series_for_tour: 1" in out
    conn.close()


# ---- run_recent ----

def test_run_recent_empty_then_populated():
    conn = _db()
    assert "No opportunities logged yet" in run_recent(conn, 10)
    with make_client() as client:
        run_check(client, OrientedModel("Player Aaa", "Player Bbb", 0.60), make_cfg(), conn, "atp", "Aaa", "Bbb")
    assert "Recent opportunities (1):" in run_recent(conn, 10)
    conn.close()


# ---- Phase 5: /result, /close, /stats ----

def _logged_opp(conn, client):
    """Log one real opportunity (Player Aaa yes @ 0.50) via run_check, return its id."""
    run_check(client, OrientedModel("Player Aaa", "Player Bbb", 0.60), make_cfg(), conn, "atp", "Aaa", "Bbb")
    return 1


def test_parse_result_args():
    assert parse_result_args("1043 win 0.54 85") == (1043, "win", 0.54, 85)
    assert parse_result_args("7 loss 54") == (7, "loss", 0.54, None)   # cents + default contracts
    assert parse_result_args("5 void") == (5, "void", None, None)      # walkover/refund
    assert parse_result_args("7 draw 0.5") is None                     # bad result
    assert parse_result_args("7 win 150") is None                      # 150c = $1.50, out of range
    assert parse_result_args("garbage") is None


def test_run_result_records_fill_and_pnl():
    conn = _db()
    with make_client() as client:
        _logged_opp(conn, client)
    out = run_result(conn, 1, "win", 0.50, 100, make_cfg())
    assert "Recorded opp #1" in out and "WIN" in out
    row = conn.execute("SELECT fill_price, contracts_filled, result, pnl FROM outcomes WHERE opp_id=1").fetchone()
    assert row["result"] == "win" and row["fill_price"] == 0.5 and row["contracts_filled"] == 100
    assert row["pnl"] == pytest.approx(100 - 50 - 1.75)  # net of fee
    assert run_result(conn, 999, "win", 0.5, 10, make_cfg()) == "No opportunity #999 to record."
    conn.close()


def test_run_result_void_excluded_from_stats():
    conn = _db()
    oid = _capture_opp(conn)
    out = run_result(conn, oid, "void", None, None, make_cfg())
    assert "VOID" in out
    row = conn.execute("SELECT result, pnl FROM outcomes WHERE opp_id=?", (oid,)).fetchone()
    assert row["result"] == "void" and row["pnl"] == 0.0
    conn.close()


def make_capture_client(status="active", yes_levels=(("0.45", "100"),), no_levels=(("0.50", "50"),)):
    """Mock for capture_close: serves get_market (status) + orderbook (for the same-side mid)."""
    def handler(request):
        p = request.url.path
        if p.endswith("/orderbook"):
            return httpx.Response(200, json={"orderbook_fp": {
                "yes_dollars": [list(x) for x in yes_levels], "no_dollars": [list(x) for x in no_levels]}})
        if "/markets/" in p:  # GET /markets/{ticker}
            return httpx.Response(200, json={"market": {"ticker": "M", "event_ticker": "E", "status": status,
                                                        "yes_sub_title": "Player Aaa", "no_sub_title": "Player Aaa"}})
        raise AssertionError(f"unexpected {request.url}")
    return KalshiClient(base_url="https://x/trade-api/v2", transport=httpx.MockTransport(handler))


def _soon():
    """A start time ~10 min out: future, but inside CAPTURE_EARLIEST (60m) so a capture proceeds now."""
    return (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(timespec="seconds")


_SOON = "__soon__"  # sentinel default -> _soon(); explicit None stays None (untimed); a literal date is used as-is


def _capture_opp(conn, occurrence=_SOON, side="yes"):
    occ = _soon() if occurrence == _SOON else occurrence
    return insert_opportunity(conn, ts="t", tour="ATP", market_ticker="M", market_player="Player Aaa",
                              side=side, price=0.50, p_model=0.6, net_edge=0.08, trigger_reason="prematch_value",
                              occurrence_datetime=occ)


def test_capture_close_records_the_same_side_mid_pre_match():
    conn = _db()
    oid = _capture_opp(conn)  # occurrence far in the future -> pre-match
    with make_capture_client() as client:
        r = capture_close(client, conn, oid, source="auto")
    assert r["ok"] and r["closing_price"] == pytest.approx(0.475)  # mid of yes_bid 0.45 / yes_ask 0.50
    row = conn.execute("SELECT closing_price, closing_source FROM outcomes WHERE opp_id=?", (oid,)).fetchone()
    assert row["closing_price"] == pytest.approx(0.475) and row["closing_source"] == "auto"
    conn.close()


def test_capture_close_marks_missed_when_late():
    conn = _db()
    oid = _capture_opp(conn, occurrence="2020-01-01T00:00:00Z")  # long past scheduled start
    with make_capture_client() as client:
        r = capture_close(client, conn, oid, source="manual")
    assert not r["ok"] and r["reason"] == "too_late"
    row = conn.execute("SELECT closing_price, closing_source FROM outcomes WHERE opp_id=?", (oid,)).fetchone()
    assert row["closing_price"] is None and row["closing_source"].startswith("missed")
    assert pending_captures(conn) == []  # a missed row is excluded from pending (not re-scheduled)
    conn.close()


def test_capture_close_marks_missed_when_market_not_active():
    conn = _db()
    oid = _capture_opp(conn)
    with make_capture_client(status="settled") as client:  # in-play/settled -> would leak the outcome
        r = capture_close(client, conn, oid, source="auto")
    assert not r["ok"] and r["reason"] == "not_active"
    assert "settled" in conn.execute("SELECT closing_source FROM outcomes WHERE opp_id=?", (oid,)).fetchone()[0]
    conn.close()


def test_capture_close_marks_missed_without_a_two_sided_book():
    conn = _db()
    oid = _capture_opp(conn)
    with make_capture_client(no_levels=()) as client:  # no No bids -> no yes_ask -> no mid
        r = capture_close(client, conn, oid, source="auto")
    assert not r["ok"] and r["reason"] == "no_price"
    conn.close()


def test_capture_close_fail_closed_on_unknown_start_and_pre_escape():
    conn = _db()
    oid = _capture_opp(conn, occurrence=None)  # untimed (e.g. an outright final) -> can't tell pre-match from in-play
    with make_capture_client() as client:
        auto = capture_close(client, conn, oid, source="auto")            # auto never force-captures
    assert not auto["ok"] and auto["reason"] == "unknown_start"           # fail-closed -> marked missed
    row = conn.execute("SELECT closing_price, closing_source FROM outcomes WHERE opp_id=?", (oid,)).fetchone()
    assert row["closing_price"] is None and row["closing_source"].startswith("missed:unknown_start")
    # a human /close ... pre overrides the refusal when they confirm it's pre-match
    conn2 = _db()
    oid2 = _capture_opp(conn2, occurrence=None)
    with make_capture_client() as client:
        forced = capture_close(client, conn2, oid2, source="manual", force_prematch=True)
    assert forced["ok"] and forced["closing_price"] == pytest.approx(0.475)
    conn.close()
    conn2.close()


def make_sharp_client(price_a=1.5, price_b=2.6, status=200):
    """Mock the-odds-api returning a Wimbledon Player Aaa vs Player Bbb h2h with a pinnacle price."""
    ev = [{"home_team": "Player Aaa", "away_team": "Player Bbb", "commence_time": "2099-01-01T00:00:00Z",
           "bookmakers": [{"key": "pinnacle", "markets": [{"key": "h2h", "outcomes": [
               {"name": "Player Aaa", "price": price_a}, {"name": "Player Bbb", "price": price_b}]}]}]}]
    body = ev if status == 200 else {}
    return SharpOddsClient("K", transport=httpx.MockTransport(lambda r: httpx.Response(status, json=body)))


def _capture_opp_sharp(conn, occurrence=_SOON):
    occ = _soon() if occurrence == _SOON else occurrence
    return insert_opportunity(conn, ts="t", tour="ATP", event="Wimbledon Men Singles", market_ticker="M",
                              market_player="Player Aaa", opponent="Player Bbb", side="yes", price=0.50,
                              p_model=0.6, net_edge=0.08, trigger_reason="prematch_value", occurrence_datetime=occ)


def test_capture_close_records_sharp_close_and_source():
    conn = _db()
    oid = _capture_opp_sharp(conn)
    with make_capture_client() as client, make_sharp_client() as sharp:
        r = capture_close(client, conn, oid, source="auto", sharp_client=sharp)
    assert r["ok"] and r["sharp_source"] == "pinnacle" and r["sharp_close"] is not None
    row = conn.execute("SELECT closing_price, sharp_close, sharp_source FROM outcomes WHERE opp_id=?", (oid,)).fetchone()
    assert row["closing_price"] == pytest.approx(0.475) and row["sharp_close"] is not None and row["sharp_source"] == "pinnacle"
    conn.close()


def test_capture_close_survives_sharp_error_keeps_kalshi_close():
    conn = _db()
    oid = _capture_opp_sharp(conn)
    with make_capture_client() as client, make_sharp_client(status=500) as sharp:  # sharp API 500s
        r = capture_close(client, conn, oid, source="auto", sharp_client=sharp)
    assert r["ok"] and r["closing_price"] == pytest.approx(0.475)   # Kalshi close still captured
    assert r["sharp_close"] is None                                 # sharp failed -> NULL, no crash / no missed
    conn.close()


def test_capture_close_too_early_stays_pending():
    conn = _db()
    far = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat(timespec="seconds")  # > CAPTURE_EARLIEST (60m)
    oid = _capture_opp(conn, occurrence=far)
    with make_capture_client() as client:
        r = capture_close(client, conn, oid, source="manual")
    assert not r["ok"] and r["reason"] == "too_early"
    assert conn.execute("SELECT count(*) FROM outcomes WHERE opp_id=?", (oid,)).fetchone()[0] == 0  # NOT missed
    assert [row["id"] for row in pending_captures(conn)] == [oid]   # still pending for a capture nearer start
    with make_capture_client() as client:                          # 'pre' overrides the too-early refusal
        r2 = capture_close(client, conn, oid, source="manual", force_prematch=True)
    assert r2["ok"] and r2["closing_price"] == pytest.approx(0.475)
    conn.close()


def test_capture_close_already_captured_is_noop_and_preserves_sharp():
    conn = _db()
    oid = _capture_opp_sharp(conn)
    with make_capture_client() as client, make_sharp_client() as sharp:
        first = capture_close(client, conn, oid, source="manual", sharp_client=sharp)
    good = first["sharp_close"]
    assert first["ok"] and first["sharp_source"] == "pinnacle" and good is not None
    # a re-fire with a FAILING sharp client must NOT overwrite the good sharp_close with NULL, nor relabel it
    with make_capture_client() as client, make_sharp_client(status=500) as sharp:
        again = capture_close(client, conn, oid, source="auto", sharp_client=sharp)
    assert again["reason"] == "already_captured" and again["sharp_close"] == good
    row = conn.execute("SELECT sharp_close, sharp_source, closing_source FROM outcomes WHERE opp_id=?", (oid,)).fetchone()
    assert row["sharp_close"] == good and row["sharp_source"] == "pinnacle" and row["closing_source"] == "manual"
    conn.close()


def test_capture_close_late_reclose_does_not_relabel_captured_row():
    conn = _db()
    oid = _capture_opp(conn)                        # near-future -> captures now
    with make_capture_client() as client:
        capture_close(client, conn, oid, source="auto")
    with make_capture_client() as client:           # a later /close must not mark the clean row missed
        r = capture_close(client, conn, oid, source="manual")
    assert r["ok"] and r["reason"] == "already_captured"
    assert conn.execute("SELECT closing_source FROM outcomes WHERE opp_id=?", (oid,)).fetchone()[0] == "auto"
    conn.close()


def test_capture_close_sharp_only_when_kalshi_book_thin():
    conn = _db()
    oid = _capture_opp_sharp(conn)
    with make_capture_client(no_levels=()) as client, make_sharp_client() as sharp:  # one-sided Kalshi book -> no mid
        r = capture_close(client, conn, oid, source="auto", sharp_client=sharp)
    assert r["ok"] and r["closing_price"] is None and r["sharp_source"] == "pinnacle"
    row = conn.execute("SELECT closing_price, sharp_close, closing_source FROM outcomes WHERE opp_id=?", (oid,)).fetchone()
    assert row["closing_price"] is None and row["sharp_close"] is not None and row["closing_source"] == "sharp_only:auto"
    conn.close()


def test_heartbeat_text_includes_sharp_line():
    import matador.bot as bot
    conn = _db()
    _capture_opp(conn)
    txt = bot._heartbeat_text(conn, make_cfg())
    assert "sharp 0 pinnacle / 0 consensus" in txt and "coverage 0%" in txt
    conn.close()


def test_run_stats_after_close_and_result():
    conn = _db()
    oid = _capture_opp(conn)                    # future occurrence -> capturable now
    with make_capture_client() as client:
        run_close(client, conn, oid)            # captures the mid
    run_result(conn, oid, "win", 0.50, 100, make_cfg())
    out = run_stats(conn, make_cfg())
    assert "Paper-trading stats" in out
    assert "Trades recorded: 1" in out and "1W/0L" in out
    assert "Captures: 0 auto / 1 manual / 0 sharp-only / 0 missed" in out  # /close is a manual capture
    # no sharp client in this test -> sharp track empty; Kalshi CLV shown as informational
    assert "No pinnacle closing lines yet" in out and "Go-live gate" not in out
    assert "(info) Kalshi-close CLV" in out and "over 1 bet(s) / 1 week(s)" in out
    conn.close()


# ---- auto-scheduled capture ----

def test_schedule_pending_captures_schedules_timed_opps_only(tmp_path):
    dbp = str(tmp_path / "m.db")
    conn = connect(dbp)
    init_db(conn)
    base = dict(ts="t", tour="ATP", side="yes", price=0.5, p_model=0.6, net_edge=0.08, trigger_reason="prematch_value")
    insert_opportunity(conn, market_ticker="T-A", occurrence_datetime="2099-01-01T00:00:00Z", **base)  # id 1, future
    insert_opportunity(conn, market_ticker="T-B", occurrence_datetime=None, **base)                     # id 2, no time
    conn.close()
    app = build_application("1:x", make_cfg(db_path=dbp), object(), chat_id=42)
    assert schedule_pending_captures(app) == 1                 # only the opp with an occurrence is scheduled
    assert app.job_queue.get_jobs_by_name("close:1")
    assert schedule_pending_captures(app) == 0                 # idempotent -- already scheduled, not duplicated


def test_schedule_pending_captures_schedules_immediate_job_for_past_due(tmp_path):
    """A past-due stored start is no longer marked missed at startup -- it gets an immediate job
    that re-checks the LIVE market (so a postponed match isn't false-missed). No network here."""
    dbp = str(tmp_path / "m.db")
    conn = connect(dbp)
    init_db(conn)
    base = dict(ts="t", tour="ATP", side="yes", price=0.5, p_model=0.6, net_edge=0.08, trigger_reason="prematch_value")
    insert_opportunity(conn, market_ticker="T-A", occurrence_datetime="2020-01-01T00:00:00Z", **base)  # long past
    conn.close()
    app = build_application("1:x", make_cfg(db_path=dbp), object(), chat_id=42)
    assert schedule_pending_captures(app) == 1                 # scheduled (immediate), NOT marked missed
    assert app.job_queue.get_jobs_by_name("close:1")
    conn2 = connect(dbp)
    init_db(conn2)
    assert conn2.execute("SELECT count(*) FROM outcomes").fetchone()[0] == 0   # no missed row written
    assert [r["id"] for r in pending_captures(conn2)] == [1]                   # still pending, not consumed
    conn2.close()


# ---- WS3: postponement reconciliation (live market is the single source of truth) ----

def make_reconcile_client(status="active", occurrence="2099-01-01T00:00:00Z",
                          yes_levels=(("0.45", "100"),), no_levels=(("0.50", "50"),)):
    """Like make_capture_client but get_market carries an occurrence_datetime (so auto_capture can
    compare the live start against the stored one)."""
    def handler(request):
        p = request.url.path
        if p.endswith("/orderbook"):
            return httpx.Response(200, json={"orderbook_fp": {
                "yes_dollars": [list(x) for x in yes_levels], "no_dollars": [list(x) for x in no_levels]}})
        if "/markets/" in p:
            return httpx.Response(200, json={"market": {"ticker": "M", "event_ticker": "E", "status": status,
                                                        "yes_sub_title": "Player Aaa", "no_sub_title": "Player Aaa",
                                                        "occurrence_datetime": occurrence}})
        raise AssertionError(f"unexpected {request.url}")
    return KalshiClient(base_url="https://x/trade-api/v2", transport=httpx.MockTransport(handler))


# ---- capture-timing guards: the only defence against recording an IN-PLAY price as the "close" ----

_GRACE_START = datetime(2026, 8, 10, 13, 0, tzinfo=timezone.utc)   # a fixed, unambiguous scheduled start


def test_capture_timing_constants_are_pinned_tight():
    """Pinned ABSOLUTELY, on purpose.

    Kalshi trades tennis in-play and the market stays 'active' right through the match, so status
    cannot distinguish pre-match from in-play -- a tight grace is the only real guard. Widening
    CAPTURE_LATE_GRACE to 12h, or RESCHEDULE_EPSILON to a day, used to pass the entire suite, because
    the only 'late' test was months late and every reschedule test moved starts by months. A
    partially-resolved 'close' is poison in the BINDING metric and invisible until the gate is read.

    Asserting the values (not just relative behaviour) is deliberate: a test written as
    `start + CAPTURE_LATE_GRACE + 1min` scales with the constant and would stay green if it widened.
    """
    assert CAPTURE_LATE_GRACE == timedelta(minutes=5)
    assert RESCHEDULE_EPSILON == timedelta(minutes=2)


@pytest.mark.parametrize("minutes_late,expect_capture", [(4, True), (6, False)])
def test_capture_close_grace_boundary(minutes_late, expect_capture):
    """4 minutes past start still captures; 6 minutes past is a miss, not a snapshot."""
    conn = _db()
    oid = _capture_opp(conn, occurrence=_GRACE_START.isoformat())
    with make_capture_client() as client:
        r = capture_close(client, conn, oid, source="auto",
                          now=_GRACE_START + timedelta(minutes=minutes_late))
    row = conn.execute("SELECT closing_price, closing_source FROM outcomes WHERE opp_id=?", (oid,)).fetchone()
    if expect_capture:
        assert r["ok"] and r["closing_price"] == pytest.approx(0.475)
        assert row["closing_source"] == "auto"
    else:
        assert not r["ok"] and r["reason"] == "too_late"
        assert row["closing_price"] is None and row["closing_source"].startswith("missed")
    conn.close()


@pytest.mark.parametrize("shift_minutes,action", [(-30, "captured"), (30, "rescheduled")])
def test_auto_capture_handles_a_realistic_half_hour_court_shuffle(shift_minutes, action):
    """A 30-minute move -- the realistic case (a reordered court), not the months-apart fixtures.

    Moved EARLIER by 30min with now = stored-5min: the match is already 25 minutes in, so the stored
    time must be corrected and the row MISSED rather than captured at an in-play price. Moved LATER,
    it is still pre-match, so re-arm. Under a day-wide RESCHEDULE_EPSILON neither move registers at
    all and the stored time is silently left wrong.
    """
    stored = _GRACE_START
    live = stored + timedelta(minutes=shift_minutes)
    now = stored - timedelta(minutes=5)
    conn = _db()
    oid = _capture_opp(conn, occurrence=stored.isoformat())
    with make_reconcile_client(occurrence=live.isoformat()) as client:
        res = auto_capture(client, conn, oid, now=now)
    assert res["action"] == action
    row = conn.execute("SELECT occurrence_datetime FROM opportunities WHERE id=?", (oid,)).fetchone()
    assert row["occurrence_datetime"] == live.isoformat()          # corrected in BOTH directions
    out = conn.execute("SELECT closing_price, closing_source FROM outcomes WHERE opp_id=?", (oid,)).fetchone()
    if action == "captured":
        assert res["result"]["reason"] == "too_late"               # 25 min in -> refuse, don't snapshot
        assert out["closing_price"] is None and out["closing_source"].startswith("missed")
    else:
        assert out is None                                        # nothing recorded; the row stays pending
        assert [r["id"] for r in pending_captures(conn)] == [oid]
    conn.close()


def test_auto_capture_reschedules_on_postponement():
    conn = _db()
    oid = _capture_opp(conn, occurrence="2099-01-01T00:00:00Z")           # stored start
    with make_reconcile_client(occurrence="2099-06-01T00:00:00Z") as client:  # live shows a much later start
        res = auto_capture(client, conn, oid)
    assert res["action"] == "rescheduled" and res["new_start"] == "2099-06-01T00:00:00Z"
    row = conn.execute("SELECT occurrence_datetime FROM opportunities WHERE id=?", (oid,)).fetchone()
    assert row["occurrence_datetime"] == "2099-06-01T00:00:00Z"           # stored time corrected
    assert conn.execute("SELECT count(*) FROM outcomes WHERE opp_id=?", (oid,)).fetchone()[0] == 0  # nothing captured/missed
    assert [r["id"] for r in pending_captures(conn)] == [oid]             # still pending -> will re-arm
    conn.close()


def test_auto_capture_captures_when_time_unchanged():
    conn = _db()
    soon = _soon()                                       # near-future: unchanged AND inside the capture window
    oid = _capture_opp(conn, occurrence=soon)
    with make_reconcile_client(occurrence=soon) as client:  # same start (within epsilon)
        res = auto_capture(client, conn, oid)
    assert res["action"] == "captured" and res["result"]["ok"]
    assert res["result"]["closing_price"] == pytest.approx(0.475)         # mid of the same-side book
    conn.close()


def test_auto_capture_corrects_earlier_start_and_misses_when_now_past():
    # Match moved EARLIER: live start is already in the past while stored was (wrongly) future and the
    # market is still active/in-play. Correct the stored time and MISS -- never snapshot an in-play price.
    conn = _db()
    oid = _capture_opp(conn, occurrence="2099-01-01T00:00:00Z")
    with make_reconcile_client(occurrence="2020-01-01T00:00:00Z") as client:
        res = auto_capture(client, conn, oid)
    assert res["action"] == "captured" and not res["result"]["ok"] and res["result"]["reason"] == "too_late"
    row = conn.execute("SELECT occurrence_datetime FROM opportunities WHERE id=?", (oid,)).fetchone()
    assert row["occurrence_datetime"] == "2020-01-01T00:00:00Z"           # stored corrected to the real (past) start
    conn.close()


# ---- WS2: scheduled systematic scan ----

class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


class _FakeJobContext:
    """Minimal stand-in for a PTB job CallbackContext (bot_data + application + a capturing bot)."""
    def __init__(self, app):
        self.application = app
        self.bot_data = app.bot_data
        self.bot = _FakeBot()


def _app_for_job(tmp_path, **cfg_over):
    dbp = str(tmp_path / "m.db")
    conn = connect(dbp)
    init_db(conn)
    conn.close()
    return build_application("1:x", make_cfg(db_path=dbp, **cfg_over), object(), chat_id=42)


def test_build_application_registers_scheduled_scan_only_when_configured():
    off = build_application("1:x", make_cfg(), object(), chat_id=42)
    assert not off.job_queue.get_jobs_by_name("scheduled_scan")          # None cadence -> no timer
    on = build_application("1:x", make_cfg(scan_interval_hours=8.0), object(), chat_id=42)
    assert on.job_queue.get_jobs_by_name("scheduled_scan")               # configured -> repeating job armed


def test_scheduled_scan_job_dms_on_new_alert_and_arms_captures(tmp_path, monkeypatch):
    app = _app_for_job(tmp_path, scan_interval_hours=8.0)
    monkeypatch.setattr("matador.bot._scheduled_scan_job", lambda *a: ("🎾 VALUE ALERT — ATP · Wimbledon\nopp #1", [1]))
    ctx = _FakeJobContext(app)
    asyncio.run(scheduled_scan_job(ctx))
    assert len(ctx.bot.sent) == 1 and ctx.bot.sent[0][0] == 42 and "VALUE ALERT" in ctx.bot.sent[0][1]


def test_scheduled_scan_job_quiet_when_no_new_alert_unless_announce(tmp_path, monkeypatch):
    # A standing edge that /scan re-renders but does NOT re-log (n_new=0) must not re-ping.
    monkeypatch.setattr("matador.bot._scheduled_scan_job",
                        lambda *a: ("🎾 VALUE ALERT — standing edge (already logged)", []))
    quiet = _FakeJobContext(_app_for_job(tmp_path))                      # scan_announce defaults False
    asyncio.run(scheduled_scan_job(quiet))
    assert quiet.bot.sent == []                                         # nothing NEW -> no DM (even though text has an alert block)
    loud = _FakeJobContext(_app_for_job(tmp_path, scan_announce=True))
    asyncio.run(scheduled_scan_job(loud))
    assert len(loud.bot.sent) == 1                                      # announce -> DM regardless


def test_scheduled_scan_job_swallows_scan_errors(tmp_path, monkeypatch):
    def boom(*a):
        raise RuntimeError("kalshi down")
    monkeypatch.setattr("matador.bot._scheduled_scan_job", boom)
    ctx = _FakeJobContext(_app_for_job(tmp_path, scan_interval_hours=8.0))
    asyncio.run(scheduled_scan_job(ctx))                                # must not raise (repeating job survives)
    assert ctx.bot.sent == []


def test_scheduled_scan_new_count_dedups_standing_edge_across_cycles(tmp_path, monkeypatch):
    """_scheduled_scan_job reports NEW logged opps: the first sweep logs one, the second finds it
    standing (deduped) -> n_new=0, which is what keeps the scheduled job quiet on a standing edge."""
    dbp = str(tmp_path / "m.db")
    conn = connect(dbp)
    init_db(conn)
    conn.close()
    monkeypatch.setattr("matador.bot._client", lambda cfg, demo: make_client())
    cfg = make_cfg(db_path=dbp)
    model = OrientedModel("Player Aaa", "Player Bbb", 0.60)
    text1, new1 = _scheduled_scan_job(cfg, model, False, ["atp"])
    _text2, new2 = _scheduled_scan_job(cfg, model, False, ["atp"])
    assert new1 == [1] and "VALUE ALERT" in text1   # first sweep logs a new opp, and names its id
    assert new2 == []                              # second sweep: standing edge deduped -> nothing new


# ---- sharp fair probability AT ENTRY (the venue-basis vs line-drift decomposition) ----

def _entry_cfg(tmp_path, **over):
    """A cfg whose db exists and whose odds_api_key_path points at a real (non-empty) key file --
    _sharp_client returns None on a missing/empty key, which would short-circuit the fill."""
    dbp = str(tmp_path / "m.db")
    conn = connect(dbp)
    init_db(conn)
    key = tmp_path / "odds.txt"
    key.write_text("K")
    return make_cfg(db_path=dbp, odds_api_key_path=str(key), **over), conn


def test_sharp_entry_job_fills_the_entry_columns(tmp_path, monkeypatch):
    cfg, conn = _entry_cfg(tmp_path)
    oid = _capture_opp_sharp(conn)
    conn.close()
    monkeypatch.setattr("matador.bot._sharp_client", lambda c: make_sharp_client())
    assert _sharp_entry_job(cfg, [oid]) == 1
    conn = connect(cfg.db_path)
    row = conn.execute("SELECT sharp_entry, sharp_entry_source FROM opportunities WHERE id=?", (oid,)).fetchone()
    # 1.5 / 2.6 devigged -> the Yes side (Player Aaa) is the favorite, so entry prob is well above half.
    assert row["sharp_entry"] is not None and 0.5 < row["sharp_entry"] < 1.0
    assert row["sharp_entry_source"] == "pinnacle"
    conn.close()


def test_sharp_entry_job_is_a_noop_without_a_sharp_client(tmp_path, monkeypatch):
    """No key configured -> the columns stay NULL. NULL is the honest 'no reference' encoding; the
    week-12 decomposition filters on it rather than being handed a fabricated number."""
    cfg, conn = _entry_cfg(tmp_path)
    oid = _capture_opp_sharp(conn)
    conn.close()
    monkeypatch.setattr("matador.bot._sharp_client", lambda c: None)
    assert _sharp_entry_job(cfg, [oid]) == 0
    conn = connect(cfg.db_path)
    assert conn.execute("SELECT sharp_entry FROM opportunities WHERE id=?", (oid,)).fetchone()["sharp_entry"] is None
    conn.close()


def test_sharp_entry_job_never_raises_and_returns_zero(tmp_path, monkeypatch):
    cfg, conn = _entry_cfg(tmp_path)
    oid = _capture_opp_sharp(conn)
    conn.close()

    def boom(c):
        raise RuntimeError("odds api exploded")
    monkeypatch.setattr("matador.bot._sharp_client", boom)
    assert _sharp_entry_job(cfg, [oid]) == 0          # swallowed: instrumentation can't take down a cycle
    assert _sharp_entry_job(cfg, []) == 0             # nothing logged this cycle -> no client built at all


def test_scheduled_scan_dms_before_and_despite_the_sharp_entry_fill(tmp_path, monkeypatch):
    """The alert must reach the owner even if the entry fill fails, and must not WAIT on it.

    This is the ordering guarantee that makes the fill safe to run on the live box: it is
    instrumentation, so it sits strictly after the DM and its failure is invisible to the alert path.
    """
    app = _app_for_job(tmp_path, scan_interval_hours=8.0)
    monkeypatch.setattr("matador.bot._scheduled_scan_job", lambda *a: ("🎾 VALUE ALERT — ATP\nopp #1", [1]))
    order = []
    ctx = _FakeJobContext(app)

    async def spy_send(chat_id, text):
        order.append("dm")
        ctx.bot.sent.append((chat_id, text))
    monkeypatch.setattr(ctx.bot, "send_message", spy_send)

    def failing_fill(cfg, ids):
        order.append("fill")
        raise RuntimeError("odds api down")
    monkeypatch.setattr("matador.bot._sharp_entry_job", failing_fill)

    with pytest.raises(RuntimeError):   # the job itself doesn't swallow -- _sharp_entry_job does
        asyncio.run(scheduled_scan_job(ctx))
    assert order == ["dm", "fill"]      # DM first, always
    assert len(ctx.bot.sent) == 1 and "VALUE ALERT" in ctx.bot.sent[0][1]


def test_migration_adds_the_sharp_entry_columns_to_an_existing_db(tmp_path):
    """A DB created before these columns existed must gain them -- the live droplet's matador.db has
    11 rows, so a re-CREATE is not an option and _MIGRATIONS is the only path."""
    dbp = str(tmp_path / "old.db")
    conn = sqlite3.connect(dbp)
    conn.executescript(  # the pre-sharp_entry shape: same table, minus the two new columns
        "CREATE TABLE opportunities (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, "
        "tour TEXT NOT NULL, market_ticker TEXT NOT NULL, side TEXT NOT NULL CHECK (side IN ('yes','no')), "
        "price REAL NOT NULL, p_model REAL NOT NULL, net_edge REAL NOT NULL);"
    )
    conn.execute("INSERT INTO opportunities (ts,tour,market_ticker,side,price,p_model,net_edge) "
                 "VALUES ('t','ATP','M','yes',0.5,0.6,0.08)")
    conn.commit()
    conn.close()
    live = connect(dbp)
    init_db(live)                                        # must ALTER, not fail, and must not drop the row
    cols = {r[1] for r in live.execute("PRAGMA table_info(opportunities)")}
    assert {"sharp_entry", "sharp_entry_source"} <= cols
    assert live.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0] == 1
    live.close()


# ---- split_message ----

def test_split_message_packs_whole_lines_and_is_lossless():
    text = "\n".join(f"line-{i}" for i in range(200))
    parts = split_message(text, limit=40)
    assert all(len(p) <= 40 for p in parts)
    assert "\n".join(parts) == text


# ---- wiring smoke test (no token/network needed) ----

def test_build_application_sets_bot_data_and_registers_handlers():
    app = build_application("123:ABC", make_cfg(), object(), chat_id=42, demo=True)
    assert app.bot_data["chat_id"] == 42 and app.bot_data["demo"] is True
    assert app.bot_data["default_tour"] == "atp"
    assert app.job_queue is not None  # job-queue extra installed -> auto CLV capture can schedule
    # assert WHICH handlers are wired, not just how many -- a bare count says nothing about a
    # mis-registration, and /preview must be present or the no-log path is unreachable from Telegram.
    registered = {h.callback.__name__ for h in app.handlers[0]}
    assert registered == {
        "cmd_check", "cmd_preview", "cmd_find", "cmd_scan", "cmd_recent",
        "cmd_result", "cmd_close", "cmd_stats", "cmd_notes", "cmd_help",
    }


def test_help_and_notes_text():
    import matador.bot as bot
    assert all(c in bot.HELP for c in ("/find", "/result", "/close", "/stats", "/notes"))
    assert bot.NOTES.startswith("📘") and "net edge" in bot.NOTES and "CLV" in bot.NOTES
