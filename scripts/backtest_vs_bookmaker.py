"""Does p_model beat the sharp bookmaker CLOSING line? (edge/ROI + sharpness, by experience)

Replays our held-out matches, joins tennis-data.co.uk per-season closing odds (AvgW/AvgL) by
surname-initial pair + date, and reports: model-vs-market sharpness (Brier, Brier-optimal blend
weight, who wins disagreements) and a flat-stake ROI backtest of our >= min_net_edge rule,
segmented by player experience.

This is a PROXY validation, NOT forward CLV: the bookmaker close is the sharpest line (hardest
to beat) and we bet vs the vigged decimal odds with no Kalshi fee. Result to date: p_model does
NOT beat the close (w* ~ 0, negative ROI) -- see scripts/backtest_vs_kalshi.py for our actual
(softer) venue. Needs data/model.json (scripts/build_ratings.py) and match data under data/.

    .venv/bin/python scripts/backtest_vs_bookmaker.py [start_year end_year]   # default 2025 2026
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import pandas as pd  # noqa: E402

from matador.backtest import de_vig, replay_predictions, roi_by_experience, sharpness, tennisdata_key  # noqa: E402
from matador.config import load_config  # noqa: E402
from matador.model.elo import KFactor  # noqa: E402
from matador.sackmann import load_matches  # noqa: E402

DATA_DIR = "data"
ODDS_DIR = Path(DATA_DIR) / "odds"   # cached tennis-data.co.uk files (gitignored under data/)
BASE_URL = "http://www.tennis-data.co.uk"  # HTTP only; their HTTPS/TLS is broken


def _fetch_odds(tour: str, year: int) -> Path | None:
    """Download (and cache) one season's tennis-data.co.uk file. ATP = /{yr}/{yr}.xlsx,
    WTA = /{yr}w/{yr}.xlsx. Their server is flaky, so retry."""
    ODDS_DIR.mkdir(parents=True, exist_ok=True)
    dest = ODDS_DIR / f"{tour}_{year}.xlsx"
    if dest.exists():
        return dest
    seg = f"{year}" if tour == "atp" else f"{year}w"
    url = f"{BASE_URL}/{seg}/{year}.xlsx"
    for _ in range(4):
        try:
            r = httpx.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=40, follow_redirects=True)
            if r.status_code == 200 and r.content[:2] == b"PK":  # xlsx = zip
                dest.write_bytes(r.content)
                return dest
        except Exception:
            pass
    print(f"  [warn] could not fetch {tour} {year} odds ({url})")
    return None


def _market_lookup(tour: str, years: range) -> dict:
    """(frozenset(winner_key, loser_key)) -> list of (date, p_market_for_winner, winner_odds).
    Keeps de-vigged prob (for the edge decision) AND the actual vigged winner odds (for P&L)."""
    mkt: dict = {}
    for yr in years:
        path = _fetch_odds(tour, yr)
        if not path:
            continue
        df = pd.read_excel(path)
        if "AvgW" not in df.columns:
            continue
        for r in df.dropna(subset=["AvgW", "AvgL", "Winner", "Loser", "Date"]).itertuples(index=False):
            key = frozenset({tennisdata_key(r.Winner), tennisdata_key(r.Loser)})
            mkt.setdefault(key, []).append((pd.Timestamp(r.Date), de_vig(float(r.AvgW), float(r.AvgL)), float(r.AvgW)))
    return mkt


def main() -> None:
    cfg = load_config()
    e = cfg.elo
    art = json.loads((Path(DATA_DIR) / "model.json").read_text())
    years = range(int(sys.argv[1]) if len(sys.argv) > 2 else 2025, (int(sys.argv[2]) if len(sys.argv) > 2 else 2026) + 1)
    k = KFactor(e.k_num, e.k_shift, e.k_pow)

    diag_rows: list[tuple[float, float]] = []   # (p_model_winner, p_market_winner)
    bets: list[tuple] = []                        # (tour, n_bet, pnl)
    joined = eligible = 0
    for tour in ("atp", "wta"):
        try:
            matches = load_matches(tour, DATA_DIR)
        except FileNotFoundError as exc:
            print(f"[{tour}] no data: {exc}")
            continue
        scales = {int(bo): float(s) for bo, s in art["tours"][tour]["scales"].items()}
        preds = replay_predictions(
            matches, surface_weight=e.surface_weight, scales=scales, min_matches=cfg.min_matches,
            shrinkage_n0=e.shrinkage_n0, initial=e.initial_rating, k=k, holdout_from_year=years.start,
        )
        eligible += len(preds)
        mkt = _market_lookup(tour, years)
        for p in preds:
            recs = mkt.get(frozenset({p.key_a, p.key_b}))
            if not recs:
                continue
            date, qw, aw = min(recs, key=lambda r: abs((r[0] - p.date).days))
            if abs((date - p.date).days) > 3:
                continue
            joined += 1
            diag_rows.append((p.p_a, qw))
            # bet the higher-edge side (gross edge vs de-vigged prob; flat $1 P&L at ACTUAL vigged odds)
            edge_w, edge_l = p.p_a - qw, (1 - p.p_a) - (1 - qw)
            if max(edge_w, edge_l) >= cfg.min_net_edge:
                if edge_w >= edge_l:
                    bets.append((tour, p.n_a, aw - 1.0))            # back winner at actual odds -> won
                else:
                    bets.append((tour, p.n_b, -1.0))                 # back loser -> lost

    print(f"eligible held-out predictions: {eligible:,} | joined to bookmaker odds: {joined:,}\n")
    s = sharpness([d[0] for d in diag_rows], [d[1] for d in diag_rows])
    print(f"=== model vs bookmaker CLOSE (winner-side, n={s.get('n', 0):,}) ===")
    if s.get("n"):
        print(f"  Brier: model={s['brier_model']:.4f}  market={s['brier_market']:.4f}")
        print(f"  optimal blend weight on MODEL: w*={s['blend_w_star']:.2f}  (0 => market subsumes model)")
        print(f"  disagree on favorite: {s['disagree_frac']:.1%}  ->  market right {s['market_right_on_disagree']:.1%} vs model {s['model_right_on_disagree']:.1%}")
    b = pd.DataFrame(bets, columns=["tour", "n_bet", "pnl"])
    print(f"\n=== flat-stake backtest (>= {cfg.min_net_edge:.0%} edge; vs vigged decimal odds, no Kalshi fee) ===")
    for lab, n, roi, pnl in roi_by_experience(b):
        print(f"  {lab:<22} bets={n:>5}  ROI={roi:>+7.1%}  pnl={pnl:>+8.1f}u")
    print("\nNOTE: proxy vs the SHARP close, not forward CLV; see backtest_vs_kalshi.py for our real venue.")


if __name__ == "__main__":
    main()
