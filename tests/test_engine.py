import json
from datetime import date

import httpx
import pytest

from matador.config import Config
from matador.engine import Opportunity, depth_at_ask, evaluate_match, evaluate_resolution, log_opportunity, scan_series, spread
from matador.kalshi.client import KalshiClient, MatchResolution
from matador.model.artifact import Model
from matador.model.probability import WinProbability
from matador.storage import connect, get_opportunity, init_db


def make_cfg(**overrides) -> Config:
    kwargs = dict(bankroll=1000.0, min_liquidity=10.0, max_spread=0.10)
    kwargs.update(overrides)
    return Config(**kwargs)


class FakeModel:
    """Injects a fixed WinProbability so engine gates are tested independent of the real model."""

    def __init__(self, wp: WinProbability):
        self._wp = wp

    def predict(self, *args, **kwargs) -> WinProbability:
        return self._wp


def book(yes_levels, no_levels) -> dict:
    return {"orderbook_fp": {"yes_dollars": yes_levels, "no_dollars": no_levels}}


def make_resolution(**overrides) -> MatchResolution:
    fields = dict(
        event_ticker="KXATPMATCH-26JUL04AB", market_ticker="KXATPMATCH-26JUL04AB-A",
        title="Aaa vs Bbb", yes_sub_title="Player Aaa", no_sub_title="Player Aaa",
        yes_player_key="aaa_p", opponent="Player Bbb",
        competition="Wimbledon Men Singles", occurrence_datetime="2026-07-04T13:00:00Z",
    )
    fields.update(overrides)
    return MatchResolution(**fields)


def _eval(model, orderbook, cfg, **kw):
    kw.setdefault("surface", "Grass")
    kw.setdefault("best_of", 3)
    kw.setdefault("event_date", date(2026, 7, 4))
    return evaluate_resolution(make_resolution(), orderbook, model, cfg, "atp", **kw)


# best No bid 0.50 -> yes_ask 0.50; best Yes bid 0.45 -> yes_bid 0.45 (spread 0.05); no level size funds depth
LIQUID_BOOK = book(yes_levels=[["0.45", "100"]], no_levels=[["0.50", "50"]])


def test_alert_on_yes_side():
    r = _eval(FakeModel(WinProbability(0.60, "ok")), LIQUID_BOOK, make_cfg())
    assert r.status == "alert"
    assert r.opportunity.side == "yes"
    assert r.opportunity.price == pytest.approx(0.50)
    assert r.opportunity.p_model == pytest.approx(0.60)
    assert r.opportunity.contracts >= 1
    assert r.opportunity.trigger_reason == "prematch_value"
    assert r.flagged is False  # net edge ~0.08 < adverse_gap 0.15


def test_alert_on_no_side_when_model_favors_opponent():
    r = _eval(FakeModel(WinProbability(0.30, "ok")), LIQUID_BOOK, make_cfg())
    assert r.status == "alert" and r.opportunity.side == "no"


def test_flagged_when_edge_is_implausibly_large():
    r = _eval(FakeModel(WinProbability(0.90, "ok")), LIQUID_BOOK, make_cfg())
    assert r.status == "alert" and r.flagged is True  # huge gap -> adverse-selection flag


def test_abstain_empty_book():
    r = _eval(FakeModel(WinProbability(0.60, "ok")), book([], []), make_cfg())
    assert r.status == "abstain" and r.reason == "empty_book"


def test_abstain_no_edge():
    r = _eval(FakeModel(WinProbability(0.51, "ok")), LIQUID_BOOK, make_cfg())
    assert r.status == "abstain" and r.reason == "no_edge"


def test_abstain_propagates_model_reason():
    r = _eval(FakeModel(WinProbability(None, "insufficient_history(3,40<20)")), LIQUID_BOOK, make_cfg())
    assert r.status == "abstain" and r.reason.startswith("insufficient_history")


def test_abstain_spread_too_wide():
    wide = book(yes_levels=[["0.30", "100"]], no_levels=[["0.50", "50"]])  # spread 0.50-0.30 = 0.20
    r = _eval(FakeModel(WinProbability(0.60, "ok")), wide, make_cfg(max_spread=0.10))
    assert r.status == "abstain" and r.reason == "spread_too_wide"


def test_abstain_insufficient_liquidity():
    thin = book(yes_levels=[["0.45", "100"]], no_levels=[["0.50", "5"]])  # depth 5 < min_liquidity 10
    r = _eval(FakeModel(WinProbability(0.60, "ok")), thin, make_cfg(min_liquidity=10.0))
    assert r.status == "abstain" and r.reason == "insufficient_liquidity"


