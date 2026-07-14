"""scripts/scan.py dry-run core -- collect_liquidity sweeps H2H markets AND outright finals."""
import importlib.util
from pathlib import Path

import httpx

from matador.config import Config
from matador.kalshi.client import KalshiClient

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "scan.py"


def _load():
    spec = importlib.util.spec_from_file_location("scan_mod", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cfg() -> Config:
    return Config(bankroll=1000.0, min_liquidity=10.0, max_spread=0.10)  # series defaults: KXATPMATCH / KXATP


def _slam_client():
    """No open H2H markets; a Grand Slam final only in the outright series (2 active contracts)."""
    event = {"event_ticker": "KXATP-FINAL", "title": "Wimbledon",
             "product_metadata": {"competition": "Wimbledon Men Singles"}}
    markets = [{"ticker": f"KXATP-FINAL-{s}", "event_ticker": "KXATP-FINAL", "status": "active",
                "yes_sub_title": n, "no_sub_title": n} for s, n in (("A", "Player Aaa"), ("B", "Player Bbb"))]

    def handler(request):
        p = request.url.path
        if p.endswith("/events"):
            evs = {"events": [event]} if request.url.params.get("series_ticker") == "KXATP" else {"events": []}
            return httpx.Response(200, json=evs)
        if p.endswith("/orderbook"):
            return httpx.Response(200, json={"orderbook_fp": {"yes_dollars": [["0.45", "100"]], "no_dollars": [["0.50", "50"]]}})
        if p.endswith("/markets"):
            et = request.url.params.get("event_ticker")
            return httpx.Response(200, json={"markets": markets if et == "KXATP-FINAL" else []})
        raise AssertionError(f"unexpected {request.url}")

    return KalshiClient(base_url="https://x/trade-api/v2", transport=httpx.MockTransport(handler))


def test_collect_liquidity_includes_outright_final_and_tags_tier():
    mod = _load()
    with _slam_client() as client:
        records = mod.collect_liquidity(client, _cfg(), "atp")
    assert len(records) == 2                            # the final's two active contracts (no H2H markets)
    assert {r[0] for r in records} == {"Grand Slam"}    # tier derived from the competition string
    assert all(r[2] == 50 for r in records)             # depth at the reconstructed yes ask (No bids @ .50)
    assert all(abs(r[1] - 0.05) < 1e-9 for r in records)  # spread = yes_ask .50 - yes_bid .45
