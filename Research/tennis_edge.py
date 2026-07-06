"""
tennis_edge.py
==============
Computational spine for tennis match-winner edge.

Pipeline:
    serve_win_prob   (Barnett-Clarke opponent adjustment)
        -> game_win_prob / tiebreak_win_prob / set_win_prob   (closed-form Markov)
        -> match_win_prob                                     (p_model)
    devig_multiplicative / devig_shin  -> p_fair (from a reference market)
    edge_and_stake    -> edge = p_model - p_fair, EV, fractional Kelly

Pure standard library. No external deps. All probabilities are for "player A".

Feed the serve/return inputs from Sackmann data (tennis_atp / tennis_wta):
    spw = service points won / service points played
    rpw = return  points won / return  points played
computed on a trailing window (e.g. last 12 months) and, ideally, per surface.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache
from math import comb

_EPS = 1e-9


# --------------------------------------------------------------------------- #
# 1. Serve point-win probability  (Barnett-Clarke combination)
# --------------------------------------------------------------------------- #
def serve_win_prob(spw_i: float, rpw_j: float, avg_spw: float) -> float:
    """
    Probability player i wins a point ON SERVE against returner j.

        p = spw_i - rpw_j + (1 - avg_spw)

    Intuition: start from i's own service-points-won rate, then adjust for how
    much better/worse than tour-average the opponent returns.  If both players
    are league-average the result collapses back to avg_spw.

    spw_i    : i's fraction of service points won   (surface-specific)
    rpw_j    : j's fraction of return  points won   (surface-specific)
    avg_spw  : tour/surface average service-points-won (~0.64 ATP hard)
    """
    p = spw_i - rpw_j + (1.0 - avg_spw)
    return min(max(p, _EPS), 1.0 - _EPS)


def serve_probs(a_spw, a_rpw, b_spw, b_rpw, avg_spw):
    """Convenience: return (pa, pb) = point-win-on-serve for A and for B."""
    pa = serve_win_prob(a_spw, b_rpw, avg_spw)   # A serving, B returning
    pb = serve_win_prob(b_spw, a_rpw, avg_spw)   # B serving, A returning
    return pa, pb


# --------------------------------------------------------------------------- #
# 2. Game hold probability  (server wins a game from 0-0)
# --------------------------------------------------------------------------- #
def game_win_prob(p: float) -> float:
    """P(server wins a service game | point-win prob p)."""
    q = 1.0 - p
    base = p**4 * (1 + 4 * q + 10 * q * q)          # win to 0 / 15 / 30
    denom = p * p + q * q
    deuce = 20 * p**3 * q**3 * (p * p / denom) if denom else 0.0
    return base + deuce


# --------------------------------------------------------------------------- #
# 3. Tiebreak win probability  (7-point, win by 2)
# --------------------------------------------------------------------------- #
def tiebreak_win_prob(pa: float, pb: float, a_serves_first: bool = True) -> float:
    """
    P(A wins a 7-point tiebreak).
        pa = P(A wins a point on A's serve)
        pb = P(B wins a point on B's serve)
    Service order: first server serves 1 point, then serve alternates every 2.
    """
    def first_serves(n: int) -> bool:              # does the FIRST server serve point n (0-based)?
        if n == 0:
            return True
        return ((n - 1) // 2) % 2 == 1

    @lru_cache(maxsize=None)
    def P(a: int, b: int) -> float:
        if a >= 7 and a - b >= 2:
            return 1.0
        if b >= 7 and b - a >= 2:
            return 0.0
        if a + b > 60:                             # numerical guard on the win-by-2 tail
            return 1.0 if a > b else 0.0
        a_on_serve = first_serves(a + b) == a_serves_first
        p_a_pt = pa if a_on_serve else (1.0 - pb)  # prob A wins THIS point
        return p_a_pt * P(a + 1, b) + (1.0 - p_a_pt) * P(a, b + 1)

    return P(0, 0)


# --------------------------------------------------------------------------- #
# 4. Set win probability  (first to 6, win by 2, tiebreak at 6-6)
# --------------------------------------------------------------------------- #
def set_win_prob(pa: float, pb: float, a_serves_first: bool = True) -> float:
    """P(A wins a standard advantage-tiebreak set). Serve alternates each game."""
    ga = game_win_prob(pa)                          # A holds
    gb = game_win_prob(pb)                          # B holds

    @lru_cache(maxsize=None)
    def S(a: int, b: int) -> float:
        if a >= 6 and a - b >= 2:
            return 1.0
        if b >= 6 and b - a >= 2:
            return 0.0
        if a == 6 and b == 6:                       # game 13 -> first server serves TB point 1
            return tiebreak_win_prob(pa, pb, a_serves_first=a_serves_first)
        a_serves = ((a + b) % 2 == 0) == a_serves_first
        win_game = ga if a_serves else (1.0 - gb)   # prob A wins this game
        return win_game * S(a + 1, b) + (1.0 - win_game) * S(a, b + 1)

    return S(0, 0)


# --------------------------------------------------------------------------- #
# 5. Match win probability
# --------------------------------------------------------------------------- #
def match_win_prob(pa: float, pb: float, best_of: int = 3) -> float:
    """
    P(A wins the match).

    Sets are treated as i.i.d. Bernoulli(p_set), with p_set averaged over which
    player serves the opening game of the set. The cross-set serve-threading
    effect is second-order and well inside model error; drop the averaging and
    thread it exactly only if you later find it matters for your markets.
    """
    p_set = 0.5 * (set_win_prob(pa, pb, True) + set_win_prob(pa, pb, False))
    need = best_of // 2 + 1                          # sets required to win
    # race to `need`: A wins, conceding b_sets along the way
    return sum(
        comb(need - 1 + b, b) * p_set**need * (1.0 - p_set)**b
        for b in range(need)
    )


# --------------------------------------------------------------------------- #
# 6. De-vig a two-way market  ->  p_fair
# --------------------------------------------------------------------------- #
def devig_multiplicative(odds_a: float, odds_b: float):
    """Proportional de-vig. Baseline; biased for heavy favorites."""
    ia, ib = 1.0 / odds_a, 1.0 / odds_b
    s = ia + ib
    return ia / s, ib / s


def devig_shin(odds_a: float, odds_b: float):
    """
    Shin (1993) de-vig: models a fraction z of insider money, which corrects
    favorite-longshot bias better than the proportional method. Solves for z
    by bisection so the fair probabilities sum to 1.
    """
    ia, ib = 1.0 / odds_a, 1.0 / odds_b
    s = ia + ib                                     # overround (> 1)

    def probs(z):
        def pi(x):
            return (math.sqrt(z * z + 4 * (1 - z) * x * x / s) - z) / (2 * (1 - z))
        return pi(ia), pi(ib)

    lo, hi = 0.0, 1.0 - 1e-9
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        pa, pb = probs(mid)
        if pa + pb > 1.0:                           # sum decreases in z
            lo = mid
        else:
            hi = mid
    return probs(0.5 * (lo + hi))


# --------------------------------------------------------------------------- #
# 7. Edge + fractional Kelly
# --------------------------------------------------------------------------- #
@dataclass
class EdgeResult:
    p_model: float          # your calibrated probability
    p_fair: float           # de-vigged reference (e.g. Pinnacle) probability
    edge: float             # p_model - p_fair  (calibration edge)
    odds_offered: float     # decimal price you can actually take (the outlier book)
    ev_per_unit: float      # expected profit per unit staked at odds_offered
    kelly_full: float       # full-Kelly fraction of bankroll
    kelly_frac: float       # fractional-Kelly stake (recommended)

    def __str__(self):
        return (
            f"p_model={self.p_model:.4f}  p_fair={self.p_fair:.4f}  "
            f"edge={self.edge:+.4f}\n"
            f"odds_offered={self.odds_offered:.3f}  "
            f"EV/unit={self.ev_per_unit:+.4f}\n"
            f"kelly_full={self.kelly_full:.4f}  "
            f"kelly_frac={self.kelly_frac:.4f}"
        )


def edge_and_stake(p_model, odds_offered, p_fair, kelly_fraction=0.25) -> EdgeResult:
    """
    Compute the edge against a reference fair price and the stake against the
    price you can actually get.

    Key discipline: p_fair should come from the SHARPEST market (Pinnacle) as
    your truth, while odds_offered is the outlier/soft book or exchange you're
    betting into. Bet only when edge and EV are both positive AND clear your
    threshold for model error + fees + slippage.
    """
    b = odds_offered - 1.0
    kelly_full = max(0.0, (b * p_model - (1.0 - p_model)) / b) if b > 0 else 0.0
    return EdgeResult(
        p_model=p_model,
        p_fair=p_fair,
        edge=p_model - p_fair,
        odds_offered=odds_offered,
        ev_per_unit=p_model * odds_offered - 1.0,
        kelly_full=kelly_full,
        kelly_frac=kelly_full * kelly_fraction,
    )


# --------------------------------------------------------------------------- #
# 8. Bot decision core: turn model + live prices into a fire/no-fire signal
# --------------------------------------------------------------------------- #
@dataclass
class Opportunity:
    side: str               # label, e.g. player A's name
    book: str               # book/exchange offering the best price
    p_model: float
    p_fair: float           # de-vigged sharpest reference
    edge: float             # p_model - p_fair
    odds: float             # best available decimal price for this side
    ev_per_unit: float
    kelly_frac: float
    fire: bool              # True -> notify to buy

    def __str__(self):
        tag = "FIRE" if self.fire else "hold"
        return (f"[{tag}] {self.side} @ {self.odds:.3f} ({self.book})  "
                f"p_model={self.p_model:.3f} p_fair={self.p_fair:.3f} "
                f"edge={self.edge:+.3f} EV={self.ev_per_unit:+.3f} "
                f"stake={self.kelly_frac:.3f}")


def scan_side(side, p_model, ref_odds_a, ref_odds_b, offered_prices,
              is_side_a=True, min_ev=0.02, min_edge=None,
              kelly_fraction=0.25, devig=devig_shin) -> Opportunity:
    """
    Decide whether to notify a buy on one side of a match.

    side            : label for the side you're pricing (e.g. "Alcaraz")
    p_model         : your model's win prob for THIS side
    ref_odds_a/b    : the two-way price from your SHARPEST book (-> p_fair)
    offered_prices  : {book_name: decimal_odds} you can actually take for this side
    is_side_a       : True if `side` is player A in the reference line
    min_ev          : EV/unit threshold to fire (covers fees/slippage/model error);
                      this is the PRIMARY gate -- it's what you actually earn
    min_edge        : optional floor on p_model - p_fair. None = fire on EV alone
                      (pure price overlay). Set >0 to also demand model conviction.
    """
    fair_a, fair_b = devig(ref_odds_a, ref_odds_b)
    p_fair = fair_a if is_side_a else fair_b
    book, odds = max(offered_prices.items(), key=lambda kv: kv[1])  # best price
    r = edge_and_stake(p_model, odds, p_fair, kelly_fraction)
    fire = (r.ev_per_unit >= min_ev) and (min_edge is None or r.edge >= min_edge)
    return Opportunity(side, book, p_model, p_fair, r.edge, odds,
                       r.ev_per_unit, r.kelly_frac, fire)


# --------------------------------------------------------------------------- #
# Demo
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    # --- inputs you'd pull/compute from Sackmann data (surface-specific) ---
    AVG_SPW = 0.64                     # ATP hard-court average service pts won

    # Realistic: A only modestly better than B (this is the normal case).
    a = dict(spw=0.655, rpw=0.400)     # player A
    b = dict(spw=0.628, rpw=0.378)     # player B

    pa, pb = serve_probs(a["spw"], a["rpw"], b["spw"], b["rpw"], AVG_SPW)
    print(f"serve point-win:  A={pa:.4f}  B={pb:.4f}")
    print(f"hold prob:        A={game_win_prob(pa):.4f}  B={game_win_prob(pb):.4f}")
    print(f"set prob (A):     {set_win_prob(pa, pb):.4f}")

    p_bo3 = match_win_prob(pa, pb, best_of=3)
    p_bo5 = match_win_prob(pa, pb, best_of=5)
    print(f"match prob (A):   Bo3={p_bo3:.4f}   Bo5={p_bo5:.4f}")

    # --- sharp reference book prices A at 1.34, B at 3.70 (~1.7% vig) ---
    # A well-calibrated model on a liquid match lands NEAR this fair price.
    odds_a_ref, odds_b_ref = 1.34, 3.70          # sharp reference book
    fair_a_mult, _ = devig_multiplicative(odds_a_ref, odds_b_ref)
    fair_a_shin, _ = devig_shin(odds_a_ref, odds_b_ref)
    print(f"\np_fair (mult):    {fair_a_mult:.4f}")
    print(f"p_fair (Shin):    {fair_a_shin:.4f}")

    # --- a soft book / exchange offers A at 1.40 (better than the 1.34 fair) ---
    res = edge_and_stake(p_model=p_bo3, odds_offered=1.40,
                         p_fair=fair_a_shin, kelly_fraction=0.25)
    print("\n--- edge on A @ 1.40 (quarter-Kelly) ---")
    print(res)
    print("\nNote: edge~0 means the model AGREES with the sharp line (good, it's")
    print("calibrated). Positive EV here comes from the soft book's price beating")
    print("fair, not from the model. A large positive `edge` would be the model")
    print("claiming its own edge -- trust it only after CLV confirms it.")

    # --- bot decision core: scan live prices across books for side A ---
    print("\n--- bot scan (side A across books) ---")
    live_prices_a = {"pinnacle": 1.34, "bet365": 1.38, "softbook": 1.40}
    opp = scan_side("Player A", p_bo3, odds_a_ref, odds_b_ref,
                    live_prices_a, is_side_a=True, min_ev=0.02)
    print(opp)
