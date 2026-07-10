from matador.tournament import tournament_context


def test_slam_surface_and_best_of():
    assert tournament_context("Wimbledon Men Singles", "atp") == ("Grass", 5)
    assert tournament_context("Wimbledon Women Singles", "wta") == ("Grass", 3)
    assert tournament_context("Roland Garros Men Singles", "atp") == ("Clay", 5)
    assert tournament_context("French Open Women Singles", "wta") == ("Clay", 3)
    assert tournament_context("US Open Men Singles", "atp") == ("Hard", 5)
    assert tournament_context("Australian Open Men Singles", "ATP") == ("Hard", 5)  # tour case-insensitive


def test_non_slam_and_unknown_fall_back_to_bo3_overall():
    assert tournament_context("Cincinnati Masters Men Singles", "atp") == (None, 3)
    assert tournament_context("Wuhan Women Singles", "wta") == (None, 3)
    assert tournament_context(None, "atp") == (None, 3)
    assert tournament_context("", "wta") == (None, 3)