def test_abstain_unresolved_when_no_opponent():
    res = make_resolution(opponent=None)
    r = evaluate_resolution(res, LIQUID_BOOK, FakeModel(WinProbability(0.6, "ok")), make_cfg(), "atp",
                            surface="Grass", best_of=3, event_date=date(2026, 7, 4))
    assert r.status == "abstain" and r.reason == "unresolved_market"


def test_depth_at_ask_and_spread():
    ob = book(yes_levels=[["0.40", "30"], ["0.30", "20"]], no_levels=[["0.60", "15"], ["0.55", "25"], ["0.30", "100"]])
    # buy Yes at ask 0.42 -> match No bids >= 0.58 -> only the 0.60 level (15)
    assert depth_at_ask(ob, "yes", 0.42) == pytest.approx(15.0)
    # a worse limit ask 0.46 -> No bids >= 0.54 -> 0.60 + 0.55 = 40
    assert depth_at_ask(ob, "yes", 0.46) == pytest.approx(40.0)
    # buy No at ask 0.71 -> match Yes bids >= 0.29 -> 0.40 + 0.30 = 50
    assert depth_at_ask(ob, "no", 0.71) == pytest.approx(50.0)
    from matador.kalshi.client import reconstruct_asks
    assert spread(reconstruct_asks(LIQUID_BOOK)) == pytest.approx(0.05)


def _opp(**overrides) -> Opportunity:
    fields = dict(
        ts="2026-07-04T12:00:00+00:00", tour="ATP", event="Wimbledon Men Singles", match="Aaa vs Bbb",
        market_ticker="KXATPMATCH-26JUL04AB-A", event_ticker="KXATPMATCH-26JUL04AB", side="yes", price=0.50,
        p_model=0.60, net_edge=0.08, suggested_stake=40.0, contracts=80, liquidity=50.0,
        trigger_reason="prematch_value", occurrence_datetime="2026-07-04T13:00:00Z", flagged=False, score_state=None,
    )
    fields.update(overrides)
    return Opportunity(**fields)


def test_log_opportunity_dedups_unless_forced():
    conn = connect(":memory:")
    init_db(conn)
    first = log_opportunity(conn, _opp())
    assert first == 1
    assert get_opportunity(conn, first)["market_ticker"] == "KXATPMATCH-26JUL04AB-A"

    assert log_opportunity(conn, _opp()) is None          # same (market_ticker, side) -> deduped
    assert log_opportunity(conn, _opp(side="no")) == 2    # different side -> logged
    assert log_opportunity(conn, _opp(), force=True) == 3  # forced -> logged
    conn.close()


def test_stored_opportunity_carries_clv_fields():
    conn = connect(":memory:")
    init_db(conn)
    log_opportunity(conn, _opp(flagged=True))
    row = get_opportunity(conn, 1)
    assert row["event_ticker"] == "KXATPMATCH-26JUL04AB"
    assert row["occurrence_datetime"] == "2026-07-04T13:00:00Z"
    assert row["flagged"] == 1
    conn.close()


# ---- I/O-wrapper coverage (scan_series, evaluate_match) via a mock KalshiClient ----

_EVENT = "KXATPMATCH-26JUL04AB"
_EVENTS = {"events": [{"event_ticker": _EVENT, "title": "Aaa vs Bbb", "product_metadata": {"competition": "Wimbledon Men Singles"}}]}


def _mk(ticker, name):
    return {"ticker": ticker, "event_ticker": _EVENT, "status": "active", "yes_sub_title": name,
            "no_sub_title": name, "occurrence_datetime": "2026-07-04T13:00:00Z"}


_MARKETS = {_EVENT: [_mk(_EVENT + "-A", "Player Aaa"), _mk(_EVENT + "-B", "Player Bbb")]}


class OrientedModel:
    """Returns p only when predict is called with (name_a, name_b) in the expected order, so a
    swapped-arg orientation regression turns the alert into a 'wrong_orientation' abstain."""

    def __init__(self, a, b, p):
        self._a, self._b, self._p = a, b, p

    def predict(self, tour, name_a, name_b, *args, **kwargs):
        if name_a == self._a and name_b == self._b:
            return WinProbability(self._p, "ok")
        return WinProbability(None, "wrong_orientation")


def make_client(markets=None, fail_orderbook=False) -> KalshiClient:
    markets = markets if markets is not None else _MARKETS

    def handler(request):
        path = request.url.path
        if path.endswith("/events"):
            return httpx.Response(200, json=_EVENTS)
        if path.endswith("/orderbook"):
            return httpx.Response(500 if fail_orderbook else 200, json={} if fail_orderbook else LIQUID_BOOK)
        if path.endswith("/markets"):
            et = request.url.params.get("event_ticker")
            return httpx.Response(200, json={"markets": markets.get(et, [])})
        raise AssertionError(f"unexpected {request.url}")

    return KalshiClient(base_url="https://x/trade-api/v2", transport=httpx.MockTransport(handler))


