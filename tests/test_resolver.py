import json
from pathlib import Path

import httpx

from matador.kalshi.client import KalshiClient

FIXTURES = Path(__file__).parent / "fixtures"
BASE_URL = "https://external-api.demo.kalshi.co/trade-api/v2"
EVENT_TICKER = "KXATPMATCH-26JUL04DIMBER"
DIM_TICKER = f"{EVENT_TICKER}-DIM"
BER_TICKER = f"{EVENT_TICKER}-BER"


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text())


def make_client(markets_by_event: dict | None = None) -> KalshiClient:
    events = load_fixture("events_atp.json")
    bundle = load_fixture("resolver_case_dimitrov_berrettini.json")
    markets_by_event = markets_by_event or {EVENT_TICKER: bundle["markets"]}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events"):
            return httpx.Response(200, json=events)
        if request.url.path.endswith("/markets"):
            event_ticker = request.url.params.get("event_ticker")
            return httpx.Response(200, json={"markets": markets_by_event.get(event_ticker, [])})
        raise AssertionError(f"unexpected request: {request.url}")

    return KalshiClient(base_url=BASE_URL, transport=httpx.MockTransport(handler))


def test_resolve_match_finds_the_right_event_and_market():
    with make_client() as client:
        resolution = client.resolve_match("KXATPMATCH", "Grigor Dimitrov", "Matteo Berrettini")

    assert resolution is not None
    assert resolution.event_ticker == EVENT_TICKER
    assert resolution.market_ticker == DIM_TICKER
    assert resolution.yes_sub_title == "Grigor Dimitrov"
    assert resolution.yes_player_key == "dimitrov_g"
    # opponent comes from the SIBLING market (a market's own no_sub_title is the same player)
    assert resolution.opponent == "Matteo Berrettini"
    assert resolution.competition == "Wimbledon Men Singles"
    assert resolution.occurrence_datetime is not None and resolution.occurrence_datetime.startswith("2026-07-04")


def test_resolve_match_is_order_independent():
    with make_client() as client:
        resolution = client.resolve_match("KXATPMATCH", "Matteo Berrettini", "Grigor Dimitrov")

    assert resolution is not None
    assert resolution.market_ticker == BER_TICKER
    assert resolution.yes_player_key == "berrettini_m"


def test_resolve_match_accepts_surname_only_query():
    with make_client() as client:
        resolution = client.resolve_match("KXATPMATCH", "Dimitrov", "Berrettini")

    assert resolution is not None
    assert resolution.market_ticker == DIM_TICKER


def test_resolve_match_finds_multiword_surname_event_end_to_end():
    # "de Minaur vs Svajda" -- a real event whose title is a multi-word lowercase-particle
    # surname; exercises the full path against real captured market data.
    markets = load_fixture("markets_de_minaur_svajda.json")["markets"]
    with make_client(markets_by_event={"KXATPMATCH-26JUL04DESVA": markets}) as client:
        resolution = client.resolve_match("KXATPMATCH", "Alex de Minaur", "Zachary Svajda")

    assert resolution is not None
    assert resolution.market_ticker == "KXATPMATCH-26JUL04DESVA-DE"
    assert resolution.yes_player_key == "de_minaur_a"


def test_resolve_match_abstains_when_event_matches_but_market_list_is_empty():
    # The event title matches (de Minaur vs Svajda), but the market list is empty --
    # abstains rather than fabricating a ticker.
    with make_client(markets_by_event={"KXATPMATCH-26JUL04DESVA": []}) as client:
        resolution = client.resolve_match("KXATPMATCH", "Alex de Minaur", "Zachary Svajda")

    assert resolution is None


def test_resolve_match_returns_none_when_no_event_matches():
    with make_client() as client:
        resolution = client.resolve_match("KXATPMATCH", "Nobody One", "Nobody Two")

    assert resolution is None


def test_resolve_match_abstains_when_event_found_but_no_market_matches():
    with make_client(markets_by_event={EVENT_TICKER: []}) as client:
        resolution = client.resolve_match("KXATPMATCH", "Grigor Dimitrov", "Matteo Berrettini")

    assert resolution is None


def test_resolve_match_disambiguates_duplicate_titles_by_date():
    events = load_fixture("events_atp.json")
    duplicate_event = dict(events["events"][5])  # Dimitrov vs Berrettini, cloned under a new ticker
    duplicate_event["event_ticker"] = "KXATPMATCH-26AUG01DIMBER"
    events_with_duplicate = {"events": events["events"] + [duplicate_event]}

    bundle = load_fixture("resolver_case_dimitrov_berrettini.json")
    other_date_market = dict(bundle["markets"][0])
    other_date_market["event_ticker"] = duplicate_event["event_ticker"]
    other_date_market["occurrence_datetime"] = "2026-08-01T13:00:00Z"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events"):
            return httpx.Response(200, json=events_with_duplicate)
        event_ticker = request.url.params.get("event_ticker")
        if event_ticker == EVENT_TICKER:
            return httpx.Response(200, json={"markets": bundle["markets"]})
        if event_ticker == duplicate_event["event_ticker"]:
            return httpx.Response(200, json={"markets": [other_date_market]})
        raise AssertionError(f"unexpected event_ticker: {event_ticker}")

    from datetime import date

    with KalshiClient(base_url=BASE_URL, transport=httpx.MockTransport(handler)) as client:
        no_date = client.resolve_match("KXATPMATCH", "Grigor Dimitrov", "Matteo Berrettini")
        assert no_date is None  # two candidates, no date given -- ambiguous, abstain

        resolved = client.resolve_match(
            "KXATPMATCH", "Grigor Dimitrov", "Matteo Berrettini", event_date=date(2026, 7, 4)
        )
        assert resolved is not None
        assert resolved.event_ticker == EVENT_TICKER
