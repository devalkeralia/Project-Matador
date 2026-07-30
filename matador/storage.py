import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    tour TEXT NOT NULL,
    event TEXT,
    match TEXT,
    market_ticker TEXT NOT NULL,
    event_ticker TEXT,
    market_player TEXT,         -- yes_sub_title: the player the Yes contract pays out on (self-describing alert log)
    opponent TEXT,              -- the other player's full name (for the sharp-line pair match at close capture)
    side TEXT NOT NULL CHECK (side IN ('yes', 'no')),
    price REAL NOT NULL,
    p_model REAL NOT NULL,
    net_edge REAL NOT NULL,
    suggested_stake REAL,
    contracts INTEGER,
    liquidity REAL,
    trigger_reason TEXT CHECK (trigger_reason IN ('prematch_value', 'inplay_meanrev', 'situational')),
    occurrence_datetime TEXT,   -- scheduled match time; Phase 5/6 uses it to fetch the closing line for CLV
    flagged INTEGER DEFAULT 0,  -- adverse-selection: net edge >= adverse_gap (possible late news)
    experience INTEGER,         -- min prior-match count of the two players (thin-player flag + CLV segmentation)
    staleness INTEGER,          -- max days since either player last played (layoff CLV segmentation; instrumentation only)
    score_state TEXT,
    -- Sharp (Pinnacle, Shin-devigged) fair prob of the TAKEN side AT ENTRY, captured post-decision.
    -- Pairs with outcomes.sharp_close to split a positive gate into skill vs venue basis:
    --   sharp_close - sharp_entry  = the sharp line moving toward us AFTER we committed (skill)
    --   sharp_entry - price        = a standing Kalshi-vs-Pinnacle basis (NOT skill)
    -- Must be collected live: the-odds-api historical odds are not on the free tier.
    sharp_entry REAL,
    sharp_entry_source TEXT     -- 'pinnacle' | 'consensus' (which reference produced sharp_entry)
);

CREATE TABLE IF NOT EXISTS outcomes (
    opp_id INTEGER PRIMARY KEY REFERENCES opportunities(id),
    fill_price REAL,
    contracts_filled INTEGER,
    closing_price REAL,            -- same-side market price at scheduled match start (the CLV baseline)
    closing_captured_at TEXT,      -- when the closing line was snapshotted (ISO)
    closing_source TEXT,           -- how it was captured: 'manual' (/close) or 'auto' (scheduled)
    sharp_close REAL,              -- Shin-devigged sharp (Pinnacle) fair prob of the TAKEN side at close -- the sharp go-live gate's baseline
    sharp_source TEXT,             -- 'pinnacle' | 'consensus' (which sharp reference produced sharp_close)
    result TEXT CHECK (result IS NULL OR result IN ('win', 'loss', 'void')),  -- 'void' = walkover/refund (excluded from CLV, hit-rate, P&L)
    pnl REAL,
    clv REAL
);

