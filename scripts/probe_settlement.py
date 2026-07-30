"""Survey how Kalshi actually SETTLES tennis markets, and save a finalized-market fixture.

Exists because the auto-recorder's correctness rests on three empirical claims that were otherwise
only prose in the docs:
  1. settled tennis markets report `status = 'finalized'` (NOT 'settled'),
  2. `result` is one of 'yes' | 'no' | 'scalar',
  3. 'scalar' is a PARTIAL settlement where the two mirrored markets SPLIT the dollar.
This repo's rule is measure-don't-assert, so the measurement has to be re-runnable.

    .venv/bin/python scripts/probe_settlement.py            # print the survey
    .venv/bin/python scripts/probe_settlement.py --save     # also refresh the fixture

Read-only against public Kalshi endpoints; no auth, no orders.
"""
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from matador.kalshi.client import KalshiClient  # noqa: E402

PROD = "https://external-api.kalshi.com/trade-api/v2"
SERIES = ("KXATPMATCH", "KXWTAMATCH")
FIXTURE = Path("tests/fixtures/market_finalized_sample.json")


def survey(client, series: str) -> list[dict]:
    raw = client._request("GET", "/markets", params={
        "series_ticker": series, "status": "settled", "limit": 1000, "mve_filter": "exclude"}).json()
    markets = raw.get("markets", [])
    results = Counter(m.get("result") for m in markets)
    print(f"\n=== {series}: {len(markets)} settled markets")
    print(f"    status : {dict(Counter(m.get('status') for m in markets))}")
    print(f"    result : {dict(results)}")
    total = sum(results.values()) or 1
    for value, n in results.most_common():
        print(f"      {str(value):8s} {n:5d}  {n / total:5.1%}")
    scalars = [m for m in markets if m.get("result") == "scalar"]
    if scalars:
        print(f"    scalar settlement_value_dollars: "
              f"{sorted({m.get('settlement_value_dollars') for m in scalars})}")
        # A scalar SPLITS the dollar across the pair -- show one pair to make that concrete.
        by_event: dict = {}
        for m in scalars:
            by_event.setdefault(m["event_ticker"], []).append(m)
        pair = next((v for v in by_event.values() if len(v) == 2), None)
        if pair:
            print("    e.g. one pair, summing to $1.00:")
            for m in pair:
                print(f"      {m['ticker']:34s} {m.get('yes_sub_title'):22s} "
                      f"-> ${m.get('settlement_value_dollars')}")
    return markets


def main() -> None:
    with KalshiClient(base_url=PROD) as client:
        markets = [m for s in SERIES for m in survey(client, s)]
    if "--save" in sys.argv:
        sample = next((m for m in markets if m.get("result") in ("yes", "no")), None)
        scalar = next((m for m in markets if m.get("result") == "scalar"), None)
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(json.dumps({"finalized_yes_or_no": sample, "finalized_scalar": scalar},
                                      indent=2, sort_keys=True))
        print(f"\nwrote {FIXTURE}")


if __name__ == "__main__":
    main()
