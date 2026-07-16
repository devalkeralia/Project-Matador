"""matador/sharp.py -- sport-key map, PURE sharp_fair_prob, SharpOddsClient, and the opp glue."""
import httpx
import pytest

from matador.backtest import devig_shin
from matador.sharp import SharpOddsClient, sharp_fair_for_opp, sharp_fair_prob, sport_key


def _book(key, a_name, a_price, b_name, b_price):
    return {"key": key, "markets": [{"key": "h2h", "outcomes": [
        {"name": a_name, "price": a_price}, {"name": b_name, "price": b_price}]}]}


def _event(home, away, books, commence="2026-08-15T18:00:00Z"):
    return {"home_team": home, "away_team": away, "commence_time": commence, "bookmakers": books}


PINN = _book("pinnacle", "Jannik Sinner", 1.30, "Alexander Zverev", 3.80)
EV = [_event("Jannik Sinner", "Alexander Zverev", [PINN])]


# ---- sport_key ----

def test_sport_key_maps_covered_tournaments_only():
    assert sport_key("atp", "Wimbledon Men Singles") == "tennis_atp_wimbledon"
    assert sport_key("wta", "WTA Cincinnati Open") == "tennis_wta_cincinnati_open"
    assert sport_key("atp", "Canadian Open (Toronto)") == "tennis_atp_canadian_open"
    assert sport_key("atp", "Bastad") is None     # uncovered 250 -> no fetch
    assert sport_key("atp", None) is None


# ---- sharp_fair_prob (pure) ----

def test_sharp_fair_prob_yes_and_no_orientation():
    p_yes, src = sharp_fair_prob(EV, "Jannik Sinner", "Alexander Zverev", "yes", "2026-08-15T18:00:00Z")
    assert src == "pinnacle" and p_yes == pytest.approx(devig_shin(1.30, 3.80), abs=1e-4)
    p_no, _ = sharp_fair_prob(EV, "Jannik Sinner", "Alexander Zverev", "no", "2026-08-15T18:00:00Z")
    assert p_no == pytest.approx(1 - devig_shin(1.30, 3.80), abs=1e-4)   # taken side = the opponent


def test_sharp_fair_prob_requires_full_pair_match():
    assert sharp_fair_prob(EV, "Carlos Alcaraz", "Novak Djokovic", "yes", None) == (None, None)   # neither present
    assert sharp_fair_prob(EV, "Jannik Sinner", "Novak Djokovic", "yes", None) == (None, None)    # one overlap only
    assert sharp_fair_prob([], "Jannik Sinner", "Alexander Zverev", "yes", None) == (None, None)  # empty board


def test_sharp_fair_prob_consensus_fallback_when_pinnacle_absent():
    books = [_book("bet365", "Jannik Sinner", 1.28, "Alexander Zverev", 4.00),
             _book("williamhill", "Jannik Sinner", 1.32, "Alexander Zverev", 3.60)]
    ev = [_event("Jannik Sinner", "Alexander Zverev", books)]
    p, src = sharp_fair_prob(ev, "Jannik Sinner", "Alexander Zverev", "yes", None, consensus_fallback=True)
    assert src == "consensus" and p is not None
    assert sharp_fair_prob(ev, "Jannik Sinner", "Alexander Zverev", "yes", None, consensus_fallback=False) == (None, None)
    # a single book is too noisy to call a consensus
    one = [_event("Jannik Sinner", "Alexander Zverev", [_book("bet365", "Jannik Sinner", 1.28, "Alexander Zverev", 4.0)])]
    assert sharp_fair_prob(one, "Jannik Sinner", "Alexander Zverev", "yes", None) == (None, None)


def test_sharp_fair_prob_bad_prices_and_doubles():
    bad = [_event("Jannik Sinner", "Alexander Zverev", [_book("pinnacle", "Jannik Sinner", 1.0, "Alexander Zverev", 3.8)])]
    assert sharp_fair_prob(bad, "Jannik Sinner", "Alexander Zverev", "yes", None) == (None, None)   # price <= 1.0
    assert sharp_fair_prob(EV, "A/B", "C/D", "yes", None) == (None, None)                            # doubles skipped


