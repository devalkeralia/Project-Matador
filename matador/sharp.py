"""Sharp-line reference via the-odds-api (Pinnacle) -- the binding go-live gate's truth proxy.

CLV vs Kalshi's OWN close is circular (can't tell a soft Kalshi line from model error). This module
fetches a SHARP bookmaker's closing line (Pinnacle, EU region) so clv.summarize can gate on "did our
entry beat the sharp close?" -- the canonical, de-circularized edge test. Read-only odds data only;
the bot still never places orders.

Design: `SharpOddsClient` isolates all the-odds-api I/O; `sharp_fair_prob` is PURE (takes event
dicts) so it's unit-testable without HTTP. Fair probabilities come from matador.backtest.devig_shin
(Shin de-vig). Coverage is per-tournament (Slams + Masters + big 500s) -- a match we can't map, find,
or price simply has NO sharp reference (sharp_close stays NULL), which is fail-safe: that bet drops
out of the sharp go-live gate rather than poisoning it.
"""
from __future__ import annotations

import logging
import statistics
import time
from datetime import datetime, timezone

import httpx

from matador.backtest import devig_shin
from matador.names import canonical_key, surname_key

log = logging.getLogger(__name__)

# the-odds-api tennis sport keys are per-tournament and irregular, so map a competition-string
# keyword -> the key SUFFIX (the tour prefix `tennis_atp_`/`tennis_wta_` is added at lookup). A
# wrong-tour key (e.g. a WTA Paris that doesn't exist) just 404s -> [] -> no sharp ref (fail-safe).
# Confirmed against GET /v4/sports (2026-07); RE-VERIFY the slugs live at the August Masters.
_SUFFIX_BY_KEYWORD = {
    "australian open": "aus_open_singles",
    "french open": "french_open", "roland garros": "french_open",
    "wimbledon": "wimbledon",
    "us open": "us_open",
    "indian wells": "indian_wells",
    "miami": "miami_open",
    "monte": "monte_carlo_masters",
    "madrid": "madrid_open",
    "rome": "italian_open", "italian": "italian_open",
    "canadian": "canadian_open", "canada": "canadian_open", "toronto": "canadian_open", "montreal": "canadian_open",
    "cincinnati": "cincinnati_open",
    "shanghai": "shanghai_masters",
    "paris": "paris_masters",
    "dubai": "dubai",
    "halle": "halle_open",
    "queen": "queens_club_champ",
    "hamburg": "hamburg_open",
    "munich": "munich",
    "barcelona": "barcelona_open",
    "qatar": "qatar_open", "doha": "qatar_open",
    "china open": "china_open", "beijing": "china_open",
    "stuttgart": "stuttgart_open",
    "strasbourg": "strasbourg",
    "wuhan": "wuhan_open",
    "bad homburg": "bad_homburg_open",
    "charleston": "charleston_open",
    "berlin": "german_open",
}


def sport_key(tour: str, competition: str | None) -> str | None:
    """Kalshi competition string (e.g. 'Wimbledon Men Singles') + tour -> a the-odds-api sport key
    (e.g. 'tennis_atp_wimbledon'), or None if not covered. None => no fetch (this map is the credit guard)."""
    if not competition:
        return None
    comp = competition.lower()
    for keyword, suffix in _SUFFIX_BY_KEYWORD.items():
        if keyword in comp:
            return f"tennis_{tour.lower()}_{suffix}"
    return None


class SharpOddsClient:
    """Read-only the-odds-api v4 client (tennis h2h). Mirrors KalshiClient's httpx wrapper; short
    timeout so a slow sharp read can't blow the pre-match capture window."""

    def __init__(self, api_key: str, base_url: str = "https://api.the-odds-api.com/v4",
                 region: str = "eu", consensus_fallback: bool = True, timeout: float = 5.0,
                 transport: httpx.BaseTransport | None = None):
        self._api_key = api_key
        self._region = region
        self.consensus_fallback = consensus_fallback  # carried on the client so callers needn't thread cfg
        self._client = httpx.Client(base_url=base_url, timeout=timeout, transport=transport)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "SharpOddsClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def fetch_h2h(self, sport_key: str) -> list[dict]:
        """All upcoming h2h events for a tournament (decimal odds, EU books), or [] on empty/404.
        Retries 429/503; logs remaining credits. Other HTTP errors propagate (caller wraps them)."""
        params = {"apiKey": self._api_key, "regions": self._region, "markets": "h2h", "oddsFormat": "decimal"}
        for attempt in range(5):
            resp = self._client.request("GET", f"/sports/{sport_key}/odds", params=params)
            if resp.status_code in (429, 503) and attempt < 4:
                time.sleep(0.5 * 2 ** attempt)
                continue
            if resp.status_code == 404:
                return []  # unknown / out-of-season sport key
            resp.raise_for_status()
            remaining = resp.headers.get("x-requests-remaining")
            if remaining is not None:
                log.info("the-odds-api %s: %s credits remaining", sport_key, remaining)
            data = resp.json()
            return data if isinstance(data, list) else []
        return []


