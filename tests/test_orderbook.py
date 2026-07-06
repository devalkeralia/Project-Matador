import json
from pathlib import Path

from matador.kalshi.client import reconstruct_asks

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text())


def test_reconstruct_asks_against_real_liquid_orderbook():
    # captured live from Kalshi production (read-only) -- KXATPMATCH-26JUL04DIMBER-DIM
    quotes = reconstruct_asks(load_fixture("orderbook_liquid.json"))
    assert quotes.yes_bid == 0.41
    assert quotes.no_bid == 0.58
    assert quotes.yes_ask == 0.42
    assert quotes.no_ask == 0.59


def test_reconstruct_asks_matches_the_markets_own_quoted_prices():
    quotes = reconstruct_asks(load_fixture("orderbook_liquid.json"))
    market = load_fixture("market_single_liquid.json")
    assert quotes.yes_ask == float(market["yes_ask_dollars"])
    assert quotes.no_ask == float(market["no_ask_dollars"])


def test_reconstruct_asks_empty_book_returns_none_not_a_fabricated_price():
    quotes = reconstruct_asks({"orderbook_fp": {"yes_dollars": [], "no_dollars": []}})
    assert quotes.yes_bid is None
    assert quotes.no_bid is None
    assert quotes.yes_ask is None
    assert quotes.no_ask is None


def test_reconstruct_asks_empty_book_fixture_from_probe():
    # captured live from Kalshi demo -- a real market with zero trading activity
    quotes = reconstruct_asks(load_fixture("orderbook_sample.json"))
    assert quotes.yes_ask is None
    assert quotes.no_ask is None


def test_reconstruct_asks_one_sided_book_only_guards_the_empty_side():
    one_sided = {"orderbook_fp": {"yes_dollars": [["0.40", "100"]], "no_dollars": []}}
    quotes = reconstruct_asks(one_sided)
    assert quotes.yes_bid == 0.40
    assert quotes.no_bid is None
    assert quotes.no_ask == 0.60  # 1 - yes_bid, still computable
    assert quotes.yes_ask is None  # no No bids -> can't reconstruct Yes ask


def test_reconstruct_asks_never_returns_an_ask_at_or_above_one():
    edge = {"orderbook_fp": {"yes_dollars": [["0.99", "10"]], "no_dollars": [["0.99", "10"]]}}
    quotes = reconstruct_asks(edge)
    assert quotes.yes_ask < 1.0
    assert quotes.no_ask < 1.0


def test_reconstruct_asks_takes_max_price_not_last_element():
    # levels intentionally out of order -- must not assume sorted input
    book = {"orderbook_fp": {"yes_dollars": [["0.10", "5"], ["0.45", "5"], ["0.20", "5"]], "no_dollars": []}}
    quotes = reconstruct_asks(book)
    assert quotes.yes_bid == 0.45
