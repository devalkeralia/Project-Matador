import numpy as np
import pandas as pd
import pytest

from matador.model.calibration import (
    Records,
    brier_score,
    evaluate,
    fit_scale,
    log_loss,
    reliability_table,
    split_by_date,
    walk_forward,
)


def test_brier_and_log_loss_known_values():
    y = np.array([1, 0, 1, 0])
    assert brier_score(y.astype(float), y) == 0.0  # perfect
    assert brier_score(np.full(4, 0.5), y) == pytest.approx(0.25)
    assert log_loss(np.full(4, 0.5), y) == pytest.approx(np.log(2), rel=1e-6)


def test_reliability_table_counts_cover_all():
    p = np.array([0.05, 0.15, 0.85, 0.95])
    y = np.array([0, 0, 1, 1])
    table = reliability_table(p, y, n_bins=10)
    assert sum(row[3] for row in table) == 4  # every point falls in some bin


def test_fit_scale_recovers_known_scale():
    rng = np.random.default_rng(0)
    diff = rng.uniform(-500, 500, 30_000)
    true_scale = 300.0
    p = 1.0 / (1.0 + 10.0 ** (-diff / true_scale))
    y = (rng.random(diff.size) < p).astype(float)
    assert fit_scale(diff, y) == pytest.approx(true_scale, abs=60.0)


def _matches(rows):
    cols = ["tourney_date", "match_num", "round", "surface", "best_of", "winner_id", "winner_name", "loser_id", "loser_name", "score"]
    return pd.DataFrame(rows, columns=cols)


def test_walk_forward_no_lookahead_and_orientation():
    df = _matches([
        [pd.Timestamp("2024-01-01"), 1, "F", "Hard", 3, 1, "P One", 2, "P Two", "6-4 6-4"],
        [pd.Timestamp("2024-02-01"), 1, "F", "Hard", 5, 2, "P Two", 1, "P One", "6-4 6-4 6-4"],
    ])
    rec = walk_forward(df, surface_weight=0.7, min_matches=0)
    assert len(rec.y) == 2
    assert list(rec.best_of) == [3, 5]
    # first match: both players at the initial rating -> diff 0; a = lower id = 1, who won -> y=1
    assert rec.diff[0] == pytest.approx(0.0)
    assert rec.y[0] == 1
    # second match: player 1 (a) is now higher-rated (won match 1) so diff>0, but player 1 lost -> y=0
    assert rec.diff[1] > 0
    assert rec.y[1] == 0


def test_min_matches_gate_suppresses_early_records():
    df = _matches([
        [pd.Timestamp("2024-01-01"), 1, "F", "Hard", 3, 1, "P One", 2, "P Two", "6-4 6-4"],
    ])
    assert len(walk_forward(df, surface_weight=0.7, min_matches=5).y) == 0  # neither player has history yet


def test_evaluate_and_split_by_date():
    rec = Records(
        date=np.array([pd.Timestamp("2021-06-01"), pd.Timestamp("2023-06-01")], dtype=object),
        diff=np.array([100.0, -100.0]),
        y=np.array([1, 0]),
        best_of=np.array([3, 5]),
    )
    report = evaluate(rec, scales={3: 400.0, 5: 400.0})
    assert report["overall"]["n"] == 2
    assert "bo3" in report and "bo5" in report

    from datetime import date

    train, test = split_by_date(rec, date(2022, 12, 31))
    assert len(train.y) == 1 and len(test.y) == 1
    assert train.y[0] == 1 and test.y[0] == 0
