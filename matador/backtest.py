"""Backtest helpers: walk-forward held-out predictions + model-vs-market diagnostics.

Shared by scripts/backtest_vs_bookmaker.py (vs tennis-data.co.uk closing odds) and
scripts/backtest_vs_kalshi.py (vs Kalshi's own pre-match line), and the seed of the Phase-6
validation harness. These measure whether p_model has edge OVER a market -- distinct from
matador.model.calibration, which measures p_model vs OUTCOMES.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from matador.model.elo import KFactor, RatingBook, prepare_matches
from matador.model.probability import blended_rating, prob_from_diff
from matador.names import canonical_key


@dataclass(frozen=True)
class Prediction:
    """One held-out, pre-match model prediction (no lookahead)."""

    date: pd.Timestamp
    key_a: str   # canonical_key of the match winner
    key_b: str   # canonical_key of the match loser
    n_a: int     # winner's prior-match count as of the match (for experience segmenting)
    n_b: int     # loser's prior-match count as of the match
    p_a: float   # model P(winner beats loser)


def replay_predictions(
    matches: pd.DataFrame,
    *,
    surface_weight: float,
    scales: dict[int, float],
    min_matches: int,
    shrinkage_n0: float,
    initial: float = 1500.0,
    k: KFactor | None = None,
    holdout_from_year: int = 2025,
) -> list[Prediction]:
    """Replay matches chronologically; emit a Prediction for each match in/after
    holdout_from_year where BOTH players already have >= min_matches prior matches. The
    model prob uses only pre-update ratings, so there is no lookahead."""
    book = RatingBook(initial=initial, k=k)
    out: list[Prediction] = []
    for row in prepare_matches(matches).itertuples(index=False):
        w, l = row.winner_id, row.loser_id
        raw_bo = getattr(row, "best_of", None)
        bo = int(raw_bo) if raw_bo is not None and not pd.isna(raw_bo) else 0
        surf = getattr(row, "surface", None)
        nw, nl = book.overall_count(w), book.overall_count(l)
        d = row.tourney_date
        yr = d.year if hasattr(d, "year") else int(str(d)[:4])
        if bo in (3, 5) and nw >= min_matches and nl >= min_matches and yr >= holdout_from_year:
            p = prob_from_diff(
                blended_rating(book, w, surf, surface_weight, shrinkage_n0=shrinkage_n0)
                - blended_rating(book, l, surf, surface_weight, shrinkage_n0=shrinkage_n0),
                scales[bo],
            )
            # key off the player NAME (what the market-side join canonicalizes), not the opaque id
            out.append(Prediction(pd.Timestamp(d), canonical_key(str(row.winner_name)), canonical_key(str(row.loser_name)), nw, nl, p))
        book.update(w, l, surf, d.date() if hasattr(d, "date") else d)
    return out


def de_vig(odds_w: float, odds_l: float) -> float:
    """Two-way de-vig of decimal odds -> the market's implied P(winner)."""
    iw, il = 1.0 / odds_w, 1.0 / odds_l
    return iw / (iw + il)


def sharpness(model_p, market_p) -> dict:
    """Winner-side model-vs-market comparison (both are P(actual winner), outcome = 1):
    Brier of each, the Brier-optimal blend weight on the MODEL (0 => market subsumes the
    model), and who is right when they disagree on the favorite."""
    p, q = np.asarray(model_p, dtype=float), np.asarray(market_p, dtype=float)
    if len(p) == 0:
        return {"n": 0}
    ws = np.linspace(0, 1, 101)
    briers = [np.mean((1 - (w * p + (1 - w) * q)) ** 2) for w in ws]
    dis = (p > 0.5) != (q > 0.5)
    return {
        "n": int(len(p)),
        "brier_model": float(np.mean((1 - p) ** 2)),
        "brier_market": float(np.mean((1 - q) ** 2)),
        "blend_w_star": float(ws[int(np.argmin(briers))]),
        "disagree_frac": float(dis.mean()),
        "model_right_on_disagree": float((p[dis] > 0.5).mean()) if dis.any() else float("nan"),
        "market_right_on_disagree": float((q[dis] > 0.5).mean()) if dis.any() else float("nan"),
    }


_DEFAULT_BUCKETS = ((20, 50, "thin/breakout <50"), (50, 200, "[50,200)"), (200, 10**9, "established 200+"))


def roi_by_experience(bets: pd.DataFrame, buckets=_DEFAULT_BUCKETS) -> list[tuple]:
    """bets needs columns `n_bet` (bet player's prior-match count) and `pnl` (per-unit-stake
    P&L). Returns [(label, n, roi, total_pnl)], overall first then per experience bucket."""
    def row(label, s):
        return (label, len(s), float(s.pnl.mean()) if len(s) else 0.0, float(s.pnl.sum()))

    out = [row("overall", bets)]
    for lo, hi, lab in buckets:
        out.append(row(lab, bets[(bets.n_bet >= lo) & (bets.n_bet < hi)]))
    return out
