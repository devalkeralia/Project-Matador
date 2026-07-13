"""Net-of-fee edge and 1/4-Kelly sizing for a binary Kalshi contract.

Pure functions (no I/O, no market fetch) so the Phase-3 alert loop and the Phase-6 backtest
size stakes identically -- a live-vs-backtest divergence here would poison every result.

Kalshi mechanics (see RESEARCH-KALSHI.md / DESIGN-DECISIONS.md): a contract costs `price`
dollars (0-1) and pays $1 if it resolves Yes. Backing a player = buy Yes at yes_price;
laying = buy No at no_price. p_model is P(the Yes player wins).

  net_edge = (p - price) - fee_coefficient * price * (1 - price)   # fee peaks near 0.50
  f*       = net_edge / (1 - price)                                 # Kelly on the NET edge
  stake    = min(kelly_fraction * f* * bankroll, max_stake_pct * bankroll)   # cap FIRST
  contracts= floor(stake / price)

Evaluate both sides and take the better actionable one; abstain when neither clears the
min_net_edge / price-band / >=1-contract gates.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class EdgeResult:
    side: str          # 'yes' or 'no'
    price: float       # cost per contract for this side
    p: float           # model win prob for this side
    net_edge: float
    stake: float
    contracts: int


def net_edge(p: float, price: float, fee_coefficient: float) -> float:
    """Per-contract net-of-fee edge of buying at `price` when the true win prob is `p`. Uses the
    linear fee approximation (fee_coefficient*price*(1-price)) as the Kelly-edge input for sizing.
    The net-CLV go-live gate uses this SAME linear per-contract fee (on the entry price); only
    realized P&L (clv.net_pnl) uses the exact round-up fee (kalshi_fee)."""
    return (p - price) - fee_coefficient * price * (1.0 - price)


def kalshi_fee(price: float, contracts: int, fee_coefficient: float) -> float:
    """Kalshi's taker fee for an ORDER of `contracts` at `price`: fee_coefficient*C*P*(1-P),
    rounded UP to the next cent (per order, not per contract). The round-up is a fixed penalty the
    linear per-contract approximation omits -- material for the small orders a personal bankroll
    produces, so REALIZED P&L (clv.net_pnl) uses this. (The net-CLV gate keeps the linear
    per-contract fee -- a per-order round-up can't apply to a size-independent CLV metric.)"""
    # round(...,6) first: bare float noise (e.g. 0.07*100*0.25 -> 1.75000000000000002) would
    # otherwise let ceil spuriously bump a whole cent (1.75 -> 1.76).
    return math.ceil(round(fee_coefficient * contracts * price * (1.0 - price) * 100.0, 6)) / 100.0


def kelly_stake(edge: float, price: float, *, bankroll: float, kelly_fraction: float, max_stake_pct: float) -> tuple[float, int]:
    """Fractional-Kelly stake on the NET edge, capped at max_stake_pct of bankroll BEFORE
    converting to a whole number of contracts."""
    f_star = edge / (1.0 - price)
    stake = min(kelly_fraction * f_star * bankroll, max_stake_pct * bankroll)
    contracts = math.floor(stake / price + 1e-9)  # +eps: plain // under-counts at exact cent prices (100/0.05)
    return stake, contracts


def _evaluate_side(side: str, p: float, price: float | None, cfg, kelly_fraction: float) -> EdgeResult | None:
    """One side, or None if not actionable. `cfg` supplies fee_coefficient, min_net_edge,
    max_price, min_price, bankroll, max_stake_pct; `kelly_fraction` is passed in so the caller can
    apply an extra haircut (e.g. for thin players)."""
    if price is None or not (0.0 < price < 1.0):
        return None
    if price > cfg.max_price:
        return None
    if cfg.min_price is not None and price < cfg.min_price:
        return None
    edge = net_edge(p, price, cfg.fee_coefficient)
    if edge < cfg.min_net_edge:
        return None
    stake, contracts = kelly_stake(
        edge, price, bankroll=cfg.bankroll, kelly_fraction=kelly_fraction, max_stake_pct=cfg.max_stake_pct,
    )
    if contracts < 1:
        return None
    return EdgeResult(side=side, price=price, p=p, net_edge=edge, stake=stake, contracts=contracts)


def evaluate_market(p_model: float, yes_price: float | None, no_price: float | None, cfg, *, kelly_fraction: float | None = None) -> EdgeResult | None:
    """Best actionable side of a binary market, or None to abstain. p_model = P(Yes player
    wins); the No side uses (1 - p_model) against no_price. `kelly_fraction` overrides
    cfg.kelly_fraction (the caller haircuts it for thin/uncertain bets)."""
    kf = cfg.kelly_fraction if kelly_fraction is None else kelly_fraction
    candidates = [
        _evaluate_side("yes", p_model, yes_price, cfg, kf),
        _evaluate_side("no", 1.0 - p_model, no_price, cfg, kf),
    ]
    actionable = [c for c in candidates if c is not None]
    return max(actionable, key=lambda c: c.net_edge, default=None)
