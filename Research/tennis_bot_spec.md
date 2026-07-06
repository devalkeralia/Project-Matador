# Tennis Betting Bot — Edge Detection Spec & Handoff

**Purpose of this document:** a self-contained brief to feed into a Claude Code session. It defines what "edge" means for a tennis match-winner bot, the math to compute it, a tested reference module, the bot's decision loop, and — critically — how to validate that any signal is *real* before trusting it. Read the validation section (§9) as seriously as the math; a bot that fires "opportunities" it can't prove via closing-line value is a loss generator with good UX.

---

## 1. Goal & scope

Build a bot that, for each upcoming match, computes a model win probability, compares it to the market, and **notifies you when a buy has positive expected value at a price you can actually take**. Scope is the **match-winner (two-way) market** — the cleanest case (no draw, literally YES/NO). Everything generalizes to set/game markets later because the Markov layer already produces those distributions.

Non-goals for v1: in-play/live modeling, multi-outcome (futures) arbitrage, automated order placement. Notify-only first; automate execution only after CLV is proven.

---

## 2. What "edge" means here (read this first)

The edge number itself is trivial arithmetic:

```
edge = p_model - p_fair
```

where `p_fair` is the market's **vig-stripped** probability from your sharpest reference book. The entire difficulty is (1) producing a *calibrated* `p_model` and (2) proving it via **closing-line value (CLV)**. Two distinct profit sources, keep them separate in the code:

- **Model edge:** `p_model > p_fair` — your model thinks the true probability exceeds even the sharp de-vigged line. Rare on liquid matches; exists in soft lower-tier markets.
- **Price overlay:** even if `p_model ≈ p_fair`, a book offering odds *better than the fair price* is +EV. This is line-shopping, and it's the bulk of realistically capturable value.

The bot's fire gate is **EV at the best available price** (what you actually earn). Edge-vs-fair is an optional conviction filter on top.

**North-star metric: CLV.** The closing line is the most efficient price in the market and is consistently hard to beat. If your bets don't, on average, get prices better than the closing (Pinnacle) line, you have no edge — regardless of what backtested ROI says. Build CLV logging in from day one.

---

## 3. Pipeline architecture

```
serve inputs (spw, rpw, per surface, trailing window)   [from Sackmann data]
        │  Barnett–Clarke opponent adjustment
        ▼
p_serve_A, p_serve_B   (point-win-on-serve for each player)
        │  closed-form Markov: point → game → tiebreak → set
        ▼
p_set  →  match_win_prob(best_of)   =  p_model
        │
        │   live odds feed  →  devig_shin(sharpest two-way line)  =  p_fair
        ▼
scan_side():  best available price → EV, edge, fractional Kelly → FIRE / hold
        │
        ▼
notify + LOG (price taken, timestamp)  →  later join with closing line → CLV report
```

---

## 4. The math

### 4.1 Serve point-win probability (Barnett–Clarke combination)

For player *i* serving against returner *j*:

```
p_serve_i = spw_i − rpw_j + (1 − avg_spw)
```

- `spw_i` = i's fraction of **service** points won (surface-specific, trailing window)
- `rpw_j` = j's fraction of **return** points won
- `avg_spw` = tour/surface average service-points-won (~0.64 ATP hard)

Sanity: if both players are league-average it collapses to `avg_spw`. If the opponent returns better than average, `p_serve_i` drops. Clamp to (0,1).

### 4.2 Game hold probability

Closed form for the server winning a game from 0–0, with `q = 1−p`:

```
P_game(p) = p⁴(1 + 4q + 10q²) + 20 p³q³ · p²/(p²+q²)
```

The first term is winning to 0/15/30; the second is reaching deuce (20 ways) then winning the deuce subgame with probability `p²/(p²+q²)`. Check: `P_game(0.5) = 0.5`.

### 4.3 Tiebreak (7-point, win by 2)

No clean closed form worth trusting — use exact recursion over `(a_points, b_points)` with the alternating-serve pattern (first server serves 1 point, then serve alternates every 2 points). Memoized; converges fast.

### 4.4 Set (first to 6, win by 2, tiebreak at 6–6)

Recursion over `(a_games, b_games)`, serve alternating each game, using hold probabilities `P_game(p_serve_A)` and `P_game(p_serve_B)`. At 6–6, call the tiebreak with the correct first server (the player due to serve game 13).

### 4.5 Match

