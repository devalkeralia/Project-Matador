"""Closing-line value (CLV), net-of-fee P&L, and the go-live bootstrap gate.

Pure math over the opportunities-x-outcomes rows (matador.storage.settled_bets) -- no I/O -- so
/stats and any future backtest compute the same numbers. CLV is the project's binding go-live
metric: did we get a better price than the market's close?

  clv          = closing_price - entry_price          # same-side, GROSS (fees tracked separately)
  entry_price  = actual fill_price if recorded, else the logged alert price
  net P&L      = contracts*((1 if win else 0) - fill) - fee_coefficient*fill*(1-fill)*contracts
  go-live gate = lower bound of a cluster (by event) bootstrap 95% CI on mean CLV > 0, >= 200 bets

The bootstrap clusters by event because the two contracts of one match yield correlated bets;
resampling whole events (not individual bets) keeps the CI honest.
"""
from __future__ import annotations

import numpy as np

MIN_BETS = 200  # go-live floor on the number of CLV observations


def clv(entry_price: float, closing_price: float) -> float:
    """Same-side CLV (gross): positive = we entered at a better price than the close."""
    return closing_price - entry_price


def net_pnl(result: str, fill_price: float, contracts: int, fee_coefficient: float) -> float:
    """Realized $ P&L of a settled position, net of the Kalshi fee (same fee basis as edge.net_edge)."""
    payoff = contracts * (1.0 if result == "win" else 0.0)
    cost = contracts * fill_price
    fee = fee_coefficient * fill_price * (1.0 - fill_price) * contracts
    return payoff - cost - fee


def bootstrap_mean_ci(values, clusters, *, level: float = 0.95, n_boot: int = 10000, seed: int = 0):
    """Percentile CI for the mean of `values`, resampling whole CLUSTERS (e.g. events) with
    replacement so intra-cluster correlation doesn't shrink the interval. Returns (lo, hi), or
    None if there are no values. Deterministic for a given seed."""
    values = np.asarray(values, dtype=float)
    if values.size == 0:
        return None
    groups: dict = {}
    for i, c in enumerate(clusters):
        groups.setdefault(c, []).append(i)
    idx_by_cluster = [np.asarray(g) for g in groups.values()]
    n = len(idx_by_cluster)
    rng = np.random.default_rng(seed)
    means = np.empty(n_boot)
    for b in range(n_boot):
        pick = rng.integers(0, n, size=n)  # resample n clusters with replacement
        means[b] = values[np.concatenate([idx_by_cluster[j] for j in pick])].mean()
    alpha = (1.0 - level) / 2.0
    return float(np.quantile(means, alpha)), float(np.quantile(means, 1.0 - alpha))


def summarize(bets, fee_coefficient: float, *, seed: int = 0) -> dict:
    """Aggregate settled_bets rows into the /stats figures: hit rate, net P&L / ROI, mean CLV +
    its cluster-bootstrap 95% CI, and the go-live flag. CLV uses the actual fill when recorded,
    else the logged alert price; P&L needs a recorded fill + contracts + result."""
    clv_values, clv_clusters = [], []
    total_pnl = staked = 0.0
    wins = results = 0
    for b in bets:
        entry = b["fill_price"] if b["fill_price"] is not None else b["price"]
        if b["closing_price"] is not None and entry is not None:
            clv_values.append(clv(entry, b["closing_price"]))
            clv_clusters.append(b["event_ticker"] or b["market_ticker"])
        if b["result"] is not None:
            results += 1
            wins += 1 if b["result"] == "win" else 0
            if b["fill_price"] is not None and b["contracts_filled"]:
                total_pnl += net_pnl(b["result"], b["fill_price"], b["contracts_filled"], fee_coefficient)
                staked += b["fill_price"] * b["contracts_filled"]
    ci = bootstrap_mean_ci(clv_values, clv_clusters, seed=seed) if clv_values else None
    return {
        "n_opportunities": len(bets),
        "n_results": results,
        "wins": wins,
        "hit_rate": (wins / results) if results else None,
        "total_pnl": total_pnl,
        "staked": staked,
        "roi": (total_pnl / staked) if staked else None,
        "n_clv": len(clv_values),
        "mean_clv": (float(np.mean(clv_values)) if clv_values else None),
        "clv_ci": ci,
        "go_live": bool(ci and ci[0] > 0 and len(clv_values) >= MIN_BETS),
    }
