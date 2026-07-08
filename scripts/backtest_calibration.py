"""Walk-forward calibration report for the v1 Elo model, per tour.

Needs match data under data/tennis_atp/ and data/tennis_wta/ (see matador/sackmann.py for
the current sources: ATP = TML-Database, WTA = Kaggle Sackmann mirror). Run by hand:

    .venv/bin/python scripts/backtest_calibration.py

For each tour it replays history chronologically (no lookahead), fits per-format logistic
scales on all but the last few seasons, and reports Brier / log-loss / reliability on that
held-out tail, overall and per Bo3/Bo5. Tours are evaluated independently.
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from matador.config import load_config  # noqa: E402
from matador.model.calibration import evaluate, fit_scale, split_by_date, walk_forward  # noqa: E402
from matador.model.elo import KFactor  # noqa: E402
from matador.sackmann import load_matches  # noqa: E402

DATA_DIR = "data"
HELD_OUT_YEARS = 2  # evaluate on the last N seasons; train on everything before


def _report_tour(tour: str, cfg) -> None:
    e = cfg.elo
    try:
        matches = load_matches(tour, DATA_DIR)
    except FileNotFoundError as exc:
        print(f"\n[{tour.upper()}] no data: {exc}")
        return
    print(f"\n############ {tour.upper()} — {len(matches):,} matches ############")
    records = walk_forward(
        matches, surface_weight=e.surface_weight, min_matches=cfg.min_matches,
        initial=e.initial_rating, k=KFactor(e.k_num, e.k_shift, e.k_pow), shrinkage_n0=e.shrinkage_n0,
    )
    if len(records.y) == 0:
        print("  no eligible predictions (not enough history?)")
        return
    max_year = max(d.year for d in records.date)
    cutoff = max_year - HELD_OUT_YEARS
    train, test = split_by_date(records, date(cutoff, 12, 31))
    print(
        f"  eligible (both players >= {cfg.min_matches} matches): {len(records.y):,}  |  "
        f"train <= {cutoff}: {len(train.y):,}  |  test {cutoff + 1}-{max_year}: {len(test.y):,}"
    )

    scales: dict[int, float] = {}
    for bo in (3, 5):
        m = train.best_of == bo
        scales[bo] = fit_scale(train.diff[m], train.y[m]) if m.sum() > 200 else 400.0
    print(f"  fitted format scales: Bo3={scales[3]:.0f}  Bo5={scales[5]:.0f}")

    for label, r in evaluate(test, scales).items():
        print(
            f"  [{label}] n={r['n']:,}  base={r['base_rate']:.3f}  "
            f"Brier={r['brier']:.4f}  logloss={r['log_loss']:.4f}"
        )
        for _mid, pred, obs, cnt in r["reliability"]:
            print(f"      {pred:.2f} -> {obs:.2f}  (n={cnt})")


def main() -> None:
    cfg = load_config()
    for tour in ("atp", "wta"):
        _report_tour(tour, cfg)


if __name__ == "__main__":
    main()
