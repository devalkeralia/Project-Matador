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

from matador.edge import evaluate_market, net_edge
from matador.kalshi.client import BestQuotes, MatchResolution, reconstruct_asks
from matador.model.probability import resolve_player
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
    market_player: str                 # yes_sub_title: the player the market's Yes contract pays out on
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
    experience: int | None             # min prior-match count of the two players (thin-player flag + CLV segmentation)
    score_state: str | None


@dataclass(frozen=True)
class Diagnostics:
    """Analysis snapshot for a /check reply when the market was priced but no alert fired, so the
    user still sees where it stands: prices, model prob, per-side net edge (see alerts.format_no_alert)."""

    match: str
    market_player: str                 # yes_sub_title
    opponent: str
    p_model: float                     # model P(market_player beats opponent)
    yes_price: float | None
    no_price: float | None
    yes_net_edge: float | None
    no_net_edge: float | None
    min_net_edge: float                # the alert threshold, for the "below threshold" message
    depth: float | None                # executable depth (contracts) at the Yes ask


@dataclass(frozen=True)
class EvalResult:
    status: str                        # "alert" | "abstain"
    reason: str                        # "ok" or an abstain reason
    opportunity: Opportunity | None = None
    flagged: bool = False              # adverse-selection: implausibly large edge -> manual scrutiny
    diagnostics: Diagnostics | None = None  # set on a priced-but-no-alert abstain, for a rich /check reply


