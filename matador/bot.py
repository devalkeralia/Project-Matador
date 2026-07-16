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
import logging
import re
from collections import Counter
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from itertools import chain
from pathlib import Path

from telegram import Update
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes, filters

from matador import storage
from matador.alerts import (
    format_abstain, format_alert, format_close, format_find, format_no_alert,
    format_recent, format_result, format_scan, format_stats,
)
from matador.clv import net_pnl, summarize
from matador.engine import evaluate_match, list_open_matches, log_opportunity, scan_outright_finals, scan_series
from matador.kalshi.client import KalshiClient
from matador.sharp import SharpOddsClient, sharp_fair_for_opp
from matador.storage import get_opportunity, last_opportunity, pending_captures, recent_opportunities, settled_bets

log = logging.getLogger(__name__)

PROD_BASE = "https://external-api.kalshi.com/trade-api/v2"  # public read-only; --demo uses cfg.kalshi_base_url

HELP = (
    "🎾 Matador — tennis value alerts (paper only; I never place orders)\n\n"
    "/check — value-check one match now; alerts if it's mispriced, else shows the analysis\n"
    "        usage: /check <player> vs <player> [atp|wta]\n"
    "        e.g.   /check Sinner vs Zverev\n"
    "/find [atp|wta] — list open matches; checkable ones ranked by model strength\n"
    "/scan — sweep all open ATP/WTA markets for value\n"
    "/recent [n] — the last n logged opportunities (default 10)\n"
    "/close [opp_id] [pre] — capture the closing line near match start (no id = all pending; 'pre' = confirm pre-match on an untimed market)\n"
    "/result <opp_id> <win|loss> <fill_price> [contracts] — record how a trade went\n"
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
    "• /result <opp_id> <win|loss> <fill_price> [contracts] — record the outcome + your fill.\n"
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


def run_check(client, model, cfg, conn, tour: str, a: str, b: str) -> str:
    """Evaluate one match; on a qualifying edge log it (deduped) and format the alert, else a
    friendly abstain. On a dedup the alert shows the PRIOR opp id and a not-re-logged note."""
    result = evaluate_match(client, model, cfg, tour, a, b)
    if result.status == "abstain":
        # Priced-but-no-alert -> analysis snapshot (+ /notes hint); earlier abstains -> friendly reason.
        if result.diagnostics is not None:
            return format_no_alert(result.reason, result.diagnostics) + _NOTES_FOOTER
        return format_abstain(result.reason)
    opp = result.opportunity
    opp_id = log_opportunity(conn, opp)
    warn = _exposure_warning(conn, cfg)
    if opp_id is None:  # a prior alert for this contract+side still stands
        prior = last_opportunity(conn, opp.market_ticker, opp.side)
        return format_alert(opp, prior["id"], cfg.bankroll) + "\n(already logged — not re-logged)" + warn + _NOTES_FOOTER
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
                opp_id = last_opportunity(conn, opp.market_ticker, opp.side)["id"]
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
                "market_player": opp["market_player"], "closing_price": prior["closing_price"],
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
        return {"opp_id": opp_id, "ok": True, "side": opp["side"], "market_player": opp["market_player"],
                "closing_price": None, "entry_price": opp["price"], "sharp_close": sharp_close, "sharp_source": sharp_source}
    storage.record_outcome(conn, opp_id, closing_price=mid, closing_captured_at=now.isoformat(timespec="seconds"),
                           closing_source=source, sharp_close=sharp_close, sharp_source=sharp_source)
    return {"opp_id": opp_id, "ok": True, "side": opp["side"], "market_player": opp["market_player"],
            "closing_price": mid, "entry_price": opp["price"], "sharp_close": sharp_close, "sharp_source": sharp_source}


def auto_capture(client, conn, opp_id: int, *, now: datetime | None = None, sharp_client=None) -> dict:
    """Scheduled-capture arbiter: the LIVE Kalshi market is the single source of truth for the
    match start. When it differs materially from the stored time (a reschedule in EITHER direction),
    correct the stored time first, then:
      - if the corrected start is still in the FUTURE -> signal a reschedule (re-arm; do NOT capture),
        so a stale time can't false-'miss' a match that got pushed back;
      - if it's now in the PAST -> fall through to capture_close, which (against the CORRECTED time)
        marks it missed rather than snapshotting an in-play price -- closing the CLV-poison gap.
    An unchanged (within-epsilon) time falls straight through to capture_close as before.

    Returns {"action": "rescheduled", ...} or {"action": "captured", "result": <capture_close dict>}.
    All Kalshi contact lives here (not in schedule_pending_captures, which stays DB-only)."""
    opp = get_opportunity(conn, opp_id)
    now = now or datetime.now(timezone.utc)
    if opp is not None:
        market = client.get_market(opp["market_ticker"])
        stored = _parse_dt(opp["occurrence_datetime"])
        live = _parse_dt(market.occurrence_datetime)
        moved = (
            market.status in ("active", "open")
            and live is not None and stored is not None
            and abs((live - stored).total_seconds()) > RESCHEDULE_EPSILON.total_seconds()
        )
        if moved:
            storage.update_occurrence(conn, opp_id, market.occurrence_datetime)  # correct EITHER direction
            if live > now:  # still snapshot-able pre-match -> re-arm for the new start, don't capture now
                return {"action": "rescheduled", "opp_id": opp_id, "new_start": market.occurrence_datetime}
    return {"action": "captured",
            "result": capture_close(client, conn, opp_id, source="auto", now=now, sharp_client=sharp_client)}


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


def _check_job(cfg, model, demo, tour, a, b) -> str:
    with _client(cfg, demo) as client:
        return _with_conn(cfg, lambda conn: run_check(client, model, cfg, conn, tour, a, b))


def _scan_job(cfg, model, demo, tours) -> str:
    with _client(cfg, demo) as client:
        return _with_conn(cfg, lambda conn: run_scan(client, model, cfg, conn, tours))


def _scheduled_scan_job(cfg, model, demo, tours) -> tuple[str, int]:
    """Worker for the scheduled scan: run the sweep and report how many opportunities were NEWLY
    logged this cycle -- so the async job DMs only on genuinely new alerts, not a standing edge
    that /scan re-renders (but dedups and does not re-log) on every cycle."""
    with _client(cfg, demo) as client:
        def work(conn):
            before = conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
            text = run_scan(client, model, cfg, conn, tours)
            after = conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
            return text, after - before
        return _with_conn(cfg, work)


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
        text, n_new = await asyncio.to_thread(_scheduled_scan_job, cfg, bd["model"], bd["demo"], cfg.tours)
    except Exception:
        log.exception("scheduled scan failed")
        return
    schedule_pending_captures(context.application)  # arm auto-capture for any freshly-logged opps
    log.info("scheduled scan complete: %d new alert(s)", n_new)
    if n_new > 0 or cfg.scan_announce:
        for chunk in split_message(text):
            await context.bot.send_message(chat_id=bd["chat_id"], text=chunk)


# ---- daily heartbeat (liveness: a silent outage otherwise looks like 'no edge found') ----

def _heartbeat_text(conn, cfg) -> str:
    s = summarize(settled_bets(conn), cfg)
    c = s["captures"]
    return (f"💓 Matador OK — {s['n_opportunities']} opps, {s['n_clv']} Kalshi-closed over {s['n_clusters']} week(s); "
            f"captures {c['auto']}a/{c['manual']}m/{c['sharp_only']}s/{c['missed']}x; {len(pending_captures(conn))} pending; "
            f"sharp {s['n_sharp']} pinnacle / {s['n_consensus']} consensus (coverage {s['sharp_coverage']:.0%}); "
            f"open exposure ${storage.open_exposure(conn):.0f}.")


def _heartbeat_job(cfg) -> str:
    return _with_conn(cfg, lambda conn: _heartbeat_text(conn, cfg))


async def heartbeat_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Daily DM so the owner can tell 'running, no edge yet' from 'silently down' -- a wedged
    long-poll / token conflict otherwise looks identical to a quiet market for days."""
    bd = context.bot_data
    try:
        msg = await asyncio.to_thread(_heartbeat_job, bd["cfg"])
    except Exception:
        log.exception("heartbeat failed")
        return
    await context.bot.send_message(chat_id=bd["chat_id"], text=msg)


async def on_startup(application: Application) -> None:
    n = schedule_pending_captures(application)
    log.info("Matador started; reconciled %d pending closing-line capture(s)", n)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log any unhandled handler/job exception. Without this a transient scheduled-scan or
    auto-capture failure would be swallowed silently -- here it's recorded (and the always-on
    process keeps running) so a multi-week paper-test leaves a diagnosable trail."""
    log.error("unhandled error in handler/job", exc_info=context.error)


def build_application(token: str, cfg, model, chat_id, *, demo: bool = False, default_tour: str = "atp") -> Application:
    """Build the PTB app: stash shared read-only state in bot_data and register the commands,
    each gated to the owner's chat both by filters.Chat and the in-handler is_authorized check."""
    app = ApplicationBuilder().token(token).post_init(on_startup).build()
    app.bot_data.update(cfg=cfg, model=model, demo=demo, chat_id=int(chat_id), default_tour=default_tour)
    app.add_error_handler(on_error)
    chat_filter = filters.Chat(chat_id=int(chat_id))
    app.add_handler(CommandHandler("check", cmd_check, filters=chat_filter))
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
    if app.job_queue is not None and cfg.heartbeat_hours:
        app.job_queue.run_repeating(
            heartbeat_job,
            interval=cfg.heartbeat_hours * 3600.0,
            first=cfg.heartbeat_hours * 3600.0,  # not on startup -- one interval in
            name="heartbeat",
            job_kwargs={"max_instances": 1, "coalesce": True},
        )
    return app
