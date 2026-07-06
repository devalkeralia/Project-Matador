from pathlib import Path

import pandas as pd
import pytest

from matador.sackmann import load_matches

FIXTURE_DATA_DIR = Path(__file__).parent / "fixtures" / "sackmann_data"


def test_load_matches_reads_the_requested_year():
    matches = load_matches("atp", FIXTURE_DATA_DIR, years=[2024])

    assert len(matches) == 3
    assert set(matches["winner_name"]) == {"Jannik Sinner", "Carlos Alcaraz"}


def test_load_matches_parses_tourney_date_as_datetime():
    matches = load_matches("atp", FIXTURE_DATA_DIR, years=[2024])

    assert pd.api.types.is_datetime64_any_dtype(matches["tourney_date"])
    assert matches.loc[0, "tourney_date"] == pd.Timestamp("2024-07-01")


def test_load_matches_without_years_loads_all_available_files():
    matches = load_matches("atp", FIXTURE_DATA_DIR)

    assert len(matches) == 3  # only one fixture year exists, but the glob path works


def test_load_matches_raises_for_missing_requested_year():
    with pytest.raises(FileNotFoundError):
        load_matches("atp", FIXTURE_DATA_DIR, years=[1999])


def test_load_matches_raises_when_tour_directory_has_no_files(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_matches("wta", tmp_path)


def test_load_matches_preserves_core_columns():
    matches = load_matches("atp", FIXTURE_DATA_DIR, years=[2024])

    for column in ("surface", "winner_name", "loser_name", "best_of", "round", "winner_rank", "loser_rank"):
        assert column in matches.columns
