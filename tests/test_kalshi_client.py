import json
from pathlib import Path

import httpx
import pytest

from matador.kalshi.client import KalshiClient
from matador.kalshi.market import Market

FIXTURES = Path(__file__).parent / "fixtures"
BASE_URL = "https://external-api.demo.kalshi.co/trade-api/v2"
TICKER = "KXATPMATCH-26JUL04DIMBER-DIM"


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text())


def make_client(handler, signer=None) -> KalshiClient:
    transport = httpx.MockTransport(handler)
    return KalshiClient(base_url=BASE_URL, signer=signer, transport=transport)


def test_get_markets_requires_mve_filter_when_series_ticker_given():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["series_ticker"] == "KXATPMATCH"
        assert request.url.params["mve_filter"] == "exclude"
        return httpx.Response(200, json=load_fixture("markets_atp_sample.json"))

    with make_client(handler) as client:
        markets = client.get_markets(series_ticker="KXATPMATCH")

    assert len(markets) == 5
    assert all(isinstance(m, Market) for m in markets)
    assert markets[0].ticker == TICKER


def test_get_market_unwraps_the_market_key():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/trade-api/v2/markets/{TICKER}"
        return httpx.Response(200, json={"market": load_fixture("market_single_liquid.json")})

    with make_client(handler) as client:
        market = client.get_market(TICKER)

    assert market.ticker == TICKER
    assert market.yes_ask == 0.42
    assert market.yes_sub_title == "Grigor Dimitrov"


def test_get_orderbook_returns_raw_json():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/trade-api/v2/markets/{TICKER}/orderbook"
        return httpx.Response(200, json=load_fixture("orderbook_liquid.json"))

    with make_client(handler) as client:
        book = client.get_orderbook(TICKER)

    assert "orderbook_fp" in book


def test_best_quotes_reconstructs_from_the_orderbook():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=load_fixture("orderbook_liquid.json"))

    with make_client(handler) as client:
        quotes = client.best_quotes(TICKER)

    assert quotes.yes_ask == 0.42
    assert quotes.no_ask == 0.59


def test_get_events_returns_parsed_list():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["series_ticker"] == "KXATPMATCH"
        assert request.url.params["status"] == "open"
        return httpx.Response(200, json=load_fixture("events_atp.json"))

    with make_client(handler) as client:
        events = client.get_events("KXATPMATCH")

    assert len(events) == 16


def test_get_events_follows_cursor_across_pages():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.url.params))
        if "cursor" not in request.url.params:
            return httpx.Response(200, json={"events": [{"event_ticker": "A"}], "cursor": "page2"})
        return httpx.Response(200, json={"events": [{"event_ticker": "B"}], "cursor": ""})

    with make_client(handler) as client:
        events = client.get_events("KXATPMATCH")

    assert [e["event_ticker"] for e in events] == ["A", "B"]
    assert len(calls) == 2
    assert calls[1]["cursor"] == "page2"


def test_request_raises_on_http_error_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    with make_client(handler) as client:
        with pytest.raises(httpx.HTTPStatusError):
            client.get_market(TICKER)


def test_check_auth_sends_the_three_signed_headers(signer):
    seen_headers = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.update(request.headers)
        return httpx.Response(200, json={"balance": 100})

    with make_client(handler, signer=signer) as client:
        result = client.check_auth()

    assert result == {"balance": 100}
    assert seen_headers["kalshi-access-key"] == "test-key-id"
    assert "kalshi-access-timestamp" in seen_headers
    assert "kalshi-access-signature" in seen_headers


def test_check_auth_without_signer_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should never reach the network")

    with make_client(handler, signer=None) as client:
        with pytest.raises(RuntimeError):
            client.check_auth()


def test_request_retries_on_429_then_succeeds(monkeypatch):
    import matador.kalshi.client as kc
    monkeypatch.setattr(kc.time, "sleep", lambda *_: None)  # no real backoff in tests
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(200, json={"market": load_fixture("market_single_liquid.json")})

    with make_client(handler) as client:
        market = client.get_market(TICKER)

    assert market.ticker == TICKER
    assert len(calls) == 3  # 429, 429, 200


def test_request_gives_up_after_exhausting_retries(monkeypatch):
    import matador.kalshi.client as kc
    monkeypatch.setattr(kc.time, "sleep", lambda *_: None)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503, json={"error": "unavailable"})

    with make_client(handler) as client:
        with pytest.raises(httpx.HTTPStatusError):
            client.get_market(TICKER)

    assert len(calls) == 5  # 5 attempts, then raise
