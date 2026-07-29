"""Does Kalshi's in-play tennis price OVERREACT to a set win? (offline; read-only; no trades)

THE THESIS UNDER TEST. Buy an underdog pre-match; when they win a set the price jumps; sell into the
jump for a profit larger than the true probability shift warrants. This is the "in-play
mean-reversion" pilot that RESEARCH-KALSHI section 3 left as the project's one genuinely open
question -- and flagged with a mechanical reason to doubt: a SPORTSBOOK line can lag because
something sets it, but Kalshi is an ORDER BOOK that reprices when someone takes the other side, and
Kalshi's own docs say its in-play pricing tracks fast data feeds. So the overreaction that is well
documented at books may simply be arbitraged away here.

WHY THIS TEST NEEDS NO LIVE-SCORE FEED. A set completion is the largest, fastest price event in a
tennis match, so it shows up in 1-minute candlesticks as a big jump. We therefore never need to know
WHEN sets ended: we detect jumps from the price series itself and ask the only question that matters
economically -- after a large fast move, does the price partially COME BACK?

    continuation(k) = sign(jump) * (price[t+k] - price[t])

      continuation < 0  -> the move partly reverses          => OVERREACTION (thesis supported)
      continuation ~ 0  -> the new level sticks              => efficient repricing (thesis dead)
      continuation > 0  -> the move keeps going              => UNDERREACTION (opposite trade)

WHAT A POSITIVE RESULT WOULD STILL NOT PROVE. Two things this cannot tell you, both of which decide
whether the strategy is real:
  1. DEPTH. RESEARCH-KALSHI calls per-match in-play depth "the biggest evidence gap"; live set
     markets were seen quoting depth of 1-1681 contracts. Reversion you can only sell 20 contracts
     into is not a strategy. Candlesticks carry no book, so depth needs separate measurement.
  2. FEES. A round trip pays two fees, and the exit lands near 50c where 0.07*P*(1-P) is MAXIMAL:
     ~3.2c on a 30c->55c trade. Any measured reversion must clear that bar to be tradeable, and the
     "steady small profits" framing is exactly the regime the fee curve destroys.
  Note the DIRECTION of the trade being tested: you are long the underdog and exit by SELLING, so
  your executable exit is the BID, not the mid. The report gives both.

THE ARTIFACT THAT WOULD FAKE A POSITIVE, AND HOW IT IS KILLED. Bid-ask bounce: a trade at the ask
followed by one at the bid looks like ~half a spread of "reversion" with no information in it at all.
The candles carry yes_bid AND yes_ask, so everything here is measured on the MID -- which does not
bounce -- rather than on the trade price. The real spread is reported alongside as a sanity band.

The candles also carry volume, so the "can you actually sell into it?" question is partly answerable
after all: post-jump traded volume is an upper bound on what you could have exited into. It is an
upper bound, not a fill guarantee -- you would have been competing with those trades, not adding to
them.


================================================================================
PRE-REGISTRATION (written 2026-07-29, BEFORE any holdout numbers were computed)
================================================================================
DISCOVERY SET: the first 400 settled markets returned by the API -- ATP only, closes
2026-06-25..07-29, 816 jump events across 220 matches. Findings that generated the
hypotheses below:

    fade net of spread+fees, all prices:   5m -0.0348 | 15m -0.0198 | 30m -0.0070
                                          45m +0.0116 | 60m +0.0281
    fade @30m by landing price:  <20c +0.1088 | 20-35c +0.0314 | 35-65c -0.0441
                                 65-80c +0.0175 | >80c +0.1195 (CI +0.039..+0.215)

Those came from scanning 5 horizons x 5 price buckets = 25 cells. At 95% confidence,
~72% of such scans throw at least one "significant" cell by chance, so NONE of it is
believed until it replicates. The mid-book negative is the exception: it has a
mechanical cause (fee = 0.07*P*(1-P) peaks at 0.50), so it is the most likely to hold.

HOLDOUT: the remaining 1,384 markets, never examined -- 482 ATP + 902 WTA. WTA is an
independent population (always Bo3, more breaks, higher variance).

PRIMARY TESTS -- exactly three, fixed now, no substitutions:
  H1  fade @60m, all prices               PASS iff holdout CI lower bound > 0
  H2  fade @30m, landing price >80c       PASS iff holdout CI lower bound > 0
  H3  fade @30m, landing price 35-65c     PASS iff holdout CI upper bound < 0  (the
                                          mechanical prediction: mid-book LOSES)

DECISION RULES, fixed now:
  - Holm correction across the three; a cell that only survives uncorrected is reported
    as suggestive, not passed.
  - REPLICATION REQUIREMENT: H1/H2 must hold in ATP-holdout AND WTA-holdout separately.
    An effect in one tour only is treated as FAILED, not as "works on ATP".
  - Any cell with n < 30 in the holdout is reported as underpowered and cannot PASS.
  - If H2 spans zero on the holdout, the discovery >80c result is declared the expected
    multiple-comparisons artifact and the extreme-price strategy is DEAD.
  - Depth is NOT tested here and no PASS implies tradeability: an edge you can fill 5
    contracts into is not a strategy.

WHAT NO OUTCOME CHANGES: the pre-match paper test. This is offline research on settled
markets; p_model stays frozen either way.
================================================================================

    .venv/bin/python scripts/inplay_overreaction.py [--jump 0.10] [--limit 400]
    .venv/bin/python scripts/inplay_overreaction.py --offset 400 --limit 1400   # HOLDOUT
    .venv/bin/python scripts/inplay_overreaction.py --offset 400 --tour wta     # WTA only

Read-only: touches no DB, no model, no config that the live paper test depends on. Candles are cached
under data/candles_cache/ so re-runs are cheap.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from matador.clv import bootstrap_mean_ci  # noqa: E402

FEE_COEFF = 0.07   # Kalshi taker fee = 0.07 * P * (1-P) per contract, peaking at P=0.50


def _fee(price: float) -> float:
    """Per-contract Kalshi taker fee. Peaks mid-book, which is exactly where a faded jump lands --
    the fee curve is structurally hostile to round-tripping near 50c."""
    return FEE_COEFF * price * (1.0 - price)

API = "https://external-api.kalshi.com/trade-api/v2"
HDR = {"User-Agent": "Mozilla/5.0"}
SERIES = ("KXATPMATCH", "KXWTAMATCH")
CACHE = Path("data/candles_cache")

# Exclude the last stretch before close: the price is converging on the 0.99/0.01 settlement value,
# so a late jump followed by settlement drift reads as "continuation" for reasons unrelated to
# overreaction. Also skip the first candles, which are often stale pre-match quotes.
SETTLEMENT_BUFFER_MIN = 20
HORIZONS_MIN = (5, 15, 30, 45, 60)


def _get(url: str, params: dict, tries: int = 4):
    for attempt in range(tries):
        try:
            r = httpx.get(url, params=params, headers=HDR, timeout=40)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 503):          # rate-limited: back off
                time.sleep(1.5 * (attempt + 1))
                continue
            return None
        except httpx.HTTPError:
            time.sleep(1.0 * (attempt + 1))
    return None


def settled_markets(limit: int, offset: int = 0, tour: str | None = None) -> list[dict]:
    """Settled tennis markets, one per event (the two player-markets are mirror images, so taking
    both would double-count every jump; pick the lexicographically lower ticker for determinism --
    the same anchor-stability lesson as the dedup fix)."""
    out: dict[str, dict] = {}
    want = SERIES if tour is None else (f"KX{tour.upper()}MATCH",)
    for series in want:
        cursor = None
        while len(out) < limit + offset:
            params = {"series_ticker": series, "status": "settled", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            j = _get(f"{API}/markets", params)
            if not j:
                break
            for m in j.get("markets", []):
                if not (m.get("close_time") and m.get("open_time") and m.get("event_ticker")):
                    continue
                ev = m["event_ticker"]
                if ev not in out or m["ticker"] < out[ev]["ticker"]:
                    out[ev] = m
            cursor = j.get("cursor")
            if not cursor:
                break
    # offset preserves the API's pagination order, so --offset 400 is exactly the set the
    # discovery run did NOT touch.
    return list(out.values())[offset:offset + limit]


def _ts(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).replace(tzinfo=timezone.utc).timestamp())


def candles(market: dict) -> list[dict]:
    """[{ts, mid, bid, ask, spread, volume}] at 1-MINUTE resolution, cached. 1 minute is the finest
    the endpoint serves and is required here: a reversion that plays out over minutes is invisible in
    the 60-min candles the pre-match backtest uses.

    Everything downstream uses MID, not the trade price: the trade price bounces between bid and ask,
    which would manufacture apparent reversion out of pure microstructure."""
    CACHE.mkdir(parents=True, exist_ok=True)
    series = market["ticker"].split("-")[0]
    cache = CACHE / f"{market['ticker']}.json"
    if cache.exists():
        j = json.loads(cache.read_text())
    else:
        close, open_ = _ts(market["close_time"]), _ts(market["open_time"])
        # A match is hours, not days; cap the window so the candle range stays servable.
        j = _get(f"{API}/series/{series}/markets/{market['ticker']}/candlesticks",
                 {"start_ts": max(open_, close - 8 * 3600), "end_ts": close, "period_interval": 1})
        if j is None:
            return []
        cache.write_text(json.dumps(j))
    rows = []
    for c in j.get("candlesticks", []):
        if not c.get("end_period_ts"):
            continue
        bid = (c.get("yes_bid") or {}).get("close_dollars")
        ask = (c.get("yes_ask") or {}).get("close_dollars")
        if bid in (None, "") or ask in (None, ""):
            continue                                  # need BOTH sides for a mid
        try:
            b, a = float(bid), float(ask)
            vol = float((c.get("volume_fp") or 0) or 0)
        except (TypeError, ValueError):
            continue
        if not (0.0 < b <= a < 1.0):
            continue
        rows.append({"ts": int(c["end_period_ts"]), "mid": (a + b) / 2.0,
                     "bid": b, "ask": a, "spread": a - b, "volume": vol})
    return sorted(rows, key=lambda r: r["ts"])


def find_jumps(series: list[dict], close_ts: int, min_jump: float) -> list[dict]:
    """Jumps of >= min_jump in the MID between consecutive candles, with the subsequent path.

    Excludes anything within SETTLEMENT_BUFFER_MIN of close (settlement convergence would masquerade
    as continuation) and any horizon that would run past that buffer."""
    events = []
    for i in range(1, len(series)):
        a, b = series[i - 1], series[i]
        if b["ts"] - a["ts"] > 180:            # a gap (illiquid stretch) is not a "fast" move
            continue
        move = b["mid"] - a["mid"]
        if abs(move) < min_jump:
            continue
        if close_ts - b["ts"] < SETTLEMENT_BUFFER_MIN * 60:
            continue
        sign = 1.0 if move > 0 else -1.0
        later, exitable = {}, {}
        for h in HORIZONS_MIN:
            target = b["ts"] + h * 60
            if target > close_ts - SETTLEMENT_BUFFER_MIN * 60:
                continue
            after = [r for r in series if r["ts"] >= target]
            if not after:
                continue
            nxt = after[0]
            # signed CONTINUATION on the MID: negative = the move partly came back = overreaction
            later[h] = sign * (nxt["mid"] - b["mid"])
            # EXECUTABLE FADE, net of fees -- the trade the reversion actually implies. NOTE this is
            # NOT "hold the underdog through the jump": that conditions on the jump having happened,
            # which you cannot know in advance, and simply re-measures the jump (an earlier version of
            # this metric did exactly that and reported a meaningless +11c).
            # Up-jump  -> the jumped side looks too expensive: SELL at bid[t], buy back at ask[t+k].
            # Down-jump-> it looks too cheap:                   BUY at ask[t],  sell at   bid[t+k].
            if sign > 0:
                gross = b["bid"] - nxt["ask"]
                fees = _fee(b["bid"]) + _fee(nxt["ask"])
            else:
                gross = nxt["bid"] - b["ask"]
                fees = _fee(b["ask"]) + _fee(nxt["bid"])
            exitable[h] = gross - fees
        if later:
            events.append({"ts": b["ts"], "move": move, "abs": abs(move), "spread": b["spread"],
                           "landed": b["mid"], "cont": later, "exit": exitable})
    return events


def _line(label: str, vals: list[float], clusters: list[str]) -> None:
    if len(vals) < 5:
        print(f"  {label:>10} {len(vals):>5}   (too few)")
        return
    ci = bootstrap_mean_ci(vals, clusters, seed=0)
    ci_s = f"[{ci[0]:+.4f}, {ci[1]:+.4f}]" if ci else "n/a"
    print(f"  {label:>10} {len(vals):>5} {statistics.mean(vals):>+9.4f} "
          f"{statistics.median(vals):>+9.4f} {ci_s:>28}")


def report(events: list[dict], clusters: list[str]) -> None:
    n = len(events)
    print(f"\n=== JUMP EVENTS: {n} across {len(set(clusters))} matches ===")
    if not n:
        print("  no qualifying jumps -- lower --jump or raise --limit")
        return
    sizes = [e["abs"] for e in events]
    spreads = [e["spread"] for e in events]
    half = statistics.median(spreads) / 2.0
    print(f"  jump size (mid): median {statistics.median(sizes):.3f}  max {max(sizes):.3f}")
    print(f"  spread AT the jump: median {statistics.median(spreads):.3f}  "
          f"-> half-spread reference +/-{half:.3f}")
    # Deliberately NOT reporting a depth number: volume_fp's units are unverified ("fp" implies a
    # fixed-point scaling), and a depth claim we cannot interpret is worse than none. Depth remains
    # the open question RESEARCH-KALSHI calls the biggest evidence gap.

    print("\n  MID continuation = sign(jump) x (mid[t+k] - mid[t]).  NEGATIVE = reversion = overreaction")
    print(f"  {'horizon':>10} {'n':>5} {'mean':>9} {'median':>9} {'95% CI (match-clustered)':>28}")
    ref = None
    for h in HORIZONS_MIN:
        vals = [e["cont"][h] for e in events if h in e["cont"]]
        cl = [c for e, c in zip(events, clusters) if h in e["cont"]]
        _line(f"{h}m", vals, cl)
        if h == 15 and len(vals) >= 5:
            ref = (statistics.mean(vals), bootstrap_mean_ci(vals, cl, seed=0))

    print("\n  EXECUTABLE FADE of the jump, crossing the spread BOTH ways, NET of Kalshi fees.")
    print("  (Positive = the overreaction is actually harvestable. This is the number that decides it.)")
    for h in HORIZONS_MIN:
        vals = [e["exit"][h] for e in events if h in e["exit"]]
        cl = [c for e, c in zip(events, clusters) if h in e["exit"]]
        _line(f"{h}m", vals, cl)

    if all(e["landed"] < 0.20 or e["landed"] > 0.80 for e in events):
        print("\n  POOLED EXTREMES (<20c or >80c) -- the pre-registered cell:")
        for h in (30, 45):
            v = [e["exit"][h] for e in events if h in e["exit"]]
            cl = [c for e, c in zip(events, clusters) if h in e["exit"]]
            _line(f"{h}m", v, cl)
        print()
    print("\n  EXECUTABLE FADE @30m by the PRICE the jump landed at (fee = 0.07*P*(1-P), so the")
    print("  curve collapses at the extremes -- if the fade ever pays, it pays there):")
    for lo, hi, lab in ((0.0, 0.20, "<20c"), (0.20, 0.35, "20-35c"), (0.35, 0.65, "35-65c"),
                        (0.65, 0.80, "65-80c"), (0.80, 1.01, ">80c")):
        sub = [(e, c) for e, c in zip(events, clusters)
               if 30 in e["exit"] and lo <= e["landed"] < hi]
        if len(sub) >= 5:
            v = [e["exit"][30] for e, _ in sub]
            ci = bootstrap_mean_ci(v, [c for _, c in sub], seed=0)
            ci_s = f"[{ci[0]:+.4f}, {ci[1]:+.4f}]" if ci else "n/a"
            print(f"    {lab:>7} n={len(v):>4}  {statistics.mean(v):+.4f}  {ci_s}  "
                  f"(fee/leg ~{_fee(statistics.median([e['landed'] for e, _ in sub])):.4f})")

    print("\n  MID continuation @15m by jump size:")
    for lo, hi, lab in ((0.10, 0.15, "10-15c"), (0.15, 0.25, "15-25c"), (0.25, 1.01, "25c+")):
        sub = [e for e in events if lo <= e["abs"] < hi and 15 in e["cont"]]
        cl = [c for e, c in zip(events, clusters) if lo <= e["abs"] < hi and 15 in e["cont"]]
        if len(sub) >= 5:
            ci = bootstrap_mean_ci([e["cont"][15] for e in sub], cl, seed=0)
            ci_s = f"[{ci[0]:+.4f}, {ci[1]:+.4f}]" if ci else "n/a"
            print(f"    {lab:>7} n={len(sub):>4}  {statistics.mean(e['cont'][15] for e in sub):+.4f}  {ci_s}")

    print("\n=== VERDICT ===")
    if not ref:
        print("  INCONCLUSIVE -- too few events at the 15m horizon.")
        return
    mean, ci = ref
    ex = [e["exit"][15] for e in events if 15 in e["exit"]]
    ex_cl = [c for e, c in zip(events, clusters) if 15 in e["exit"]]
    ex_mean = statistics.mean(ex) if ex else None
    ex_ci = bootstrap_mean_ci(ex, ex_cl, seed=0) if len(ex) >= 5 else None

    # Two SEPARATE questions. They can disagree, and here they are expected to.
    if ci and ci[1] < 0:
        micro = "exceeds" if abs(mean) > half else "is INSIDE"
        print(f"  1) DOES IT OVERREACT?  YES. Mid reverts {mean:+.4f} @15m, CI {ci} entirely below 0,")
        print(f"     and that {micro} the half-spread band ({half:.4f}) -- so it is not microstructure.")
    elif ci and ci[0] > 0:
        print(f"  1) DOES IT OVERREACT?  NO -- the OPPOSITE. Mid CONTINUES {mean:+.4f} @15m.")
    else:
        print(f"  1) DOES IT OVERREACT?  NOT ESTABLISHED. Mid {mean:+.4f} @15m, CI spans 0. A")
        print("     consistently negative mean with a CI spanning 0 is underpowered, not a refutation.")

    if ex_ci is None:
        print("  2) IS IT HARVESTABLE?   too few events to say.")
    elif ex_ci[0] > 0:
        print(f"  2) IS IT HARVESTABLE?   YES: fading nets {ex_mean:+.4f}/contract after spread+fees,")
        print(f"     CI {ex_ci} above 0. Depth is then the only remaining unknown.")
    elif ex_ci[1] < 0:
        print(f"  2) IS IT HARVESTABLE?   NO. Fading nets {ex_mean:+.4f}/contract after crossing the")
        print(f"     spread twice and paying two fees, CI {ex_ci} entirely BELOW 0.")
        print("     The inefficiency is real but smaller than the friction required to reach it.")
    else:
        print(f"  2) IS IT HARVESTABLE?   BORDERLINE: {ex_mean:+.4f}/contract, CI {ex_ci} spans 0.")

    print(f"\n  Distribution note: mean {mean:+.4f} but MEDIAN {statistics.median([e['cont'][15] for e in events if 15 in e['cont']]):+.4f}.")
    print("  If those disagree in sign, the reversion is TAIL-DRIVEN: most jumps continue slightly and")
    print("  a minority revert hard. That is the opposite of a 'steady small profits' strategy.")
    print("\n  Not tested here: achievable DEPTH at the reverting price (volume_fp units unverified),")
    print("  and whether the fade turns positive at EXTREME prices where the fee curve collapses.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Test whether Kalshi in-play tennis prices overreact")
    ap.add_argument("--jump", type=float, default=0.10, help="minimum 1-minute move to count (default 0.10)")
    ap.add_argument("--limit", type=int, default=400, help="max settled markets to scan")
    ap.add_argument("--offset", type=int, default=0, help="skip the first N markets (400 = the holdout)")
    ap.add_argument("--tour", choices=("atp", "wta"), help="restrict to one tour (for the replication test)")
    ap.add_argument("--split", choices=("early", "late"), help="temporal half, by market close time")
    ap.add_argument("--extremes", action="store_true",
                    help="report ONLY jumps landing <20c or >80c, pooled (both share the fee-collapse rationale; "
                         "pooling is what buys enough n to split at all)")
    args = ap.parse_args()

    mkts = settled_markets(args.limit, offset=args.offset, tour=args.tour)
    if args.split:
        mkts.sort(key=lambda m: _ts(m["close_time"]))
        half = len(mkts) // 2
        mkts = mkts[:half] if args.split == "early" else mkts[half:]
        span = (mkts[0]["close_time"][:10], mkts[-1]["close_time"][:10]) if mkts else ("-", "-")
        print(f"*** TEMPORAL SPLIT '{args.split}': {len(mkts)} markets, closes {span[0]}..{span[1]} ***")
    if args.offset:
        print(f"*** HOLDOUT RUN: skipping the first {args.offset} markets"
              f"{' , tour=' + args.tour if args.tour else ''} ***")
    print(f"settled tennis markets (one per event): {len(mkts)}")
    if not mkts:
        print("none available -- Kalshi exposes only ~5-6 weeks of settled tennis")
        return

    events, clusters = [], []
    for i, m in enumerate(mkts, 1):
        cs = candles(m)
        if len(cs) < 30:
            continue
        for e in find_jumps(cs, _ts(m["close_time"]), args.jump):
            if args.extremes and not (e["landed"] < 0.20 or e["landed"] > 0.80):
                continue
            events.append(e)
            clusters.append(m["event_ticker"])          # cluster by MATCH: sets within one match correlate
        if i % 50 == 0:
            print(f"  ...{i}/{len(mkts)} markets, {len(events)} jumps so far")

    report(events, clusters)


if __name__ == "__main__":
    main()
