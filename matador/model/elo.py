"""Surface-weighted match Elo -- the v1 statistical baseline (see DESIGN-DECISIONS.md).

A pure-Python rating engine over Sackmann match history. Ratings update match-by-match
in chronological order, so a match's pre-update ratings depend only on prior matches
(no lookahead). Ratings are keyed by Sackmann player id (winner_id/loser_id) -- stable
and collision-free; the name-resolution join is applied only at predict time.

The serve/return point-by-point Markov model is v2 (reference/tennis_edge.py); do not
build it here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

SURFACES = ("Hard", "Clay", "Grass")

# Sackmann round labels in within-tournament chronological order, so a final is never
# processed before an earlier round of the same event (a small anti-leakage measure).
_ROUND_ORDER = {
    "RR": 0, "BR": 0, "R128": 1, "R64": 2, "R32": 3, "R16": 4,
    "QF": 5, "SF": 6, "F": 7,
}

# Score tokens that mean "no completed match on court" -- excluded from rating updates.
# Retirements (RET) are KEPT: they had an on-court winner.
_WALKOVER_TOKENS = ("W/O", "WALKOVER", "DEF")


def canonical_surface(surface: object) -> str | None:
    """Map a Sackmann surface label to Hard/Clay/Grass (Carpet->Hard); None if unknown/blank."""
    if not isinstance(surface, str):
        return None
    return {"hard": "Hard", "clay": "Clay", "grass": "Grass", "carpet": "Hard"}.get(surface.strip().lower())


@dataclass(frozen=True)
class KFactor:
    """Decaying per-player K-factor K = num/(n+shift)^pow (538/Sackmann): large while a
    player's match count n is small (cold-start), settling as history accrues."""

    num: float = 250.0
    shift: float = 5.0
    pow: float = 0.4

    def __call__(self, n: int) -> float:
        return self.num / (n + self.shift) ** self.pow


def expected_score(rating_a: float, rating_b: float) -> float:
    """Elo expectation P(A beats B) on the 400-point logistic used for rating updates."""
    return 1.0 / (1.0 + 10.0 ** ((rating_b - rating_a) / 400.0))


@dataclass
class PlayerInfo:
    """Roster entry for the name-resolution join (canonical_key -> id)."""

    player_id: str
    name: str
    last_date: date | None
    matches: int


class RatingBook:
    """Mutable overall + per-surface Elo ratings keyed by an opaque string player id."""

    def __init__(self, initial: float = 1500.0, k: KFactor | None = None):
        self.initial = initial
        self.k = k or KFactor()
        self._overall: dict[str, float] = {}
        self._overall_n: dict[str, int] = {}
        self._surface: dict[tuple[str, str], float] = {}
        self._surface_n: dict[tuple[str, str], int] = {}
        self._last_date: dict[str, date] = {}

    def overall_rating(self, pid: str) -> float:
        return self._overall.get(pid, self.initial)

    def overall_count(self, pid: str) -> int:
        return self._overall_n.get(pid, 0)

    def surface_rating(self, pid: str, surface: str) -> float:
        return self._surface.get((pid, surface), self.initial)

    def surface_count(self, pid: str, surface: str) -> int:
        return self._surface_n.get((pid, surface), 0)

    def last_played(self, pid: str) -> date | None:
        return self._last_date.get(pid)

    @classmethod
    def from_artifact(cls, players: dict, initial: float = 1500.0) -> "RatingBook":
        """Rehydrate a predict-time book from a build_ratings.py artifact `players` dict
        ({overall, overall_n, surface{}, last_date} per id). Surface *counts* aren't
        persisted -- they're only needed during the chronological build, not at predict
        time (win_probability reads overall_count, last_played, and the blended rating)."""
        book = cls(initial=initial)
        for pid, rec in players.items():
            book._overall[pid] = float(rec["overall"])
            book._overall_n[pid] = int(rec["overall_n"])
            for surf, rating in rec.get("surface", {}).items():
                book._surface[(pid, surf)] = float(rating)
            last = rec.get("last_date")
            if last:
                book._last_date[pid] = date.fromisoformat(last)
        return book

    def update(self, winner_id: str, loser_id: str, surface: object, match_date: date | None) -> None:
        """Apply one match result to the overall and (if the surface is known) surface ratings."""
        rw, rl = self.overall_rating(winner_id), self.overall_rating(loser_id)
        ew = expected_score(rw, rl)
        self._overall[winner_id] = rw + self.k(self.overall_count(winner_id)) * (1.0 - ew)
        self._overall[loser_id] = rl - self.k(self.overall_count(loser_id)) * (1.0 - ew)
        self._overall_n[winner_id] = self.overall_count(winner_id) + 1
        self._overall_n[loser_id] = self.overall_count(loser_id) + 1

        surf = canonical_surface(surface)
        if surf is not None:
            rws, rls = self.surface_rating(winner_id, surf), self.surface_rating(loser_id, surf)
            ews = expected_score(rws, rls)
            self._surface[(winner_id, surf)] = rws + self.k(self.surface_count(winner_id, surf)) * (1.0 - ews)
            self._surface[(loser_id, surf)] = rls - self.k(self.surface_count(loser_id, surf)) * (1.0 - ews)
            self._surface_n[(winner_id, surf)] = self.surface_count(winner_id, surf) + 1
            self._surface_n[(loser_id, surf)] = self.surface_count(loser_id, surf) + 1

        if match_date is not None:
            self._last_date[winner_id] = match_date
            self._last_date[loser_id] = match_date


