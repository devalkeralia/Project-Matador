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
    score_state TEXT
);

CREATE TABLE IF NOT EXISTS outcomes (
    opp_id INTEGER PRIMARY KEY REFERENCES opportunities(id),
    fill_price REAL,
    contracts_filled INTEGER,
    closing_price REAL,            -- same-side market price at scheduled match start (the CLV baseline)
    closing_captured_at TEXT,      -- when the closing line was snapshotted (ISO)
    closing_source TEXT,           -- how it was captured: 'manual' (/close) or 'auto' (scheduled)
    result TEXT CHECK (result IS NULL OR result IN ('win', 'loss')),
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
    "score_state",
)

_OUTCOME_COLUMNS = (
    "fill_price", "contracts_filled", "closing_price", "closing_captured_at", "closing_source",
    "result", "pnl", "clv",
)

# Columns added after the first schema shipped -- ALTER them onto pre-existing DBs (see _ensure_columns).
_MIGRATIONS = {
    "opportunities": {"market_player": "TEXT"},
    "outcomes": {"closing_captured_at": "TEXT", "closing_source": "TEXT"},
}


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    for table, columns in _MIGRATIONS.items():
        _ensure_columns(conn, table, columns)
    conn.commit()


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


def get_opportunity(conn: sqlite3.Connection, opp_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM opportunities WHERE id = ?", (opp_id,)).fetchone()


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
        "       oc.result, oc.contracts_filled, oc.pnl, oc.clv "
        "FROM opportunities o LEFT JOIN outcomes oc ON oc.opp_id = o.id ORDER BY o.id"
    ).fetchall()


def pending_captures(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Opportunities that still have no captured closing line -- the work list for /close (all) and
    the auto-capture scheduler."""
    return conn.execute(
        "SELECT o.id, o.market_ticker, o.side, o.occurrence_datetime "
        "FROM opportunities o LEFT JOIN outcomes oc ON oc.opp_id = o.id "
        "WHERE oc.closing_price IS NULL ORDER BY o.id"
    ).fetchall()