def _abstain(reason: str, diagnostics: Diagnostics | None = None) -> EvalResult:
    return EvalResult("abstain", reason, diagnostics=diagnostics)


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

    # The market IS priced from here on -- capture a snapshot so a no-alert /check can still show
    # prices, model prob, and per-side net edge (see matador.alerts.format_no_alert).
    yes_ne = net_edge(wp.p, quotes.yes_ask, cfg.fee_coefficient) if quotes.yes_ask is not None else None
    no_ne = net_edge(1.0 - wp.p, quotes.no_ask, cfg.fee_coefficient) if quotes.no_ask is not None else None
    diag = Diagnostics(
        match=resolution.title, market_player=resolution.yes_sub_title, opponent=resolution.opponent,
        p_model=round(wp.p, 4), yes_price=quotes.yes_ask, no_price=quotes.no_ask,
        yes_net_edge=round(yes_ne, 4) if yes_ne is not None else None,
        no_net_edge=round(no_ne, 4) if no_ne is not None else None,
        min_net_edge=cfg.min_net_edge,
        depth=round(depth_at_ask(orderbook, "yes", quotes.yes_ask), 2) if quotes.yes_ask is not None else None,
    )

    # Uncertainty-aware sizing: haircut the Kelly fraction for THIN players (their Elo is
    # overconfident), so a real-but-uncertain edge is sized down rather than suppressed.
    thin = wp.experience is not None and wp.experience < cfg.thin_matches
    kf = cfg.kelly_fraction * cfg.thin_kelly_haircut if thin else cfg.kelly_fraction
    edge = evaluate_market(wp.p, quotes.yes_ask, quotes.no_ask, cfg, kelly_fraction=kf)
    if edge is None:
        return _abstain("no_edge", diag)

    sp = spread(quotes)
    if sp is None:
        return _abstain("one_sided_book", diag)   # a book side is empty -- distinct from a genuinely wide spread
    if sp > cfg.max_spread:
        return _abstain("spread_too_wide", diag)
    depth = depth_at_ask(orderbook, edge.side, edge.price)
    if depth < cfg.min_liquidity:
        return _abstain("insufficient_liquidity", diag)

    flagged = edge.net_edge >= cfg.adverse_gap
    opp = Opportunity(
        ts=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        tour=tour.upper(),
        event=resolution.competition or resolution.title,
        match=resolution.title,
        market_ticker=resolution.market_ticker,
        event_ticker=resolution.event_ticker,
        market_player=resolution.yes_sub_title,
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
        experience=wp.experience,
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
        # Not a head-to-head market -- try the tournament-outright series (a Grand Slam final is
        # listed only there; at the final it collapses to an H2H). See resolve_outright_final.
        outright = getattr(cfg.series, f"{tour.lower()}_outright", None)
        if outright is not None:
            resolution = client.resolve_outright_final(outright, player_a, player_b)
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


def scan_outright_finals(client, model, cfg, tour) -> Iterator[EvalResult]:
    """/scan companion: surface tournament FINALS that Kalshi lists only as outright markets
    (a KXATP/KXWTA event down to exactly two active contracts). A full-field futures market
    (more than two active) is not a head-to-head, so it is skipped."""
    outright = getattr(cfg.series, f"{tour.lower()}_outright", None)
    if outright is None:
        return
    for event in client.get_events(outright, status="open"):
        try:
            active = [m for m in client.get_markets(event_ticker=event["event_ticker"]) if m.status == "active"]
            if len(active) != 2:
                continue  # only a final (two left) collapses the outright to a head-to-head
            yes_market, opp_market = active[0], active[1]
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


@dataclass(frozen=True)
class MatchInfo:
    """One open match for /find: who's playing, whether the model can price it, and a strength
    score (higher, lower overall Elo) used to rank 'top' matches (a rankings proxy)."""

    tour: str
    player_a: str
    player_b: str
    event: str                              # competition, or the event title
    is_final: bool
    modellable: bool                        # both players resolve in the tour's index
    strength: tuple[float, float] | None    # (higher Elo, lower Elo); None if not modellable


def _player_strength(model, tour: str, name: str, as_of: date) -> float | None:
    """Player's overall Elo (a rankings proxy for /find), or None if unresolved in the tour index."""
    tm = model.tours.get(tour.lower())
    if tm is None:
        return None
    pid = resolve_player(tm.name_index, name, as_of)
    return None if pid is None else tm.book.overall_rating(pid)


def _match_info(model, tour, a, b, event, occurrence, *, is_final) -> MatchInfo:
    as_of = date.fromisoformat(occurrence[:10]) if occurrence else date.today()
    sa = _player_strength(model, tour, a, as_of)
    sb = _player_strength(model, tour, b, as_of)
    modellable = sa is not None and sb is not None
    event_name = (event.get("product_metadata") or {}).get("competition") or event.get("title", "")
    return MatchInfo(
        tour=tour.upper(), player_a=a, player_b=b, event=event_name, is_final=is_final,
        modellable=modellable, strength=(max(sa, sb), min(sa, sb)) if modellable else None,
    )


def list_open_matches(client, model, cfg, tour) -> list[MatchInfo]:
    """/find: enumerate this tour's open matches -- H2H events plus any outright FINAL (two active
    contracts) -- flagging which the model can price. Read-only; no orderbook fetch, no pricing."""
    matches: list[MatchInfo] = []
    series = getattr(cfg.series, tour.lower(), None)
    if series is not None:
        for event in client.get_events(series, status="open"):
            try:
                markets = client.get_markets(event_ticker=event["event_ticker"])
                if len(markets) < 2:
                    continue
                matches.append(_match_info(model, tour, markets[0].yes_sub_title, markets[1].yes_sub_title,
                                           event, markets[0].occurrence_datetime, is_final=False))
            except Exception:  # one bad event must not abort the listing
                continue
    outright = getattr(cfg.series, f"{tour.lower()}_outright", None)
    if outright is not None:
        for event in client.get_events(outright, status="open"):
            try:
                active = [m for m in client.get_markets(event_ticker=event["event_ticker"]) if m.status == "active"]
                if len(active) != 2:
                    continue
                matches.append(_match_info(model, tour, active[0].yes_sub_title, active[1].yes_sub_title,
                                           event, active[0].occurrence_datetime, is_final=True))
            except Exception:
                continue
    return matches


def log_opportunity(conn, opp: Opportunity, *, force: bool = False) -> int | None:
    """Insert a paper opportunity, deduping on (market_ticker, side) unless force=True. Returns
    the new row id, or None if a prior alert for this contract+side already exists."""
    if not force and last_opportunity(conn, opp.market_ticker, opp.side) is not None:
        return None
    return insert_opportunity(
        conn,
        ts=opp.ts, tour=opp.tour, event=opp.event, match=opp.match,
        market_ticker=opp.market_ticker, event_ticker=opp.event_ticker, market_player=opp.market_player,
        side=opp.side, price=opp.price,
        p_model=opp.p_model, net_edge=opp.net_edge, suggested_stake=opp.suggested_stake,
        contracts=opp.contracts, liquidity=opp.liquidity, trigger_reason=opp.trigger_reason,
        occurrence_datetime=opp.occurrence_datetime, flagged=int(opp.flagged), experience=opp.experience,
        score_state=opp.score_state,
    )
