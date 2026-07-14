import sqlite3

import pytest

from matador.storage import (
    connect, get_opportunity, init_db, insert_opportunity, pending_captures,
    record_outcome, recent_opportunities, settled_bets, update_occurrence,
)


@pytest.fixture
def db():
    conn = connect(":memory:")
    init_db(conn)
    yield conn
    conn.close()


def make_opportunity(conn, **overrides):
    fields = dict(
        ts="2026-07-02T12:00:00Z",
        tour="ATP",
        event="Wimbledon",
        match="Dimitrov vs Berrettini",
        market_ticker="KXATPMATCH-26JUL04DIMBER-DIM",
        market_player="Grigor Dimitrov",
        side="yes",
        price=0.42,
        p_model=0.50,
        net_edge=0.04,
        suggested_stake=46.0,
        contracts=85,
        liquidity=1500.0,
        trigger_reason="prematch_value",
        score_state=None,
    )
    fields.update(overrides)
    return insert_opportunity(conn, **fields)


def test_init_db_creates_both_tables(db):
    tables = {row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"opportunities", "outcomes"} <= tables


def test_insert_opportunity_returns_id_and_round_trips(db):
    opp_id = make_opportunity(db)
    assert opp_id == 1

    row = get_opportunity(db, opp_id)
    assert row["market_ticker"] == "KXATPMATCH-26JUL04DIMBER-DIM"
    assert row["side"] == "yes"
    assert row["price"] == 0.42
    assert row["trigger_reason"] == "prematch_value"
    assert row["score_state"] is None


def test_insert_opportunity_rejects_unknown_field(db):
    with pytest.raises(ValueError):
        insert_opportunity(db, **{"ts": "x", "tour": "ATP", "market_ticker": "T", "side": "yes",
                                   "price": 0.5, "p_model": 0.5, "net_edge": 0.0, "bogus_field": 1})


def test_insert_opportunity_rejects_invalid_side(db):
    with pytest.raises(sqlite3.IntegrityError):
        make_opportunity(db, side="maybe")


def test_insert_opportunity_rejects_invalid_trigger_reason(db):
    with pytest.raises(sqlite3.IntegrityError):
        make_opportunity(db, trigger_reason="astrology")


def test_record_outcome_links_to_opportunity(db):
    opp_id = make_opportunity(db)
    record_outcome(db, opp_id, fill_price=0.43, contracts_filled=80, closing_price=0.55, result="win", pnl=9.6, clv=0.12)

    row = db.execute("SELECT * FROM outcomes WHERE opp_id = ?", (opp_id,)).fetchone()
    assert row["result"] == "win"
    assert row["clv"] == 0.12


def test_record_outcome_rejects_unknown_opportunity_due_to_fk(db):
    with pytest.raises(sqlite3.IntegrityError):
        record_outcome(db, 999, result="win")


def test_record_outcome_rejects_invalid_result(db):
    opp_id = make_opportunity(db)
    with pytest.raises(sqlite3.IntegrityError):
        record_outcome(db, opp_id, result="push")


def test_record_outcome_allows_null_result(db):
    opp_id = make_opportunity(db)
    record_outcome(db, opp_id, fill_price=0.43)  # trade placed, outcome not known yet
    row = db.execute("SELECT * FROM outcomes WHERE opp_id = ?", (opp_id,)).fetchone()
    assert row["result"] is None


def test_get_opportunity_returns_none_for_missing_id(db):
    assert get_opportunity(db, 12345) is None


def test_recent_opportunities_orders_newest_first_and_respects_limit(db):
    ids = [make_opportunity(db, market_ticker=f"T-{i}") for i in range(5)]

    top_two = recent_opportunities(db, limit=2)

    assert [row["id"] for row in top_two] == list(reversed(ids))[:2]


# ---- Phase 5: upsert, migration, join queries ----

def test_record_outcome_upsert_merges_partial_writes(db):
    opp_id = make_opportunity(db)
    record_outcome(db, opp_id, closing_price=0.56, closing_captured_at="2026-07-13T13:00:00Z", closing_source="auto")
    record_outcome(db, opp_id, fill_price=0.42, contracts_filled=80, result="win", pnl=9.6)  # later, merges
    row = db.execute("SELECT * FROM outcomes WHERE opp_id = ?", (opp_id,)).fetchone()
    assert row["closing_price"] == 0.56 and row["closing_source"] == "auto"  # first write preserved
    assert row["fill_price"] == 0.42 and row["result"] == "win"              # second write merged in
    assert db.execute("SELECT count(*) FROM outcomes").fetchone()[0] == 1    # still one row (no PK collision)


