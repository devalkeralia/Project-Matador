import httpx

from matador.bot import (
    build_application,
    is_authorized,
    parse_check_args,
    run_check,
    run_recent,
    run_scan,
    split_message,
)
from matador.config import Config
from matador.kalshi.client import KalshiClient
from matador.model.probability import WinProbability
from matador.storage import connect, init_db, recent_opportunities


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
    assert len(app.handlers[0]) == 6  # check, find(+findmatch), scan, recent, notes(+helpnotes), help(+start)


def test_help_and_notes_text():
    import matador.bot as bot
    assert "/notes" in bot.HELP and "/find" in bot.HELP
    assert bot.NOTES.startswith("📘") and "net edge" in bot.NOTES
