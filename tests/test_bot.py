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
from matador.storage import connect, init_db, insert_opportunity, pending_captures, recent_opportunities, settled_bets


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
    assert "auto/manual/sharp-only/missed" in txt      # the a/m/s/x legend is spelled out once
    conn.close()


# ---- the heartbeat must answer the questions that otherwise need SSH ----

def test_scan_status_line_distinguishes_ok_failed_and_never_ran():
    """A scan failing every cycle for weeks must not read as 'Matador OK, quiet market'."""
    import matador.bot as bot
    now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    assert bot._scan_status_line(None, now) == "no scan yet since restart"
    assert bot._scan_status_line({}, now) == "no scan yet since restart"
    two_h = (now - timedelta(hours=2)).isoformat(timespec="seconds")
    assert bot._scan_status_line({"finished_at": two_h, "ok": True, "n_new": 0}, now) == "last scan 2h ago: ok, 0 new"
    failed = bot._scan_status_line({"finished_at": two_h, "ok": False, "n_new": 0}, now)
    assert "FAILED" in failed and "2h ago" in failed


def test_scheduled_scan_stamps_bot_data_on_success_and_on_failure(tmp_path, monkeypatch):
    """The except path must stamp too -- an unstamped failure is invisible, which is the whole bug."""
    app = _app_for_job(tmp_path, scan_interval_hours=8.0)
    monkeypatch.setattr("matador.bot._sharp_entry_job", lambda cfg, ids: 0)
    monkeypatch.setattr("matador.bot._scheduled_scan_job", lambda *a: ("alert\nopp #1", [1]))
    asyncio.run(scheduled_scan_job(_FakeJobContext(app)))
    assert app.bot_data["scan"]["ok"] is True and app.bot_data["scan"]["n_new"] == 1

    def boom(*a):
        raise RuntimeError("kalshi 403 from this IP")
    monkeypatch.setattr("matador.bot._scheduled_scan_job", boom)
    asyncio.run(scheduled_scan_job(_FakeJobContext(app)))
    assert app.bot_data["scan"]["ok"] is False and app.bot_data["scan"]["finished_at"]


def test_heartbeat_flags_bets_awaiting_result():
    """roi is None until /result is entered, and roi >= 0 is a hard go-live co-gate."""
    import matador.bot as bot
    conn = _db()
    now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    # Kalshi's own '...Z' spelling, matching what actually lands in the column -- see storage._shift_iso
    # on why the threshold compare depends on both sides using it.
    def _z(dt):
        return dt.isoformat(timespec="seconds").replace("+00:00", "Z")
    long_done = _z(now - timedelta(hours=30))
    just_started = _z(now - timedelta(hours=1))
    overdue_id = _capture_opp(conn, occurrence=long_done)
    _capture_opp(conn, occurrence=just_started)         # too recent to nag about
    txt = bot._heartbeat_text(conn, make_cfg(), now=now)
    assert f"awaiting /result: #{overdue_id}" in txt
    assert "1 awaiting" in txt                           # only the >12h one
    conn.close()


def test_heartbeat_flags_pending_rows_with_no_start_time():
    """schedule_pending_captures cannot arm a timer without a start, so these sit forever in silence."""
    import matador.bot as bot
    conn = _db()
    oid = _capture_opp(conn, occurrence=None)
    txt = bot._heartbeat_text(conn, make_cfg(), now=datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc))
    assert f"NO start time (#{oid})" in txt and "/close <id> pre" in txt
    conn.close()


def test_heartbeat_warns_only_when_odds_credits_run_low():
    import matador.bot as bot
    conn = _db()
    cfg, now = make_cfg(), datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    assert "credits low" not in bot._heartbeat_text(conn, cfg, credits=500, now=now)
    assert "credits low" not in bot._heartbeat_text(conn, cfg, credits=None, now=now)   # never fetched yet
    low = bot._heartbeat_text(conn, cfg, credits=12, now=now)
    assert "credits low: 12" in low and "sharp_close goes NULL" in low
    conn.close()


