"""Telegram message formatters -- pure text, no PTB and no I/O.

Turn the Phase-3 engine's EvalResult / Opportunity into the human-readable strings the bot
replies with. Kept separate from bot.py so every format is unit-testable without a token or a
running event loop. PAPER alerts only -- these describe a suggested manual trade, never an order.
"""
from __future__ import annotations

from matador.clv import MIN_BETS
from matador.engine import Opportunity


def _cents(price: float) -> str:
    return f"{price * 100:.0f}¢"


def format_alert(opp: Opportunity, opp_id: int, bankroll: float) -> str:
    """The mandated VALUE ALERT template. `opp_id` is the logged row id (the prior id on a dedup).

    `opp.liquidity` is order-book depth in CONTRACTS at the target ask; * price approximates the
    dollar depth available. Buying No on a "{market_player} wins" market backs the opponent -- the
    quoted player is always the market's Yes subject, whichever side has the edge.
    """
    lines = [
        f"\U0001f3be VALUE ALERT — {opp.tour} · {opp.event}",
        f"{opp.match} · pre-match",
        f'BUY {opp.side.upper()} "{opp.market_player} wins" @ {_cents(opp.price)}'
        f"  ({opp.market_ticker}, depth ~${opp.liquidity * opp.price:.0f})",
        f"Model {opp.p_model:.1%} | Market {_cents(opp.price)} | Net edge {opp.net_edge:+.1%} (after fee)",
        f"Stake ${opp.suggested_stake:.0f} → {opp.contracts} contracts "
        f"(¼-Kelly on net edge, bankroll ${bankroll:,.0f})",
        f"opp #{opp_id}",
    ]
    if opp.flagged:
        lines.append("⚠️ Large edge — check for late news (injury/withdrawal)")
    return "\n".join(lines)


# Exact engine/model abstain reasons -> friendly text. Parameterized reasons
# (e.g. "insufficient_history(3,40<20)") are matched by prefix below.
_ABSTAIN_TEXT = {
    "empty_book": "No open order book for that market right now.",
    "unresolved_market": "Couldn't find that match on Kalshi (no open market, or the names didn't resolve).",
    "unresolved_player": "Couldn't match one of those players to the model's ratings.",
    "no_series_for_tour": "No Kalshi series is configured for that tour.",
    "no_edge": "No value — the price is fair (no positive net-of-fee edge either way).",
    "one_sided_book": "The order book is one-sided (a bid side is empty), so I can't price a spread.",
    "spread_too_wide": "The bid–ask spread is too wide to trust the price.",
    "insufficient_liquidity": "Not enough order-book depth at the target price.",
    "stale_ratings": "Those player ratings are too stale to price this match safely.",
}

_ABSTAIN_PREFIX = (
    ("insufficient_history(", "Not enough match history to model one of these players."),
    ("unknown_format(", "I don't have a fitted scale for this match format (best-of)."),
    ("unknown_tour(", "That tour isn't in the model."),
    ("error:", "Something went wrong pricing that market."),
)


def format_abstain(reason: str) -> str:
    """Map an engine/model abstain reason to friendly text; unknown reasons degrade to a
    plain 'Abstained: {reason}' rather than crashing."""
    if reason in _ABSTAIN_TEXT:
        return _ABSTAIN_TEXT[reason]
    for prefix, text in _ABSTAIN_PREFIX:
        if reason.startswith(prefix):
            return text
    return f"Abstained: {reason}"


def _compact(n: float) -> str:
    """Compact count: 281427 -> '281k', 5300 -> '5.3k', 900 -> '900'."""
    if n >= 10_000:
        return f"{n / 1000:.0f}k"
    if n >= 1_000:
        return f"{n / 1000:.1f}k"
    return f"{n:.0f}"


