import time
from dataclasses import dataclass
from datetime import date, datetime

import httpx

from matador.kalshi.auth import KalshiSigner
from matador.kalshi.market import Market
from matador.names import canonical_key, keys_from_title, surname_key


@dataclass(frozen=True)
class BestQuotes:
    yes_bid: float | None
    no_bid: float | None
    yes_ask: float | None  # reconstructed = 1 - no_bid; None if no No bids (empty-book guard)
    no_ask: float | None  # reconstructed = 1 - yes_bid; None if no Yes bids (empty-book guard)


def reconstruct_asks(orderbook: dict) -> BestQuotes:
    """Kalshi's orderbook returns resting bids only, as [price_str, size_str] levels per side.

    Yes ask = 1 - best No bid; No ask = 1 - best Yes bid. An empty side means that ask is
    unknown (None), never a fabricated >=1.00 -- callers must never divide by (1 - price) <= 0.
    """
    book = orderbook.get("orderbook_fp") or {}
    yes_prices = [float(price) for price, _size in (book.get("yes_dollars") or [])]
    no_prices = [float(price) for price, _size in (book.get("no_dollars") or [])]

    best_yes_bid = max(yes_prices, default=None)
    best_no_bid = max(no_prices, default=None)

    yes_ask = round(1 - best_no_bid, 4) if best_no_bid is not None else None
    no_ask = round(1 - best_yes_bid, 4) if best_yes_bid is not None else None

    return BestQuotes(yes_bid=best_yes_bid, no_bid=best_no_bid, yes_ask=yes_ask, no_ask=no_ask)


@dataclass(frozen=True)
class MatchResolution:
    event_ticker: str
    market_ticker: str
    title: str
    yes_sub_title: str
    no_sub_title: str
    yes_player_key: str  # canonical_key of whichever player this market's Yes side pays out on
    opponent: str | None = None  # the OTHER competitor's full name (a Kalshi market's no_sub_title is the same player as yes_sub_title, so the opponent comes from the sibling market)
    competition: str | None = None  # event product_metadata.competition, e.g. "Wimbledon Men Singles"
    occurrence_datetime: str | None = None  # the chosen market's scheduled match datetime (ISO)


def _occurrence_date(market: Market) -> date | None:
    if not market.occurrence_datetime:
        return None
    return datetime.fromisoformat(market.occurrence_datetime.replace("Z", "+00:00")).date()


