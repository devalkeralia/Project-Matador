"""Phase-3 edge engine: reprice a live Kalshi tennis match, size a 1/4-Kelly stake, gate on
liquidity, and produce a loggable opportunity. On-demand only; NEVER places orders.

The decision core (evaluate_resolution) is pure -- no network, no DB -- so it is unit-testable
offline. evaluate_match / scan_series are thin I/O wrappers around a KalshiClient; log_opportunity
is the only DB side-effect. Edge/sizing math is reused from matador.edge, probabilities from
matador.model.artifact.Model, market resolution + orderbook from matador.kalshi.client. Output
feeds forward CLV paper-testing -- these are PAPER opportunities, not orders.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterator

from matador.edge import evaluate_market
from matador.kalshi.client import BestQuotes, MatchResolution, reconstruct_asks
from matador.names import canonical_key
from matador.storage import insert_opportunity, last_opportunity
from matador.tournament import tournament_context


@dataclass(frozen=True)
class Opportunity:
    """A qualifying paper opportunity -- exactly the fields storage.insert_opportunity needs."""

    ts: str
    tour: str
    event: str
    match: str
    market_ticker: str
    event_ticker: str
    side: str
    price: float
    p_model: float
    net_edge: float
    suggested_stake: float
    contracts: int
    liquidity: float
    trigger_reason: str
    occurrence_datetime: str | None   # scheduled match time; Phase 5/6 fetches the closing line for CLV
    flagged: bool                      # adverse-selection: net edge >= adverse_gap
    score_state: str | None


@dataclass(frozen=True)
class EvalResult:
    status: str                        # "alert" | "abstain"
    reason: str                        # "ok" or an abstain reason
    opportunity: Opportunity | None = None
    flagged: bool = False              # adverse-selection: implausibly large edge -> manual scrutiny


def _abstain(reason: str) -> EvalResult:
    return EvalResult("abstain", reason)


def spread(quotes: BestQuotes) -> float | None:
    """Yes-side bid-ask spread (symmetric across sides); None if either book side is empty."""
    if quotes.yes_ask is None or quotes.yes_bid is None:
        return None
    return round(quotes.yes_ask - quotes.yes_bid, 4)


def depth_at_ask(orderbook: dict, side: str, target_ask: float) -> float:
    """Executable depth to buy `side` at `target_ask`: buying Yes is filled by resting NO bids at
    price >= 1 - target_ask (buying No, by Yes bids). Sum those opposing resting-bid sizes."""
    book = orderbook.get("orderbook_fp") or {}
    ladder = book.get("no_dollars" if side == "yes" else "yes_dollars") or []
    threshold = round(1.0 - target_ask, 4)
    return sum(float(size) for price, size in ladder if float(price) >= threshold)


def evaluate_resolution(
    resolution: MatchResolution,
    orderbook: dict,
    model,
    cfg,
    tour: str,
    *,
    surface: object,
    best_of: int | None,
    event_date: date | None,
) -> EvalResult:
    """Pure decision core: quotes -> p_model -> net-of-fee edge -> liquidity/spread gate ->
    Opportunity. p_model is oriented to the market's Yes player: predict(yes_player, opponent)."""
    quotes = reconstruct_asks(orderbook)
    if quotes.yes_ask is None and quotes.no_ask is None:
        return _abstain("empty_book")
    if not resolution.opponent:
        return _abstain("unresolved_market")

    # Fail closed: a pre-match Kalshi market is imminent, so if it carries no date use today()
    # as the staleness reference rather than silently disabling the stale-ratings gate.
    as_of = event_date or date.today()
    wp = model.predict(
        tour.lower(), resolution.yes_sub_title, resolution.opponent, surface, best_of,
        as_of=as_of, max_staleness_days=cfg.elo.max_staleness_days,
    )
    if wp.p is None:
        return _abstain(wp.reason)

    edge = evaluate_market(wp.p, quotes.yes_ask, quotes.no_ask, cfg)
    if edge is None:
        return _abstain("no_edge")

    sp = spread(quotes)
    if sp is None:
        return _abstain("one_sided_book")   # a book side is empty -- distinct from a genuinely wide spread
    if sp > cfg.max_spread:
        return _abstain("spread_too_wide")
    depth = depth_at_ask(orderbook, edge.side, edge.price)
    if depth < cfg.min_liquidity:
        return _abstain("insufficient_liquidity")

    flagged = edge.net_edge >= cfg.adverse_gap
    opp = Opportunity(
        ts=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        tour=tour.upper(),
        event=resolution.competition or resolution.title,
        match=resolution.title,
        market_ticker=resolution.market_ticker,
        event_ticker=resolution.event_ticker,
        side=edge.side,
        price=edge.price,
        p_model=round(edge.p, 4),
        net_edge=round(edge.net_edge, 4),
        suggested_stake=round(edge.stake, 2),
        contracts=edge.contracts,
        liquidity=round(depth, 2),
        trigger_reason="prematch_value",
        occurrence_datetime=resolution.occurrence_datetime,
        flagged=flagged,
        score_state=None,
    )
    return EvalResult("alert", "ok", opp, flagged=flagged)


