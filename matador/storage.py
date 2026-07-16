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
    score_state TEXT
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

-- Dedup lookup: a polling alert loop checks last_opportunity() before re-inserting a
-- still-standing pre-match edge, so one edge doesn't fire on every poll.
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


def update_occurrence(conn: sqlite3.Connection, opp_id: int, occurrence_datetime: str) -> None:
    """Refresh a logged opportunity's scheduled match time -- the ONLY writer of this column after
    insert. Called when the live Kalshi market shows the match was postponed, so the closing-line
    capture can re-arm against the corrected start (see bot.auto_capture)."""
    conn.execute("UPDATE opportunities SET occurrence_datetime = ? WHERE id = ?", (occurrence_datetime, opp_id))
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
    """Most recent alert logged for this contract + side, or None -- the dedup lookup the
    alert layer checks before re-inserting the same standing pre-match edge."""
    return conn.execute(
        "SELECT * FROM opportunities WHERE market_ticker = ? AND side = ? ORDER BY id DESC LIMIT 1",
        (market_ticker, side),
    ).fetchone()


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