def test_heartbeat_header_degrades_when_anything_is_wrong():
    """The first line is all that gets read on a busy day, so it must not say OK over a broken body."""
    import matador.bot as bot
    conn = _db()
    cfg, now = make_cfg(), datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    _capture_opp(conn)                                        # healthy, near-future start
    assert bot._heartbeat_text(conn, cfg, now=now).startswith("💓 Matador OK")

    failed = {"finished_at": (now - timedelta(hours=9)).isoformat(timespec="seconds"), "ok": False}
    hdr = bot._heartbeat_text(conn, cfg, None, failed, 5, now)
    assert hdr.startswith("🚨 Matador — 2 PROBLEM(S)")         # failed scan + low credits
    assert "Matador OK" not in hdr
    conn.close()


# ---- dead-man's switch ----

def test_ping_dead_man_switch_skips_when_unconfigured_and_never_raises(monkeypatch):
    import matador.bot as bot
    assert bot.ping_dead_man_switch(None) is None      # opt-in: no URL -> no network call at all
    assert bot.ping_dead_man_switch("") is None

    def boom(url, timeout=None):
        raise httpx.ConnectError("healthchecks down")
    monkeypatch.setattr(bot.httpx, "get", boom)
    # A monitoring outage must never propagate into the bot -- the switch is the LEAST important thing here.
    assert "dead-man ping failed" in bot.ping_dead_man_switch("https://hc-ping.com/uuid")


def test_heartbeat_pings_the_switch_ONLY_AFTER_the_dm_lands(tmp_path, monkeypatch):
    """The ordering is the entire value of the switch.

    Pinging before (or independently of) the DM would prove only 'the job is scheduled' -- which is
    still true when the long-poll is wedged and no message is reaching the owner. Pinging after a
    successful send makes the check measure engine -> DB -> Telegram, so a wedged poller stops the
    pings and the external service raises the alarm.
    """
    import matador.bot as bot
    app = _app_for_job(tmp_path)
    app.bot_data["healthcheck_url"] = "https://hc-ping.com/uuid"
    ctx = _FakeJobContext(app)
    order = []

    async def spy_send(chat_id, text):
        order.append("dm")
    monkeypatch.setattr(ctx.bot, "send_message", spy_send)
    monkeypatch.setattr(bot.httpx, "get", lambda url, timeout=None: order.append("ping") or _Ping())
    asyncio.run(bot.heartbeat_job(ctx))
    assert order == ["dm", "ping"]


def test_heartbeat_does_not_ping_when_the_dm_itself_fails(tmp_path, monkeypatch):
    """A failed DM must leave the pings STOPPED -- that silence is the alert."""
    import matador.bot as bot
    app = _app_for_job(tmp_path)
    app.bot_data["healthcheck_url"] = "https://hc-ping.com/uuid"
    ctx = _FakeJobContext(app)
    pinged = []

    async def failing_send(chat_id, text):
        raise RuntimeError("telegram 409 conflict: another poller holds this token")
    monkeypatch.setattr(ctx.bot, "send_message", failing_send)
    monkeypatch.setattr(bot.httpx, "get", lambda url, timeout=None: pinged.append(url) or _Ping())
    with pytest.raises(RuntimeError):
        asyncio.run(bot.heartbeat_job(ctx))
    assert pinged == []            # no DM -> no ping -> the check goes red, which is the point


class _Ping:
    status_code = 200


def test_build_application_carries_the_healthcheck_url():
    app = build_application("1:x", make_cfg(), object(), chat_id=42, healthcheck_url="https://hc-ping.com/u")
    assert app.bot_data["healthcheck_url"] == "https://hc-ping.com/u"
    assert build_application("1:x", make_cfg(), object(), chat_id=42).bot_data["healthcheck_url"] is None


def test_sharp_client_records_remaining_credits_for_the_heartbeat():
    """Exhaustion is otherwise silent: fetch raises, the caller swallows, sharp_close stays NULL,
    and the only symptom is sharp_coverage sagging under the co-gate weeks later."""
    import matador.sharp as sharp_mod

    def handler(request):
        return httpx.Response(200, json=[], headers={"x-requests-remaining": "37"})
    client = SharpOddsClient("K", transport=httpx.MockTransport(handler))
    with client:
        client.fetch_h2h("tennis_atp_cincinnati")
    assert client.requests_remaining == 37
    assert sharp_mod.last_requests_remaining() == 37   # survives the client being discarded


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


# ---- auto-recorded settlement (the ONLY unattended writer to the live paper sample) ----

