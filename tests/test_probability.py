from datetime import date

import pytest

from matador.model.elo import PlayerInfo, RatingBook
from matador.model.probability import (
    blended_rating,
    prob_from_diff,
    resolve_player,
    win_probability,
)


def test_prob_from_diff_basics():
    assert prob_from_diff(0.0, 400.0) == pytest.approx(0.5)
    assert prob_from_diff(200.0, 400.0) > 0.5
    assert prob_from_diff(-200.0, 400.0) < 0.5


def test_smaller_scale_favors_the_favorite_more():
    # This is how the per-format scale encodes Bo5: a steeper (smaller) scale pushes the
    # favorite's probability higher for the SAME rating gap.
    diff = 150.0
    assert prob_from_diff(diff, 300.0) > prob_from_diff(diff, 500.0) > 0.5


def test_blended_rating_weights_surface_and_overall():
    book = RatingBook()
    book._overall[1] = 1600.0
    book._surface[(1, "Clay")] = 1800.0
    assert blended_rating(book, 1, "Clay", 0.7) == pytest.approx(0.7 * 1800 + 0.3 * 1600)
    # unknown surface -> overall only
    assert blended_rating(book, 1, None, 0.7) == pytest.approx(1600.0)


def _seasoned_book(matches: int = 25) -> RatingBook:
    """A book where players 1 and 2 each have >= `matches` prior matches."""
    book = RatingBook()
    for i in range(matches):
        book.update(1, 999_000 + i, "Hard", date(2024, 1, 1))  # player 1 wins a lot
        book.update(2, 998_000 + i, "Hard", date(2024, 1, 1))
    return book


def test_win_probability_symmetry_and_ok():
    book = _seasoned_book()
    scales = {3: 400.0, 5: 400.0}
    a = win_probability(book, 1, 2, "Hard", 3, surface_weight=0.7, scales=scales, min_matches=20)
    b = win_probability(book, 2, 1, "Hard", 3, surface_weight=0.7, scales=scales, min_matches=20)
    assert a.ok and b.ok
    assert a.p + b.p == pytest.approx(1.0)


def test_win_probability_abstains_on_insufficient_history():
    book = _seasoned_book(matches=5)  # only 5 prior matches each
    r = win_probability(book, 1, 2, "Hard", 3, surface_weight=0.7, scales={3: 400.0, 5: 400.0}, min_matches=20)
    assert r.p is None and "insufficient_history" in r.reason


def test_win_probability_abstains_on_unknown_format():
    book = _seasoned_book()
    r = win_probability(book, 1, 2, "Hard", 1, surface_weight=0.7, scales={3: 400.0, 5: 400.0}, min_matches=20)
    assert r.p is None and "unknown_format" in r.reason


def test_win_probability_abstains_on_stale_ratings():
    book = _seasoned_book()  # last played 2024-01-01
    r = win_probability(
        book, 1, 2, "Hard", 3, surface_weight=0.7, scales={3: 400.0, 5: 400.0},
        min_matches=20, max_staleness_days=30, as_of=date(2024, 6, 1),
    )
    assert r.p is None and r.reason == "stale_ratings"


def test_resolve_player():
    index = {
        "sinner_j": {100: PlayerInfo(100, "Jannik Sinner", date(2024, 6, 1), 40)},
        "williams_s": {
            1: PlayerInfo(1, "Serena Williams", date(2010, 6, 1), 50),
            2: PlayerInfo(2, "Steve Williams", date(2024, 6, 1), 30),
        },
    }
    assert resolve_player(index, "Jannik Sinner") == 100
    assert resolve_player(index, "Nobody Here") is None
    # ambiguous key without a date -> abstain
    assert resolve_player(index, "S Williams") is None
    # with a date, pick the id whose activity is nearest
    assert resolve_player(index, "S Williams", event_date=date(2024, 5, 1)) == 2


def test_win_probability_ok_with_fresh_ratings():
    book = _seasoned_book()  # last played 2024-01-01
    r = win_probability(
        book, 1, 2, "Hard", 3, surface_weight=0.7, scales={3: 400.0, 5: 400.0},
        min_matches=20, max_staleness_days=365, as_of=date(2024, 2, 1),
    )
    assert r.ok  # within the staleness window -> not abstained


def test_win_probability_requires_as_of_when_staleness_set():
    # Fail closed: a staleness limit with no as_of would silently skip the gate.
    book = _seasoned_book()
    with pytest.raises(ValueError):
        win_probability(
            book, 1, 2, "Hard", 3, surface_weight=0.7, scales={3: 400.0, 5: 400.0},
            min_matches=20, max_staleness_days=365,
        )


def test_shrinkage_pulls_thin_ratings_toward_the_mean():
    book = RatingBook()  # initial 1500
    book._overall["thin"], book._overall_n["thin"] = 1900.0, 10   # +400 dev, few matches
    book._overall["vet"], book._overall_n["vet"] = 1900.0, 190    # +400 dev, established
    assert blended_rating(book, "thin", None, 0.3) == 1900.0                      # off by default
    assert blended_rating(book, "thin", None, 0.3, shrinkage_n0=10) == pytest.approx(1700.0)  # keeps 10/20 of +400
    # the established player is barely touched (190/200 of the deviation kept)
    assert blended_rating(book, "vet", None, 0.3, shrinkage_n0=10) == pytest.approx(1500 + (190 / 200) * 400)


def test_shrinkage_tempers_a_thin_favorites_probability():
    book = RatingBook()
    book._overall["fav"], book._overall_n["fav"] = 1800.0, 10     # thin favorite
    book._overall["dog"], book._overall_n["dog"] = 1500.0, 200    # established, average
    scales = {3: 400.0, 5: 400.0}
    raw = win_probability(book, "fav", "dog", None, 3, surface_weight=0.3, scales=scales, min_matches=5)
    shrunk = win_probability(book, "fav", "dog", None, 3, surface_weight=0.3, scales=scales, min_matches=5, shrinkage_n0=10)
    assert raw.ok and shrunk.ok
    assert 0.5 < shrunk.p < raw.p  # shrinkage pulls the thin favorite toward 0.5 but keeps them favored
