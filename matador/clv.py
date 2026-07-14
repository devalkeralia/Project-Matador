"""Closing-line value (CLV), net-of-fee P&L, and the go-live bootstrap gate.

Pure math over the opportunities-x-outcomes rows (matador.storage.settled_bets) -- no I/O -- so
/stats and any future backtest compute the same numbers. CLV is the project's binding go-live
metric: after fees, did we get a better price than the market's close?

  entry        = the objective LOGGED ALERT price (user-entered fills feed P&L only, not CLV)
  gross clv    = closing_mid - entry                          # spread-neutral, on the taken side
  net clv      = gross clv - fee_coefficient*entry*(1-entry)  # subtract the per-contract entry fee
  net P&L      = contracts*((1 if win else 0) - fill) - kalshi_fee(fill, contracts)   # exact round-up
  go-live gate = net-CLV cluster (by trading DAY) bootstrap 95% CI lower bound > min_effect_size,
                 with >= 200 bets AND >= min_clv_clusters distinct days

Clustering is by DAY, not event: correlated model bias lives across a day's matches, and there's
~one bet per event so event-clustering was inert. void (walkover/refund) rows are excluded entirely.
"""
from __future__ import annotations

import numpy as np

from matador.edge import kalshi_fee

MIN_BETS = 200        # go-live floor on the number of CLV observations
_ESTABLISHED = 200    # experience boundary for the 'established' segmentation bucket


def clv(entry_price: float, closing_price: float) -> float:
    """Same-side GROSS CLV: positive = we entered at a better price than the close."""
    return closing_price - entry_price


def net_pnl(result: str, fill_price: float, contracts: int, fee_coefficient: float) -> float:
    """Realized $ P&L of a settled position, net of the exact (round-up) Kalshi fee."""
    payoff = contracts * (1.0 if result == "win" else 0.0)
    return payoff - contracts * fill_price - kalshi_fee(fill_price, contracts, fee_coefficient)


def bootstrap_mean_ci(values, clusters, *, level: float = 0.95, n_boot: int = 10000, seed: int = 0):
    """Percentile CI for the mean of `values`, resampling whole CLUSTERS (e.g. trading days) with
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


def _experience_bucket(exp, thin: int) -> str:
    if exp is None:
        return "unknown"
    if exp < thin:
        return f"thin(<{thin})"
    if exp < _ESTABLISHED:
        return f"mid({thin}-{_ESTABLISHED})"
    return f"established({_ESTABLISHED}+)"


def summarize(bets, cfg, *, seed: int = 0) -> dict:
    """Aggregate settled_bets rows into the /stats figures: hit rate, net P&L / ROI, mean NET CLV
    + its day-cluster bootstrap 95% CI, per-experience segmentation, and the go-live flag. CLV uses
    the objective logged alert price as entry; P&L uses the recorded fill. 'void' rows are excluded."""
    fee = cfg.fee_coefficient
    rows = []  # (net_clv, gross_clv, day, experience) per CLV-eligible bet
    total_pnl = staked = 0.0
    wins = results = 0
    captures = {"auto": 0, "manual": 0, "missed": 0}  # closing-line capture health (data quality)
    for b in bets:
        src = b["closing_source"]  # None (not attempted) | 'auto' | 'manual' | 'missed:<reason>[<src>]'
        if src is not None:        # count capture attempts regardless of void/result (measures the mechanism)
            bucket = "missed" if src.startswith("missed") else src
            if bucket in captures:
                captures[bucket] += 1
        res = b["result"]
        if res == "void":
            continue  # walkover/refund -- excluded from every metric
        entry = b["price"]
        if b["closing_price"] is not None and entry is not None:
            gross = clv(entry, b["closing_price"])
            net = gross - fee * entry * (1.0 - entry)  # per-contract entry-fee drag, same price units
            day = (b["occurrence_datetime"] or b["ts"] or "")[:10]
            rows.append((net, gross, day, b["experience"]))
        if res in ("win", "loss"):
            results += 1
            wins += 1 if res == "win" else 0
            if b["fill_price"] is not None and b["contracts_filled"]:
                total_pnl += net_pnl(res, b["fill_price"], b["contracts_filled"], fee)
                staked += b["fill_price"] * b["contracts_filled"]

    net_clvs = [r[0] for r in rows]
    days = [r[2] for r in rows]
    n_clusters = len({d for d in days})
    ci = bootstrap_mean_ci(net_clvs, days, seed=seed) if net_clvs else None
    go_live = bool(
        ci and ci[0] > cfg.min_effect_size and len(net_clvs) >= MIN_BETS and n_clusters >= cfg.min_clv_clusters
    )
    buckets: dict = {}
    for net, _gross, _day, exp in rows:
        buckets.setdefault(_experience_bucket(exp, cfg.thin_matches), []).append(net)
    return {
        "n_opportunities": len(bets),
        "n_results": results,
        "wins": wins,
        "hit_rate": (wins / results) if results else None,
        "total_pnl": total_pnl,
        "staked": staked,
        "roi": (total_pnl / staked) if staked else None,
        "n_clv": len(net_clvs),
        "n_clusters": n_clusters,
        "mean_clv": (float(np.mean(net_clvs)) if net_clvs else None),                 # NET -- the gate metric
        "mean_gross_clv": (float(np.mean([r[1] for r in rows])) if rows else None),   # informational
        "clv_ci": ci,
        "min_effect_size": cfg.min_effect_size,
        "min_clusters": cfg.min_clv_clusters,
        "go_live": go_live,
        "buckets": {lab: {"n": len(v), "mean_clv": float(np.mean(v))} for lab, v in buckets.items()},
        "captures": captures,  # {auto, manual, missed} -- closing-line capture health
    }
