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
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from itertools import chain

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
from matador.storage import get_opportunity, last_opportunity, pending_captures, recent_opportunities, settled_bets

PROD_BASE = "https://external-api.kalshi.com/trade-api/v2"  # public read-only; --demo uses cfg.kalshi_base_url

HELP = (
    "🎾 Matador — tennis value alerts (paper only; I never place orders)\n\n"
    "/check — value-check one match now; alerts if it's mispriced, else shows the analysis\n"
    "        usage: /check <player> vs <player> [atp|wta]\n"
    "        e.g.   /check Sinner vs Zverev\n"
    "/find [atp|wta] — list open matches; checkable ones ranked by model strength\n"
    "/scan — sweep all open ATP/WTA markets for value\n"
    "/recent [n] — the last n logged opportunities (default 10)\n"
    "/close [opp_id] — capture the closing line near match start (no id = all pending)\n"
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
CAPTURE_LATE_GRACE = timedelta(minutes=15)    # refuse a capture more than this past scheduled start (Kalshi trades in-play -> price would no longer be the pre-match CLOSE)


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
    if opp_id is None:  # a prior alert for this contract+side still stands
        prior = last_opportunity(conn, opp.market_ticker, opp.side)
        return format_alert(opp, prior["id"], cfg.bankroll) + "\n(already logged — not re-logged)" + _NOTES_FOOTER
    return format_alert(opp, opp_id, cfg.bankroll) + _NOTES_FOOTER


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
    return format_scan(alerts, tally, cfg.bankroll)


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


def capture_close(client, conn, opp_id: int, *, source: str, now: datetime | None = None) -> dict:
    """Snapshot the CLOSING LINE (same-side MID) for a logged opportunity -- the PRE-match price.
    Read-only against Kalshi + a paper log write; shared by /close and the auto-job. Refuses (marks
    'missed', never fabricates) when the market is not active or we're materially past scheduled
    start (Kalshi trades in-play, so a late snapshot would leak the outcome into CLV)."""
    opp = get_opportunity(conn, opp_id)
    if opp is None:
        return {"opp_id": opp_id, "ok": False, "reason": "no_such_opp"}
    now = now or datetime.now(timezone.utc)
    start = _parse_dt(opp["occurrence_datetime"])
    if start is not None and now > start + CAPTURE_LATE_GRACE:
        _mark_missed(conn, opp_id, "late", source)
        return {"opp_id": opp_id, "ok": False, "reason": "too_late"}
    market = client.get_market(opp["market_ticker"])
    if market.status not in ("active", "open"):
        _mark_missed(conn, opp_id, market.status or "unknown", source)
        return {"opp_id": opp_id, "ok": False, "reason": "not_active", "status": market.status}
    mid = _same_side_mid(client.best_quotes(opp["market_ticker"]), opp["side"])
    if mid is None:
        _mark_missed(conn, opp_id, "no_two_sided_book", source)
        return {"opp_id": opp_id, "ok": False, "reason": "no_price"}
    storage.record_outcome(conn, opp_id, closing_price=mid, closing_captured_at=now.isoformat(timespec="seconds"), closing_source=source)
    return {"opp_id": opp_id, "ok": True, "side": opp["side"], "market_player": opp["market_player"],
            "closing_price": mid, "entry_price": opp["price"]}


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


def run_close(client, conn, opp_id: int | None = None) -> str:
    """Capture the closing line for one opp, or (no id) every opportunity still missing one."""
    if opp_id is not None:
        return format_close(capture_close(client, conn, opp_id, source="manual"))
    pend = pending_captures(conn)
    if not pend:
        return "Nothing to close — every logged opportunity already has a closing line."
    results = [capture_close(client, conn, r["id"], source="manual") for r in pend]
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


def _recent_job(cfg, n) -> str:
    return _with_conn(cfg, lambda conn: run_recent(conn, n))


def _find_job(cfg, model, demo, tours) -> str:
    with _client(cfg, demo) as client:
        return run_find(client, model, cfg, tours)


def _result_job(cfg, opp_id, result, fill_price, contracts) -> str:
    return _with_conn(cfg, lambda conn: run_result(conn, opp_id, result, fill_price, contracts, cfg))


def _close_job(cfg, demo, opp_id) -> str:
    with _client(cfg, demo) as client:
        return _with_conn(cfg, lambda conn: run_close(client, conn, opp_id))


def _auto_capture_job(cfg, demo, opp_id) -> str:
    """The scheduled (auto) capture path -- same as /close but tags the source 'auto'."""
    with _client(cfg, demo) as client:
        return _with_conn(cfg, lambda conn: format_close(capture_close(client, conn, opp_id, source="auto")))


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
    if context.args:
        if not context.args[0].isdigit():
            await update.message.reply_text("Usage: /close [opp_id]  (no id = capture all pending)")
            return
        opp_id = int(context.args[0])
    else:
        await update.message.reply_text("Capturing closing lines…")  # batch read; takes a moment
    text = await asyncio.to_thread(_close_job, bd["cfg"], bd["demo"], opp_id)
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
            if now > start + CAPTURE_LATE_GRACE:  # too late for a clean pre-match snapshot -> mark missed once
                _mark_missed(conn, row["id"], "stale", "auto")
                continue
            # fire a few minutes BEFORE start so the snapshot is pre-match (not in-play)
            jq.run_once(capture_job, when=max(0.0, (start - CAPTURE_BEFORE_START - now).total_seconds()),
                        name=f"close:{row['id']}", data=row["id"])
            scheduled += 1
    finally:
        conn.close()
    return scheduled


async def capture_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """One-shot job: snapshot the closing line for one opportunity, then confirm to the owner chat."""
    bd = context.bot_data
    msg = await asyncio.to_thread(_auto_capture_job, bd["cfg"], bd["demo"], context.job.data)
    await context.bot.send_message(chat_id=bd["chat_id"], text=msg)


async def on_startup(application: Application) -> None:
    schedule_pending_captures(application)


def build_application(token: str, cfg, model, chat_id, *, demo: bool = False, default_tour: str = "atp") -> Application:
    """Build the PTB app: stash shared read-only state in bot_data and register the commands,
    each gated to the owner's chat both by filters.Chat and the in-handler is_authorized check."""
    app = ApplicationBuilder().token(token).post_init(on_startup).build()
    app.bot_data.update(cfg=cfg, model=model, demo=demo, chat_id=int(chat_id), default_tour=default_tour)
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
    return app
