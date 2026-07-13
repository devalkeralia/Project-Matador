"""Pre-match win probability from the Elo RatingBook.

Blend surface + overall Elo -> a format-calibrated logistic -> P(player wins), with the
model-exists / abstain gate: never turn a provisional or thinly-supported rating into a
real probability (see MASTER-PROMPT.md "Model-exists gate").
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from matador.model.elo import PlayerInfo, RatingBook, canonical_surface


def blended_rating(book: RatingBook, pid: str, surface: object, surface_weight: float, *, shrinkage_n0: float = 0.0) -> float:
    """surface_weight*surface_elo + (1-surface_weight)*overall_elo (overall alone if the
    surface is unknown), then shrunk toward the mean for thin histories: a player with n
    prior matches keeps a fraction n/(n+shrinkage_n0) of their deviation from the initial
    rating (shrinkage_n0=0 disables it).

    Raw Elo over-rates low-sample favorites (measured: ~+8pts overconfident); shrinkage keeps
    a hot newcomer above average but not as extreme as raw Elo claims, so p_model is honest.
    As n grows the shrinkage relaxes, so a genuine breakout earns full credit within ~50-80
    matches while a fluke stays tempered. This CALIBRATES thin players (it does not suppress
    them): a real edge vs the market survives, an overconfidence mirage does not."""
    overall = book.overall_rating(pid)
    surf = canonical_surface(surface)
    # Fall back to overall Elo when the surface is unknown OR the player has NO history on it --
    # otherwise a strong player with no (e.g.) grass matches gets dragged toward the 1500 default
    # by a phantom surface rating (e.g. Wimbledon), understating them purely from missing data.
    if surf is None or not book.has_surface(pid, surf):
        blended = overall
    else:
        blended = surface_weight * book.surface_rating(pid, surf) + (1.0 - surface_weight) * overall
    if shrinkage_n0 > 0:
        n = book.overall_count(pid)
        blended = book.initial + (n / (n + shrinkage_n0)) * (blended - book.initial)
    return blended


def prob_from_diff(diff: float, scale: float) -> float:
    """Logistic P(A wins) from a blended-rating difference (diff = R_a - R_b) at a format
    scale. A smaller scale is a steeper curve -- it favors the favorite more, which is how
    the per-format scale encodes Bo5 (best-of-5 favors the stronger player vs Bo3)."""
    return 1.0 / (1.0 + 10.0 ** (-diff / scale))


@dataclass(frozen=True)
class WinProbability:
    p: float | None   # P(player_a wins), or None when abstaining
    reason: str       # "ok", or the abstain reason
    experience: int | None = None  # min prior-match count of the two players (thin-player flag/segmentation)

    @property
    def ok(self) -> bool:
        return self.p is not None


def win_probability(
    book: RatingBook,
    player_a: str,
    player_b: str,
    surface: object,
    best_of: int,
    *,
    surface_weight: float,
    scales: dict[int, float],
    min_matches: int,
    max_staleness_days: int | None = None,
    as_of: date | None = None,
    shrinkage_n0: float = 0.0,
) -> WinProbability:
    """P(player_a beats player_b). Abstains (p=None) rather than guessing when either
    player has < min_matches prior matches, the format scale is unknown, or the ratings
    are staler than max_staleness_days."""
    na, nb = book.overall_count(player_a), book.overall_count(player_b)
    if na < min_matches or nb < min_matches:
        return WinProbability(None, f"insufficient_history({na},{nb}<{min_matches})")

    scale = scales.get(int(best_of))
    if scale is None:
        return WinProbability(None, f"unknown_format(best_of={best_of})")

    if max_staleness_days is not None:
        # Fail closed: a staleness limit with no as_of would silently skip the gate.
        if as_of is None:
            raise ValueError("as_of is required when max_staleness_days is set")
        for pid in (player_a, player_b):
            last = book.last_played(pid)
            if last is None or (as_of - last).days > max_staleness_days:
                return WinProbability(None, "stale_ratings")

    diff = (
        blended_rating(book, player_a, surface, surface_weight, shrinkage_n0=shrinkage_n0)
        - blended_rating(book, player_b, surface, surface_weight, shrinkage_n0=shrinkage_n0)
    )
    return WinProbability(prob_from_diff(diff, scale), "ok", experience=min(na, nb))


def resolve_player(
    name_index: dict[str, dict[str, PlayerInfo]],
    name: str,
    event_date: date | None = None,  # retained for API stability; no longer used (see below)
) -> str | None:
    """canonical_key(name) -> a single player id, or None (unknown / ambiguous).

    Operates on a SINGLE tour's name index (the Model holds one per tour), so an ATP name
    can never resolve to a WTA player or vice-versa. A key mapping to several ids is a
    same-surname+initial collision (e.g. Xiyu vs Xinyu Wang). Resolve it ONLY by an exact
    normalized full-name match against the candidates' stored names; if the name doesn't pin
    down exactly one, ABSTAIN (None) rather than guess -- a wrong player would feed a confident
    but bogus probability into a real-money-bound bet. (An earlier nearest-last-date tiebreak
    was a guess and is dropped.)
    """
    from matador.names import canonical_key, normalize

    bucket = name_index.get(canonical_key(name))
    if not bucket:
        return None
    if len(bucket) == 1:
        return next(iter(bucket))
    target = normalize(name)
    matches = [pid for pid, info in bucket.items() if normalize(info.name) == target]
    return matches[0] if len(matches) == 1 else None
