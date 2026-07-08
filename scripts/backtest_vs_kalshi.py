"""Does p_model beat KALSHI's own pre-match line? (our real venue -- the one that matters)

Pulls settled KXATPMATCH/KXWTAMATCH markets from Kalshi's PUBLIC production API (read-only, no
auth), groups the two player-markets per match, recovers each match's PRE-MATCH price from the
candlesticks (a candle ~6h before close, before the in-play surge -- last_price is the ~0.99
settlement value and is NOT usable), joins our held-out predictions, and reports model-vs-Kalshi
sharpness + a net-of-fee ROI backtest of our >= min_net_edge rule, segmented by experience.

Findings to date: vs the SHARP bookmaker close our model has NO edge (w*=0, -10.6% ROI; see
backtest_vs_bookmaker.py). Vs KALSHI the result is INCONCLUSIVE: Kalshi's API exposes only ~5-6
weeks of settled tennis and only ~25% of matches have a recoverable pre-match candle, so the
~170-match result swings run-to-run (w* ~0.1-0.6, ROI ~+1..+14%) -- too small/noisy/subset-biased
to conclude edge or no-edge. Whether Kalshi's (softer) lines are beatable can only be settled by
FORWARD CLV paper-testing. Needs data/model.json + match data under data/.

    .venv/bin/python scripts/backtest_vs_kalshi.py
"""
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import pandas as pd  # noqa: E402

from matador.backtest import replay_predictions, roi_by_experience, sharpness  # noqa: E402
from matador.config import load_config  # noqa: E402
from matador.model.elo import KFactor  # noqa: E402
from matador.names import canonical_key  # noqa: E402
from matador.sackmann import load_matches  # noqa: E402

DATA_DIR = "data"
API = "https://external-api.kalshi.com/trade-api/v2"   # public market data (read-only)
HDR = {"User-Agent": "Mozilla/5.0"}
PAGES = 8                     # settled-markets pages per tour (Kalshi exposes ~5-6 weeks regardless)
CANDLE_WORKERS = 4