class KalshiClient:
    """Sync Kalshi read client. Public market-data reads need no auth; the signer is only
    used for check_auth() and any future authed call."""

    def __init__(
        self,
        base_url: str,
        signer: KalshiSigner | None = None,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self._api_prefix = httpx.URL(base_url).path
        self._signer = signer
        self._client = httpx.Client(base_url=base_url, timeout=timeout, transport=transport)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "KalshiClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def _request(self, method: str, path: str, *, params: dict | None = None, authed: bool = False) -> httpx.Response:
        headers = {}
        if authed:
            if self._signer is None:
                raise RuntimeError("authed request requires a signer")
            headers = self._signer.headers(method, self._api_prefix + path)
        for attempt in range(5):
            response = self._client.request(method, path, params=params, headers=headers)
            if response.status_code in (429, 503) and attempt < 4:
                time.sleep(0.5 * 2 ** attempt)  # backoff on rate-limit / transient unavailability
                continue
            response.raise_for_status()
            return response
        return response  # unreachable: the final attempt returns or raises above

    def get_markets(
        self,
        *,
        series_ticker: str | None = None,
        event_ticker: str | None = None,
        status: str | None = None,
        tickers: str | None = None,
        limit: int = 1000,
    ) -> list[Market]:
        params: dict = {"limit": limit}
        if series_ticker:
            params["series_ticker"] = series_ticker
            params["mve_filter"] = "exclude"
        if event_ticker:
            params["event_ticker"] = event_ticker
        if status:
            params["status"] = status
        if tickers:
            params["tickers"] = tickers

        markets: list[Market] = []
        cursor = None
        while True:
            if cursor:
                params["cursor"] = cursor
            data = self._request("GET", "/markets", params=params).json()
            markets.extend(Market.from_api(m) for m in data.get("markets", []))
            cursor = data.get("cursor")
            if not cursor:
                break
        return markets

    def get_market(self, ticker: str) -> Market:
        data = self._request("GET", f"/markets/{ticker}").json()
        return Market.from_api(data["market"])

    def get_orderbook(self, ticker: str, depth: int | None = None) -> dict:
        params = {"depth": depth} if depth is not None else None
        return self._request("GET", f"/markets/{ticker}/orderbook", params=params).json()

    def best_quotes(self, ticker: str, depth: int | None = None) -> BestQuotes:
        return reconstruct_asks(self.get_orderbook(ticker, depth=depth))

    def get_events(self, series_ticker: str, status: str = "open") -> list[dict]:
        events: list[dict] = []
        params = {"series_ticker": series_ticker, "status": status, "limit": 200}
        cursor = None
        while True:
            if cursor:
                params["cursor"] = cursor
            data = self._request("GET", "/events", params=params).json()
            events.extend(data.get("events", []))
            cursor = data.get("cursor")
            if not cursor:
                break
        return events

    def check_auth(self) -> dict:
        return self._request("GET", "/portfolio/balance", authed=True).json()

    def resolve_match(
        self,
        series_ticker: str,
        player_a: str,
        player_b: str,
        event_date: date | None = None,
    ) -> MatchResolution | None:
        """Find the Kalshi market for a match by title -- never by parsing the ticker.

        Kalshi lists one event per match (title "Surname vs Surname") and, under it, one
        market per player ("will X win?"), each already carrying its own yes/no quotes.
        Abstains (returns None) rather than guessing when the event is missing, ambiguous,
        or the market list doesn't actually contain either target player.
        """
        wanted = {surname_key(canonical_key(player_a)), surname_key(canonical_key(player_b))}

        candidates = []
        for event in self.get_events(series_ticker, status="open"):
            parsed = keys_from_title(event.get("title", ""))
            if parsed is not None and set(parsed) == wanted:
                candidates.append(event)

        if not candidates:
            return None

        if event_date is not None:
            candidates = [e for e in candidates if self._event_occurs_on(e, event_date)]

        if len(candidates) != 1:
            return None  # none left, or still ambiguous -- abstain rather than guess

        event = candidates[0]
        markets = self.get_markets(event_ticker=event["event_ticker"])

        # Compare by surname only: player_a/player_b may be given surname-only (matching
        # Kalshi's own title convention), which canonical_key alone can't match against a
        # market's full-name yes_sub_title (e.g. "Dimitrov" vs "Grigor Dimitrov" -> "dimitrov_g").
        surname_a = surname_key(canonical_key(player_a))
        surname_b = surname_key(canonical_key(player_b))
        market = next((m for m in markets if surname_key(canonical_key(m.yes_sub_title)) == surname_a), None)
        if market is None:
            market = next((m for m in markets if surname_key(canonical_key(m.yes_sub_title)) == surname_b), None)
        if market is None:
            return None

        opponent_market = next((m for m in markets if m.ticker != market.ticker), None)
        return MatchResolution(
            event_ticker=event["event_ticker"],
            market_ticker=market.ticker,
            title=event.get("title", ""),
            yes_sub_title=market.yes_sub_title,
            no_sub_title=market.no_sub_title,
            yes_player_key=canonical_key(market.yes_sub_title),
            opponent=opponent_market.yes_sub_title if opponent_market else None,
            competition=(event.get("product_metadata") or {}).get("competition"),
            occurrence_datetime=market.occurrence_datetime,
        )

    def _event_occurs_on(self, event: dict, event_date: date) -> bool:
        markets = self.get_markets(event_ticker=event["event_ticker"])
        return any(_occurrence_date(m) == event_date for m in markets)