-- Contract-level lookup (last_opportunity): the alert layer's prior-id message and the
-- no-opponent dedup fallback. NOT the dedup key -- that is the economic position, keyed on
-- (event_ticker, backed player) via last_position(); see its docstring for why.
CREATE INDEX IF NOT EXISTS idx_opportunities_market ON opportunities(market_ticker, side);
"""

_OPPORTUNITY_COLUMNS = (
    "ts",
    "tour",
    "event",
    "match",
    "market_ticker",
    "event_ticker",
    "market_player",
    "opponent",
    "side",
    "price",
    "p_model",
    "net_edge",
    "suggested_stake",
    "contracts",
    "liquidity",
    "trigger_reason",
    "occurrence_datetime",
    "flagged",
    "experience",
    "staleness",
    "score_state",
)

_OUTCOME_COLUMNS = (
    "fill_price", "contracts_filled", "closing_price", "closing_captured_at", "closing_source",
    "sharp_close", "sharp_source", "result", "pnl", "clv",
)

# Columns added after the first schema shipped -- ALTER them onto pre-existing DBs (see _ensure_columns).
_MIGRATIONS = {
    "opportunities": {
        "event_ticker": "TEXT", "market_player": "TEXT", "occurrence_datetime": "TEXT",
        "flagged": "INTEGER DEFAULT 0", "experience": "INTEGER", "opponent": "TEXT",
        "staleness": "INTEGER", "sharp_entry": "REAL", "sharp_entry_source": "TEXT",
    },
    "outcomes": {
        "closing_captured_at": "TEXT", "closing_source": "TEXT",
        "sharp_close": "REAL", "sharp_source": "TEXT",
    },
}


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")   # concurrent reads don't block the capture writer
    conn.execute("PRAGMA busy_timeout = 5000")  # a locked writer waits up to 5s instead of erroring out (scan + capture jobs run on separate threads)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    for table, columns in _MIGRATIONS.items():
        _ensure_columns(conn, table, columns)
    _migrate_outcomes_result_check(conn)
    conn.commit()


def _migrate_outcomes_result_check(conn: sqlite3.Connection) -> None:
    """SQLite can't ALTER a CHECK, so a pre-'void' outcomes table would reject 'void' results.
    If the stored table def lacks 'void' AND has no rows, rebuild it from SCHEMA (safe at 0 rows).
    A populated old table is left as-is (rare; 'void' inserts would fail until a manual migration)."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='outcomes'").fetchone()
    if row and "void" not in (row["sql"] or "") and conn.execute("SELECT count(*) FROM outcomes").fetchone()[0] == 0:
        conn.execute("DROP TABLE outcomes")
        conn.executescript(SCHEMA)  # CREATE IF NOT EXISTS: rebuilds outcomes, no-ops opportunities/index


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    """Add any missing nullable columns to an existing table (CREATE TABLE IF NOT EXISTS won't
    alter one that predates a column). Idempotent -- skips columns that already exist."""
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, decl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _validated_insert(conn: sqlite3.Connection, table: str, allowed_columns: tuple[str, ...], fields: dict) -> sqlite3.Cursor:
    unknown = set(fields) - set(allowed_columns)
    if unknown:
        raise ValueError(f"unknown {table} field(s): {sorted(unknown)}")
    columns = list(fields)
    placeholders = ", ".join("?" for _ in columns)
    cursor = conn.execute(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
        [fields[c] for c in columns],
    )
    conn.commit()
    return cursor


def insert_opportunity(conn: sqlite3.Connection, **fields) -> int:
    cursor = _validated_insert(conn, "opportunities", _OPPORTUNITY_COLUMNS, fields)
    return cursor.lastrowid


def record_outcome(conn: sqlite3.Connection, opp_id: int, **fields) -> None:
    """Upsert the outcome row for `opp_id`, merging only the provided fields. Closing-line capture
    (near match start) and result-recording (after the match) write at different times to the same
    one-per-opportunity row, so this MERGES rather than replacing (an INSERT would collide on the PK)."""
    unknown = set(fields) - set(_OUTCOME_COLUMNS)
    if unknown:
        raise ValueError(f"unknown outcomes field(s): {sorted(unknown)}")
    columns = ["opp_id", *fields]
    placeholders = ", ".join("?" for _ in columns)
    updates = ", ".join(f"{c} = excluded.{c}" for c in fields) or "opp_id = excluded.opp_id"
    conn.execute(
        f"INSERT INTO outcomes ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT(opp_id) DO UPDATE SET {updates}",
        [opp_id, *(fields[c] for c in fields)],
    )
    conn.commit()


def record_result_if_absent(conn: sqlite3.Connection, opp_id: int, **fields) -> bool:
    """Record an outcome ONLY if no result is stored yet. Returns True if this call wrote it.

    The auto-recorder reads its "don't overwrite" guard, then makes a Kalshi round trip, then writes --
    a window in which the owner can type /result and have it silently replaced by a fabricated fill.
    Making the absence check part of the WRITE closes that race, so a human entry always wins.
    """
    unknown = set(fields) - set(_OUTCOME_COLUMNS)
    if unknown:
        raise ValueError(f"unknown outcomes field(s): {sorted(unknown)}")
    columns = ["opp_id", *fields]
    placeholders = ", ".join("?" for _ in columns)
    updates = ", ".join(f"{c} = excluded.{c}" for c in fields)
    cur = conn.execute(
        f"INSERT INTO outcomes ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT(opp_id) DO UPDATE SET {updates} WHERE outcomes.result IS NULL",
        [opp_id, *(fields[c] for c in fields)],
    )
    conn.commit()
    return cur.rowcount > 0


