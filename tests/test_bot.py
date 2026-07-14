import asyncio

import httpx
import pytest

from matador.bot import (
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
    split_message,
)
from matador.config import Config
from matador.kalshi.client import KalshiClient
from matador.model.probability import WinProbability
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


def _capture_opp(conn, occurrence="2099-01-01T00:00:00Z", side="yes"):
    return insert_opportunity(conn, ts="t", tour="ATP", market_ticker="M", market_player="Player Aaa",
                              side=side, price=0.50, p_model=0.6, net_edge=0.08, trigger_reason="prematch_value",
                              occurrence_datetime=occurrence)


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


def test_run_stats_after_close_and_result():
    conn = _db()
    oid = _capture_opp(conn)                    # future occurrence -> capturable now
    with make_capture_client() as client:
        run_close(client, conn, oid)            # captures the mid
    run_result(conn, oid, "win", 0.50, 100, make_cfg())
    out = run_stats(conn, make_cfg())
    assert "Paper-trading stats" in out
    assert "Trades recorded: 1" in out and "1W/0L" in out
    assert "Captures: 0 auto / 1 manual / 0 missed" in out  # /close is a manual capture
    assert "1 bet(s) over 1 week(s)" in out and "Go-live gate" in out
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
    oid = _capture_opp(conn, occurrence="2099-01-01T00:00:00Z")
    with make_reconcile_client(occurrence="2099-01-01T00:00:00Z") as client:  # same start (within epsilon)
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
    monkeypatch.setattr("matador.bot._scheduled_scan_job", lambda *a: ("🎾 VALUE ALERT — ATP · Wimbledon\nopp #1", 1))
    ctx = _FakeJobContext(app)
    asyncio.run(scheduled_scan_job(ctx))
    assert len(ctx.bot.sent) == 1 and ctx.bot.sent[0][0] == 42 and "VALUE ALERT" in ctx.bot.sent[0][1]


def test_scheduled_scan_job_quiet_when_no_new_alert_unless_announce(tmp_path, monkeypatch):
    # A standing edge that /scan re-renders but does NOT re-log (n_new=0) must not re-ping.
    monkeypatch.setattr("matador.bot._scheduled_scan_job",
                        lambda *a: ("🎾 VALUE ALERT — standing edge (already logged)", 0))
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
    text1, n1 = _scheduled_scan_job(cfg, model, False, ["atp"])
    _text2, n2 = _scheduled_scan_job(cfg, model, False, ["atp"])
    assert n1 == 1 and "VALUE ALERT" in text1   # first sweep logs a new opp
    assert n2 == 0                              # second sweep: standing edge deduped -> nothing new


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
    assert len(app.handlers[0]) == 9  # check, find, scan, recent, result, close, stats, notes, help


def test_help_and_notes_text():
    import matador.bot as bot
    assert all(c in bot.HELP for c in ("/find", "/result", "/close", "/stats", "/notes"))
    assert bot.NOTES.startswith("📘") and "net edge" in bot.NOTES and "CLV" in bot.NOTES