def format_no_alert(reason: str, d) -> str:
    """Self-explaining /check reply when the market WAS priced but no alert fired: walk the user
    through the prices, the model's view, and the per-side edge math to the conclusion, so they
    understand *why* the result is what it is. `d` is an engine.Diagnostics."""
    thr = f"+{d.min_net_edge:.1%}"

    def cents(p):
        return f"{p * 100:.0f}¢" if p is not None else "—"

    def side_line(name, side, price, model_p, net):
        if price is None or net is None:
            return f"  {name} ({side}): — no price on this side of the book"
        gap = model_p - price               # my probability vs the market's implied probability
        fee = abs(gap - net)                # net = gap - fee, so the fee drag is (gap - net)
        verdict = f"clears {thr} ✅" if net >= d.min_net_edge else f"below {thr} ✗"
        return (f"  {name} ({side} @ {cents(price)}): my {model_p:.1%} vs market {price:.0%} "
                f"= {gap:+.1%}, -{fee:.1%} fee → net edge {net:+.1%} — {verdict}")

    conclusion = {
        "no_edge": f"No value. My model and the market agree to within a fee's width, so neither side "
                   f"clears the {thr} edge bar — nothing worth betting.",
        "one_sided_book": "No alert — one side of the order book is empty, so I can't price a fair spread.",
        "spread_too_wide": "No alert — even where there's an edge, the bid–ask spread is too wide to trust the price.",
        "insufficient_liquidity": "No alert — there isn't enough resting order-book depth at the target price to fill a stake.",
    }.get(reason, format_abstain(reason))

    depth = f"Order-book depth ~{_compact(d.depth)} contracts" if d.depth is not None else "Order-book depth: n/a"
    return "\n".join([
        f"\U0001f3be {d.match} · pre-match",
        "",
        "Market price (= the market's implied chance to win):",
        f"  {d.market_player}: {cents(d.yes_price)}",
        f"  {d.opponent}: {cents(d.no_price)}",
        f"  {depth}",
        "",
        f"My model: {d.market_player} {d.p_model:.1%}  ·  {d.opponent} {1 - d.p_model:.1%}",
        "",
        f"Value check (edge = my % − market price, after Kalshi fee; alert needs ≥ {thr}):",
        side_line(d.market_player, "Yes", d.yes_price, d.p_model, d.yes_net_edge),
        side_line(d.opponent, "No", d.no_price, 1 - d.p_model, d.no_net_edge),
        "",
        f"→ {conclusion}",
    ])


_FIND_OTHERS_CAP = 20  # cap the not-modellable list so a huge slate doesn't wall the message


def format_find(matches, top_n: int) -> str:
    """/find: list open matches grouped by tour -- modellable ones ranked by model strength (top
    `top_n` numbered), then the rest, one match per line. `matches` are engine.MatchInfo."""
    tours = []
    for m in matches:  # preserve first-seen tour order (ATP before WTA)
        if m.tour not in tours:
            tours.append(m.tour)
    out = ["🎾 Open matches — checkable ones ranked by model strength"]
    for tour in tours:
        group = [m for m in matches if m.tour == tour]
        modellable = sorted((m for m in group if m.modellable), key=lambda m: m.strength, reverse=True)
        others = [m for m in group if not m.modellable]
        out.append("")
        out.append(f"{tour} — {len(group)} open, {len(modellable)} modellable")
        out.append("")
        if modellable:
            out.append("Model can price (ranked):")
            for i, m in enumerate(modellable[:top_n], 1):
                tag = " · FINAL" if m.is_final else ""
                out.append(f"  {i}. {m.player_a} vs {m.player_b} — {m.event}{tag}")
            if len(modellable) > top_n:
                out.append(f"  …and {len(modellable) - top_n} more")
        else:
            out.append("Model can price: none right now")
        if others:
            out.append("")
            out.append(f"Not modellable ({len(others)}):")
            for m in others[:_FIND_OTHERS_CAP]:
                out.append(f"  • {m.player_a} vs {m.player_b}")
            if len(others) > _FIND_OTHERS_CAP:
                out.append(f"  …and {len(others) - _FIND_OTHERS_CAP} more")
    return "\n".join(out)


def format_recent(rows) -> str:
    """One compact line per logged opportunity (newest first), for /recent. `rows` are
    sqlite3.Row from storage.recent_opportunities."""
    if not rows:
        return "No opportunities logged yet."
    lines = [f"\U0001f4cb Recent opportunities ({len(rows)}):"]
    for r in rows:
        flag = " ⚠️" if r["flagged"] else ""
        lines.append(
            f'#{r["id"]}  {r["tour"]}  BUY {r["side"].upper()} "{r["market_player"]} wins" '
            f'@ {_cents(r["price"])}  ({r["net_edge"]:+.1%}, {r["contracts"]}c){flag}'
        )
    return "\n".join(lines)