def update_occurrence(conn: sqlite3.Connection, opp_id: int, occurrence_datetime: str) -> None:
    """Refresh a logged opportunity's scheduled match time -- the ONLY writer of this column after
    insert. Called when the live Kalshi market shows the match was postponed, so the closing-line
    capture can re-arm against the corrected start (see bot.auto_capture)."""
    conn.execute("UPDATE opportunities SET occurrence_datetime = ? WHERE id = ?", (occurrence_datetime, opp_id))
    conn.commit()


def set_sharp_entry(conn: sqlite3.Connection, opp_id: int, prob: float, source: str) -> None:
    """Record the sharp fair prob observed AT ENTRY -- the only writer of these columns after insert.

    Written post-decision (see bot._sharp_entry_job), never as part of the alert path, and only when
    a probability was actually resolved: leaving the column NULL is the honest encoding of 'no sharp
    reference for this row', which the week-12 decomposition filters on."""
    conn.execute("UPDATE opportunities SET sharp_entry = ?, sharp_entry_source = ? WHERE id = ?",
                 (prob, source, opp_id))
    conn.commit()


def get_opportunity(conn: sqlite3.Connection, opp_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM opportunities WHERE id = ?", (opp_id,)).fetchone()


def get_outcome(conn: sqlite3.Connection, opp_id: int) -> sqlite3.Row | None:
    """The outcome row for an opportunity, or None if none recorded yet -- lets capture_close tell an
    already-captured row from a fresh one (idempotent capture)."""
    return conn.execute("SELECT * FROM outcomes WHERE opp_id = ?", (opp_id,)).fetchone()


def recent_opportunities(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM opportunities ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def last_opportunity(conn: sqlite3.Connection, market_ticker: str, side: str) -> sqlite3.Row | None:
    """Most recent alert logged for this contract + side, or None. Retained for the alert layer's
    'show the PRIOR opp id' message; the DEDUP decision uses last_position() instead -- see why."""
    return conn.execute(
        "SELECT * FROM opportunities WHERE market_ticker = ? AND side = ? ORDER BY id DESC LIMIT 1",
        (market_ticker, side),
    ).fetchone()


def last_position(conn: sqlite3.Connection, event_ticker: str, backed_player: str) -> sqlite3.Row | None:
    """Most recent alert for this ECONOMIC POSITION -- the event plus the player actually backed --
    or None. This, not (market_ticker, side), is the correct dedup identity.

    Why: a Kalshi event has one market per player, so ONE position is expressible two ways --
    yes on the backed player's market, or no on the opponent's. Which anchor you get depends on
    caller-specific ordering: scan_series sorts by ticker, while client.resolve_match anchors on
    whichever player the owner TYPED first. So the same bet arrived under two different
    (market_ticker, side) keys and logged twice -- observed live 2026-07-29 as Shapovalov/Hijikata
    under both -SHA/no and -HIJ/yes, both backing Hijikata at 41c.

    That is not cosmetic: a duplicate double-weights the match in the CLV mean AND in the ISO-week
    cluster bootstrap, narrowing the CI, and inflates the count toward the >=200 floor -- biasing
    the go-live gate TOWARD a false positive, invisible until the gate is read weeks later.

    Keyed on the stored `event_ticker` (identifies the match regardless of anchor) and the backed
    player, resolved from side: market_player on yes, opponent on no.
    """
    return conn.execute(
        "SELECT * FROM opportunities WHERE event_ticker = ? AND side IS NOT NULL "
        "  AND (CASE WHEN side = 'yes' THEN market_player ELSE opponent END) = ? "
        "ORDER BY id DESC LIMIT 1",
        (event_ticker, backed_player),
    ).fetchone()


def awaiting_result(conn: sqlite3.Connection, now_iso: str, hours: int = 12) -> list[sqlite3.Row]:
    """Bets whose match started > `hours` ago but which still have no win/loss/void recorded.

    The go-live gate hard-requires realized net-ROI >= 0, and roi is None until /result is entered --
    so a few forgotten weeks make the week-12 read unreadable, or biased toward whichever results the
    owner happened to feel like entering. Unlike a match result, a paper FILL price cannot be
    reconstructed after the fact, which is why this needs surfacing while the memory is fresh."""
    # COALESCE to the log timestamp so a row with no scheduled start is still swept and still nagged.
    # Filtering those out left them in no work list at all: never auto-recorded, never listed, ROI
    # missing forever. No such row exists live today (verified 2026-07-30), which is exactly why it
    # would have gone unnoticed.
    return conn.execute(
        "SELECT o.id, o.occurrence_datetime FROM opportunities o "
        "  LEFT JOIN outcomes oc ON oc.opp_id = o.id "
        "WHERE COALESCE(o.occurrence_datetime, o.ts) < ? "
        "  AND (oc.result IS NULL) ORDER BY COALESCE(o.occurrence_datetime, o.ts)",
        (_shift_iso(now_iso, -hours),),
    ).fetchall()


def _shift_iso(now_iso: str, hours: int) -> str:
    """`now_iso` moved by `hours`, rendered in Kalshi's own `...THH:MM:SSZ` shape.

    The comparison in awaiting_result is a STRING compare, which is only order-preserving because
    every occurrence_datetime we store comes from Kalshi in that one zero-offset format -- so the
    threshold must be emitted in it too, not as the `+00:00` spelling Python defaults to. The two
    spellings of an identical instant would sort differently ('+' < 'Z'), which at worst mis-bins a
    bet sitting exactly on the 12-hour line -- harmless here, but only by luck, so keep the formats
    aligned rather than relying on that.
    """
    from datetime import datetime, timedelta, timezone
    try:
        dt = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return now_iso
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt + timedelta(hours=hours)).astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def untimed_pending(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Pending-capture rows with NO scheduled start time.

    These are invisible failures: bot.schedule_pending_captures skips them (it cannot arm a timer
    without a start), so they never auto-capture, never DM a miss, and sit in the pending count
    forever. The only way out is a manual `/close <id> pre`, which the owner has to be told to run."""
    return [r for r in pending_captures(conn) if not r["occurrence_datetime"]]


def settled_bets(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every logged opportunity LEFT JOINed to its outcome (closing line / fill / result) -- the
    input to /stats. Rows without an outcome carry NULLs; the stats layer filters (CLV needs a
    closing_price, P&L/hit-rate need a result)."""
    return conn.execute(
        "SELECT o.*, oc.fill_price, oc.closing_price, oc.closing_captured_at, oc.closing_source, "
        "       oc.sharp_close, oc.sharp_source, oc.result, oc.contracts_filled, oc.pnl, oc.clv "
        "FROM opportunities o LEFT JOIN outcomes oc ON oc.opp_id = o.id ORDER BY o.id"
    ).fetchall()


def open_exposure(conn: sqlite3.Connection) -> float:
    """Total suggested stake ($) across logged opportunities with no recorded outcome yet -- the
    aggregate open exposure the alert layer warns on (correlated same-day alerts can sum past the
    bankroll; there's no per-alert cap for that)."""
    row = conn.execute(
        "SELECT COALESCE(SUM(o.suggested_stake), 0) FROM opportunities o "
        "LEFT JOIN outcomes oc ON oc.opp_id = o.id WHERE oc.result IS NULL"
    ).fetchone()
    return float(row[0])


def pending_captures(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Opportunities that still have no captured closing line -- the work list for /close (all) and
    the auto-capture scheduler."""
    return conn.execute(
        "SELECT o.id, o.market_ticker, o.side, o.occurrence_datetime "
        "FROM opportunities o LEFT JOIN outcomes oc ON oc.opp_id = o.id "
        "WHERE oc.closing_price IS NULL AND oc.closing_source IS NULL ORDER BY o.id"  # exclude already-attempted (missed) rows
    ).fetchall()