def test_sharp_fair_prob_compound_surnames_match():
    ev = [_event("Felix Auger-Aliassime", "Alejandro Davidovich Fokina",
                 [_book("pinnacle", "Felix Auger-Aliassime", 1.70, "Alejandro Davidovich Fokina", 2.20)])]
    p, src = sharp_fair_prob(ev, "Felix Auger-Aliassime", "Alejandro Davidovich Fokina", "yes", None)
    assert src == "pinnacle" and p is not None


def test_sharp_fair_prob_two_events_tiebreak_on_commence():
    e1 = _event("Jannik Sinner", "Alexander Zverev", [PINN], commence="2026-08-15T10:00:00Z")
    e2 = _event("Jannik Sinner", "Alexander Zverev",
                [_book("pinnacle", "Jannik Sinner", 1.50, "Alexander Zverev", 2.60)], commence="2026-08-15T20:00:00Z")
    p, _ = sharp_fair_prob([e1, e2], "Jannik Sinner", "Alexander Zverev", "yes", "2026-08-15T19:30:00Z")
    assert p == pytest.approx(devig_shin(1.50, 2.60), abs=1e-4)   # nearest to 19:30 is e2 (20:00)


# ---- SharpOddsClient.fetch_h2h (MockTransport) ----

def _client(handler):
    return SharpOddsClient("KEY", transport=httpx.MockTransport(handler))


def test_fetch_h2h_builds_request_and_parses():
    seen = {}

    def handler(req):
        seen["path"], seen["params"] = req.url.path, dict(req.url.params)
        return httpx.Response(200, json=EV, headers={"x-requests-remaining": "499"})

    with _client(handler) as c:
        out = c.fetch_h2h("tennis_atp_wimbledon")
    assert out == EV
    assert seen["path"].endswith("/sports/tennis_atp_wimbledon/odds")
    assert seen["params"]["regions"] == "eu" and seen["params"]["markets"] == "h2h"
    assert seen["params"]["oddsFormat"] == "decimal" and seen["params"]["apiKey"] == "KEY"


def test_fetch_h2h_404_and_empty_return_empty_list():
    with _client(lambda r: httpx.Response(404, json={})) as c:
        assert c.fetch_h2h("tennis_atp_nope") == []
    with _client(lambda r: httpx.Response(200, json=[])) as c:
        assert c.fetch_h2h("tennis_atp_wimbledon") == []


# ---- sharp_fair_for_opp (glue: cache, uncovered skip, never raises) ----

def _opp_row(**o):
    d = dict(tour="atp", event="Wimbledon Men Singles", market_player="Jannik Sinner",
             opponent="Alexander Zverev", side="yes", occurrence_datetime="2026-08-15T18:00:00Z", market_ticker="T")
    d.update(o)
    return d


def test_sharp_fair_for_opp_memoizes_by_sport_key():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(200, json=EV)

    with _client(handler) as c:
        cache = {}
        p1, s1 = sharp_fair_for_opp(c, _opp_row(), cache=cache)
        sharp_fair_for_opp(c, _opp_row(), cache=cache)   # same sport_key -> served from cache
    assert s1 == "pinnacle" and p1 is not None and calls["n"] == 1


def test_sharp_fair_for_opp_uncovered_skips_fetch():
    with _client(lambda r: (_ for _ in ()).throw(AssertionError("must not fetch"))) as c:
        assert sharp_fair_for_opp(c, _opp_row(event="Bastad")) == (None, None)


def test_sharp_fair_for_opp_swallows_errors():
    with _client(lambda r: httpx.Response(500, json={})) as c:
        assert sharp_fair_for_opp(c, _opp_row()) == (None, None)   # 5xx -> wrapped -> (None, None)
