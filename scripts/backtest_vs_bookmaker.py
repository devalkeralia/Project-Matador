"""Does p_model beat the sharp bookmaker CLOSING line? (edge/ROI + sharpness, by experience + tier)

Replays our held-out matches, joins tennis-data.co.uk per-season closing odds (AvgW/AvgL) by
surname-initial pair + date, and reports: model-vs-market sharpness (Brier, Brier-optimal blend
weight, who wins disagreements) and a flat-stake ROI backtest of our >= min_net_edge rule,
segmented by player experience AND by tournament tier.

Fidelity choices (do NOT change the no-edge verdict; they tighten the proxy):
  - the edge decision uses the SHIN-devigged fair prob (favorite-longshot correction), not the
    proportional de_vig; the P&L leg still pays at the vigged AvgW you'd actually get;
  - retirements/walkovers are excluded (Comment != "Completed") -- not clean pre-match outcomes;
  - results segment by tier (ATP `Series` / WTA `Tier`) since we'd only trade liquid Slams/Masters.

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

from matador.backtest import devig_shin, replay_predictions, roi_by_experience, sharpness, tennisdata_key  # noqa: E402
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
    """(frozenset(winner_key, loser_key)) -> list of (date, p_shin_winner, winner_odds, tier, round).
    Keeps the SHIN fair prob (for the edge decision) AND the actual vigged winner odds (for P&L).
    Retirements/walkovers are dropped via the Comment column; tier is `Series` (ATP) / `Tier` (WTA)."""
    tier_col = "Series" if tour == "atp" else "Tier"
    mkt: dict = {}
    for yr in years:
        path = _fetch_odds(tour, yr)
        if not path:
            continue
        df = pd.read_excel(path)
        if "AvgW" not in df.columns:
            continue
        if "Comment" in df.columns:
            df = df[df["Comment"] == "Completed"]  # exclude Retired / Walkover / not-completed
        has_tier, has_round = tier_col in df.columns, "Round" in df.columns
        for r in df.dropna(subset=["AvgW", "AvgL", "Winner", "Loser", "Date"]).itertuples(index=False):
            key = frozenset({tennisdata_key(r.Winner), tennisdata_key(r.Loser)})
            tier = getattr(r, tier_col) if has_tier else "?"
            rnd = getattr(r, "Round") if has_round else "?"
            mkt.setdefault(key, []).append(
                (pd.Timestamp(r.Date), devig_shin(float(r.AvgW), float(r.AvgL)), float(r.AvgW), tier or "?", rnd or "?"))
    return mkt


def main() -> None:
    cfg = load_config()
    e = cfg.elo
    art = json.loads((Path(DATA_DIR) / "model.json").read_text())
    years = range(int(sys.argv[1]) if len(sys.argv) > 2 else 2025, (int(sys.argv[2]) if len(sys.argv) > 2 else 2026) + 1)
    k = KFactor(e.k_num, e.k_shift, e.k_pow)

    diag_rows: list[tuple] = []   # (p_model_winner, p_market_winner, tier)
    bets: list[tuple] = []         # (tour, n_bet, pnl, tier)
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
            date, qw, aw, tier, _rnd = min(recs, key=lambda r: abs((r[0] - p.date).days))
            if abs((date - p.date).days) > 3:
                continue
            joined += 1
            diag_rows.append((p.p_a, qw, tier))
            # bet the higher-edge side (gross edge vs Shin fair prob; flat $1 P&L at ACTUAL vigged odds)
            edge_w, edge_l = p.p_a - qw, (1 - p.p_a) - (1 - qw)
            if max(edge_w, edge_l) >= cfg.min_net_edge:
                if edge_w >= edge_l:
                    bets.append((tour, p.n_a, aw - 1.0, tier))       # back winner at actual odds -> won
                else:
                    bets.append((tour, p.n_b, -1.0, tier))            # back loser -> lost

    print(f"eligible held-out predictions: {eligible:,} | joined to bookmaker odds: {joined:,}\n")
    d = pd.DataFrame(diag_rows, columns=["p_model", "p_market", "tier"])
    s = sharpness(d.p_model.tolist(), d.p_market.tolist())
    print(f"=== model vs bookmaker CLOSE (winner-side, n={s.get('n', 0):,}) ===")
    if s.get("n"):
        print(f"  Brier: model={s['brier_model']:.4f}  market={s['brier_market']:.4f}")
        print(f"  optimal blend weight on MODEL: w*={s['blend_w_star']:.2f}  (0 => market subsumes model)")
        print(f"  disagree on favorite: {s['disagree_frac']:.1%}  ->  market right {s['market_right_on_disagree']:.1%} vs model {s['model_right_on_disagree']:.1%}")
    if not d.empty:
        print("  by tier (Brier model vs market, blend w* on model):")
        for tier, grp in sorted(d.groupby("tier"), key=lambda kv: -len(kv[1])):
            st = sharpness(grp.p_model.tolist(), grp.p_market.tolist())
            print(f"    {str(tier):<16} n={st['n']:>5}  model={st['brier_model']:.4f}  market={st['brier_market']:.4f}  w*={st['blend_w_star']:.2f}")

    b = pd.DataFrame(bets, columns=["tour", "n_bet", "pnl", "tier"])
    print(f"\n=== flat-stake backtest (>= {cfg.min_net_edge:.0%} edge; vs vigged decimal odds, no Kalshi fee) ===")
    print("  by experience:")
    for lab, n, roi, pnl in roi_by_experience(b):
        print(f"    {lab:<22} bets={n:>5}  ROI={roi:>+7.1%}  pnl={pnl:>+8.1f}u")
    if not b.empty:
        print("  by tier (the liquid Slams/Masters are where we'd actually trade):")
        for tier, grp in sorted(b.groupby("tier"), key=lambda kv: -len(kv[1])):
            print(f"    {str(tier):<22} bets={len(grp):>5}  ROI={grp.pnl.mean():>+7.1%}  pnl={grp.pnl.sum():>+8.1f}u")
    print("\nNOTE: proxy vs the SHARP close, not forward CLV; see backtest_vs_kalshi.py for our real venue.")
    print("      (These fidelity fixes tighten the proxy; they do NOT change the no-edge verdict.)")


if __name__ == "__main__":
    main()
