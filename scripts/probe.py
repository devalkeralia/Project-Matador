"""Manual, read-only connectivity probe against the Kalshi API.

Resolves Phase 1's build-time unknowns in one run: whether the demo environment lists
tennis, the real market/orderbook field names and types, the WTA series ticker, and
whether the RSA signer works end-to-end. Saves each successful response to
tests/fixtures/ so the offline test suite can run against real shapes without a network
call. This is NOT a pytest test -- run it by hand:

    .venv/bin/python scripts/probe.py
"""

import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from matador.config import load_config, load_secrets  # noqa: E402
from matador.kalshi.auth import KalshiSigner  # noqa: E402

PRODUCTION_BASE = "https://external-api.kalshi.com/trade-api/v2"
FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
WTA_TICKER_CANDIDATES = ["KXWTAMATCH", "KXWTA", "KXWTAWINNER", "KXWTAMATCHWINNER"]


def _get(client: httpx.Client, path: str, **params) -> httpx.Response:
    return client.get(path, params=params or None)


def save_fixture(name: str, data) -> None:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURES_DIR / name
    path.write_text(json.dumps(data, indent=2))
    print(f"    saved -> {path.relative_to(FIXTURES_DIR.parent.parent)}")


def probe_events(client: httpx.Client, base_url: str, series_ticker: str) -> list[dict] | None:
    print(f"\n[events] GET {base_url}/events?series_ticker={series_ticker}&status=open")
    try:
        resp = _get(client, "/events", series_ticker=series_ticker, status="open")
        resp.raise_for_status()
        events = resp.json().get("events", [])
        print(f"    status={resp.status_code} events={len(events)}")
        return events
    except httpx.HTTPError as exc:
        print(f"    FAILED: {exc}")
        return None


def probe_markets(client: httpx.Client, series_ticker: str) -> list[dict] | None:
    print(f"\n[markets] GET /markets?series_ticker={series_ticker}&mve_filter=exclude&limit=5")
    try:
        resp = _get(client, "/markets", series_ticker=series_ticker, mve_filter="exclude", limit=5)
        resp.raise_for_status()
        markets = resp.json().get("markets", [])
        print(f"    status={resp.status_code} markets={len(markets)}")
        if markets:
            print(f"    sample keys: {sorted(markets[0].keys())}")
            print(f"    sample market: {json.dumps(markets[0], indent=2)}")
        return markets
    except httpx.HTTPError as exc:
        print(f"    FAILED: {exc}")
        return None


def probe_orderbook(client: httpx.Client, ticker: str) -> dict | None:
    print(f"\n[orderbook] GET /markets/{ticker}/orderbook")
    try:
        resp = _get(client, f"/markets/{ticker}/orderbook")
        resp.raise_for_status()
        book = resp.json()
        print(f"    status={resp.status_code}")
        print(f"    raw: {json.dumps(book, indent=2)}")
        return book
    except httpx.HTTPError as exc:
        print(f"    FAILED: {exc}")
        return None


def probe_wta_ticker(client: httpx.Client) -> str | None:
    print("\n[wta-discovery] trying candidate series tickers via GET /series/{ticker}")
    for candidate in WTA_TICKER_CANDIDATES:
        try:
            resp = client.get(f"/series/{candidate}")
            print(f"    {candidate}: status={resp.status_code}")
            if resp.status_code == 200:
                print(f"    FOUND -> {json.dumps(resp.json(), indent=2)}")
                return candidate
        except httpx.HTTPError as exc:
            print(f"    {candidate}: FAILED ({exc})")
    print("    no candidate matched -- WTA ticker needs manual lookup (see Kalshi's tennis markets page)")
    return None


def probe_auth(base_url: str, key_id: str, private_key_path: str) -> bool:
    print(f"\n[auth] GET {base_url}/portfolio/balance (signed)")
    try:
        signer = KalshiSigner(key_id=key_id, private_key_path=private_key_path)
        api_prefix = httpx.URL(base_url).path
        signing_path = f"{api_prefix}/portfolio/balance"
        headers = signer.headers("GET", signing_path)
        with httpx.Client(base_url=base_url, timeout=10.0) as client:
            resp = client.get("/portfolio/balance", headers=headers)
        print(f"    status={resp.status_code}")
        print(f"    body: {resp.text[:500]}")
        return resp.status_code == 200
    except Exception as exc:  # noqa: BLE001 -- probe script: report and keep going
        print(f"    FAILED: {exc}")
        return False


def main() -> None:
    config = load_config()
    secrets = load_secrets()

    demo_base = config.kalshi_base_url
    print(f"Demo base: {demo_base}")
    print(f"Production base: {PRODUCTION_BASE}")

    market_base = demo_base
    with httpx.Client(base_url=demo_base, timeout=10.0) as client:
        events = probe_events(client, demo_base, config.series.atp)

    if not events:
        print("\nDemo returned no ATP events -- falling back to production for market data reads.")
        market_base = PRODUCTION_BASE
        with httpx.Client(base_url=PRODUCTION_BASE, timeout=10.0) as client:
            events = probe_events(client, PRODUCTION_BASE, config.series.atp)

    if events:
        save_fixture("events_atp.json", {"events": events})

    with httpx.Client(base_url=market_base, timeout=10.0) as client:
        markets = probe_markets(client, config.series.atp)
        if markets:
            save_fixture("markets_atp_sample.json", {"markets": markets})
            book = probe_orderbook(client, markets[0]["ticker"])
            if book:
                save_fixture("orderbook_sample.json", book)

        wta_ticker = probe_wta_ticker(client)

    auth_ok = probe_auth(demo_base, secrets.kalshi_key_id, secrets.kalshi_private_key_path)

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Market data base to use : {market_base}")
    print(f"ATP events found        : {len(events) if events else 0}")
    print(f"WTA series ticker       : {wta_ticker or 'NOT FOUND -- set manually in config.yaml'}")
    print(f"Auth check (demo)       : {'OK' if auth_ok else 'FAILED -- see output above'}")
    print(f"Fixtures saved to       : {FIXTURES_DIR}")


if __name__ == "__main__":
    main()
