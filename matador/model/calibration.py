"""Walk-forward calibration harness for the v1 Elo model.

No-lookahead replay -> Brier score, log-loss, reliability curve (overall and per Bo3/Bo5),
plus per-format logistic-scale fitting. This is the Phase-2 "validate before you trust it"
gate: a miscalibrated p_model makes every downstream net-edge number fiction.

Match orientation is a = the lower player id (outcome-independent), so the evaluation
labels aren't biased toward 1 the way "always predict the winner" would be.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from matador.model.elo import KFactor, RatingBook, prepare_matches
from matador.model.probability import blended_rating, prob_from_diff

_CLIP = 1e-12


@dataclass
class Records:
    """Pre-match walk-forward observations (a = lower player id)."""

    date: np.ndarray      # match dates
    diff: np.ndarray      # blended rating diff, R_a - R_b
    y: np.ndarray         # 1 if player a won, else 0
    best_of: np.ndarray   # int (3 or 5)
    experience: np.ndarray | None = None  # min prior-match count of the two players (for per-experience calibration)


def walk_forward(
    matches: pd.DataFrame,
    *,
    surface_weight: float,
    min_matches: int,
    initial: float = 1500.0,
    k: KFactor | None = None,
    shrinkage_n0: float = 0.0,
) -> Records:
    """Replay matches chronologically; record the pre-update blended diff + outcome for
    every match where BOTH players already have >= min_matches prior matches."""
    book = RatingBook(initial=initial, k=k)
    dates: list = []
    diffs: list[float] = []
    ys: list[int] = []
    bos: list[int] = []
    exps: list[int] = []
    for row in prepare_matches(matches).itertuples(index=False):
        w, l = row.winner_id, row.loser_id
        raw_bo = getattr(row, "best_of", None)
        best_of = int(raw_bo) if raw_bo is not None and not pd.isna(raw_bo) else 0
        surface = getattr(row, "surface", None)
        if best_of in (3, 5) and book.overall_count(w) >= min_matches and book.overall_count(l) >= min_matches:
            a, b = (w, l) if w < l else (l, w)
            diffs.append(blended_rating(book, a, surface, surface_weight, shrinkage_n0=shrinkage_n0) - blended_rating(book, b, surface, surface_weight, shrinkage_n0=shrinkage_n0))
            dates.append(row.tourney_date)
            ys.append(1 if a == w else 0)
            bos.append(best_of)
            exps.append(min(book.overall_count(w), book.overall_count(l)))  # pre-update prior-match counts
        md = row.tourney_date.date() if hasattr(row.tourney_date, "date") else row.tourney_date
        book.update(w, l, surface, md)
    return Records(np.array(dates, dtype=object), np.array(diffs), np.array(ys, dtype=int),
                   np.array(bos, dtype=int), np.array(exps, dtype=int))


def brier_score(p: np.ndarray, y: np.ndarray) -> float:
    return float(np.mean((np.asarray(p) - np.asarray(y)) ** 2))


def log_loss(p: np.ndarray, y: np.ndarray) -> float:
    p = np.clip(np.asarray(p, dtype=float), _CLIP, 1 - _CLIP)
    y = np.asarray(y, dtype=float)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def reliability_table(p: np.ndarray, y: np.ndarray, n_bins: int = 10) -> list[tuple[float, float, float, int]]:
    """Binned (mean_predicted, mean_observed, count) per probability bin -- a calibrated
    model has mean_predicted ~= mean_observed in every populated bin."""
    p, y = np.asarray(p, dtype=float), np.asarray(y, dtype=float)
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    idx = np.clip(np.digitize(p, edges[1:-1]), 0, n_bins - 1)
    rows = []
    for b in range(n_bins):
        m = idx == b
        if m.any():
            rows.append((0.5 * (edges[b] + edges[b + 1]), float(p[m].mean()), float(y[m].mean()), int(m.sum())))
    return rows


def fit_scale(diff: np.ndarray, y: np.ndarray, lo: float = 50.0, hi: float = 2000.0) -> float:
    """Scale minimizing log-loss of prob_from_diff(diff, scale): coarse grid then refine
    (dependency-free, robust to the shape of the loss curve)."""
    diff, y = np.asarray(diff, dtype=float), np.asarray(y, dtype=float)

    def loss(s: float) -> float:
        p = np.clip(1.0 / (1.0 + 10.0 ** (-diff / s)), _CLIP, 1 - _CLIP)
        return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))

    grid = np.linspace(lo, hi, 60)
    best = float(min(grid, key=loss))
    step = (hi - lo) / 60.0
    fine = [s for s in np.linspace(best - step, best + step, 41) if s > 0]
    return float(min(fine, key=loss))


def evaluate(records: Records, scales: dict[int, float]) -> dict:
    """Brier / log-loss / reliability for the given format scales, overall and per format."""
    out: dict = {}
    buckets = [
        ("overall", np.ones(len(records.y), dtype=bool)),
        ("bo3", records.best_of == 3),
        ("bo5", records.best_of == 5),
    ]
    if records.experience is not None:  # segment by player experience (surfaces thin-player miscalibration the aggregate hides)
        exp = records.experience
        buckets += [
            ("exp<50", exp < 50),
            ("exp50-199", (exp >= 50) & (exp < 200)),
            ("exp200+", exp >= 200),
        ]
    for label, mask in buckets:
        if not mask.any():
            continue
        diff, y, bo = records.diff[mask], records.y[mask], records.best_of[mask]
        p = np.array([prob_from_diff(d, scales[int(b)]) for d, b in zip(diff, bo)])
        out[label] = {
            "n": int(mask.sum()),
            "base_rate": float(y.mean()),
            "brier": brier_score(p, y),
            "log_loss": log_loss(p, y),
            "reliability": reliability_table(p, y),
        }
    return out


def split_by_date(records: Records, train_end: date) -> tuple[Records, Records]:
    """Chronological train/held-out split at train_end (inclusive on train)."""
    dates = np.array([d.date() if hasattr(d, "date") else d for d in records.date])
    train = dates <= train_end
    return _subset(records, train), _subset(records, ~train)


def _subset(r: Records, m: np.ndarray) -> Records:
    return Records(r.date[m], r.diff[m], r.y[m], r.best_of[m],
                   None if r.experience is None else r.experience[m])
