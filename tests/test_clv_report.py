"""scripts/clv_report.py -- offline CLV segmentation (delegates every subset to clv.summarize)."""
import importlib.util
from pathlib import Path

from matador.clv import summarize
from matador.config import Config
from matador.storage import connect, init_db, insert_opportunity, record_outcome, settled_bets

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "clv_report.py"


def _load():
    spec = importlib.util.spec_from_file_location("clv_report_mod", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cfg() -> Config:
    return Config(bankroll=1000.0, min_liquidity=10.0, max_spread=0.10)


def _seed():
    conn = connect(":memory:")
    init_db(conn)
    base = dict(ts="2026-08-05T12:00:00Z", trigger_reason="prematch_value",
                occurrence_datetime="2026-08-05T13:00:00Z", experience=100)

    def opp(tour, ticker, price):
        return insert_opportunity(conn, tour=tour, market_ticker=ticker, side="yes",
                                  price=price, p_model=0.6, net_edge=0.05, **base)

    record_outcome(conn, opp("ATP", "A", 0.70), closing_price=0.74, closing_source="auto")    # ATP favorite
    record_outcome(conn, opp("ATP", "B", 0.30), closing_price=0.33, closing_source="manual")  # ATP longshot
    record_outcome(conn, opp("WTA", "W", 0.50), closing_price=0.55, closing_source="auto")    # WTA midprice
    record_outcome(conn, opp("ATP", "M", 0.50), closing_source="missed:late[auto]",           # ATP missed -> no closing_price
                   closing_captured_at="2026-08-05T14:00:00Z")
    return conn


def test_clv_report_segments_match_manual_summarize():
    mod = _load()
    cfg = _cfg()
    conn = _seed()
    bets = settled_bets(conn)
    segs = mod.segment_summaries(bets, cfg)

    # A tour segment must equal summarize() on the same subset filtered by hand (no re-derived math).
    atp_seg = dict(segs["tour"])["ATP"]
    atp_manual = summarize([b for b in bets if b["tour"] == "ATP"], cfg)
    assert atp_seg["n_clv"] == atp_manual["n_clv"] == 2   # 2 ATP captured; the missed row has no closing_price
    assert atp_seg["mean_clv"] == atp_manual["mean_clv"]

    bands = dict(segs["price_band"])
    assert bands[mod._BANDS[2]]["n_clv"] == 1             # favorite: ATP 0.70
    assert bands[mod._BANDS[0]]["n_clv"] == 1             # longshot: ATP 0.30

    caps = summarize(bets, cfg)["captures"]
    assert caps == {"auto": 2, "manual": 1, "sharp_only": 0, "missed": 1}
    conn.close()