def _parse_dt(iso) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _h2h_prices(bookmaker: dict) -> dict | None:
    """{outcome_name: decimal_price} for a bookmaker's h2h market, or None if absent/incomplete."""
    for mkt in bookmaker.get("markets", []):
        if mkt.get("key") == "h2h":
            prices = {o["name"]: float(o["price"]) for o in mkt.get("outcomes", []) if o.get("name") and o.get("price")}
            return prices if len(prices) == 2 else None
    return None


def _p_market_player(prices: dict, market_player_key: str) -> float | None:
    """Shin-devigged P(market_player) from a book's two-way {name: decimal} h2h prices, oriented to
    the market_player via surname key. None if the names don't line up or a price is degenerate."""
    by_key = {surname_key(canonical_key(name)): odds for name, odds in prices.items()}
    o_mp = by_key.get(market_player_key)
    opp_keys = [k for k in by_key if k != market_player_key]
    if o_mp is None or len(opp_keys) != 1:
        return None
    o_opp = by_key[opp_keys[0]]
    if o_mp <= 1.0 or o_opp <= 1.0:
        return None  # devig_shin needs valid decimal odds (> 1.0)
    return devig_shin(o_mp, o_opp)


def sharp_fair_prob(events, market_player, opponent, side, occurrence_datetime, *, consensus_fallback=True):
    """PURE. From a the-odds-api events list, the Shin-devigged fair probability of the TAKEN side
    winning, plus the source ('pinnacle' | 'consensus'), or (None, None) on any miss.

    Full-PAIR name match (mirrors client.resolve_match's set idiom); Pinnacle preferred, else the
    median of the other EU books (if consensus_fallback and >= 2 price it). `side` orients: the taken
    side is market_player when side=='yes', else the opponent."""
    if not market_player or not opponent or "/" in market_player or "/" in opponent:
        return None, None  # need both singles names
    want = {surname_key(canonical_key(market_player)), surname_key(canonical_key(opponent))}
    if len(want) != 2:
        return None, None  # the pair collapses to one key -> can't disambiguate
    cands = [e for e in events
             if {surname_key(canonical_key(e.get("home_team") or "")),   # `or ""` -> a JSON-null team can't TypeError the scan
                 surname_key(canonical_key(e.get("away_team") or ""))} == want]
    if not cands:
        return None, None
    if len(cands) > 1:  # essentially never for a full-pair match; tiebreak on nearest start
        target = _parse_dt(occurrence_datetime)
        if target is None:
            return None, None
        cands.sort(key=lambda e: abs(((_parse_dt(e.get("commence_time")) or target) - target).total_seconds()))
    books = {b.get("key"): b for b in cands[0].get("bookmakers", [])}
    mk = surname_key(canonical_key(market_player))

    p_mp, source = None, None
    if "pinnacle" in books:
        prices = _h2h_prices(books["pinnacle"])
        if prices is not None:
            p_mp = _p_market_player(prices, mk)
            source = "pinnacle" if p_mp is not None else None
    if p_mp is None and consensus_fallback:
        others = []
        for k, b in books.items():
            if k == "pinnacle":
                continue
            prices = _h2h_prices(b)
            if prices is not None:
                p = _p_market_player(prices, mk)
                if p is not None:
                    others.append(p)
        if len(others) >= 2:  # a lone book is too noisy to call a "consensus"
            p_mp, source = statistics.median(others), "consensus"
    if p_mp is None:
        return None, None
    fair_taken = p_mp if side == "yes" else 1.0 - p_mp
    return round(fair_taken, 4), source


def sharp_fair_for_opp(client, opp, *, cache=None):
    """Glue: fetch + compute the sharp fair prob for a logged opportunity Row. Returns (prob, source)
    or (None, None). NEVER raises -- a sharp miss must not disturb the Kalshi closing-line capture.
    `cache` (a dict) memoizes fetch_h2h by sport_key within a batch /close. Uses the client's
    consensus_fallback setting."""
    try:
        key = sport_key(opp["tour"], opp["event"])
        opponent = opp["opponent"]
        if key is None or not opponent:
            return None, None
        if cache is not None and key in cache:
            events = cache[key]
        else:
            try:
                events = client.fetch_h2h(key)
            except Exception:
                if cache is not None:
                    cache[key] = []  # negative-cache: a failing sport_key must not re-run the retry ladder per row
                raise
            if cache is not None:
                cache[key] = events
        return sharp_fair_prob(events, opp["market_player"], opponent, opp["side"],
                               opp["occurrence_datetime"], consensus_fallback=client.consensus_fallback)
    except Exception:
        log.warning("sharp fair-prob failed for opp %s", opp["market_ticker"], exc_info=True)
        return None, None