def make_settled_client(result="yes", status="finalized", settlement_value="1.0000", expect_ticker="M"):
    """Mock a FINALIZED Kalshi market. Note 'finalized', not 'settled' -- that is what the live API
    returns for settled tennis markets, verified 2026-07-30 against 2,000 of them.

    Serves ONLY `expect_ticker`: a handler that answers any /markets/ path let a mutation fetching the
    wrong field (event_ticker instead of market_ticker) pass the whole suite."""
    def handler(request):
        if request.url.path.endswith(f"/markets/{expect_ticker}"):
            return httpx.Response(200, json={"market": {
                "ticker": expect_ticker, "event_ticker": "E", "status": status, "result": result,
                "settlement_value_dollars": settlement_value,
                "yes_sub_title": "Player Aaa", "no_sub_title": "Player Aaa"}})
        return httpx.Response(404, json={"error": f"wrong ticker: {request.url.path}"})
    return KalshiClient(base_url="https://x/trade-api/v2", transport=httpx.MockTransport(handler))


def _settleable_opp(conn, side="yes"):
    """A bet whose match is well past its start, with a logged price and contract count."""
    past = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat(timespec="seconds").replace("+00:00", "Z")
    return insert_opportunity(conn, ts="t", tour="ATP", market_ticker="M", market_player="Player Aaa",
                              opponent="Player Bbb", side=side, price=0.40, p_model=0.6, net_edge=0.08,
                              contracts=10, trigger_reason="prematch_value", occurrence_datetime=past)


@pytest.mark.parametrize("side,settled,expect", [
    ("yes", "yes", "win"),    # we took Yes on Player Aaa's market and Aaa won
    ("yes", "no", "loss"),
    ("no", "no", "win"),      # we took No -- backing the OPPONENT -- and Aaa lost
    ("no", "yes", "loss"),
])
def test_auto_record_result_maps_our_side_not_the_market_side(side, settled, expect):
    from matador.bot import auto_record_result
    conn = _db()
    oid = _settleable_opp(conn, side=side)
    with make_settled_client(result=settled) as client:
        a = auto_record_result(client, conn, oid, make_cfg())
    assert a["action"] == "recorded" and a["result"] == expect
    row = conn.execute("SELECT result, fill_price, contracts_filled, pnl FROM outcomes WHERE opp_id=?", (oid,)).fetchone()
    assert row["result"] == expect
    assert row["fill_price"] == pytest.approx(0.40) and row["contracts_filled"] == 10  # the LOGGED alert price
    assert (row["pnl"] > 0) is (expect == "win")
    conn.close()


def test_auto_record_result_voids_a_scalar_because_the_match_was_never_played():
    """A 'scalar' means NO BALL WAS PLAYED — Kalshi refunded both sides at the prevailing price.

    Established empirically: of 17 scalar matches inside our results archive's coverage, 16 are absent
    from it entirely (including marquee main-draw pairings) and the one present scored 'W/O'; the market
    rules require a winner "after a ball has been played". So this is exactly the schema's `void` —
    walkover/refund — and it must be EXCLUDED from CLV, not counted: a match that never happened has no
    closing line to have beaten, so keeping it would measure a phantom event.
    """
    from matador.bot import auto_record_result
    from matador.clv import summarize
    conn = _db()
    oid = _settleable_opp(conn)
    from matador.storage import record_outcome
    record_outcome(conn, oid, closing_price=0.44, closing_source="auto")   # a close was captured
    with make_settled_client(result="scalar", settlement_value="0.7500") as client:
        a = auto_record_result(client, conn, oid, make_cfg())
    assert a["action"] == "recorded" and a["result"] == "void" and a["pnl"] == 0.0
    row = conn.execute("SELECT result, pnl FROM outcomes WHERE opp_id=?", (oid,)).fetchone()
    assert row["result"] == "void" and row["pnl"] == 0.0
    s = summarize(settled_bets(conn), make_cfg())
    assert s["n_clv"] == 0 and s["n_results"] == 0    # excluded from every metric, per the void contract
    conn.close()


