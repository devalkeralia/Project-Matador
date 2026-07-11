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
from itertools import chain

from telegram import Update
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes, filters

from matador import storage
from matador.alerts import format_abstain, format_alert, format_find, format_no_alert, format_recent, format_scan
from matador.engine import evaluate_match, list_open_matches, log_opportunity, scan_outright_finals, scan_series
from matador.kalshi.client import KalshiClient
from matador.storage import last_opportunity, recent_opportunities

PROD_BASE = "https://external-api.kalshi.com/trade-api/v2"  # public read-only; --demo uses cfg.kalshi_base_url

HELP = (
    "🎾 Matador — tennis value alerts (paper only; I never place orders)\n\n"
    "/check — value-check one match now; alerts if it's mispriced, else shows the analysis\n"
    "        usage: /check <player> vs <player> [atp|wta]\n"
    "        e.g.   /check Sinner vs Zverev\n"
    "/find [atp|wta] — list open matches; checkable ones ranked by model strength\n"
    "/scan — sweep all open ATP/WTA markets for value\n"
    "/recent [n] — the last n logged opportunities (default 10)\n"
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
    "Signals only — I never place orders. You trade manually on Kalshi."
)

_NOTES_FOOTER = "\n\nℹ️ /notes — how to read this message"
_RECENT_DEFAULT = 10
_RECENT_MAX = 50
_FIND_TOP_N = 5


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


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authed(update, context):
        return
    bd = context.bot_data
    await update.message.reply_text("Scanning open markets…")  # sweep can take a few seconds
    text = await asyncio.to_thread(_scan_job, bd["cfg"], bd["model"], bd["demo"], bd["cfg"].tours)
    await _reply_chunked(update, text)


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


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authed(update, context):
        return
    await update.message.reply_text(HELP)


async def cmd_notes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authed(update, context):
        return
    await update.message.reply_text(NOTES)


def build_application(token: str, cfg, model, chat_id, *, demo: bool = False, default_tour: str = "atp") -> Application:
    """Build the PTB app: stash shared read-only state in bot_data and register the commands,
    each gated to the owner's chat both by filters.Chat and the in-handler is_authorized check."""
    app = ApplicationBuilder().token(token).build()
    app.bot_data.update(cfg=cfg, model=model, demo=demo, chat_id=int(chat_id), default_tour=default_tour)
    chat_filter = filters.Chat(chat_id=int(chat_id))
    app.add_handler(CommandHandler("check", cmd_check, filters=chat_filter))
    app.add_handler(CommandHandler(["find", "findmatch"], cmd_find, filters=chat_filter))
    app.add_handler(CommandHandler("scan", cmd_scan, filters=chat_filter))
    app.add_handler(CommandHandler("recent", cmd_recent, filters=chat_filter))
    app.add_handler(CommandHandler(["notes", "helpnotes"], cmd_notes, filters=chat_filter))
    app.add_handler(CommandHandler(["help", "start"], cmd_help, filters=chat_filter))
    return app