def _context(resolution: MatchResolution, tour: str, surface, best_of, event_date):
    """Fill in surface / best_of / event_date from Kalshi metadata unless the caller overrode them."""
    d_surface, d_best_of = tournament_context(resolution.competition, tour)
    if surface is None:
        surface = d_surface
    if best_of is None:
        best_of = d_best_of
    if event_date is None and resolution.occurrence_datetime:
        event_date = date.fromisoformat(resolution.occurrence_datetime[:10])
    return surface, best_of, event_date


def evaluate_match(client, model, cfg, tour, player_a, player_b, *, surface=None, best_of=None, event_date=None) -> EvalResult:
    """/check: resolve one match on Kalshi, then evaluate it."""
    series = getattr(cfg.series, tour.lower(), None)
    if series is None:
        return _abstain("no_series_for_tour")
    resolution = client.resolve_match(series, player_a, player_b, event_date)
    if resolution is None:
        return _abstain("unresolved_market")
    orderbook = client.get_orderbook(resolution.market_ticker)
    surface, best_of, event_date = _context(resolution, tour, surface, best_of, event_date)
    return evaluate_resolution(resolution, orderbook, model, cfg, tour, surface=surface, best_of=best_of, event_date=event_date)


def scan_series(client, model, cfg, tour) -> Iterator[EvalResult]:
    """/scan: one on-demand pass over a series' open events (no continuous polling)."""
    series = getattr(cfg.series, tour.lower(), None)
    if series is None:
        return
    for event in client.get_events(series, status="open"):
        try:
            markets = client.get_markets(event_ticker=event["event_ticker"])
            if len(markets) < 2:
                continue  # need both player-markets to know the matchup
            yes_market, opp_market = markets[0], markets[1]
            resolution = MatchResolution(
                event_ticker=event["event_ticker"],
                market_ticker=yes_market.ticker,
                title=event.get("title", ""),
                yes_sub_title=yes_market.yes_sub_title,
                no_sub_title=yes_market.no_sub_title,
                yes_player_key=canonical_key(yes_market.yes_sub_title),
                opponent=opp_market.yes_sub_title,
                competition=(event.get("product_metadata") or {}).get("competition"),
                occurrence_datetime=yes_market.occurrence_datetime,
            )
            orderbook = client.get_orderbook(resolution.market_ticker)
            surface, best_of, event_date = _context(resolution, tour, None, None, None)
            yield evaluate_resolution(resolution, orderbook, model, cfg, tour, surface=surface, best_of=best_of, event_date=event_date)
        except Exception as exc:  # one bad market must not abort the whole sweep
            yield _abstain(f"error:{type(exc).__name__}")


def log_opportunity(conn, opp: Opportunity, *, force: bool = False) -> int | None:
    """Insert a paper opportunity, deduping on (market_ticker, side) unless force=True. Returns
    the new row id, or None if a prior alert for this contract+side already exists."""
    if not force and last_opportunity(conn, opp.market_ticker, opp.side) is not None:
        return None
    return insert_opportunity(
        conn,
        ts=opp.ts, tour=opp.tour, event=opp.event, match=opp.match,
        market_ticker=opp.market_ticker, event_ticker=opp.event_ticker, side=opp.side, price=opp.price,
        p_model=opp.p_model, net_edge=opp.net_edge, suggested_stake=opp.suggested_stake,
        contracts=opp.contracts, liquidity=opp.liquidity, trigger_reason=opp.trigger_reason,
        occurrence_datetime=opp.occurrence_datetime, flagged=int(opp.flagged), score_state=opp.score_state,
    )
