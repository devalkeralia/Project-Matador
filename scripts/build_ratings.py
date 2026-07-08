"""Build the v1 Elo model artifact (data/model.json) the bot loads at predict time.

Builds ATP and WTA INDEPENDENTLY -- separate ratings, name index, and fitted per-format
logistic scales per tour -- so the two tours never share an Elo or a name bucket. Needs
match data under data/tennis_{atp,wta}/ (see matador/sackmann.py for the sources). Run:

    .venv/bin/python scripts/build_ratings.py   ->   data/model.json  (gitignored)

Each tour's section holds current ratings for players with >= min_matches history, a
name-resolution index (canonical_key -> ids) filtered to those same rated players, and the
fitted Bo3/Bo5 scales. Load it back with matador.model.artifact.Model.from_artifact.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from matador.config import load_config  # noqa: E402
from matador.model.calibration import fit_scale, walk_forward  # noqa: E402
from matador.model.elo import SURFACES, KFactor, build_name_index, build_ratings  # noqa: E402
from matador.sackmann import load_matches  # noqa: E402

DATA_DIR = "data"


def _build_tour(tour: str, cfg, k: KFactor) -> dict | None:
    e = cfg.elo
    try:
        matches = load_matches(tour, DATA_DIR)
    except FileNotFoundError as exc:
        print(f"[warn] {tour}: {exc}")
        return None

    book = build_ratings(matches, initial=e.initial_rating, k=k)
    index = build_name_index(matches)

    records = walk_forward(matches, surface_weight=e.surface_weight, min_matches=cfg.min_matches, initial=e.initial_rating, k=k, shrinkage_n0=e.shrinkage_n0)
    scales: dict[int, float] = {}
    for bo in (3, 5):
        m = records.best_of == bo
        scales[bo] = fit_scale(records.diff[m], records.y[m]) if m.sum() > 200 else 400.0

    # Ratings for players with enough history; the name index is filtered to those same ids
    # so a resolved name always maps to a rated player (no unrated-pid surprises at predict).
    players: dict[str, dict] = {}
    for bucket in index.values():
        for pid, info in bucket.items():
            if pid in players or book.overall_count(pid) < cfg.min_matches:
                continue
            players[pid] = {
                "name": info.name,
                "overall": round(book.overall_rating(pid), 2),
                "overall_n": book.overall_count(pid),
                "surface": {s: round(book.surface_rating(pid, s), 2) for s in SURFACES if book.surface_count(pid, s) > 0},
                "last_date": info.last_date.isoformat() if info.last_date else None,
            }

    name_index: dict[str, dict] = {}
    for key, bucket in index.items():
        entries = {
            pid: {"name": i.name, "last_date": i.last_date.isoformat() if i.last_date else None}
            for pid, i in bucket.items() if pid in players
        }
        if entries:
            name_index[key] = entries

    print(f"  [{tour}] {len(matches):,} matches -> {len(players):,} rated players; scales Bo3={scales[3]:.0f} Bo5={scales[5]:.0f}")
    return {"scales": {str(bo): scale for bo, scale in scales.items()}, "players": players, "name_index": name_index}


def main() -> None:
    cfg = load_config()
    e = cfg.elo
    k = KFactor(e.k_num, e.k_shift, e.k_pow)

    tours: dict[str, dict] = {}
    for tour in ("atp", "wta"):
        section = _build_tour(tour, cfg, k)
        if section is not None:
            tours[tour] = section
    if not tours:
        print(f"No match data under {DATA_DIR}/tennis_*/. See matador/sackmann.py for sources.")
        return

    out = Path(cfg.model_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "initial_rating": e.initial_rating,
        "surface_weight": e.surface_weight,
        "shrinkage_n0": e.shrinkage_n0,
        "min_matches": cfg.min_matches,
        "tours": tours,
    }, indent=2))
    summary = ", ".join(f"{t}={len(s['players']):,}" for t, s in tours.items())
    print(f"wrote {out}  ({summary} rated players)")


if __name__ == "__main__":
    main()