`p_set` averaged over which player opens the set (cross-set serve-threading is second-order). Then a race to `⌈best_of/2⌉` sets treated as i.i.d. Bernoulli(`p_set`):

```
P(A wins) = Σ_{b=0}^{need−1}  C(need−1+b, b) · p_set^need · (1−p_set)^b
```

### 4.6 De-vig → p_fair

- **Multiplicative (baseline):** `p_fair_A = (1/odds_A) / (1/odds_A + 1/odds_B)`. Biased for heavy favorites.
- **Shin (use this):** models a fraction *z* of insider money; corrects favorite–longshot bias. Solve *z* by bisection so fair probs sum to 1. Tennis is full of heavy favorites, so the correction matters.

Use your **sharpest** book (Pinnacle) for `p_fair`. Bet into the **outlier/soft** book or exchange.

### 4.7 Edge, EV, and staking

```
edge         = p_model − p_fair
EV_per_unit  = p_model · odds_offered − 1
kelly_full   = (b·p_model − (1−p_model)) / b ,   b = odds_offered − 1
stake        = kelly_full · kelly_fraction        (use 0.25; full Kelly on a
                                                   noisy p over-bets and ruins)
```

---

## 5. Data sources (verified)

| Purpose | Source |
|---|---|
| Results, rankings, surface, tourney level (backbone) | `github.com/JeffSackmann/tennis_atp`, `tennis_wta` (CC BY-NC-SA) |
| Sequential point data (Markov calibration) | `github.com/JeffSackmann/tennis_pointbypoint` |
| Shot-level detail | `github.com/JeffSackmann/tennis_MatchChartingProject` |
| Historical odds incl. opening & closing (CLV ground truth) | `tennis-data.co.uk` (has Pinnacle `PSW/PSL` and avg `AvgW/AvgL` columns); OddsPortal/oddsbase archives (open + close) |
| Reference model implementations | `github.com/jgollub1/tennis_match_prediction` (JSA 2021: Elo + serve/return), `github.com/edouardthom/ATPBetting` |
| Live tradeable venue | Kalshi API (order book + Python client; tennis `kxatpmatch` series; CFTC-regulated, legal in CA) |

Note the Sackmann license is **non-commercial** — fine for a personal bot; relevant only if you ever productize.

---

## 6. The reference module (`tennis_edge.py`)

Pure standard library, tested. `serve_probs()` → `match_win_prob()` gives `p_model`; `devig_shin()` gives `p_fair`; `scan_side()` is the bot's fire/no-fire decision core. Full source:

```python
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
```

### Verified demo output

```
serve point-win:  A=0.6370  B=0.5880
hold prob:        A=0.8073  B=0.7102
set prob (A):     0.6621
match prob (A):   Bo3=0.7346   Bo5=0.7833
p_fair (mult):    0.7341
p_fair (Shin):    0.7380
[FIRE] Player A @ 1.400 (softbook)  p_model=0.735 p_fair=0.738 edge=-0.003 EV=+0.028 stake=0.018
```

