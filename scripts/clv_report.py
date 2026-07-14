"""Offline CLV report: segment the paper-test track record to read the go-live verdict.

Read-only over the SQLite log (data/matador.db). Reuses matador.clv.summarize -- the SAME
net-of-fee, day-clustered bootstrap the /stats go-live gate uses, so no math is re-derived here --
on the whole sample and on each segment: tour, entry price band, adverse-selection flag, and ISO
week. A capture-health tally (auto / manual / missed) shows data quality. Segments below the 200-bet
go-live floor are annotated as informational (their CI is too wide to gate on).

    .venv/bin/python scripts/clv_report.py
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from matador import storage  # noqa: E402
from matador.clv import MIN_BETS, summarize  # noqa: E402
from matador.config import load_config  # noqa: E402

_BANDS = ["longshot(<35¢)", "midprice(35-65¢)", "favorite(>65¢)"]  # fees peak near 50¢, shrink on favorites


def _price_band(price) -> str:
    if price is None:
        return "unknown"
    if price < 0.35:
        return _BANDS[0]
    if price <= 0.65:
        return _BANDS[1]
    return _BANDS[2]


def _week(row) -> str:
    """ISO-week label (YYYY-Www) of the scheduled match date, for a time trend."""
    d = (row["occurrence_datetime"] or row["ts"] or "")[:10]
    try:
        y, w, _ = date.fromisoformat(d).isocalendar()
        return f"{y}-W{w:02d}"
    except ValueError:
        return "unknown"


def segment_summaries(bets, cfg) -> dict:
    """{axis: [(label, clv.summarize(subset)), ...]} for each segmentation axis -- every subset run
    through the same summarize() as /stats, so per-segment figures are consistent with the gate."""
    def grouped(key_fn, order=None):
        groups: dict = {}
        for b in bets:
            groups.setdefault(key_fn(b), []).append(b)
        labels = [lab for lab in (order or sorted(groups)) if lab in groups]
        return [(lab, summarize(groups[lab], cfg)) for lab in labels]

    return {
        "tour": grouped(lambda b: b["tour"] or "?"),
        "price_band": grouped(lambda b: _price_band(b["price"]), order=_BANDS),
        "flag": grouped(lambda b: "flagged" if b["flagged"] else "unflagged", order=["flagged", "unflagged"]),
        "week": grouped(_week),
    }


def _fmt_segment(label: str, s: dict) -> str:
    n = s["n_clv"]
    if n == 0:
        return f"  {label:<20} n=0   (no captured closing lines)"
    lo, hi = s["clv_ci"]
    gate = "GO-LIVE ✅" if s["go_live"] else "not met"
    note = "" if n >= MIN_BETS else "  [informational: < 200-bet floor]"
    return f"  {label:<20} n={n:<4} mean net CLV {s['mean_clv']:+.2%}  95% CI [{lo:+.2%}, {hi:+.2%}]  {gate}{note}"


def main() -> None:
    cfg = load_config()
    conn = storage.connect(cfg.db_path)
    storage.init_db(conn)
    bets = storage.settled_bets(conn)
    conn.close()

    overall = summarize(bets, cfg)
    c = overall["captures"]
    print(f"=== CLV report — {overall['n_opportunities']} opportunities logged, "
          f"{overall['n_clv']} with a closing line ===")
    print(f"Capture health: {c['auto']} auto / {c['manual']} manual / {c['missed']} missed\n")
    print("Overall:")
    print(_fmt_segment("all", overall))

    for axis, rows in segment_summaries(bets, cfg).items():
        print(f"\nBy {axis.replace('_', ' ')}:")
        if not rows:
            print("  (no data)")
        for label, s in rows:
            print(_fmt_segment(label, s))


if __name__ == "__main__":
    main()
