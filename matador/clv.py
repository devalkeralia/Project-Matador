"""Closing-line value (CLV), net-of-fee P&L, and the go-live bootstrap gate.

Pure math over the opportunities-x-outcomes rows (matador.storage.settled_bets) -- no I/O -- so
/stats and any future backtest compute the same numbers. CLV is the project's binding go-live
metric: after fees, did we get a better price than the market's close?

  entry        = the objective LOGGED ALERT price (user-entered fills feed P&L only, not CLV)
  gross clv    = closing_mid - entry                          # spread-neutral, on the taken side
  net clv      = gross clv - fee_coefficient*entry*(1-entry)  # subtract the per-contract entry fee
  net P&L      = contracts*((1 if win else 0) - fill) - kalshi_fee(fill, contracts)   # exact round-up
  go-live gate = net-CLV cluster (by ISO WEEK) BCa bootstrap 95% CI lower bound > min_effect_size,
                 AND >= 200 bets AND >= min_clv_clusters distinct weeks AND realized net-ROI >= 0
                 AND the missed-capture rate <= max_missed_capture_rate

Clustering is by ISO WEEK, not day: correlated model bias lives at the tournament/week/model-version
scale (a single day is too fine -- intra-tournament correlation survives day-clustering and shrinks
the CI toward a false go-live). The interval is BCa (bias-corrected + accelerated), not a plain
percentile, because CLV is right-skewed. The realized-ROI + capture-health co-gates keep a rosy CLV
on a biased/thin subsample from green-lighting real money. void (walkover/refund) rows are excluded.
"""
from __future__ import annotations

from datetime import date
from statistics import NormalDist

import numpy as np

from matador.edge import kalshi_fee

MIN_BETS = 200        # go-live floor on the number of CLV observations
_ESTABLISHED = 200    # experience boundary for the 'established' segmentation bucket
_BCA_MIN_CLUSTERS = 4  # below this, jackknife acceleration is unstable -> fall back to a plain percentile
_NORM = NormalDist()


def clv(entry_price: float, closing_price: float) -> float:
    """Same-side GROSS CLV: positive = we entered at a better price than the close."""
    return closing_price - entry_price


def net_pnl(result: str, fill_price: float, contracts: int, fee_coefficient: float) -> float:
    """Realized $ P&L of a settled position, net of the exact (round-up) Kalshi fee."""
    payoff = contracts * (1.0 if result == "win" else 0.0)
    return payoff - contracts * fill_price - kalshi_fee(fill_price, contracts, fee_coefficient)


def _iso_week(iso) -> str:
    """ISO-week label (YYYY-Www) of a date/datetime string -- the CLV correlation unit."""
    s = (iso or "")[:10]
    try:
        y, w, _ = date.fromisoformat(s).isocalendar()
        return f"{y}-W{w:02d}"
    except ValueError:
        return s or "unknown"


