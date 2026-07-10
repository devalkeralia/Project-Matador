import pandas as pd
import pytest

from matador.backtest import de_vig, replay_predictions, roi_by_experience, sharpness, tennisdata_key
from matador.names import canonical_key


def test_de_vig_two_way():
    assert de_vig(2.0, 2.0) == pytest.approx(0.5)
    assert de_vig(1.5, 3.0) == pytest.approx(2 / 3)  # iw=.667,il=.333 -> .667


def test_tennisdata_key_matches_canonical_key_incl_hyphen_apostrophe():
    assert tennisdata_key("Sinner J.") == "sinner_j"
    assert tennisdata_key("Auger-Aliassime F.") == "auger_aliassime_f"      # hyphen folded (was dropped)
    assert tennisdata_key("O'Connell C.") == "oconnell_c"                    # apostrophe folded
    assert tennisdata_key("Davidovich Fokina A.") == "davidovich_fokina_a"   # compound surname
    # must equal canonical_key of the full name so the odds join lines up
    assert tennisdata_key("Auger-Aliassime F.") == canonical_key("Felix Auger-Aliassime")
    assert tennisdata_key("Davidovich Fokina A.") == canonical_key("Alejandro Davidovich Fokina")


def test_sharpness_identical_model_and_market():
    p = [0.7, 0.6, 0.8, 0.55]
    s = sharpness(p, p)
    assert s["n"] == 4
    assert s["brier_model"] == pytest.approx(s["brier_market"])
    assert s["disagree_frac"] == 0.0  # never disagree with an identical forecaster


def test_sharpness_rewards_the_better_forecaster():
    # model is perfect (winner prob 1), market is a coin flip
    s = sharpness([1.0, 1.0, 1.0, 1.0], [0.5, 0.5, 0.5, 0.5])
    assert s["brier_model"] == pytest.approx(0.0)
    assert s["brier_market"] == pytest.approx(0.25)
    assert s["blend_w_star"] == pytest.approx(1.0)  # all weight on the model
    assert s["disagree_frac"] == 1.0
    assert s["model_right_on_disagree"] == 1.0 and s["market_right_on_disagree"] == 0.0


def _matches(rows):
    cols = ["tourney_date", "match_num", "round", "surface", "best_of", "winner_id", "winner_name", "loser_id", "loser_name", "score"]
    return pd.DataFrame(rows, columns=cols)


def test_replay_predictions_holdout_and_no_lookahead():
    df = _matches([
        [pd.Timestamp("2024-01-01"), 1, "F", "Hard", 3, 1, "P One", 2, "P Two", "6-4 6-4"],
        [pd.Timestamp("2025-06-01"), 1, "F", "Hard", 3, 1, "P One", 2, "P Two", "6-4 6-4"],
    ])
    scales = {3: 400.0, 5: 400.0}
    # holdout from 2025 -> only the 2025 match is emitted; the 2024 match still trains the ratings
    preds = replay_predictions(df, surface_weight=0.7, scales=scales, min_matches=0, shrinkage_n0=0.0, holdout_from_year=2025)
    assert len(preds) == 1
    p = preds[0]
    assert p.date.year == 2025
    assert p.key_a == "one_p" and p.key_b == "two_p"
    assert p.n_a == 1 and p.n_b == 1          # each played the 2024 match already
    assert p.p_a > 0.5                          # P One won in 2024 -> favored in 2025

    # holdout from 2024 -> both emitted; the first is at even ratings (diff 0 -> p 0.5, no history)
    both = replay_predictions(df, surface_weight=0.7, scales=scales, min_matches=0, shrinkage_n0=0.0, holdout_from_year=2024)
    assert len(both) == 2
    assert both[0].p_a == pytest.approx(0.5) and both[0].n_a == 0


def test_roi_by_experience_segments():
    bets = pd.DataFrame([(30, 1.0), (30, -1.0), (300, 2.0)], columns=["n_bet", "pnl"])
    rows = dict((label, (n, roi, pnl)) for label, n, roi, pnl in roi_by_experience(bets))
    assert rows["overall"] == (3, pytest.approx(2 / 3), pytest.approx(2.0))
    assert rows["thin/breakout <50"] == (2, pytest.approx(0.0), pytest.approx(0.0))
    assert rows["established 200+"] == (1, pytest.approx(2.0), pytest.approx(2.0))