def _ts(s: str) -> int:
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def _fetch_settled(series: str) -> list[dict]:
    out, cursor = [], None
    for _ in range(PAGES):
        params = {"series_ticker": series, "status": "settled", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        j = httpx.get(f"{API}/markets", params=params, headers=HDR, timeout=40).json()
        out += j.get("markets", [])
        cursor = j.get("cursor")
        if not cursor:
            break
    return out


def _prematch_price(series: str, ticker: str, open_ts: int, close_ts: int) -> float | None:
    """Kalshi P(yes) ~6h before match end -- the pre-match plateau, before the in-play surge.
    Window capped to the last 3 days so long-open markets don't blow the candlestick range."""
    start = max(open_ts, close_ts - 3 * 86400)
    j = None
    for _ in range(3):
        try:
            j = httpx.get(f"{API}/series/{series}/markets/{ticker}/candlesticks",
                          params={"start_ts": start, "end_ts": close_ts, "period_interval": 60},
                          headers=HDR, timeout=40).json()
            break
        except Exception:
            j = None
    if not j:
        return None
    cs = [c for c in j.get("candlesticks", []) if c.get("price", {}).get("close_dollars") not in (None, "")]
    if not cs:
        return None
    target = close_ts - 6 * 3600
    pre = [c for c in cs if c["end_period_ts"] <= target] or cs[:1]
    return float(pre[-1]["price"]["close_dollars"])


def main() -> None:
    cfg = load_config()
    e = cfg.elo
    art = json.loads((Path(DATA_DIR) / "model.json").read_text())
    k = KFactor(e.k_num, e.k_shift, e.k_pow)
    fee = lambda p: cfg.fee_coefficient * p * (1 - p)

    diag_rows: list[tuple[float, float]] = []   # (p_model_winner, kalshi_prematch_winner)
    bets: list[tuple] = []
    events = joined = priced = 0
    for tour, series in (("atp", "KXATPMATCH"), ("wta", "KXWTAMATCH")):
        try:
            matches = load_matches(tour, DATA_DIR)
        except FileNotFoundError as exc:
            print(f"[{tour}] no data: {exc}")
            continue
        scales = {int(bo): float(s) for bo, s in art["tours"][tour]["scales"].items()}
        preds = replay_predictions(
            matches, surface_weight=e.surface_weight, scales=scales, min_matches=cfg.min_matches,
            shrinkage_n0=e.shrinkage_n0, initial=e.initial_rating, k=k,
        )
        lut: dict = {}
        for p in preds:
            lut.setdefault(frozenset({p.key_a, p.key_b}), []).append(p)

        ev: dict = {}
        for m in _fetch_settled(series):
            if m.get("result") in ("yes", "no"):
                ev.setdefault(m["event_ticker"], []).append(m)
        ev = {et: ms for et, ms in ev.items() if len(ms) == 2}
        events += len(ev)

        jobs = []
        for ms in ev.values():
            wm = next((m for m in ms if m["result"] == "yes"), None)   # the winning player's market
            if not wm:
                continue
            lm = next(m for m in ms if m is not wm)
            kw, kl = canonical_key(wm["yes_sub_title"]), canonical_key(lm["yes_sub_title"])
            recs = lut.get(frozenset({kw, kl}))
            if not recs:
                continue
            cdt = pd.Timestamp(wm["close_time"][:10])
            best = min(recs, key=lambda r: abs((r.date - cdt).days))
            if abs((best.date - cdt).days) > 4 or best.key_a != kw:   # our winner must match Kalshi's
                continue
            joined += 1
            jobs.append((series, wm, best))

        with ThreadPoolExecutor(max_workers=CANDLE_WORKERS) as ex:
            results = ex.map(lambda job: (job, _prematch_price(job[0], job[1]["ticker"], _ts(job[1]["open_time"]), _ts(job[1]["close_time"]))), jobs)
            for (series_, wm, best), price_w in results:
                if price_w is None or not (0.0 < price_w < 1.0):
                    continue
                priced += 1
                diag_rows.append((best.p_a, price_w))
                enet_w = (best.p_a - price_w) - fee(price_w)
                enet_l = ((1 - best.p_a) - (1 - price_w)) - fee(1 - price_w)
                if max(enet_w, enet_l) >= cfg.min_net_edge:
                    if enet_w >= enet_l:
                        bets.append((tour, best.n_a, (1.0 / price_w - 1.0) - cfg.fee_coefficient * (1 - price_w)))
                    else:
                        bets.append((tour, best.n_b, -1.0 - cfg.fee_coefficient * price_w))

    print(f"events(2-market): {events:,} | joined to our data: {joined:,} | priced (pre-match candle): {priced:,}\n")
    s = sharpness([d[0] for d in diag_rows], [d[1] for d in diag_rows])
    print(f"=== model vs KALSHI pre-match (winner-side, n={s.get('n', 0):,}) ===")
    if s.get("n"):
        px = pd.Series([d[1] for d in diag_rows])
        print(f"  pre-match price sanity: median={px.median():.2f}  extreme(<.03 or >.97)={((px < 0.03) | (px > 0.97)).mean():.0%} (high => leakage)")
        print(f"  Brier: model={s['brier_model']:.4f}  KALSHI={s['brier_market']:.4f}")
        print(f"  optimal blend weight on MODEL: w*={s['blend_w_star']:.2f}  (>0 => model adds info Kalshi lacks)")
        print(f"  disagree on favorite: {s['disagree_frac']:.1%}  ->  Kalshi right {s['market_right_on_disagree']:.1%} vs model {s['model_right_on_disagree']:.1%}")
    b = pd.DataFrame(bets, columns=["tour", "n_bet", "pnl"])
    print(f"\n=== backtest vs Kalshi pre-match (>= {cfg.min_net_edge:.0%} net-of-fee edge, flat stake) ===")
    for lab, n, roi, pnl in roi_by_experience(b):
        print(f"  {lab:<22} bets={n:>5}  ROI={roi:>+7.1%}  pnl={pnl:>+8.1f}u")
    print("\nNOTE: small single-window sample (Kalshi exposes ~5-6 weeks); promising but the real bar is forward CLV.")


if __name__ == "__main__":
    main()
