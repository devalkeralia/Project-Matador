"""Map a Kalshi event's tournament (product_metadata.competition) to model inputs.

Kalshi tennis events carry a competition string like "Wimbledon Men Singles". The model needs
the court surface (surface-weighted Elo) and best_of (per-format logistic scale). Only the four
Grand Slams need explicit handling: they fix the surface and are the only ATP events played
best-of-5. Every other singles event is best-of-3, and an unknown surface is fine -- the model
falls back to overall Elo when surface is None (see matador.model.probability.blended_rating).
"""
from __future__ import annotations

# Grand Slam name (lowercased substring) -> court surface.
_SLAM_SURFACE = {
    "wimbledon": "Grass",
    "roland garros": "Clay",
    "french open": "Clay",
    "us open": "Hard",
    "australian open": "Hard",
}


def tournament_context(competition: str | None, tour: str) -> tuple[str | None, int]:
    """(surface, best_of) for a Kalshi competition string. Slams fix the surface and make ATP
    best-of-5; every other (non-slam) singles event is best-of-3 with an unknown surface."""
    if competition:
        comp = competition.lower()
        for name, surface in _SLAM_SURFACE.items():
            if name in comp:
                return surface, (5 if tour.lower() == "atp" else 3)
    return None, 3