def test_auto_record_result_still_asks_a_human_about_an_UNKNOWN_settlement():
    """Only a settlement we have never seen warrants a human — guessing its semantics is the exact
    mistake the scalar investigation corrected."""
    from matador.bot import auto_record_result
    conn = _db()
    oid = _settleable_opp(conn)
    with make_settled_client(result="something_new", settlement_value="0.5000") as client:
        a = auto_record_result(client, conn, oid, make_cfg())
    assert a["action"] == "needs_human" and a["reason"] == "something_new"
    assert conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0] == 0   # nothing guessed
    conn.close()


def test_auto_record_result_never_overwrites_and_waits_for_settlement():
    from matador.bot import auto_record_result
    conn = _db()
    oid = _settleable_opp(conn)

    with make_settled_client(status="active") as client:            # match still in play
        a = auto_record_result(client, conn, oid, make_cfg())
    assert a["action"] == "skip" and "not_settled" in a["reason"]
    assert conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0] == 0

    # An owner-entered result is the override and must never be clobbered by the sweep.
    run_result(conn, oid, "loss", 0.55, 7, make_cfg())
    with make_settled_client(result="yes") as client:               # Kalshi says our side WON
        a = auto_record_result(client, conn, oid, make_cfg())
    assert a["action"] == "skip" and a["reason"] == "already_recorded"
    row = conn.execute("SELECT result, fill_price FROM outcomes WHERE opp_id=?", (oid,)).fetchone()
    assert row["result"] == "loss" and row["fill_price"] == pytest.approx(0.55)   # owner's entry stands
    conn.close()


def test_market_parses_settlement_fields():
    from matador.kalshi.market import Market
    m = Market.from_api({"ticker": "T", "event_ticker": "E", "status": "finalized", "result": "scalar",
                         "settlement_value_dollars": "0.2500"})
    assert m.result == "scalar" and m.settlement_value == pytest.approx(0.25)
    # Live ACTIVE markets send result as an EMPTY STRING, not a missing key (see tests/fixtures) --
    # so the `or None` coercion is load-bearing, and asserting on a missing key would not pin it.
    bare = Market.from_api({"ticker": "T", "event_ticker": "E", "status": "active", "result": ""})
    assert bare.result is None and bare.settlement_value is None


def test_auto_result_job_dms_records_and_flags_a_scalar_only_once(tmp_path, monkeypatch):
    """Recorded outcomes are always announced (so a wrong one is correctable now, not at week 12);
    a scalar is announced ONCE per process so it can't nag on every 8-hourly cycle."""
    import matador.bot as bot
    app = _app_for_job(tmp_path, scan_interval_hours=8.0)
    ctx = _FakeJobContext(app)
    monkeypatch.setattr("matador.bot._auto_result_job", lambda cfg, demo: [
        {"opp_id": 1, "action": "recorded", "result": "win", "pnl": 5.5, "fill": 0.40,
         "contracts": 10, "market_player": "Player Aaa", "side": "yes"},
        # An UNKNOWN settlement is the only kind that still asks a human.
        {"opp_id": 2, "action": "needs_human", "reason": "mystery", "settlement_value": 0.75,
         "payoff": 0.25, "entry": 0.40, "market_player": "Player Bbb", "side": "no"},
        {"opp_id": 3, "action": "skip", "reason": "not_settled(active)"},
    ])
    asyncio.run(bot.auto_result_job(ctx))
    assert len(ctx.bot.sent) == 2                                   # the skip is NOT announced
    # Assert the PAYLOAD, not just the id: this DM is the only review the owner gets of an
    # unattended write, so a wrong price or size in it defeats the whole guard.
    rec = ctx.bot.sent[0][1]
    assert "Auto-recorded opp #1" in rec and "WIN" in rec
    assert "40¢" in rec and "10c" in rec and "$+5.50" in rec
    ask = ctx.bot.sent[1][1]
    assert "Opp #2" in ask and "mystery" in ask
    assert "25¢ per contract" in ask and "75¢" not in ask   # OUR side's payoff, never Kalshi's raw value
    assert "/result 2 win 0.40" in ask and "/result 2 void" in ask  # win/loss need a fill price

    asyncio.run(bot.auto_result_job(ctx))                           # second cycle, same state
    assert len(ctx.bot.sent) == 3                                   # only the record re-DMs, not the scalar


