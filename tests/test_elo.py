import pandas as pd
import pytest

from matador.model.elo import (
    KFactor,
    RatingBook,
    build_name_index,
    build_ratings,
    canonical_surface,
    expected_score,
    prepare_matches,
)


def test_kfactor_decays_with_match_count():
    k = KFactor()
    assert k(0) == pytest.approx(250.0 / 5.0**0.4)
    assert k(0) > k(20) > k(100)


def test_expected_score_symmetric():
    assert expected_score(1500, 1500) == pytest.approx(0.5)
    assert expected_score(1700, 1300) + expected_score(1300, 1700) == pytest.approx(1.0)
    assert expected_score(1700, 1300) > 0.5


def test_canonical_surface_maps_carpet_to_hard():
    assert canonical_surface("Hard") == "Hard"
    assert canonical_surface("clay") == "Clay"
    assert canonical_surface("Carpet") == "Hard"
    assert canonical_surface("Grass") == "Grass"
    assert canonical_surface(None) is None
    assert canonical_surface("Unknown") is None
    assert canonical_surface(float("nan")) is None


def test_update_from_even_start_moves_symmetrically():
    book = RatingBook()
    book.update(winner_id=1, loser_id=2, surface="Hard", match_date=None)
    delta = KFactor()(0) * 0.5  # even start -> expected 0.5, so delta = K*(1-0.5)
    assert book.overall_rating(1) == pytest.approx(1500 + delta)
    assert book.overall_rating(2) == pytest.approx(1500 - delta)
    assert book.overall_count(1) == 1 and book.overall_count(2) == 1
    # surface ratings updated on their own (Hard) track
    assert book.surface_rating(1, "Hard") == pytest.approx(1500 + delta)
    assert book.surface_count(1, "Hard") == 1


def test_favorite_gains_little_for_expected_win():
    book = RatingBook()
    book._overall[1], book._overall[2] = 1800.0, 1400.0  # heavy favorite id 1
    book.update(winner_id=1, loser_id=2, surface="Hard", match_date=None)
    assert book.overall_rating(1) - 1800.0 < 20.0  # small reward for expected result
    assert book.overall_rating(1) > 1800.0


def test_unknown_surface_leaves_no_surface_rating():
    book = RatingBook()
    book.update(winner_id=1, loser_id=2, surface=None, match_date=None)
    assert book.overall_count(1) == 1
    assert book.surface_count(1, "Hard") == 0  # no surface bucket created


def _matches(rows):
    cols = ["tourney_date", "match_num", "round", "surface", "best_of", "winner_id", "winner_name", "loser_id", "loser_name", "score"]
    return pd.DataFrame(rows, columns=cols)


def test_prepare_matches_drops_walkovers_and_sorts():
    df = _matches([
        [pd.Timestamp("2024-01-08"), 2, "R32", "Hard", 5, 10, "B B", 20, "C C", "6-4 6-4"],
        [pd.Timestamp("2024-01-01"), 1, "R32", "Hard", 5, 1, "A A", 2, "Z Z", "W/O"],       # walkover -> dropped
        [pd.Timestamp("2024-01-08"), 1, "R64", "Hard", 5, 30, "D D", 40, "E E", "7-6 6-2"],
    ])
    out = prepare_matches(df)
    assert len(out) == 2  # walkover removed
    # sorted by date then round (R64 before R32 on the same date), then match_num
    assert list(out["winner_id"]) == ["30", "10"]


def test_build_ratings_and_name_index_roundtrip():
    df = _matches([
        [pd.Timestamp("2024-06-01"), 1, "F", "Clay", 3, 100, "Carlos Alcaraz", 200, "Alex Zverev", "6-3 6-4"],
        [pd.Timestamp("2024-07-01"), 1, "F", "Grass", 5, 100, "Carlos Alcaraz", 300, "Novak Djokovic", "6-2 6-2 7-6"],
    ])
    book = build_ratings(df)
    assert book.overall_count("100") == 2  # Alcaraz played twice
    assert book.overall_rating("100") > 1500  # won both

    index = build_name_index(df)
    assert set(index["alcaraz_c"].keys()) == {"100"}
    assert index["alcaraz_c"]["100"].matches == 2


def test_string_id_path_and_name_index_stay_consistent():
    # The production ATP path uses alphanumeric TML ids ('D875') that must NOT be int-cast;
    # they must survive prepare/build verbatim and be the exact keys in the name index.
    df = _matches([
        [pd.Timestamp("2024-06-01"), 1, "F", "Hard", 3, "D875", "Novak Djokovic", "N409", "Rafael Nadal", "6-3 6-4"],
        [pd.Timestamp("2024-07-01"), 1, "SF", "Grass", 3, "D875", "Novak Djokovic", "F324", "Roger Federer", "7-6 6-4"],
    ])
    book = build_ratings(df)
    assert book.overall_count("D875") == 2  # not coerced to an int/NaN
    index = build_name_index(df)
    assert index["djokovic_n"]["D875"].matches == 2
    assert "N409" in index["nadal_r"] and "F324" in index["federer_r"]


def test_update_asymmetric_pins_both_players_and_tracks():
    book = RatingBook()
    book._overall["w"], book._overall_n["w"] = 1800.0, 100  # favorite, lots of history
    book._overall["l"], book._overall_n["l"] = 1400.0, 0     # newcomer
    book.update("w", "l", "Clay", None)
    ew = expected_score(1800.0, 1400.0)
    # overall: each side moves by its OWN per-player K (loser's K is bigger at n=0)
    assert book.overall_rating("w") == pytest.approx(1800.0 + KFactor()(100) * (1 - ew))
    assert book.overall_rating("l") == pytest.approx(1400.0 - KFactor()(0) * (1 - ew))
    # surface track starts from the initial for both (no prior Clay), K uses surface count 0
    ews = expected_score(1500.0, 1500.0)
    assert book.surface_rating("w", "Clay") == pytest.approx(1500.0 + KFactor()(0) * (1 - ews))
    assert book.surface_rating("l", "Clay") == pytest.approx(1500.0 - KFactor()(0) * (1 - ews))


def test_retirement_kept_but_walkover_and_default_dropped():
    df = _matches([
        [pd.Timestamp("2024-01-01"), 1, "R32", "Hard", 3, 1, "A A", 2, "B B", "6-1 2-1 RET"],  # RET -> kept
        [pd.Timestamp("2024-01-02"), 1, "R32", "Hard", 3, 3, "C C", 4, "D D", "W/O"],           # walkover -> dropped
        [pd.Timestamp("2024-01-03"), 1, "R32", "Hard", 3, 5, "E E", 6, "F F", "6-2 DEF"],       # default -> dropped
    ])
    assert set(prepare_matches(df)["winner_id"]) == {"1"}  # only the retirement survives


def test_ratingbook_from_artifact_round_trip():
    from datetime import date

    players = {"x": {"name": "X", "overall": 1700.0, "overall_n": 42, "surface": {"Clay": 1800.0}, "last_date": "2026-01-05"}}
    book = RatingBook.from_artifact(players)
    assert book.overall_rating("x") == 1700.0
    assert book.overall_count("x") == 42
    assert book.surface_rating("x", "Clay") == 1800.0
    assert book.last_played("x") == date(2026, 1, 5)
    assert book.overall_rating("unknown") == 1500.0  # miss -> initial, no crash