def test_evaluate_resolution_locks_model_orientation():
    ok = _eval(OrientedModel("Player Aaa", "Player Bbb", 0.60), LIQUID_BOOK, make_cfg())
    assert ok.status == "alert" and ok.opportunity.side == "yes"
    # if the engine passed predict args swapped, OrientedModel returns None -> abstain
    swapped = _eval(OrientedModel("Player Bbb", "Player Aaa", 0.60), LIQUID_BOOK, make_cfg())
    assert swapped.status == "abstain" and swapped.reason == "wrong_orientation"


def test_scan_series_yields_alert_with_sibling_opponent():
    with make_client() as client:
        results = list(scan_series(client, OrientedModel("Player Aaa", "Player Bbb", 0.60), make_cfg(), "atp"))
    assert len(results) == 1
    assert results[0].status == "alert" and results[0].opportunity.side == "yes"
    assert results[0].opportunity.event_ticker == _EVENT  # markets[0]=yes, markets[1]=opponent


def test_scan_series_skips_single_market_event():
    with make_client(markets={_EVENT: [_mk(_EVENT + "-A", "Player Aaa")]}) as client:
        results = list(scan_series(client, OrientedModel("Player Aaa", "Player Bbb", 0.6), make_cfg(), "atp"))
    assert results == []


def test_scan_series_isolates_per_match_errors():
    with make_client(fail_orderbook=True) as client:  # 500 raises immediately (not a 429/503 retry)
        results = list(scan_series(client, OrientedModel("Player Aaa", "Player Bbb", 0.6), make_cfg(), "atp"))
    assert len(results) == 1 and results[0].status == "abstain" and results[0].reason.startswith("error:")


def test_evaluate_match_abstains_when_series_unconfigured():
    with make_client() as client:
        r = evaluate_match(client, OrientedModel("a", "b", 0.6), make_cfg(), "wta", "X", "Y")
    assert r.status == "abstain" and r.reason == "no_series_for_tour"


def test_evaluate_match_happy_path_resolves_and_alerts():
    with make_client() as client:
        r = evaluate_match(client, OrientedModel("Player Aaa", "Player Bbb", 0.60), make_cfg(), "atp", "Aaa", "Bbb")
    assert r.status == "alert" and r.opportunity.side == "yes"


def _tiny_artifact(last_date: str) -> dict:
    return {
        "initial_rating": 1500.0, "surface_weight": 0.3, "shrinkage_n0": 0.0, "min_matches": 5,
        "tours": {"atp": {
            "scales": {"3": 500.0, "5": 400.0},
            "players": {
                "player aaa": {"name": "Player Aaa", "overall": 1800.0, "overall_n": 50, "surface": {"Hard": 1850.0}, "last_date": last_date},
                "player bbb": {"name": "Player Bbb", "overall": 1600.0, "overall_n": 40, "surface": {"Hard": 1600.0}, "last_date": last_date},
            },
            "name_index": {
                "aaa_p": {"player aaa": {"name": "Player Aaa", "last_date": last_date}},
                "bbb_p": {"player bbb": {"name": "Player Bbb", "last_date": last_date}},
            },
        }},
    }


class RecordingModel:
    """Captures the (surface, best_of, as_of) predict() received, to verify _context derivation."""

    def __init__(self):
        self.args = None

    def predict(self, tour, name_a, name_b, surface, best_of, **kwargs):
        self.args = (surface, best_of, kwargs.get("as_of"))
        return WinProbability(0.5, "ok")  # no edge -> abstain, but args are recorded first


def test_context_derives_surface_best_of_and_date_from_kalshi_metadata():
    model = RecordingModel()
    with make_client() as client:
        evaluate_match(client, model, make_cfg(), "atp", "Aaa", "Bbb")
    # "Wimbledon Men Singles" -> (Grass, Bo5); event_date parsed from occurrence_datetime
    assert model.args == ("Grass", 5, date(2026, 7, 4))


def test_real_model_through_engine_alerts_and_enforces_staleness(tmp_path):
    # Fresh ratings: the real Model.predict feeds evaluate_resolution -> alert on the favored side.
    fresh = tmp_path / "fresh.json"
    fresh.write_text(json.dumps(_tiny_artifact("2026-07-01")))
    r = _eval(Model.from_artifact(fresh), LIQUID_BOOK, make_cfg(), surface="Hard", best_of=3)
    assert r.status == "alert" and r.opportunity.side == "yes"
    # Stale ratings: the staleness gate (now always on, even via event_date) abstains.
    stale = tmp_path / "stale.json"
    stale.write_text(json.dumps(_tiny_artifact("2024-01-01")))
    r2 = _eval(Model.from_artifact(stale), LIQUID_BOOK, make_cfg(), surface="Hard", best_of=3)
    assert r2.status == "abstain" and r2.reason == "stale_ratings"