def format_scan(alerts, abstain_tally, bankroll: float) -> str:
    """Render a /scan sweep: each qualifying alert block, then a one-line tally of what was
    skipped. `alerts` = list of (Opportunity, opp_id); `abstain_tally` = {reason: count}."""
    total_skipped = sum(abstain_tally.values())
    tally = ", ".join(f"{reason}: {n}" for reason, n in sorted(abstain_tally.items())) or "none"
    if not alerts:
        return f"No value alerts. Skipped {total_skipped} market(s): {tally}."
    blocks = [format_alert(opp, opp_id, bankroll) for opp, opp_id in alerts]
    footer = f"{len(alerts)} alert(s) · {total_skipped} skipped ({tally})"
    return "\n\n".join(blocks) + "\n\n" + footer


# ---- Phase 5: result / close / stats ----

def format_result(opp, result: str, fill_price: float, contracts: int, pnl: float) -> str:
    """Confirmation for /result (a recorded fill + outcome). `opp` is the opportunities Row."""
    return (
        f"✅ Recorded opp #{opp['id']}: {opp['market_player']} {opp['side'].upper()} — "
        f"{result.upper()} @ {_cents(fill_price)}, {contracts}c → P&L ${pnl:+.2f} (net of fee). "
        f"See /stats for totals."
    )


def format_close(r: dict) -> str:
    """One-line confirmation for a closing-line capture. `r` is the capture_close result dict."""
    if not r["ok"]:
        return {
            "no_such_opp": f"No opportunity #{r['opp_id']}.",
            "no_price": f"Couldn't read a two-sided price for opp #{r['opp_id']} — marked missed (excluded from CLV).",
            "too_late": f"Opp #{r['opp_id']} is past its scheduled start — too late for a clean pre-match close; marked missed.",
            "not_active": f"Opp #{r['opp_id']}'s market isn't active ({r.get('status')}) — marked missed (excluded from CLV).",
        }.get(r["reason"], f"Close failed for opp #{r['opp_id']}: {r['reason']}")
    delta_cents = (r["closing_price"] - r["entry_price"]) * 100
    return (
        f"📌 Closing line opp #{r['opp_id']}: {r['market_player']} {r['side'].upper()} @ "
        f"{_cents(r['closing_price'])} (entry {_cents(r['entry_price'])} → CLV {delta_cents:+.0f}¢)"
    )


def format_stats(s: dict) -> str:
    """Render the /stats summary (matador.clv.summarize output): hit rate, P&L, and the CLV gate."""
    lines = ["📊 Paper-trading stats", "", f"Opportunities logged: {s['n_opportunities']}"]
    if s["n_results"]:
        losses = s["n_results"] - s["wins"]
        lines.append(f"Trades recorded: {s['n_results']} — {s['wins']}W/{losses}L (hit rate {s['hit_rate']:.0%})")
        roi = f" (ROI {s['roi']:+.1%})" if s["roi"] is not None else ""
        lines.append(f"Net P&L: ${s['total_pnl']:+.2f} on ${s['staked']:.0f} staked{roi}")
    else:
        lines.append("Trades recorded: none yet (use /result)")
    lines += ["", "Closing-line value (the go-live metric, NET of fees):"]
    if s["n_clv"]:
        lo, hi = s["clv_ci"]
        lines.append(f"  {s['n_clv']} bet(s) over {s['n_clusters']} day(s)")
        lines.append(f"  Mean net CLV {s['mean_clv']:+.1%} (gross {s['mean_gross_clv']:+.1%}) · 95% CI [{lo:+.1%}, {hi:+.1%}]")
        if s["buckets"]:
            seg = ", ".join(f"{lab} {v['mean_clv']:+.1%} (n={v['n']})" for lab, v in sorted(s["buckets"].items()))
            lines.append(f"  by experience: {seg}")
        if s["go_live"]:
            gate = "✅ MET"
        else:
            gate = (f"not yet — need net-CLV CI lower bound > {s['min_effect_size']:+.1%}, "
                    f"≥ {MIN_BETS} bets ({s['n_clv']}/{MIN_BETS}), ≥ {s['min_clusters']} days ({s['n_clusters']}/{s['min_clusters']})")
        lines.append(f"  Go-live gate: {gate}")
    else:
        lines.append("  No closing lines captured yet (use /close near match start).")
    return "\n".join(lines)