def test_build_application_registers_the_auto_result_job():
    off = build_application("1:x", make_cfg(), object(), chat_id=42)
    assert not off.job_queue.get_jobs_by_name("auto_result")        # no cadence -> no timer
    on = build_application("1:x", make_cfg(scan_interval_hours=8.0), object(), chat_id=42)
    assert on.job_queue.get_jobs_by_name("auto_result")


def test_auto_result_sweep_runs_end_to_end_and_respects_the_age_filter(tmp_path, monkeypatch):
    """Exercises the REAL sweep: storage.awaiting_result -> auto_record_result -> record_outcome.

    Every other test here monkeypatches `_auto_result_job`, which left the autonomous path -- the
    entire point of the feature -- unexecuted: disabling the sweep body wholesale, or setting
    RESULT_AUTO_AFTER_HOURS to 2000, both passed the full suite.
    """
    import matador.bot as bot
    dbp = str(tmp_path / "m.db")
    conn = connect(dbp)
    init_db(conn)

    def _z(hours_ago):
        return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat(timespec="seconds").replace("+00:00", "Z")

    def _opp(occ, side="yes"):
        return insert_opportunity(conn, ts=_z(24), tour="ATP", market_ticker="M", market_player="Player Aaa",
                                  opponent="Player Bbb", side=side, price=0.40, p_model=0.6, net_edge=0.08,
                                  contracts=10, trigger_reason="prematch_value", occurrence_datetime=occ)

    finished = _opp(_z(6))          # match well over -> should be recorded
    just_started = _opp(_z(1))      # inside the age filter -> must be left alone
    untimed = _opp(None)            # no scheduled start; ts is 24h old -> must still be swept
    conn.close()

    monkeypatch.setattr("matador.bot._client", lambda cfg, demo: make_settled_client(result="yes"))
    actions = bot._auto_result_job(make_cfg(db_path=dbp), True)

    by_id = {a["opp_id"]: a for a in actions}
    assert by_id[finished]["action"] == "recorded" and by_id[finished]["result"] == "win"
    assert untimed in by_id, "a row with no occurrence_datetime must not vanish from the work list"
    assert just_started not in by_id, "the age filter must skip a match that only just started"

    conn = connect(dbp)
    assert conn.execute("SELECT COUNT(*) FROM outcomes WHERE result IS NOT NULL").fetchone()[0] == 2
    row = conn.execute("SELECT result, fill_price, contracts_filled, pnl FROM outcomes WHERE opp_id=?",
                       (finished,)).fetchone()
    from matador.clv import net_pnl
    assert row["pnl"] == pytest.approx(net_pnl("win", 0.40, 10, 0.07))   # EXACT, net of the round-up fee
    conn.close()


def test_auto_record_result_refuses_a_row_with_no_contracts():
    """A 0-size row would land in the hit-rate numerator while contributing nothing to ROI."""
    from matador.bot import auto_record_result
    conn = _db()
    oid = insert_opportunity(conn, ts="t", tour="ATP", market_ticker="M", market_player="Player Aaa",
                             opponent="Player Bbb", side="yes", price=0.40, p_model=0.6, net_edge=0.08,
                             contracts=None, trigger_reason="prematch_value",
                             occurrence_datetime=(datetime.now(timezone.utc) - timedelta(hours=6))
                             .isoformat(timespec="seconds").replace("+00:00", "Z"))
    with make_settled_client(result="yes") as client:
        a = auto_record_result(client, conn, oid, make_cfg())
    assert a["action"] == "needs_human" and a["reason"] == "no_contracts_logged"
    assert conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0] == 0
    conn.close()


def test_auto_record_result_cannot_clobber_a_result_written_mid_sweep():
    """Closes the read-guard/write TOCTOU: the guard is read, then a Kalshi call happens, then the
    write. An owner /result landing in that window must WIN, so absence is checked in the write."""
    from matador.bot import auto_record_result
    from matador.storage import record_result_if_absent
    conn = _db()
    oid = _settleable_opp(conn)

    class _RacingClient:
        """Simulates the owner typing /result while our get_market round trip is in flight."""
        def get_market(self, ticker):
            run_result(conn, oid, "void", 0.0, None, make_cfg())   # the human, mid-flight
            from matador.kalshi.market import Market
            return Market.from_api({"ticker": ticker, "event_ticker": "E", "status": "finalized",
                                    "result": "yes", "settlement_value_dollars": "1.0000"})

    a = auto_record_result(_RacingClient(), conn, oid, make_cfg())
    assert a["action"] == "skip" and a["reason"] == "already_recorded"
    assert conn.execute("SELECT result FROM outcomes WHERE opp_id=?", (oid,)).fetchone()["result"] == "void"

    # and the primitive itself refuses to overwrite
    assert record_result_if_absent(conn, oid, result="win", pnl=1.0) is False
    conn.close()


