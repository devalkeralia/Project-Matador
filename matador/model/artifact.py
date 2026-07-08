"""Predict-time model facade over the data/model.json artifact.

scripts/build_ratings.py *writes* the artifact; this loads it back and exposes a single
predict() call that the Phase-3 edge engine and the Phase-6 backtest both import, so the
resolve -> win_probability wiring lives in one place instead of being reassembled by every
caller. The artifact is PER TOUR (separate ratings + name index + fitted scales for ATP and
WTA), so a market's known tour selects its own index -- an ATP name can never resolve to a
WTA player.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from matador.model.elo import PlayerInfo, RatingBook
from matador.model.probability import WinProbability, resolve_player, win_probability


@dataclass
class TourModel:
    """One tour's rehydrated ratings, name index, and fitted per-format scales."""

    book: RatingBook
    name_index: dict[str, dict[str, PlayerInfo]]
    scales: dict[int, float]


class Model:
    def __init__(self, tours: dict[str, TourModel], *, surface_weight: float, min_matches: int, initial: float, shrinkage_n0: float = 0.0):
        self.tours = tours
        self.surface_weight = surface_weight
        self.min_matches = min_matches
        self.initial = initial
        self.shrinkage_n0 = shrinkage_n0

    @classmethod
    def from_artifact(cls, path: str | Path) -> "Model":
        data = json.loads(Path(path).read_text())
        initial = float(data.get("initial_rating", 1500.0))
        tours: dict[str, TourModel] = {}
        for tour, section in data["tours"].items():
            book = RatingBook.from_artifact(section["players"], initial)
            name_index = {
                key: {
                    pid: PlayerInfo(pid, e["name"], date.fromisoformat(e["last_date"]) if e["last_date"] else None, 0)
                    for pid, e in bucket.items()
                }
                for key, bucket in section["name_index"].items()
            }
            scales = {int(bo): float(s) for bo, s in section["scales"].items()}
            tours[tour] = TourModel(book, name_index, scales)
        return cls(tours, surface_weight=float(data["surface_weight"]), min_matches=int(data["min_matches"]), initial=initial, shrinkage_n0=float(data.get("shrinkage_n0", 0.0)))

    def predict(
        self,
        tour: str,
        name_a: str,
        name_b: str,
        surface: object,
        best_of: int,
        *,
        as_of: date | None = None,
        max_staleness_days: int | None = None,
    ) -> WinProbability:
        """P(name_a beats name_b) for a market on `tour`, resolving both names within that
        tour's index. Abstains (p=None) on an unknown tour, an unresolved/ambiguous name,
        or any of win_probability's gates (history / format / staleness)."""
        tm = self.tours.get(tour)
        if tm is None:
            return WinProbability(None, f"unknown_tour({tour})")
        pa = resolve_player(tm.name_index, name_a, as_of)
        pb = resolve_player(tm.name_index, name_b, as_of)
        if pa is None or pb is None:
            return WinProbability(None, "unresolved_player")
        return win_probability(
            tm.book, pa, pb, surface, best_of,
            surface_weight=self.surface_weight, scales=tm.scales, min_matches=self.min_matches,
            max_staleness_days=max_staleness_days, as_of=as_of, shrinkage_n0=self.shrinkage_n0,
        )
