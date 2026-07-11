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
    closing_price REAL,
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

_OUTCOME_COLUMNS = ("fill_price", "contracts_filled", "closing_price", "result", "pnl", "clv")


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


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
    _validated_insert(conn, "outcomes", ("opp_id", *_OUTCOME_COLUMNS), {"opp_id": opp_id, **fields})


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