def test_auto_record_result_leaves_an_owner_void_alone():
    from matador.bot import auto_record_result
    conn = _db()
    oid = _settleable_opp(conn)
    run_result(conn, oid, "void", 0.0, None, make_cfg())
    with make_settled_client(result="yes") as client:
        a = auto_record_result(client, conn, oid, make_cfg())
    assert a["action"] == "skip" and a["reason"] == "already_recorded"
    assert conn.execute("SELECT result FROM outcomes WHERE opp_id=?", (oid,)).fetchone()["result"] == "void"
    conn.close()


def test_heartbeat_warns_when_the_auto_result_sweep_failed():
    """A failing sweep is otherwise invisible: the 📝 list just grows, roi stays None, gate unreadable."""
    import matador.bot as bot
    conn = _db()
    now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    stamp = {"finished_at": (now - timedelta(hours=9)).isoformat(timespec="seconds"), "ok": False}
    txt = bot._heartbeat_text(conn, make_cfg(), now=now, auto_result=stamp)
    assert "auto-result sweep FAILED 9h ago" in txt and "roi stays unreadable" in txt
    assert txt.startswith("🚨")                                   # demotes the header, not just a note
    healthy = bot._heartbeat_text(conn, make_cfg(), now=now,
                                  auto_result={"finished_at": now.isoformat(), "ok": True, "n_recorded": 2})
    assert "auto-result" not in healthy                          # silent when fine
    conn.close()


def test_auto_result_job_survives_a_failed_dm_and_stamps_health(tmp_path, monkeypatch):
    """One Telegram blip must not swallow announcements of rows ALREADY written, nor permanently
    silence a scalar (the flag is added only after a successful send)."""
    import matador.bot as bot
    app = _app_for_job(tmp_path, scan_interval_hours=8.0)
    ctx = _FakeJobContext(app)
    monkeypatch.setattr("matador.bot._auto_result_job", lambda cfg, demo: [
        {"opp_id": 2, "action": "needs_human", "reason": "mystery", "settlement_value": 0.75,
         "payoff": 0.25, "entry": 0.40, "market_player": "Player Bbb", "side": "no"},
    ])
    calls = []

    async def flaky(chat_id, text):
        calls.append(text)
        raise RuntimeError("telegram 429")
    monkeypatch.setattr(ctx.bot, "send_message", flaky)
    asyncio.run(bot.auto_result_job(ctx))                 # must not raise
    assert len(calls) == 1
    assert 2 not in app.bot_data["scalar_flagged"]        # NOT flagged -> it will be retried
    assert app.bot_data["auto_result"]["ok"] is True      # the sweep itself succeeded

    sent = []

    async def ok(chat_id, text):
        sent.append(text)
    monkeypatch.setattr(ctx.bot, "send_message", ok)
    asyncio.run(bot.auto_result_job(ctx))
    assert len(sent) == 1 and 2 in app.bot_data["scalar_flagged"]   # retried, then flagged


def test_scalar_payoff_is_inverted_for_a_no_position():
    """`settlement_value` is the YES contract's value, so a 'no' holder received 1 - it.

    Reporting the raw number would be exactly backwards on every 'no' bet -- and since the owner rules
    on these by hand, a DM saying "75¢" when they actually got 25¢ talks them into booking a win on a
    loser. Worth its own test: the DM-layer test supplies `payoff` pre-computed, so it cannot catch a
    missing inversion here.
    """
    from matador.bot import auto_record_result
    for side, expected in (("yes", 0.75), ("no", 0.25)):
        conn = _db()
        oid = _settleable_opp(conn, side=side)
        with make_settled_client(result="scalar", settlement_value="0.7500") as client:
            a = auto_record_result(client, conn, oid, make_cfg())
        assert a["action"] == "recorded" and a["result"] == "void"
        assert a["payoff"] == pytest.approx(expected), f"side={side}"
        conn.close()
