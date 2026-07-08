import json

import pytest

from matador.model.artifact import Model
from matador.model.elo import RatingBook
from matador.model.probability import win_probability


def _artifact() -> dict:
    return {
        "initial_rating": 1500.0,
        "surface_weight": 0.3,
        "min_matches": 5,
        "tours": {
            "atp": {
                "scales": {"3": 500.0, "5": 400.0},
                "players": {
                    "p_sinner": {"name": "Jannik Sinner", "overall": 1800.0, "overall_n": 50, "surface": {"Hard": 1850.0}, "last_date": "2026-01-01"},
                    "p_alcaraz": {"name": "Carlos Alcaraz", "overall": 1780.0, "overall_n": 60, "surface": {"Clay": 1900.0}, "last_date": "2026-01-01"},
                },
                "name_index": {
                    "sinner_j": {"p_sinner": {"name": "Jannik Sinner", "last_date": "2026-01-01"}},
                    "alcaraz_c": {"p_alcaraz": {"name": "Carlos Alcaraz", "last_date": "2026-01-01"}},
                },
            },
            "wta": {
                "scales": {"3": 460.0, "5": 400.0},
                "players": {
                    "p_swiatek": {"name": "Iga Swiatek", "overall": 1850.0, "overall_n": 70, "surface": {"Clay": 2000.0}, "last_date": "2026-01-01"},
                    "p_gauff": {"name": "Coco Gauff", "overall": 1790.0, "overall_n": 65, "surface": {"Hard": 1820.0}, "last_date": "2026-01-01"},
                },
                "name_index": {
                    "swiatek_i": {"p_swiatek": {"name": "Iga Swiatek", "last_date": "2026-01-01"}},
                    "gauff_c": {"p_gauff": {"name": "Coco Gauff", "last_date": "2026-01-01"}},
                },
            },
        },
    }


def _write(tmp_path) -> Model:
    path = tmp_path / "model.json"
    path.write_text(json.dumps(_artifact()))
    return Model.from_artifact(path)


def test_model_predict_matches_direct_win_probability(tmp_path):
    model = _write(tmp_path)
    r = model.predict("atp", "Jannik Sinner", "Carlos Alcaraz", "Hard", 3)
    assert r.ok

    # The facade must equal computing win_probability on a book rehydrated from the same players.
    book = RatingBook.from_artifact(_artifact()["tours"]["atp"]["players"])
    direct = win_probability(
        book, "p_sinner", "p_alcaraz", "Hard", 3,
        surface_weight=0.3, scales={3: 500.0, 5: 400.0}, min_matches=5,
    )
    assert r.p == pytest.approx(direct.p)


def test_model_resolution_is_tour_scoped(tmp_path):
    model = _write(tmp_path)
    # Swiatek exists only in the WTA index -> an ATP market cannot resolve her.
    assert model.predict("atp", "Iga Swiatek", "Carlos Alcaraz", "Clay", 3).reason == "unresolved_player"
    # ...but a WTA market resolves both women fine.
    assert model.predict("wta", "Iga Swiatek", "Coco Gauff", "Clay", 3).ok


def test_model_unknown_tour_abstains(tmp_path):
    model = _write(tmp_path)
    assert "unknown_tour" in model.predict("mixed", "Jannik Sinner", "Carlos Alcaraz", "Hard", 3).reason


def test_model_applies_shrinkage_from_artifact(tmp_path):
    art = _artifact()
    art["shrinkage_n0"] = 10.0
    art["tours"]["atp"]["players"]["p_sinner"]["overall_n"] = 10  # make the favorite thin
    p1 = tmp_path / "shrunk.json"
    p1.write_text(json.dumps(art))
    shrunk = Model.from_artifact(p1)
    assert shrunk.shrinkage_n0 == 10.0

    p2 = tmp_path / "raw.json"
    p2.write_text(json.dumps(_artifact()))  # no shrinkage_n0 -> defaults 0
    raw = Model.from_artifact(p2)
    assert raw.shrinkage_n0 == 0.0

    # The thin favorite's win prob is tempered toward 0.5 under shrinkage.
    p_shrunk = shrunk.predict("atp", "Jannik Sinner", "Carlos Alcaraz", "Hard", 3).p
    p_raw = raw.predict("atp", "Jannik Sinner", "Carlos Alcaraz", "Hard", 3).p
    assert p_shrunk < p_raw