def _normalize_id(series: pd.Series) -> pd.Series:
    """Player ids as opaque string keys: numeric ids (Sackmann/Kaggle) become clean integer
    strings ('206173'); already-alphanumeric ids (TML, e.g. 'D875') pass through unchanged."""
    numeric = pd.to_numeric(series, errors="coerce")
    if numeric.notna().all():
        return numeric.astype("int64").astype(str)
    return series.astype(str)


def prepare_matches(matches: pd.DataFrame) -> pd.DataFrame:
    """Return usable, chronologically ordered matches for rating: drop rows missing player
    ids and walkovers, normalize ids to opaque string keys, and sort by
    (tourney_date, round, match_num)."""
    df = matches.dropna(subset=["winner_id", "loser_id"]).copy()
    score = df["score"].astype(str).str.upper()
    df = df[~score.str.contains("|".join(_WALKOVER_TOKENS), na=False, regex=True)]
    df["winner_id"] = _normalize_id(df["winner_id"])
    df["loser_id"] = _normalize_id(df["loser_id"])
    rounds = df["round"] if "round" in df.columns else pd.Series(index=df.index, dtype=object)
    df["_round_order"] = rounds.map(_ROUND_ORDER).fillna(0)
    sort_cols = [c for c in ("tourney_date", "_round_order", "match_num") if c in df.columns]
    return df.sort_values(sort_cols, kind="stable").drop(columns="_round_order").reset_index(drop=True)


def build_ratings(matches: pd.DataFrame, *, initial: float = 1500.0, k: KFactor | None = None) -> RatingBook:
    """Replay prepared matches in chronological order into a RatingBook."""
    book = RatingBook(initial=initial, k=k)
    for row in prepare_matches(matches).itertuples(index=False):
        md = row.tourney_date.date() if hasattr(row.tourney_date, "date") else row.tourney_date
        book.update(row.winner_id, row.loser_id, getattr(row, "surface", None), md)
    return book


def build_name_index(matches: pd.DataFrame) -> dict[str, dict[str, PlayerInfo]]:
    """Map canonical_key(name) -> {player_id: PlayerInfo}. Multiple ids under one key are
    same-surname/initial collisions, disambiguated at resolve time by event date."""
    from matador.names import canonical_key

    index: dict[str, dict[str, PlayerInfo]] = {}
    prepared = prepare_matches(matches)
    for side in ("winner", "loser"):
        for pid, name, raw_date in zip(
            prepared[f"{side}_id"],
            prepared[f"{side}_name"].astype(str),
            prepared["tourney_date"],
        ):
            md = raw_date.date() if hasattr(raw_date, "date") else raw_date
            bucket = index.setdefault(canonical_key(name), {})
            info = bucket.get(pid)
            if info is None:
                bucket[pid] = PlayerInfo(pid, name, md, 1)
            else:
                last = info.last_date if (info.last_date and md and info.last_date >= md) else (md or info.last_date)
                bucket[pid] = PlayerInfo(pid, name, last, info.matches + 1)
    return index