def test_record_outcome_rejects_unknown_field(db):
    opp_id = make_opportunity(db)
    with pytest.raises(ValueError):
        record_outcome(db, opp_id, bogus_field=1)


def test_init_db_migrates_missing_columns_idempotently():
    conn = connect(":memory:")
    # simulate a pre-Phase-5 DB: outcomes without closing_captured_at/closing_source, with data
    conn.executescript(
        "CREATE TABLE opportunities (id INTEGER PRIMARY KEY, market_ticker TEXT, side TEXT);"
        "CREATE TABLE outcomes (opp_id INTEGER PRIMARY KEY, closing_price REAL);"
    )
    conn.execute("INSERT INTO outcomes (opp_id, closing_price) VALUES (1, 0.5)")
    init_db(conn)  # should ALTER-add the missing columns
    assert {"closing_captured_at", "closing_source"} <= {r["name"] for r in conn.execute("PRAGMA table_info(outcomes)")}
    opp_cols = {r["name"] for r in conn.execute("PRAGMA table_info(opportunities)")}
    assert {"market_player", "event_ticker", "occurrence_datetime", "flagged", "experience"} <= opp_cols  # full set
    assert conn.execute("SELECT closing_price FROM outcomes WHERE opp_id=1").fetchone()[0] == 0.5  # data preserved
    init_db(conn)  # idempotent -- re-running must not error
    conn.close()


def test_void_result_migration_rebuilds_old_outcomes_check():
    conn = connect(":memory:")  # pre-'void' schema: result CHECK allows only win/loss, 0 rows
    conn.executescript(
        "CREATE TABLE opportunities (id INTEGER PRIMARY KEY, market_ticker TEXT NOT NULL, side TEXT);"
        "CREATE TABLE outcomes (opp_id INTEGER PRIMARY KEY REFERENCES opportunities(id), "
        "  result TEXT CHECK (result IS NULL OR result IN ('win','loss')));"
    )
    init_db(conn)  # 0 rows -> safe to rebuild outcomes with the 'void'-allowing CHECK
    conn.execute("INSERT INTO opportunities (id, market_ticker, side) VALUES (1, 'M', 'yes')")
    record_outcome(conn, 1, result="void")  # would have violated the old CHECK
    assert conn.execute("SELECT result FROM outcomes WHERE opp_id=1").fetchone()[0] == "void"
    conn.close()


def test_settled_bets_joins_and_pending_captures_filters(db):
    a = make_opportunity(db, market_ticker="T-A", side="yes", price=0.50)
    b = make_opportunity(db, market_ticker="T-B", side="no", price=0.30)
    record_outcome(db, a, closing_price=0.56, closing_captured_at="2026-07-13T13:00:00Z")  # only a captured
    rows = {r["id"]: r for r in settled_bets(db)}
    assert len(rows) == 2
    assert rows[a]["closing_price"] == 0.56 and rows[a]["price"] == 0.50
    assert rows[b]["closing_price"] is None                       # LEFT JOIN -> NULL outcome
    assert [r["id"] for r in pending_captures(db)] == [b]         # a has a closing line; b is pending


def test_update_occurrence_changes_only_that_column(db):
    oid = make_opportunity(db, occurrence_datetime="2026-07-04T13:00:00Z")
    update_occurrence(db, oid, "2026-07-05T15:00:00Z")  # match postponed a day
    row = get_opportunity(db, oid)
    assert row["occurrence_datetime"] == "2026-07-05T15:00:00Z"
    assert row["market_ticker"] == "KXATPMATCH-26JUL04DIMBER-DIM"  # other columns untouched
    assert row["price"] == 0.42


def test_last_opportunity_returns_latest_matching_or_none(db):
    from matador.storage import last_opportunity

    assert last_opportunity(db, "T-1", "yes") is None  # nothing logged yet
    make_opportunity(db, market_ticker="T-1", side="yes", price=0.40)
    make_opportunity(db, market_ticker="T-1", side="yes", price=0.42)
    make_opportunity(db, market_ticker="T-1", side="no", price=0.60)

    assert last_opportunity(db, "T-1", "yes")["price"] == 0.42  # most recent yes
    assert last_opportunity(db, "T-1", "no")["price"] == 0.60
    assert last_opportunity(db, "T-2", "yes") is None  # different market