Note the demo is deliberately realistic: the model (0.735) lands on the sharp fair price (0.738), so `edge ≈ 0` (that's what calibration looks like), and the +2.8% EV comes purely from a soft book pricing A at 1.40 vs the 1.34 fair. Bo5 > Bo3 for the favorite, as it should be.

---

## 7. Bot decision loop (wire-up)

Per match, on each polling tick:

1. **Build `p_model`** — pull each player's `spw`/`rpw` (trailing ~12 months, surface-specific) from the Sackmann-derived store, run `serve_probs()` → `match_win_prob(best_of)`.
2. **Get `p_fair`** — fetch the two-way line from your sharpest book (Pinnacle), `devig_shin()`.
3. **Collect offered prices** — `{book: decimal_odds}` for each side across every book/exchange you can bet.
4. **Scan** — `scan_side(side, p_model_side, ref_a, ref_b, offered_prices, ...)` for each side; if `opp.fire`, notify with the full `Opportunity` payload (side, book, price, EV, Kelly stake).
5. **Log every fire** — persist `(match_id, side, book, price_taken, timestamp, p_model, p_fair)`. After the match, join with the closing line to compute CLV. **This log is the product.**

Thresholds: start conservative — `min_ev = 0.03`–`0.05` to absorb fees/slippage/model error, `kelly_fraction = 0.25`. On Kalshi, remember the fee is charged on expected earnings (not baked into the line), so subtract it explicitly before the EV gate.

---

## 8. The line-timing hypothesis (early favorite → settles)

Observation to test: favorites are sometimes cheaper hours early and the price shortens toward the close. If real and systematic, this **is** CLV capture — the single thing that signals genuine edge. But it must be verified, not assumed:

1. **Systematic, not memory.** You'll remember favorites that shortened and forget those that drifted out (favorites lengthen too, e.g. on injury-treatment reports). Backtest the average over a large sample, per tier.
2. **Beat *fair*, not the raw close.** Some "settling" is the book tightening vig from a wide opener, not the true probability moving. Compare **de-vigged** open vs **de-vigged** close (`devig_shin` on both), else you credit yourself for juice.
3. **Not just risk premium.** Betting early carries withdrawal/retirement/late-news risk and locks capital longer; part of the early premium may be compensation for that.

**Test procedure (offline, before any live use):** from tennis-data.co.uk, for each match compute `p_open = devig_shin(open_odds)` and `p_close = devig_shin(close_odds)`. For the favorite side, measure the average `(1/close_price − 1/open_price)`-style CLV of taking the open price. Slice by tier (ATP main vs Challenger/ITF), by favorite strength, and by whether a sharp book moved first. A consistently positive de-vigged CLV in a definable subset = real, if small and capacity-limited.

**Sharper variant ("top-down"):** instead of blindly buying favorites early, wait for a market-making book (Pinnacle) to move, then hit slower books before they catch up. Higher-confidence CLV than blind early buying. Best of all: only buy early where `p_model` already says the opener is soft (`p_model > p_open`), so model + timing + CLV all reinforce.

---

## 9. Validation methodology (do not skip)

1. **Calibration first.** Reliability curve + Brier score + log-loss on out-of-sample matches. If "60%" predictions don't hit ~60%, the edge number is fiction. Fix calibration before anything else.
2. **CLV is the acceptance test.** Backtest on closing-line value, not P&L (P&L is far too noisy and survivorship-prone to trust on realistic samples). Positive average CLV over a few hundred+ bets ≈ real edge; P&L follows.
3. **Walk-forward only.** Ratings/features strictly as-of match date. The classic killer is leaking post-match info (e.g. computing "trailing" stats that include the match itself).
4. **Sample size.** Hundreds of bets before CLV is statistically meaningful. Don't scale stakes on 20 matches of good luck.
5. **Backtest gate before live:** the bot may not place (or notify for) real bets until it has demonstrated positive de-vigged CLV on held-out historical data.

---

## 10. Practical constraints & venue notes

- **Efficiency gradient.** Top ATP/WTA main-draw match-winner markets are efficient — you won't beat Pinnacle there. The edge lives in **Challenger/ITF**, women's lower tiers, and stale/soft books. Weight the bot's attention accordingly.
- **Gubbing.** Traditional books limit/close accounts that arb or consistently beat them. This caps the soft-book path — it's not a latency race you win with code, it's adversarial detection where winning gets you banned.
- **Exchanges don't gub.** On Kalshi/Betfair you trade against other users, so being sharp is fine — the binding constraint becomes **liquidity** on thin lower-tier markets, plus Kalshi's earnings-based fee.
- **Early limits are low.** Even a real early-price edge is capacity-constrained; you can't get size down hours out.

---

## 11. Implementation checklist for the Claude Code session

- [ ] Ingest Sackmann `tennis_atp`/`tennis_wta` + point-by-point into a local store; compute per-player, per-surface, trailing-window `spw`/`rpw` with **as-of-date** correctness (no leakage).
- [ ] Wire `tennis_edge.py` as the pricing/decision core (it's dependency-free).
- [ ] Odds feed: sharpest book for `p_fair`, plus a multi-book collector for `offered_prices`. Store opening + closing lines for CLV.
- [ ] Implement the CLV logger and a nightly job joining fires to closing lines.
- [ ] Backtest harness: calibration report (Brier/log-loss/reliability) + de-vigged CLV, walk-forward, sliced by tier.
- [ ] Run the §8 open-vs-close test to accept/reject the early-favorite hypothesis before enabling it as a signal.
- [ ] Only after positive held-out CLV: enable live notifications, `min_ev ≥ 0.03`, quarter-Kelly.
- [ ] (Later) Kalshi API integration for a venue that tolerates sharp action; subtract its fee in the EV gate.

**One-line acceptance criterion:** the bot ships when its fired opportunities show *positive average de-vigged CLV on out-of-sample data* — not when the backtested ROI looks good.
