"""Telegram value-alert bot -- the Phase-4 delivery layer over the Phase-3 engine.

Long-polls TELEGRAM for commands; on /check or /scan it hits Kalshi (read-only), runs the
engine, and replies with a formatted alert (or a friendly abstain), logging qualifying paper
opportunities to SQLite. PAPER ONLY -- no order/trade endpoint is reachable and no signer is
constructed. On-demand only: it polls Telegram, never Kalshi on a timer.

Design mirrors scripts/scan.py: a PTB-free, dependency-injected SYNC CORE (run_check/run_scan/
run_recent + the pure helpers) that is fully unit-testable, wrapped by thin ASYNC handlers that
offload the blocking core onto a worker thread via asyncio.to_thread so the event loop stays
responsive. Each command opens its own KalshiClient + sqlite connection inside that thread
(sqlite is thread-affine); the Model is loaded once at startup and shared read-only.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import Counter
from contextlib import nullcontext
from datetime import date, datetime, timedelta, timezone
from itertools import chain
from pathlib import Path

import httpx
from telegram import Update
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes, filters

from matador import storage
from matador.alerts import (
    format_abstain, format_alert, format_close, format_find, format_no_alert,
    format_auto_result, format_recent, format_result, format_scan, format_stats,
)
from matador.clv import net_pnl, summarize
from matador.engine import (
    backed_player, backed_player_from, evaluate_match, list_open_matches, log_opportunity,
    scan_outright_finals, scan_series,
)
from matador.kalshi.client import KalshiClient
from matador.sharp import SharpOddsClient, last_requests_remaining, sharp_fair_for_opp, sharp_start_for_opp
from matador.storage import (
    get_opportunity, last_opportunity, last_position, pending_captures, recent_opportunities, settled_bets,
)

log = logging.getLogger(__name__)

PROD_BASE = "https://external-api.kalshi.com/trade-api/v2"  # public read-only; --demo uses cfg.kalshi_base_url

HELP = (
    "🎾 Matador — tennis value alerts (paper only; I never place orders)\n\n"
    "/check — value-check one match now; alerts if it's mispriced, else shows the analysis\n"
    "        usage: /check <player> vs <player> [atp|wta]\n"
    "        e.g.   /check Sinner vs Zverev\n"
    "/preview — same as /check but NEVER logged; safe to run or demo mid-paper-test\n"
    "        usage: /preview <player> vs <player> [atp|wta]\n"
    "/find [atp|wta] — list open matches; checkable ones ranked by model strength\n"
    "/scan — sweep all open ATP/WTA markets for value\n"
    "/recent [n] — the last n logged opportunities (default 10)\n"
    "/close [opp_id] [pre] — capture the closing line near match start (no id = all pending; 'pre' = confirm pre-match on an untimed market)\n"
    "/result <opp_id> <win|loss> <fill_price> [contracts] — OVERRIDE an outcome (results are recorded\n"
    "        automatically from Kalshi's settlement; you only need this to correct one)\n"
    "/stats — hit rate, P&L, and closing-line value (the go-live metric)\n"
    "/notes — how to read an alert & the /check breakdown\n"
    "/help — this message"
)

# Longer guide to reading the messages (own command so /help stays a quick reference).
NOTES = (
    "📘 How to read Matador messages\n\n"
    "Prices are in cents = the market's implied probability (54¢ ≈ 54% chance).\n\n"
    "VALUE ALERT (a mispricing worth a bet):\n"
    '• BUY YES "X wins" @ 54¢ — buy X\'s Yes contract at 54¢ (back X); BUY NO backs the opponent.\n'
    "• Model 60% | Market 54¢ — my model's win chance vs the market's price.\n"
    "• Net edge +4.3% (after fee) — my edge once Kalshi's fee is subtracted; I only alert at ≥ +3%.\n"
    "• Stake $46 → 85 contracts — a ¼-Kelly stake (sized on the net edge, capped at your max).\n"
    "• opp #123 — the log id, for later result/CLV tracking.\n"
    "• ⚠️ — edge looks unusually large; check for late news (injury/withdrawal) before trusting it.\n\n"
    "No-value reply (I priced it, but there's no bet):\n"
    "• Market price — each player's price = the market's implied win chance.\n"
    "• My model — my estimated win chance for each player.\n"
    "• Value check — per side: my % − market price = raw gap, then − the Kalshi fee = net edge. "
    'A side only alerts at net edge ≥ +3%; if neither clears it, it\'s "no value".\n'
    "• Depth — resting order-book size; too thin and I won't alert even with an edge.\n\n"
    "Tracking (the go-live test):\n"
    "• /close [opp_id] — snapshot the market price at match start (the CLV baseline); run it near "
    "the start. No id = capture all pending.\n"
    "• Outcomes record THEMSELVES from Kalshi's settlement a few hours after each match, and you get a\n"
    "  🧾 confirmation DM. Nothing to do — including for injuries and retirements, which settle normally\n"
    "  because someone still advances. A match that was never PLAYED (withdrawal/walkover) is refunded\n"
    "  by Kalshi and auto-recorded as VOID, excluded from the stats. Only a settlement I have never seen\n"
    "  before asks you anything (⚖️).\n"
    "• /result <opp_id> <win|loss> <fill_price> [contracts] — override an outcome you disagree with.\n"
    "• /stats — hit rate, net P&L, and mean CLV with a 95% CI. CLV = closing price − your entry "
    "(positive = you beat the close). Go-live needs the CI lower bound > 0 over 200+ bets.\n\n"
    "Signals only — I never place orders. You trade manually on Kalshi."
)

_NOTES_FOOTER = "\n\nℹ️ /notes — how to read this message"
_RECENT_DEFAULT = 10
_RECENT_MAX = 50
_FIND_TOP_N = 5
CAPTURE_BEFORE_START = timedelta(minutes=5)   # auto-capture this long BEFORE scheduled start (guaranteed pre-match)
CAPTURE_LATE_GRACE = timedelta(minutes=5)     # refuse a capture more than this past scheduled start. Kalshi trades tennis IN-PLAY (market stays 'active' through the match), so status alone can't tell pre-match from in-play -- a tight grace is the only real guard against snapshotting a live price as the "close"
CAPTURE_EARLIEST = timedelta(minutes=60)      # refuse a capture more than this BEFORE start (not a miss -- leave pending): a batch /close must not snapshot tomorrow's match as its "close", omitting the late-info drift CLV exists to measure
RESCHEDULE_EPSILON = timedelta(minutes=2)     # ignore sub-epsilon start-time drift; a larger future shift = a postponement -> re-arm the capture


# ---- pure helpers (testable) ----

def is_authorized(chat_id: int, allowed: int) -> bool:
    """Owner-only guard, belt-and-suspenders with the per-handler filters.Chat."""
    return chat_id == allowed


def parse_check_args(text: str, default_tour: str) -> tuple[str, str, str] | None:
    """Parse `/check` arguments into (player_a, player_b, tour). Peels a trailing atp/wta token,
    then splits the rest on ' v '/' vs '/' vs. '. Returns None on failure (non-raising replacement
    for scan.py._split_players) so the handler can reply with a usage string."""
    text = text.strip()
    if not text:
        return None
    tour = default_tour
    words = text.split()
    if words[-1].lower() in ("atp", "wta"):
        tour = words[-1].lower()
        text = " ".join(words[:-1])
    parts = re.split(r"\s+vs?\.?\s+", text.strip(), maxsplit=1)
    if len(parts) != 2:
        return None
    a, b = parts[0].strip(), parts[1].strip()
    if not a or not b:
        return None
    return a, b, tour


def split_message(text: str, limit: int = 4096) -> list[str]:
    """Pack whole lines into <=limit-char chunks (Telegram's per-message cap) so an alert block
    never breaks mid-number. A single pathological over-long line is hard-split."""
    chunks: list[str] = []
    cur = ""
    for line in text.split("\n"):
        if len(line) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            for i in range(0, len(line), limit):
                chunks.append(line[i:i + limit])
            continue
        candidate = f"{cur}\n{line}" if cur else line
        if len(candidate) > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = candidate
    if cur:
        chunks.append(cur)
    return chunks


# ---- sync cores (PTB-free, dependency-injected: client + conn passed in) ----

def _exposure_warning(conn, cfg) -> str:
    """A warning line when total OPEN (unsettled) suggested stake exceeds max_open_exposure_pct of
    bankroll -- there's no per-alert cap for correlated same-day alerts, and the owner trades manually,
    so a flag is the right lever. Empty string when within the cap."""
    cap = cfg.max_open_exposure_pct * cfg.bankroll
    exposure = storage.open_exposure(conn)
    if exposure > cap:
        return (f"\n⚠️ Open exposure ${exposure:.0f} exceeds {cfg.max_open_exposure_pct:.0%} of bankroll "
                f"(${cap:.0f}) across unsettled bets — consider skipping or sizing down.")
    return ""


_DRY_BANNER = "🔍 PREVIEW — evaluated but NOT logged; the paper sample is untouched.\n\n"


def _dry_banner(dry: bool) -> str:
    return _DRY_BANNER if dry else ""


def _prior_position_id(conn, opp) -> int | None:
    """The id of the already-logged row that made log_opportunity dedup `opp` away, or None.

    Mirrors log_opportunity's key EXACTLY -- the economic position (event_ticker + backed player),
    falling back to (market_ticker, side) only in the same no-opponent case the dedup itself falls
    back on. Looking the prior row up by (market_ticker, side) unconditionally was a live TypeError:
    one position is expressible as yes-on-A or no-on-B, so a row logged under the other anchor was
    invisible to that lookup and prior["id"] blew up -- taking down a whole /scan sweep from inside
    scheduled_scan_job's except, which swallows it. See storage.last_position.

    None-safe by design: since the key mirrors the dedup, a miss should be unreachable, and paying
    for it with an id-less alert line beats losing a scheduled cycle's alerts to an exception.
    """
    backed = backed_player(opp)
    prior = (last_position(conn, opp.event_ticker, backed) if backed is not None
             else last_opportunity(conn, opp.market_ticker, opp.side))
    return prior["id"] if prior is not None else None


def run_check(client, model, cfg, conn, tour: str, a: str, b: str, *, dry: bool = False) -> str:
    """Evaluate one match; on a qualifying edge log it (deduped) and format the alert, else a
    friendly abstain. On a dedup the alert shows the PRIOR opp id and a not-re-logged note.

    `dry=True` (from /preview) evaluates and renders identically but writes NOTHING. It exists so
    the owner can inspect or demo the engine mid-paper-test without injecting an owner-TIMED
    opportunity into a sample whose whole purpose is unbiased scheduled sampling. Not-writing is
    deliberate rather than writing-and-filtering-later: a filter every analysis path must remember
    to apply is one forgotten call away from silent contamination."""
    result = evaluate_match(client, model, cfg, tour, a, b)
    if result.status == "abstain":
        # Priced-but-no-alert -> analysis snapshot (+ /notes hint); earlier abstains -> friendly reason.
        if result.diagnostics is not None:
            return _dry_banner(dry) + format_no_alert(result.reason, result.diagnostics) + _NOTES_FOOTER
        return _dry_banner(dry) + format_abstain(result.reason)
    opp = result.opportunity
    if dry:
        return (_dry_banner(dry) + format_alert(opp, None, cfg.bankroll)
                + _exposure_warning(conn, cfg) + _NOTES_FOOTER)
    opp_id = log_opportunity(conn, opp)
    warn = _exposure_warning(conn, cfg)
    if opp_id is None:  # a prior alert for this POSITION still stands (possibly under the other anchor)
        return (format_alert(opp, _prior_position_id(conn, opp), cfg.bankroll)
                + "\n(already logged — not re-logged)" + warn + _NOTES_FOOTER)
    return format_alert(opp, opp_id, cfg.bankroll) + warn + _NOTES_FOOTER


def run_scan(client, model, cfg, conn, tours) -> str:
    """One on-demand sweep of each tour's open events: log qualifying alerts (deduped), tally
    abstain reasons, and render the alert blocks + a one-line skipped tally."""
    alerts: list[tuple] = []
    tally: Counter = Counter()
    for tour in tours:
        if getattr(cfg.series, tour.lower(), None) is None:
            tally["no_series_for_tour"] += 1
            continue
        # H2H markets plus any tournament final listed only as an outright (Grand Slam final).
        for result in chain(scan_series(client, model, cfg, tour), scan_outright_finals(client, model, cfg, tour)):
            if result.status != "alert":
                tally[result.reason] += 1
                continue
            opp = result.opportunity
            opp_id = log_opportunity(conn, opp)
            if opp_id is None:  # still-standing edge -> show it with its prior id, don't re-log
                opp_id = _prior_position_id(conn, opp)
            alerts.append((opp, opp_id))
    return format_scan(alerts, tally, cfg.bankroll) + _exposure_warning(conn, cfg)


def run_recent(conn, n: int) -> str:
    return format_recent(recent_opportunities(conn, limit=n))


def run_find(client, model, cfg, tours, top_n: int = _FIND_TOP_N) -> str:
    """List open matches across `tours`, modellable ones ranked by model strength (top `top_n`)."""
    matches = [m for tour in tours for m in list_open_matches(client, model, cfg, tour)]
    return format_find(matches, top_n)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_dt(iso) -> datetime | None:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)  # a naive string would break aware-vs-naive compares


def _same_side_mid(quotes, side: str) -> float | None:
    """The taken side's MID (bid+ask)/2 -- spread-neutral, the honest CLV baseline. None if that
    side of the book isn't two-sided."""
    bid, ask = (quotes.yes_bid, quotes.yes_ask) if side == "yes" else (quotes.no_bid, quotes.no_ask)
    return None if bid is None or ask is None else round((bid + ask) / 2.0, 4)


def _mark_missed(conn, opp_id: int, reason: str, source: str) -> None:
    """Record that a closing-line capture was NOT taken cleanly (leaves closing_price NULL so the
    row is excluded from CLV and not re-scheduled). Better a lost datapoint than a poisoned one."""
    storage.record_outcome(conn, opp_id, closing_captured_at=_now_iso(), closing_source=f"missed:{reason}[{source}]")


def capture_close(client, conn, opp_id: int, *, source: str, now: datetime | None = None,
                  force_prematch: bool = False, sharp_client=None, sharp_cache=None) -> dict:
    """Snapshot the CLOSING LINE (same-side MID) for a logged opportunity -- the PRE-match price.
    Read-only against Kalshi + a paper log write; shared by /close and the auto-job. FAIL-CLOSED:
    refuses (marks 'missed', never fabricates) when the market is not active, we're materially past
    scheduled start, OR the scheduled start is UNKNOWN -- because Kalshi trades tennis in-play (status
    stays 'active'), so with no start time we cannot tell pre-match from in-play and would risk
    recording a live price as the 'close'. `force_prematch=True` (a human /close ... pre) overrides
    the unknown-start refusal when the owner confirms the match hasn't begun."""
    opp = get_opportunity(conn, opp_id)
    if opp is None:
        return {"opp_id": opp_id, "ok": False, "reason": "no_such_opp"}
    # Idempotent: if a real close was already recorded (Kalshi mid OR sharp), never re-capture/overwrite
    # (a re-fired auto job must not clobber a good sharp_close with NULL; a late re-/close must not
    # relabel a clean row 'missed').
    prior = storage.get_outcome(conn, opp_id)
    if prior is not None and (prior["closing_price"] is not None or prior["sharp_close"] is not None):
        return {"opp_id": opp_id, "ok": True, "reason": "already_captured", "side": opp["side"],
                "market_player": opp["market_player"], **_matchup(opp), "closing_price": prior["closing_price"],
                "entry_price": opp["price"], "sharp_close": prior["sharp_close"], "sharp_source": prior["sharp_source"]}
    now = now or datetime.now(timezone.utc)
    start = _parse_dt(opp["occurrence_datetime"])
    if start is None and not force_prematch:  # can't verify pre-match -> refuse rather than poison CLV
        _mark_missed(conn, opp_id, "unknown_start", source)
        return {"opp_id": opp_id, "ok": False, "reason": "unknown_start"}
    if start is not None and now > start + CAPTURE_LATE_GRACE:
        _mark_missed(conn, opp_id, "late", source)
        return {"opp_id": opp_id, "ok": False, "reason": "too_late"}
    if start is not None and not force_prematch and (start - now) > CAPTURE_EARLIEST:
        return {"opp_id": opp_id, "ok": False, "reason": "too_early"}  # NOT a miss -- stays pending for a capture nearer start
    market = client.get_market(opp["market_ticker"])
    if market.status not in ("active", "open"):
        _mark_missed(conn, opp_id, market.status or "unknown", source)
        return {"opp_id": opp_id, "ok": False, "reason": "not_active", "status": market.status}
    # SHARP closing line (Pinnacle) for the taken side -- the binding go-live baseline. Attempt it even
    # if the Kalshi book is thin: the gate needs only entry + sharp_close (the Kalshi mid is informational),
    # so a one-sided Kalshi book must not censor an independently-available sharp reference. Never raises.
    sharp_close = sharp_source = None
    if sharp_client is not None:
        sharp_close, sharp_source = sharp_fair_for_opp(sharp_client, opp, cache=sharp_cache)
    mid = _same_side_mid(client.best_quotes(opp["market_ticker"]), opp["side"])
    if mid is None:
        if sharp_close is None:
            _mark_missed(conn, opp_id, "no_two_sided_book", source)
            return {"opp_id": opp_id, "ok": False, "reason": "no_price"}
        # Kalshi book too thin for a mid, but we have a sharp ref -> record sharp-only (row leaves pending).
        storage.record_outcome(conn, opp_id, closing_captured_at=now.isoformat(timespec="seconds"),
                               closing_source=f"sharp_only:{source}", sharp_close=sharp_close, sharp_source=sharp_source)
        return {"opp_id": opp_id, "ok": True, "side": opp["side"], "market_player": opp["market_player"], **_matchup(opp),
                "closing_price": None, "entry_price": opp["price"], "sharp_close": sharp_close, "sharp_source": sharp_source}
    storage.record_outcome(conn, opp_id, closing_price=mid, closing_captured_at=now.isoformat(timespec="seconds"),
                           closing_source=source, sharp_close=sharp_close, sharp_source=sharp_source)
    # _matchup here too: this is the COMMON path, and without it the closing-line DM degraded to the
    # bare "{market_player} {SIDE}" fallback -- naming, on a 'no' bet, the player we bet AGAINST.
    return {"opp_id": opp_id, "ok": True, "side": opp["side"], "market_player": opp["market_player"], **_matchup(opp),
            "closing_price": mid, "entry_price": opp["price"], "sharp_close": sharp_close, "sharp_source": sharp_source}


def auto_capture(client, conn, opp_id: int, *, now: datetime | None = None, sharp_client=None) -> dict:
    """Scheduled-capture arbiter: the best available start time is the source of truth -- the SHARP
    book's per-match start when we can get it, else the LIVE Kalshi market's (Kalshi's is often a
    coarse session placeholder; see sharp.sharp_commence_time). When it differs materially from the
    stored time (a reschedule in EITHER direction), correct the stored time first, then:
      - if the corrected start is still in the FUTURE -> signal a reschedule (re-arm; do NOT capture),
        so a stale time can't false-'miss' a match that got pushed back;
      - if it's now in the PAST -> fall through to capture_close, which (against the CORRECTED time)
        marks it missed rather than snapshotting an in-play price -- closing the CLV-poison gap.
    An unchanged (within-epsilon) time falls straight through to capture_close as before.

    Returns {"action": "rescheduled", ...} or {"action": "captured", "result": <capture_close dict>}.
    All Kalshi contact lives here (not in schedule_pending_captures, which stays DB-only)."""
    opp = get_opportunity(conn, opp_id)
    now = now or datetime.now(timezone.utc)
    # One odds-api fetch serves BOTH the start check here and the sharp close inside capture_close --
    # without the shared cache this path would spend two credits per captured row instead of one.
    sharp_cache: dict = {}
    if opp is not None:
        market = client.get_market(opp["market_ticker"])
        stored = _parse_dt(opp["occurrence_datetime"])
        # Prefer the SHARP start: it is the real per-match time, where Kalshi's is a session stamp
        # that can sit hours early or days stale. None (not listed / not covered / over) -> Kalshi's.
        live_iso = market.occurrence_datetime
        if sharp_client is not None:
            sharp_start = sharp_start_for_opp(sharp_client, opp, cache=sharp_cache)   # never raises
            if sharp_start is not None:
                live_iso = sharp_start
        live = _parse_dt(live_iso)
        moved = (
            market.status in ("active", "open")
            and live is not None and stored is not None
            and abs((live - stored).total_seconds()) > RESCHEDULE_EPSILON.total_seconds()
        )
        if moved:
            storage.update_occurrence(conn, opp_id, live_iso)  # correct EITHER direction
            if live > now:  # still snapshot-able pre-match -> re-arm for the new start, don't capture now
                return {"action": "rescheduled", "opp_id": opp_id, "new_start": live_iso}
    return {"action": "captured",
            "result": capture_close(client, conn, opp_id, source="auto", now=now,
                                    sharp_client=sharp_client, sharp_cache=sharp_cache)}


def _matchup(opp) -> dict:
    """The fields every per-bet DM needs to be readable on its own: who played whom, and which of them
    we actually backed. Without these the message names only the market's Yes subject, so the owner has
    to scroll back to find the opponent -- and on a 'no' bet the player named is not even the one we
    backed. Takes a DB Row; reuses engine's single definition of the backed side."""
    return {"match": f"{opp['market_player']} vs {opp['opponent']}" if opp["opponent"] else opp["market_player"],
            "backed": backed_player_from(opp["market_player"], opp["opponent"], opp["side"]),
            "opponent": opp["opponent"]}


SETTLED_STATUSES = ("settled", "finalized")   # Kalshi reports 'finalized' on settled tennis markets
RESULT_AUTO_AFTER_HOURS = 2                   # don't even look until the match has had time to finish


def auto_record_result(client, conn, opp_id: int, cfg) -> dict:
    """Record a settled paper bet's outcome from Kalshi's own settlement. Returns an action dict.

    Exists because the go-live gate hard-requires realized net-ROI >= 0, and `roi` stays None until a
    result is entered -- so 200+ manual /result entries over 12 weeks stood between the run and a
    readable gate. Forgetting a few weeks doesn't just lose rows, it biases the sample toward whichever
    results the owner felt like typing.

    Three guards, none optional (this is the only unattended writer to the live sample):
      1. Only an unambiguous 'yes'/'no' settlement is recorded. Kalshi's THIRD value, 'scalar', is a
         PARTIAL settlement where the mirrored pair splits the dollar (0.75/0.25 observed live) -- its
         retirement/anomaly path. Mapping that to a loss would book -100% on a bet that paid back 25c
         or 75c a contract, so it is left for a human. The row keeps result NULL, which still counts
         toward CLV (the binding metric needs only entry + close) while staying out of P&L.
      2. Never overwrites an existing result -- /result stays the owner's override, including for a
         real fill at a price other than the alert price.
      3. Every auto-record is DM'd, so a wrong one can be corrected rather than discovered at week 12.

    The recorded fill is the LOGGED ALERT PRICE and the logged contract count -- exactly what a
    log-only paper bet transacted at. That makes the ROI co-gate a paper ROI at alert prices; see
    DESIGN-DECISIONS "Auto-recorded settlement".
    """
    opp = get_opportunity(conn, opp_id)
    if opp is None:
        return {"opp_id": opp_id, "action": "skip", "reason": "no_such_opp"}
    prior = storage.get_outcome(conn, opp_id)
    if prior is not None and prior["result"] is not None:
        return {"opp_id": opp_id, "action": "skip", "reason": "already_recorded"}
    market = client.get_market(opp["market_ticker"])
    if market.status not in SETTLED_STATUSES:
        return {"opp_id": opp_id, "action": "skip", "reason": f"not_settled({market.status})"}
    # Report what OUR side received, not Kalshi's raw number: settlement_value is the YES contract's
    # value, so a 'no' holder got 1 - it. The un-inverted figure would misdescribe every 'no' bet.
    sv = market.settlement_value
    payoff = None if sv is None else (sv if opp["side"] == "yes" else 1.0 - sv)
    if market.result == "scalar":
        # A 'scalar' means NO BALL WAS PLAYED, so Kalshi could not pay either side out and refunded
        # both at roughly the prevailing price -- established by checking 17 scalar matches against
        # our own results archive (16 absent, the 1 present scored 'W/O') and confirmed by the market
        # rules, which require a winner "after a ball has been played". See DESIGN-DECISIONS.
        # That is precisely this schema's 'void': a walkover/refund, excluded from CLV, hit-rate and
        # P&L. Excluding it is also right on the merits -- a match that never happened has no line to
        # have beaten, so its CLV observation would be measuring a phantom event.
        if not storage.record_result_if_absent(conn, opp_id, result="void", pnl=0.0):
            return {"opp_id": opp_id, "action": "skip", "reason": "already_recorded"}
        return {"opp_id": opp_id, "action": "recorded", "result": "void", "pnl": 0.0,
                "fill": opp["price"], "contracts": opp["contracts"] or 0, "payoff": payoff,
                "market_player": opp["market_player"], "side": opp["side"], **_matchup(opp)}
    if market.result not in ("yes", "no"):
        # An unrecognised settlement is the one case still worth a human: we have never seen one, so
        # guessing its semantics is exactly the mistake the scalar investigation just corrected.
        return {"opp_id": opp_id, "action": "needs_human", "reason": market.result or "no_result",
                "settlement_value": sv, "payoff": payoff, "entry": opp["price"],
                "market_player": opp["market_player"], "side": opp["side"], **_matchup(opp)}
    # Our side won iff Kalshi's settled side IS the side we took (yes on the market's Yes player, or
    # no on it -- which backs the opponent).
    result = "win" if market.result == opp["side"] else "loss"
    fill, contracts = opp["price"], opp["contracts"]
    if not contracts:
        # No size logged -> there is no bet to score. Recording it anyway would put a 0-contract row
        # in the hit-rate numerator while contributing nothing to ROI (see clv.summarize), i.e. a
        # confidently-reported outcome for a position that never existed.
        return {"opp_id": opp_id, "action": "needs_human", "reason": "no_contracts_logged",
                "settlement_value": market.settlement_value, "payoff": None, "entry": fill,
                "market_player": opp["market_player"], "side": opp["side"], **_matchup(opp)}
    pnl = net_pnl(result, fill, contracts, cfg.fee_coefficient)
    # Absence-checked WRITE, not a read-then-write: an owner /result landing during our Kalshi round
    # trip must win. If it did, report the skip rather than claiming a record we didn't make.
    if not storage.record_result_if_absent(conn, opp_id, fill_price=fill, contracts_filled=contracts,
                                           result=result, pnl=pnl):
        return {"opp_id": opp_id, "action": "skip", "reason": "already_recorded"}
    return {"opp_id": opp_id, "action": "recorded", "result": result, "pnl": pnl, "fill": fill,
            "contracts": contracts, "market_player": opp["market_player"], "side": opp["side"],
            **_matchup(opp)}


def run_result(conn, opp_id: int, result: str, fill_price: float, contracts: int | None, cfg) -> str:
    """Record how a trade went: upsert the fill + outcome, computing net-of-fee P&L. `contracts`
    defaults to the opportunity's suggested size."""
    opp = get_opportunity(conn, opp_id)
    if opp is None:
        return f"No opportunity #{opp_id} to record."
    if result == "void":  # walkover / refund -- excluded from CLV, hit-rate, and P&L
        storage.record_outcome(conn, opp_id, result="void", pnl=0.0)
        return f"Recorded opp #{opp_id} as VOID (walkover/refund) — excluded from stats."
    contracts = contracts if contracts is not None else opp["contracts"]
    pnl = net_pnl(result, fill_price, contracts, cfg.fee_coefficient)
    storage.record_outcome(conn, opp_id, fill_price=fill_price, contracts_filled=contracts, result=result, pnl=pnl)
    return format_result(opp, result, fill_price, contracts, pnl)


def run_close(client, conn, opp_id: int | None = None, *, force_prematch: bool = False, sharp_client=None) -> str:
    """Capture the closing line for one opp, or (no id) every opportunity still missing one.
    `force_prematch` (from `/close <id> pre`) lets the owner confirm an untimed market is pre-match."""
    if opp_id is not None:
        return format_close(capture_close(client, conn, opp_id, source="manual",
                                          force_prematch=force_prematch, sharp_client=sharp_client))
    pend = pending_captures(conn)
    if not pend:
        return "Nothing to close — every logged opportunity already has a closing line."
    cache: dict = {}  # memo the sharp board per tournament across the batch (one fetch per sport_key)
    results = [capture_close(client, conn, r["id"], source="manual",  # batch never force-captures
                             sharp_client=sharp_client, sharp_cache=cache) for r in pend]
    return f"Captured {sum(r['ok'] for r in results)}/{len(results)} closing lines:\n" + "\n".join(
        format_close(r) for r in results)


def run_stats(conn, cfg) -> str:
    return format_stats(summarize(settled_bets(conn), cfg))


def parse_result_args(text: str) -> tuple[int, str, float | None, int | None] | None:
    """Parse `/result` args '<opp_id> <win|loss> <fill_price> [contracts]' or '<opp_id> void'
    (non-raising). Fill accepts dollars (0.54) or cents (54). Returns None on malformed input."""
    parts = text.split()
    if len(parts) < 2:
        return None
    try:
        opp_id = int(parts[0])
    except ValueError:
        return None
    result = parts[1].lower()
    if result not in ("win", "loss", "void"):
        return None
    if result == "void":
        return opp_id, "void", None, None
    if len(parts) < 3:
        return None
    try:
        fill_price = float(parts[2])
        contracts = int(parts[3]) if len(parts) >= 4 else None
    except ValueError:
        return None
    if fill_price > 1:            # entered in cents, e.g. 54 -> 0.54
        fill_price /= 100.0
    if not (0.0 < fill_price < 1.0):
        return None
    return opp_id, result, fill_price, contracts


# ---- resource wrappers (run inside the worker thread; open+close fresh client & sqlite conn) ----

def _client(cfg, demo: bool) -> KalshiClient:
    return KalshiClient(base_url=cfg.kalshi_base_url if demo else PROD_BASE)  # no signer -- reads are public


def _sharp_client(cfg):
    """A SharpOddsClient (the-odds-api) if a non-empty key file is configured, else None. When None,
    sharp_close stays NULL -> the sharp go-live gate can never pass (no real money without a sharp ref)."""
    path = cfg.odds_api_key_path
    if not path:
        log.info("sharp track disabled: odds_api_key_path is unset (go-live gate cannot pass without a sharp reference)")
        return None
    try:
        api_key = Path(path).read_text().strip()
    except OSError as exc:
        log.warning("sharp track DISABLED: cannot read odds API key at %s (%s) -- go-live gate cannot pass", path, exc)
        return None
    if not api_key:
        log.warning("sharp track DISABLED: odds API key file %s is empty -- go-live gate cannot pass", path)
        return None
    return SharpOddsClient(api_key, base_url=cfg.odds_api_base_url, region=cfg.odds_region,
                           consensus_fallback=cfg.sharp_consensus_fallback)


def _with_conn(cfg, fn):
    conn = storage.connect(cfg.db_path)
    storage.init_db(conn)
    try:
        return fn(conn)
    finally:
        conn.close()


def _check_job(cfg, model, demo, tour, a, b, dry: bool = False) -> str:
    with _client(cfg, demo) as client:
        return _with_conn(cfg, lambda conn: run_check(client, model, cfg, conn, tour, a, b, dry=dry))


def _scan_job(cfg, model, demo, tours) -> str:
    with _client(cfg, demo) as client:
        return _with_conn(cfg, lambda conn: run_scan(client, model, cfg, conn, tours))


def _scheduled_scan_job(cfg, model, demo, tours) -> tuple[str, list[int]]:
    """Worker for the scheduled scan: run the sweep and return the ids of the opportunities NEWLY
    logged this cycle -- so the async job DMs only on genuinely new alerts, not a standing edge that
    /scan re-renders (but dedups and does not re-log) on every cycle, and so the sharp-at-entry fill
    knows exactly which rows to annotate. Ids (not a count) because MAX(id) is stable under the
    dedup: a cycle that logs nothing returns [], which reads the same as the old 0."""
    with _client(cfg, demo) as client:
        def work(conn):
            high_water = conn.execute("SELECT COALESCE(MAX(id), 0) FROM opportunities").fetchone()[0]
            text = run_scan(client, model, cfg, conn, tours)
            new_ids = [r[0] for r in conn.execute("SELECT id FROM opportunities WHERE id > ?", (high_water,))]
            return text, new_ids
        return _with_conn(cfg, work)


def _sharp_entry_job(cfg, opp_ids: list[int]) -> dict:
    """Record the sharp fair prob AT ENTRY for the rows a scan cycle just logged, and correct their
    start times from the same fetch. Returns {"filled", "restamped" (ids), "in_play" (ids)}.

    The start-time correction is why this job now matters to the CLV sample and not just to its
    decomposition. Kalshi's `occurrence_datetime` is a session placeholder (see
    sharp.sharp_commence_time): scheduling the closing-line capture off it lost 19 of the first 40
    captures -- 12 fired after a stamp that had already passed while the match had not started, 6
    fired at a stamp hours AFTER the real match, by which time it had finished and settled. The
    sharp feed carries the real per-match start, and we are already fetching it here.

    `in_play` is a DETECTION, not a gate: a row whose real start precedes its own alert timestamp
    was priced by a pre-match-only model against a market already under way. Nothing is auto-excluded
    on it -- it is reported so the owner doesn't trade it, and so the rate can be measured before
    anyone writes a rule against a sample of one.

    Runs strictly POST-decision -- after the alerts are logged and the DM is sent -- because it is
    instrumentation, not input: it must never delay an alert, and must never be able to suppress one.
    Hence it opens its own client/connection instead of joining the sweep, and swallows everything
    (`sharp_fair_for_opp` is already non-raising; the belt-and-braces try covers the client build and
    the writes). One cached fetch per tournament per alert-bearing cycle keeps the odds-api credits
    reserved for the BINDING close captures.

    Why at all: without an entry snapshot, a MET gate cannot be told apart from Kalshi simply trading
    a few cents under Pinnacle on the favorites min_price forces us to buy -- a standing venue basis
    that looks identical to forecasting skill. See storage.set_sharp_entry.
    """
    out: dict = {"filled": 0, "restamped": [], "in_play": []}
    if not opp_ids:
        return out
    try:
        client = _sharp_client(cfg)
        if client is None:
            return out
        cache: dict = {}
        with client:
            def work(conn):
                for opp_id in opp_ids:
                    row = get_opportunity(conn, opp_id)
                    if row is None:
                        continue
                    prob, source = sharp_fair_for_opp(client, row, cache=cache)
                    if prob is not None:
                        storage.set_sharp_entry(conn, opp_id, prob, source)
                        out["filled"] += 1
                    start_iso = sharp_start_for_opp(client, row, cache=cache)
                    start = _parse_dt(start_iso)
                    if start is None:
                        continue  # not listed -> keep Kalshi's stamp; auto_capture re-checks at fire time
                    stored = _parse_dt(row["occurrence_datetime"])
                    if stored is None or abs((start - stored).total_seconds()) > RESCHEDULE_EPSILON.total_seconds():
                        storage.update_occurrence(conn, opp_id, start_iso)
                        out["restamped"].append(opp_id)
                    logged_at = _parse_dt(row["ts"])
                    if logged_at is not None and start < logged_at:
                        out["in_play"].append(opp_id)
            _with_conn(cfg, work)
        return out
    except Exception:
        log.warning("sharp-at-entry fill failed for %s", opp_ids, exc_info=True)
        return out


def _recent_job(cfg, n) -> str:
    return _with_conn(cfg, lambda conn: run_recent(conn, n))


def _find_job(cfg, model, demo, tours) -> str:
    with _client(cfg, demo) as client:
        return run_find(client, model, cfg, tours)


def _result_job(cfg, opp_id, result, fill_price, contracts) -> str:
    return _with_conn(cfg, lambda conn: run_result(conn, opp_id, result, fill_price, contracts, cfg))


def _close_job(cfg, demo, opp_id, force_prematch=False) -> str:
    with _client(cfg, demo) as client, (_sharp_client(cfg) or nullcontext()) as sharp:
        return _with_conn(cfg, lambda conn: run_close(client, conn, opp_id, force_prematch=force_prematch, sharp_client=sharp))


def _auto_capture_job(cfg, demo, opp_id) -> dict:
    """The scheduled (auto) capture path: reconcile against the live market (postpone-aware), then
    capture-or-miss (with the sharp closing line). Returns auto_capture's action dict for capture_job."""
    with _client(cfg, demo) as client, (_sharp_client(cfg) or nullcontext()) as sharp:
        return _with_conn(cfg, lambda conn: auto_capture(client, conn, opp_id, sharp_client=sharp))


def _stats_job(cfg) -> str:
    return _with_conn(cfg, lambda conn: run_stats(conn, cfg))


# ---- async handlers (thin: auth -> offload blocking job -> chunked reply) ----

def _authed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    return chat is not None and is_authorized(chat.id, context.bot_data["chat_id"])


async def _reply_chunked(update: Update, text: str) -> None:
    for chunk in split_message(text):
        await update.message.reply_text(chunk)


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authed(update, context):
        return
    bd = context.bot_data
    parsed = parse_check_args(" ".join(context.args), bd["default_tour"])
    if parsed is None:
        await update.message.reply_text("Usage: /check <Player A> v <Player B> [atp|wta]")
        return
    a, b, tour = parsed
    text = await asyncio.to_thread(_check_job, bd["cfg"], bd["model"], bd["demo"], tour, a, b)
    await _reply_chunked(update, text)
    schedule_pending_captures(context.application)  # auto-schedule a closing-line read for any new opp


async def cmd_preview(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/preview — identical to /check but writes NOTHING, for inspecting or demoing the engine
    without contaminating the paper sample with an owner-timed opportunity. No capture is scheduled
    because no opportunity was logged."""
    if not _authed(update, context):
        return
    bd = context.bot_data
    parsed = parse_check_args(" ".join(context.args), bd["default_tour"])
    if parsed is None:
        await update.message.reply_text("Usage: /preview <Player A> v <Player B> [atp|wta]  (never logged)")
        return
    a, b, tour = parsed
    text = await asyncio.to_thread(_check_job, bd["cfg"], bd["model"], bd["demo"], tour, a, b, True)
    await _reply_chunked(update, text)


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authed(update, context):
        return
    bd = context.bot_data
    await update.message.reply_text("Scanning open markets…")  # sweep can take a few seconds
    text = await asyncio.to_thread(_scan_job, bd["cfg"], bd["model"], bd["demo"], bd["cfg"].tours)
    await _reply_chunked(update, text)
    schedule_pending_captures(context.application)  # auto-schedule closing-line reads for new opps


async def cmd_find(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authed(update, context):
        return
    bd = context.bot_data
    arg = context.args[0].lower() if context.args else None
    tours = [arg] if arg in ("atp", "wta") else bd["cfg"].tours
    await update.message.reply_text("Finding open matches…")  # enumerates events; takes a moment
    text = await asyncio.to_thread(_find_job, bd["cfg"], bd["model"], bd["demo"], tours)
    await _reply_chunked(update, text)


async def cmd_recent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authed(update, context):
        return
    n = _RECENT_DEFAULT
    if context.args and context.args[0].isdigit():
        n = max(1, min(_RECENT_MAX, int(context.args[0])))
    text = await asyncio.to_thread(_recent_job, context.bot_data["cfg"], n)
    await _reply_chunked(update, text)


async def cmd_result(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authed(update, context):
        return
    parsed = parse_result_args(" ".join(context.args))
    if parsed is None:
        await update.message.reply_text(
            "Usage: /result <opp_id> <win|loss> <fill_price> [contracts]\ne.g. /result 1043 win 54 85")
        return
    opp_id, result, fill_price, contracts = parsed
    text = await asyncio.to_thread(_result_job, context.bot_data["cfg"], opp_id, result, fill_price, contracts)
    await _reply_chunked(update, text)


async def cmd_close(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authed(update, context):
        return
    bd = context.bot_data
    opp_id = None
    force_prematch = False
    if context.args:
        if not context.args[0].isdigit():
            await update.message.reply_text(
                "Usage: /close [opp_id] [pre]  (no id = capture all pending; 'pre' = I confirm it's pre-match)")
            return
        opp_id = int(context.args[0])
        force_prematch = len(context.args) > 1 and context.args[1].lower() == "pre"
    else:
        await update.message.reply_text("Capturing closing lines…")  # batch read; takes a moment
    text = await asyncio.to_thread(_close_job, bd["cfg"], bd["demo"], opp_id, force_prematch)
    await _reply_chunked(update, text)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authed(update, context):
        return
    text = await asyncio.to_thread(_stats_job, context.bot_data["cfg"])
    await _reply_chunked(update, text)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authed(update, context):
        return
    await update.message.reply_text(HELP)


async def cmd_notes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authed(update, context):
        return
    await update.message.reply_text(NOTES)


# ---- auto-scheduled closing-line capture (a one-shot read at each match start) ----

def schedule_pending_captures(application: Application) -> int:
    """Schedule a one-shot closing-line capture at each pending opportunity's match start. Called at
    startup (reconciles across restarts) and after /check /scan (picks up freshly-logged opps).
    Deduped by job name; a past-due start fires immediately (when=0). No-op without the job-queue
    extra (manual /close still works). Returns the count newly scheduled."""
    jq = application.job_queue
    if jq is None:
        return 0
    cfg = application.bot_data["cfg"]
    now = datetime.now(timezone.utc)
    conn = storage.connect(cfg.db_path)
    storage.init_db(conn)
    scheduled = 0
    try:
        for row in pending_captures(conn):
            start = _parse_dt(row["occurrence_datetime"])
            if start is None or jq.get_jobs_by_name(f"close:{row['id']}"):
                continue  # untimed -> manual /close only; or already scheduled
            # Fire a few minutes BEFORE start so the snapshot is pre-match (not in-play). A past-due
            # row fires immediately (when=0): the job re-checks the LIVE market and either reschedules
            # (postponed) or marks missed (genuinely over) -- so a stale stored time never false-misses
            # a match Kalshi actually pushed back. This stays DB-only; all network contact is in the job.
            # misfire_grace_time=None: if the scheduler is momentarily late (busy loop / a long
            # scan), run the capture anyway rather than silently dropping it as a misfire (which
            # would leave the row uncaptured AND uncounted). capture_close then decides capture-vs-missed.
            jq.run_once(capture_job, when=max(0.0, (start - CAPTURE_BEFORE_START - now).total_seconds()),
                        name=f"close:{row['id']}", data=row["id"], job_kwargs={"misfire_grace_time": None})
            scheduled += 1
    finally:
        conn.close()
    return scheduled


def rearm_captures(application: Application, opp_ids) -> int:
    """Re-schedule the closing-line capture for rows whose stored start just MOVED. Returns the
    count re-armed.

    schedule_pending_captures alone cannot do this: it skips any row that already has a `close:{id}`
    job, so a corrected start would keep firing on the stale timer -- which is the whole defect the
    sharp start-time lookup exists to fix. Cancel first, then let the normal scheduler re-arm."""
    jq = application.job_queue
    if jq is None or not opp_ids:
        return 0
    for opp_id in opp_ids:
        for job in jq.get_jobs_by_name(f"close:{opp_id}"):
            job.schedule_removal()
    return schedule_pending_captures(application)


async def capture_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """One-shot job: reconcile against the live market, then either capture the closing line or
    (if the match was postponed) re-arm for the corrected start. Confirms to the owner chat.
    Retries a transient Kalshi read a few times before giving up, so a momentary blip in the ~5-min
    pre-start window doesn't silently lose the closing-line datapoint (the CLV baseline)."""
    bd = context.bot_data
    res = None
    for attempt in range(3):
        try:
            res = await asyncio.to_thread(_auto_capture_job, bd["cfg"], bd["demo"], context.job.data)
            break
        except Exception:
            log.warning("auto-capture attempt %d/3 failed for opp %s", attempt + 1, context.job.data, exc_info=True)
            if attempt < 2:
                await asyncio.sleep(5)
    if res is None:
        # All reads failed: leave the row pending (no fabricated close). The next scheduled scan /
        # startup re-arms it; if the match is over by then, capture_close marks it missed. No hot loop.
        return
    if res["action"] == "rescheduled":
        # The fired one-shot is already gone from the queue, so re-arming close:{id} can't clash.
        schedule_pending_captures(context.application)
        msg = f"⏱️ Opp #{res['opp_id']} postponed (new start {res['new_start']}) — closing-line capture rescheduled."
    else:
        msg = format_close(res["result"])
    await context.bot.send_message(chat_id=bd["chat_id"], text=msg)


# ---- scheduled systematic scan (a low-frequency timer, NOT continuous polling) ----

SCHEDULED_SCAN_FIRST = 30.0  # seconds after startup before the first scheduled scan (let startup settle)


async def scheduled_scan_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Low-frequency systematic /scan: removes owner-timing selection bias from the CLV sample by
    sweeping on a fixed cadence rather than only when the owner runs /scan. Runs the same blocking
    sweep as /scan on a worker thread, arms closing-line captures for any new opps, logs a one-line
    summary, and DMs the owner ONLY when a NEW opportunity was logged this cycle (or cfg.scan_announce)
    -- a still-standing edge is deduped and must not re-ping every cycle. Exceptions are logged
    (never propagate to wedge the repeating job)."""
    bd = context.bot_data
    cfg = bd["cfg"]
    try:
        text, new_ids = await asyncio.to_thread(_scheduled_scan_job, cfg, bd["model"], bd["demo"], cfg.tours)
    except Exception:
        log.exception("scheduled scan failed")
        # Record the FAILURE too: a cycle that dies silently is the case the heartbeat exists to
        # expose, so the except path must stamp bot_data exactly like the success path.
        bd["scan"] = {"finished_at": _now_iso(), "ok": False, "n_new": 0}
        return
    n_new = len(new_ids)
    bd["scan"] = {"finished_at": _now_iso(), "ok": True, "n_new": n_new}
    schedule_pending_captures(context.application)  # arm auto-capture for any freshly-logged opps
    log.info("scheduled scan complete: %d new alert(s)", n_new)
    if n_new > 0 or cfg.scan_announce:
        for chunk in split_message(text):
            await context.bot.send_message(chat_id=bd["chat_id"], text=chunk)
    # AFTER the DM: instrumentation must not sit between an edge and the owner seeing it.
    if new_ids:
        res = await asyncio.to_thread(_sharp_entry_job, cfg, new_ids)
        log.info("sharp-at-entry: filled %d/%d new opp(s); %d start time(s) corrected from the sharp feed",
                 res["filled"], n_new, len(res["restamped"]))
        rearm_captures(context.application, res["restamped"])  # the capture must follow the corrected start
        if res["in_play"]:
            ids = ", ".join(f"#{i}" for i in res["in_play"])
            log.warning("alerted on %s AFTER the real match start (sharp feed)", ids)
            await context.bot.send_message(chat_id=bd["chat_id"], text=(
                f"⚠️ Opp {ids}: the sharp book says that match was ALREADY UNDER WAY when the alert "
                f"fired (Kalshi keeps tennis markets open in-play). Don't trade it — the model prices "
                f"pre-match only, and the live price knows things it doesn't."))


def _auto_result_job(cfg, demo) -> list[dict]:
    """Sweep every bet whose match has finished but has no result, and record what Kalshi settled."""
    out: list[dict] = []
    with _client(cfg, demo) as client:
        def work(conn):
            for row in storage.awaiting_result(conn, _now_iso(), RESULT_AUTO_AFTER_HOURS):
                try:
                    out.append(auto_record_result(client, conn, row["id"], cfg))
                except Exception:  # one unreachable market must not abort the sweep
                    log.warning("auto-result failed for opp %s", row["id"], exc_info=True)
        _with_conn(cfg, work)
    return out


async def auto_result_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Timer: auto-record settled outcomes so the ROI co-gate stays readable without 200 manual entries.

    DMs only what it RECORDED (or what needs a human), never the routine 'still in play' skips. A
    'scalar' partial settlement is DM'd once per process so it can't nag every cycle, and it stays in
    the heartbeat's awaiting-/result list until the owner rules on it."""
    bd = context.bot_data
    try:
        actions = await asyncio.to_thread(_auto_result_job, bd["cfg"], bd["demo"])
    except Exception:
        log.exception("auto-result sweep failed")
        # Stamp the FAILURE so the heartbeat can say so. Without this the sweep could fail every cycle
        # for weeks while the daily DM still read "Matador OK" -- and since roi stays None, the gate
        # would be unreadable at week 12. Exactly the hole _scan_status_line exists to close.
        bd["auto_result"] = {"finished_at": _now_iso(), "ok": False, "n_recorded": 0}
        return
    recorded = sum(1 for a in actions if a["action"] == "recorded")
    bd["auto_result"] = {"finished_at": _now_iso(), "ok": True, "n_recorded": recorded}
    # Log the SUCCESS too, not just failures. The first live sweep wrote 10 outcomes and left no trace
    # in the log at all -- the only way to tell it had run was to query the DB. The heartbeat stamp
    # covers the owner; this covers anyone reading logs/matador.log after the fact.
    log.info("auto-result sweep: %d recorded, %d awaiting a human, %d skipped", recorded,
             sum(1 for a in actions if a["action"] == "needs_human"),
             sum(1 for a in actions if a["action"] == "skip"))
    flagged = bd.setdefault("scalar_flagged", set())
    for a in actions:
        if a["action"] not in ("recorded", "needs_human"):
            continue
        if a["action"] == "needs_human" and a["opp_id"] in flagged:
            continue
        try:
            await context.bot.send_message(chat_id=bd["chat_id"], text=format_auto_result(a))
        except Exception:
            # Per-send, so one transient Telegram error can't swallow the announcements of rows
            # ALREADY committed to the DB -- those are never re-announced (the next sweep skips them
            # as already_recorded), so a dropped DM is a permanently unreviewed auto-write.
            log.warning("could not DM auto-result for opp %s", a["opp_id"], exc_info=True)
            continue
        if a["action"] == "needs_human":
            flagged.add(a["opp_id"])  # only AFTER a successful send, else a blip silences it for good


# ---- daily heartbeat (liveness: a silent outage otherwise looks like 'no edge found') ----

STALE_DATA_WARN_DAYS = 10   # weekly refresh + slack; past this the feed has almost certainly stalled


def artifact_config_drift(model, cfg) -> list[str]:
    """Params where the LOADED ARTIFACT disagrees with config -- empty when coherent.

    `Model.from_artifact` reads surface_weight / min_matches / initial_rating / shrinkage_n0 and the
    fitted scales FROM THE ARTIFACT, so those are what predict() actually uses; config.yaml only
    applies at BUILD time. A drifted config therefore documents a model that isn't running -- and
    since the weekly cron rebuilds the artifact from whatever code is checked out, drift can appear
    on a Monday without anyone touching the bot.

    Reported, never fatal: refusing to start would crash-loop the container under
    `restart: unless-stopped`, and a total sampling outage is worse than a mis-documented but
    internally consistent model.
    """
    checks = (
        ("surface_weight", getattr(model, "surface_weight", None), cfg.elo.surface_weight),
        ("shrinkage_n0", getattr(model, "shrinkage_n0", None), cfg.elo.shrinkage_n0),
        ("initial_rating", getattr(model, "initial", None), cfg.elo.initial_rating),
        ("min_matches", getattr(model, "min_matches", None), cfg.min_matches),
    )
    drift = []
    for name, in_artifact, in_config in checks:
        if in_artifact is None or in_config is None:
            continue
        if abs(float(in_artifact) - float(in_config)) > 1e-9:
            drift.append(f"{name}: artifact={in_artifact} config={in_config}")
    return drift


def _model_freshness(model_path: str, today: date | None = None) -> str:
    """One-line 'model data through YYYY-MM-DD (Nd)' for the heartbeat, flagged when it stops
    advancing. This is the ONLY routine signal that the weekly refresh has silently failed -- the
    `rejected` warning goes to logs/refresh.log, which nobody reads, and the refresh is the failure
    that went unnoticed for weeks before 2026-07-27. Best-effort: never break the heartbeat."""
    try:
        art = json.loads(Path(model_path).read_text())
        tours = art.get("tours", {})
        if not tours:
            return "model data through unknown (rebuild to record it)"
        ref = today or date.today()
        parts, ages = [], []
        for tour in sorted(tours):
            stamp = tours[tour].get("data_through")
            if not stamp:
                # A tour with no stamp is itself suspicious -- name it rather than filtering it out,
                # which would hide exactly the per-tour problem this line exists to surface.
                parts.append(f"{tour} unknown")
                continue
            try:
                d = date(stamp // 10000, (stamp // 100) % 100, stamp % 100)
            except ValueError:              # malformed stamp e.g. 20261345
                parts.append(f"{tour} bad-stamp({stamp})")
                continue
            age = (ref - d).days
            ages.append(age)
            parts.append(f"{tour} {d.isoformat()} ({age}d)")
        # Age off the OLDEST tour, and flag when any tour is missing/malformed. Using max() (the
        # newest) made a single-tour freeze invisible: ATP fresh + WTA frozen 88d reported "1d" with
        # no warning, while every WTA alert priced off a frozen rating book. Upstream broke per
        # directory before (the 2026-07 Git-LFS move), so per-tour is the realistic failure.
        unhealthy = len(ages) < len(tours) or (ages and max(ages) > STALE_DATA_WARN_DAYS)
        flag = f"  ⚠️ STALE >{STALE_DATA_WARN_DAYS}d — CHECK logs/refresh.log" if unhealthy else ""
        return f"model data {' / '.join(parts)}{flag}"
    except Exception:  # missing/damaged artifact must not kill the liveness DM
        return "model freshness unavailable"


RESULT_OVERDUE_HOURS = 12    # a match this long finished should have a /result by now
_OVERDUE_IDS_SHOWN = 5       # cap the listed ids so the DM stays one readable message
LOW_CREDITS_WARN = 100       # odds-api credits below this -> warn while there's still time to top up


def _scan_status_line(scan: dict | None, now: datetime | None = None) -> str:
    """'last scan 2h ago: ok, 0 new' / 'last scan FAILED 2h ago' / 'no scan yet since restart'.

    Without this the scheduled scan can fail EVERY cycle for weeks -- a Kalshi 403 from the droplet
    IP, TLS, schema drift -- while the heartbeat still reports 'Matador OK, N opps', which is
    indistinguishable from a genuinely quiet market. Reported honestly after a restart rather than
    implying a scan happened."""
    if not scan or not scan.get("finished_at"):
        return "no scan yet since restart"
    when = _parse_dt(scan["finished_at"])
    ref = now or datetime.now(timezone.utc)
    age = f"{(ref - when).total_seconds() / 3600:.0f}h ago" if when else "at an unknown time"
    if not scan.get("ok"):
        return f"⚠️ last scan FAILED {age}"
    return f"last scan {age}: ok, {scan.get('n_new', 0)} new"


def _auto_result_status_line(auto: dict | None, now: datetime | None = None) -> str | None:
    """A ⚠️ line when the auto-result sweep last FAILED, else None (silence when healthy).

    Returns a warning rather than a status line because the sweep failing is invisible otherwise: the
    📝 awaiting-/result list just grows, `roi` stays None, and the gate is unreadable at week 12."""
    if not auto or auto.get("ok"):
        return None
    when = _parse_dt(auto.get("finished_at"))
    ref = now or datetime.now(timezone.utc)
    age = f"{(ref - when).total_seconds() / 3600:.0f}h ago" if when else "recently"
    return f"⚠️ auto-result sweep FAILED {age} — outcomes are NOT being recorded (roi stays unreadable)"


def _heartbeat_text(conn, cfg, drift: list[str] | None = None, scan: dict | None = None,
                    credits: int | None = None, now: datetime | None = None,
                    auto_result: dict | None = None) -> str:
    s = summarize(settled_bets(conn), cfg)
    c = s["captures"]
    lines = [
        f"{_model_freshness(cfg.model_path)}",
        f"{s['n_opportunities']} opps, {s['n_clv']} Kalshi-closed over {s['n_clusters']} week(s); "
        f"open exposure ${storage.open_exposure(conn):.0f}",
        f"captures {c['auto']}a/{c['manual']}m/{c['sharp_only']}s/{c['missed']}x "
        f"(auto/manual/sharp-only/missed); {len(pending_captures(conn))} pending",
        f"sharp {s['n_sharp']} pinnacle / {s['n_consensus']} consensus (coverage {s['sharp_coverage']:.0%})",
        _scan_status_line(scan, now),
    ]
    overdue = storage.awaiting_result(conn, _now_iso() if now is None else now.isoformat(timespec="seconds"),
                                     RESULT_OVERDUE_HOURS)
    if overdue:
        ids = ", ".join(f"#{r['id']}" for r in overdue[:_OVERDUE_IDS_SHOWN])
        more = f" (+{len(overdue) - _OVERDUE_IDS_SHOWN} more)" if len(overdue) > _OVERDUE_IDS_SHOWN else ""
        # Not cosmetic: roi is None until these are entered, and roi >= 0 is a hard go-live co-gate.
        lines.append(f"📝 {len(overdue)} awaiting /result: {ids}{more}")
    untimed = storage.untimed_pending(conn)
    if untimed:
        ids = ", ".join(f"#{r['id']}" for r in untimed[:_OVERDUE_IDS_SHOWN])
        lines.append(f"⚠️ {len(untimed)} pending with NO start time ({ids}) — never auto-captures; "
                     f"run /close <id> pre")
    auto_line = _auto_result_status_line(auto_result, now)
    if auto_line:
        lines.append(auto_line)
    if credits is not None and credits < LOW_CREDITS_WARN:
        lines.append(f"⚠️ odds-api credits low: {credits} left — sharp_close goes NULL when they run out")
    if drift:
        lines.append(f"⚠️ MODEL/CONFIG DRIFT ({'; '.join(drift)}) — predict() uses the ARTIFACT's values.")
    # The HEADER must degrade with the body. "Matador OK" on a message whose 5th line says the scan
    # has been failing for 9h is how a silent outage survives 84 daily DMs -- the owner reads the
    # first line and moves on. Any warning demotes the header.
    problems = sum(1 for line in lines if line.startswith("⚠️"))
    header = "💓 Matador OK — " if not problems else f"🚨 Matador — {problems} PROBLEM(S) below — "
    return header + "\n".join(lines)


def _heartbeat_job(cfg, drift: list[str] | None = None, scan: dict | None = None,
                   credits: int | None = None, auto_result: dict | None = None) -> str:
    return _with_conn(cfg, lambda conn: _heartbeat_text(conn, cfg, drift, scan, credits,
                                                        auto_result=auto_result))


def ping_dead_man_switch(url: str | None, timeout: float = 5.0) -> str | None:
    """Ping an external dead-man's-switch check (e.g. healthchecks.io). Returns a log line, or None
    when no URL is configured. NEVER raises.

    Why an external service at all: every other liveness signal here requires the OWNER to notice the
    ABSENCE of a routine message, every day, for the length of the run -- and the failures the
    heartbeat exists to catch (dead droplet, crash-looping container, a Telegram long-poll wedged by a
    second poller on the same token) are exactly the failures that also stop the heartbeat. This
    inverts it: the check alerts when the pings STOP, so absence becomes an active notification instead
    of something a human has to spot. The weekly refresh DM is not a substitute -- refresh_notify runs
    in a separate `docker compose run --rm` container and would still report 'refresh OK' with the bot
    container wedged.

    Called only AFTER the heartbeat DM has actually been sent, so the check measures the real
    end-to-end path (engine -> DB -> Telegram) rather than merely 'the process is scheduled'. That is
    what makes a wedged poller detectable: the process is alive, the DM never lands, the pings stop.
    Set the check's grace to ~26h so one late daily beat doesn't cry wolf.
    """
    if not url:
        return None
    try:
        r = httpx.get(url, timeout=timeout)
        return f"dead-man ping HTTP {r.status_code}"
    except Exception as exc:   # a healthchecks outage must never affect the bot
        return f"dead-man ping failed: {type(exc).__name__}: {exc}"


async def heartbeat_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Daily DM so the owner can tell 'running, no edge yet' from 'silently down' -- a wedged
    long-poll / token conflict otherwise looks identical to a quiet market for days."""
    bd = context.bot_data
    try:
        msg = await asyncio.to_thread(_heartbeat_job, bd["cfg"], bd.get("drift"),
                                      bd.get("scan"), last_requests_remaining(),
                                      bd.get("auto_result"))
    except Exception:
        log.exception("heartbeat failed")
        return
    await context.bot.send_message(chat_id=bd["chat_id"], text=msg)
    # AFTER the DM landed -- see ping_dead_man_switch on why the ordering is the whole point.
    status = await asyncio.to_thread(ping_dead_man_switch, bd.get("healthcheck_url"))
    if status:
        log.info("%s", status)


async def on_startup(application: Application) -> None:
    n = schedule_pending_captures(application)
    log.info("Matador started; reconciled %d pending closing-line capture(s)", n)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log any unhandled handler/job exception. Without this a transient scheduled-scan or
    auto-capture failure would be swallowed silently -- here it's recorded (and the always-on
    process keeps running) so a multi-week paper-test leaves a diagnosable trail."""
    log.error("unhandled error in handler/job", exc_info=context.error)


def build_application(token: str, cfg, model, chat_id, *, demo: bool = False, default_tour: str = "atp",
                      healthcheck_url: str | None = None) -> Application:
    """Build the PTB app: stash shared read-only state in bot_data and register the commands,
    each gated to the owner's chat both by filters.Chat and the in-handler is_authorized check."""
    app = ApplicationBuilder().token(token).post_init(on_startup).build()
    # Surface artifact-vs-config drift at startup AND daily in the heartbeat: predict() uses the
    # ARTIFACT's params, so drift means config.yaml describes a model that isn't running.
    drift = artifact_config_drift(model, cfg)
    if drift:
        log.warning("MODEL/CONFIG DRIFT -- predict() uses the ARTIFACT's values: %s", "; ".join(drift))
    app.bot_data.update(cfg=cfg, model=model, demo=demo, chat_id=int(chat_id),
                        default_tour=default_tour, drift=drift, healthcheck_url=healthcheck_url)
    app.add_error_handler(on_error)
    chat_filter = filters.Chat(chat_id=int(chat_id))
    app.add_handler(CommandHandler("check", cmd_check, filters=chat_filter))
    app.add_handler(CommandHandler("preview", cmd_preview, filters=chat_filter))
    app.add_handler(CommandHandler(["find", "findmatch"], cmd_find, filters=chat_filter))
    app.add_handler(CommandHandler("scan", cmd_scan, filters=chat_filter))
    app.add_handler(CommandHandler("recent", cmd_recent, filters=chat_filter))
    app.add_handler(CommandHandler("result", cmd_result, filters=chat_filter))
    app.add_handler(CommandHandler("close", cmd_close, filters=chat_filter))
    app.add_handler(CommandHandler("stats", cmd_stats, filters=chat_filter))
    app.add_handler(CommandHandler(["notes", "helpnotes"], cmd_notes, filters=chat_filter))
    app.add_handler(CommandHandler(["help", "start"], cmd_help, filters=chat_filter))
    # Scheduled systematic scan (only if the job-queue extra is present AND a cadence is configured).
    # max_instances:1 + coalesce collapse any overlap so a slow sweep can't stack repeating runs.
    if app.job_queue is not None and cfg.scan_interval_hours:
        app.job_queue.run_repeating(
            scheduled_scan_job,
            interval=cfg.scan_interval_hours * 3600.0,
            first=SCHEDULED_SCAN_FIRST,
            name="scheduled_scan",
            job_kwargs={"max_instances": 1, "coalesce": True},
        )
    # Auto-record settled outcomes on the SAME cadence as the scan (no new knob: the gate needs
    # results within a day, and the heartbeat only nags past 12h, so 8-hourly clears them first).
    # Its own job rather than a tail on scheduled_scan_job, whose except path returns early -- a
    # Kalshi hiccup in the sweep must not also stop results from being recorded.
    if app.job_queue is not None and cfg.scan_interval_hours:
        app.job_queue.run_repeating(
            auto_result_job,
            interval=cfg.scan_interval_hours * 3600.0,
            first=SCHEDULED_SCAN_FIRST + 60.0,   # just after the first scan, not simultaneously
            name="auto_result",
            job_kwargs={"max_instances": 1, "coalesce": True},
        )
    if app.job_queue is not None and cfg.heartbeat_hours:
        app.job_queue.run_repeating(
            heartbeat_job,
            interval=cfg.heartbeat_hours * 3600.0,
            first=cfg.heartbeat_hours * 3600.0,  # not on startup -- one interval in
            name="heartbeat",
            job_kwargs={"max_instances": 1, "coalesce": True},
        )
    return app
