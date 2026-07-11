import sqlite3

import pytest

from matador.storage import connect, get_opportunity, init_db, insert_opportunity, record_outcome, recent_opportunities


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


def test_last_opportunity_returns_latest_matching_or_none(db):
    from matador.storage import last_opportunity

    assert last_opportunity(db, "T-1", "yes") is None  # nothing logged yet
    make_opportunity(db, market_ticker="T-1", side="yes", price=0.40)
    make_opportunity(db, market_ticker="T-1", side="yes", price=0.42)
    make_opportunity(db, market_ticker="T-1", side="no", price=0.60)

    assert last_opportunity(db, "T-1", "yes")["price"] == 0.42  # most recent yes
    assert last_opportunity(db, "T-1", "no")["price"] == 0.60
    assert last_opportunity(db, "T-2", "yes") is None  # different market