def bootstrap_mean_ci(values, clusters, *, level: float = 0.95, n_boot: int = 10000, seed: int = 0):
    """BCa CI for the mean of `values`, resampling whole CLUSTERS (e.g. ISO weeks) with replacement
    so intra-cluster correlation doesn't shrink the interval. Bias-corrected + accelerated (z0 from
    the bootstrap, acceleration from a leave-one-cluster-out jackknife) so a right-skewed CLV
    distribution isn't given an over-optimistic lower bound. Falls back to a plain percentile when
    there are too few clusters or the bootstrap distribution is degenerate. Returns (lo, hi), or None
    if there are no values. Deterministic for a given seed."""
    values = np.asarray(values, dtype=float)
    if values.size == 0:
        return None
    groups: dict = {}
    for i, c in enumerate(clusters):
        groups.setdefault(c, []).append(i)
    idx_by_cluster = [np.asarray(g) for g in groups.values()]
    n = len(idx_by_cluster)
    theta_hat = float(values.mean())
    rng = np.random.default_rng(seed)
    means = np.empty(n_boot)
    for b in range(n_boot):
        pick = rng.integers(0, n, size=n)  # resample n clusters with replacement
        means[b] = values[np.concatenate([idx_by_cluster[j] for j in pick])].mean()

    alpha = (1.0 - level) / 2.0
    lo_p, hi_p = alpha, 1.0 - alpha
    prop_less = float(np.mean(means < theta_hat))
    if n >= _BCA_MIN_CLUSTERS and 0.0 < prop_less < 1.0 and means.std() > 1e-12:
        z0 = _NORM.inv_cdf(prop_less)  # bias-correction
        jack = np.array([values[np.concatenate([idx_by_cluster[j] for j in range(n) if j != k])].mean()
                         for k in range(n)])
        jbar = jack.mean()
        den = 6.0 * (np.sum((jbar - jack) ** 2) ** 1.5)
        a = float(np.sum((jbar - jack) ** 3) / den) if den != 0 else 0.0  # acceleration
        if np.isfinite(a):
            def _adj(p):
                z = _NORM.inv_cdf(p)
                return _NORM.cdf(z0 + (z0 + z) / (1.0 - a * (z0 + z)))
            lo_p, hi_p = _adj(alpha), _adj(1.0 - alpha)
    return float(np.quantile(means, lo_p)), float(np.quantile(means, hi_p))


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
    + its ISO-week-cluster BCa bootstrap 95% CI, per-experience segmentation, and the go-live flag.
    CLV uses the objective logged alert price as entry; P&L uses the recorded fill. 'void' rows are
    excluded. Go-live requires all of: CI lower bound > min_effect_size, >= 200 bets, enough week
    clusters, realized net-ROI >= 0, and a missed-capture rate within max_missed_capture_rate."""
    fee = cfg.fee_coefficient
    rows = []  # (net_clv, gross_clv, week, experience) per CLV-eligible bet
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
            week = _iso_week(b["occurrence_datetime"] or b["ts"])
            rows.append((net, gross, week, b["experience"]))
        if res in ("win", "loss"):
            results += 1
            wins += 1 if res == "win" else 0
            if b["fill_price"] is not None and b["contracts_filled"]:
                total_pnl += net_pnl(res, b["fill_price"], b["contracts_filled"], fee)
                staked += b["fill_price"] * b["contracts_filled"]

    net_clvs = [r[0] for r in rows]
    weeks = [r[2] for r in rows]
    n_clusters = len(set(weeks))
    ci = bootstrap_mean_ci(net_clvs, weeks, seed=seed) if net_clvs else None
    roi = (total_pnl / staked) if staked else None
    total_captures = sum(captures.values())
    missed_rate = (captures["missed"] / total_captures) if total_captures else 0.0
    go_live = bool(
        ci and ci[0] > cfg.min_effect_size                     # net-CLV edge, after fees, is real
        and len(net_clvs) >= MIN_BETS                          # enough observations
        and n_clusters >= cfg.min_clv_clusters                 # enough INDEPENDENT weeks (not a few tournaments)
        and roi is not None and roi >= 0.0                     # realized fills didn't lose money net of fees
        and missed_rate <= cfg.max_missed_capture_rate         # the sample isn't a thin/biased leftover
    )
    buckets: dict = {}
    for net, _gross, _week, exp in rows:
        buckets.setdefault(_experience_bucket(exp, cfg.thin_matches), []).append(net)
    return {
        "n_opportunities": len(bets),
        "n_results": results,
        "wins": wins,
        "hit_rate": (wins / results) if results else None,
        "total_pnl": total_pnl,
        "staked": staked,
        "roi": roi,
        "n_clv": len(net_clvs),
        "n_clusters": n_clusters,                                                     # distinct ISO weeks
        "mean_clv": (float(np.mean(net_clvs)) if net_clvs else None),                 # NET -- the gate metric
        "mean_gross_clv": (float(np.mean([r[1] for r in rows])) if rows else None),   # informational
        "clv_ci": ci,
        "min_effect_size": cfg.min_effect_size,
        "min_clusters": cfg.min_clv_clusters,
        "missed_rate": missed_rate,
        "max_missed_rate": cfg.max_missed_capture_rate,
        "go_live": go_live,
        "buckets": {lab: {"n": len(v), "mean_clv": float(np.mean(v))} for lab, v in buckets.items()},
        "captures": captures,  # {auto, manual, missed} -- closing-line capture health
    }
