"""Phase-3 CLI: on-demand edge scan against Kalshi. Logs PAPER opportunities; never places orders.

Reads market data from Kalshi PRODUCTION (public, read-only) by default; --demo uses the demo base
from config. Output feeds forward CLV paper-testing, not live money.

    .venv/bin/python scripts/scan.py check "Dimitrov v Berrettini" --tour atp \\
        [--surface Grass --best-of 5 --date 2026-07-04 --force]
    .venv/bin/python scripts/scan.py scan --tour atp [--tour wta]
    .venv/bin/python scripts/scan.py dry-run --tour atp   # read-only spread/depth to calibrate the gate
"""
import argparse
import re
import statistics
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from matador import storage  # noqa: E402
from matador.config import load_config  # noqa: E402
from matador.engine import depth_at_ask, evaluate_match, log_opportunity, scan_series, spread  # noqa: E402
from matador.kalshi.client import KalshiClient, reconstruct_asks  # noqa: E402
from matador.model.artifact import Model  # noqa: E402
from matador.tournament import _SLAM_SURFACE  # noqa: E402

PROD_BASE = "https://external-api.kalshi.com/trade-api/v2"

# Kalshi has no tier field; classify from the event's competition string (a Masters-1000 keyword
# list + the Slam name map). Coarse on purpose -- just enough to read the liquid-tier distribution.
_MASTERS_1000 = ("indian wells", "miami", "monte", "madrid", "rome", "canad", "toronto",
                 "montreal", "cincinnati", "shanghai", "paris")


def _split_players(text: str) -> tuple[str, str]:
    parts = re.split(r"\s+vs?\.?\s+", text.strip(), maxsplit=1)
    if len(parts) != 2:
        raise SystemExit(f'could not parse "{text}" as "Player A v Player B"')
    return parts[0].strip(), parts[1].strip()


def _client(cfg, demo: bool) -> KalshiClient:
    return KalshiClient(base_url=cfg.kalshi_base_url if demo else PROD_BASE)


def _print(result) -> None:
    if result.status == "abstain":
        print(f"  abstain: {result.reason}")
        return
    o = result.opportunity
    flag = "  [FLAG: large gap -- check recent news]" if result.flagged else ""
    print(f"  ALERT {o.match}: buy {o.side.upper()} @ {o.price:.2f}  p_model={o.p_model:.3f}  "
          f"net_edge={o.net_edge:+.3f}  ->  {o.contracts} contracts (${o.suggested_stake:.2f}); "
          f"depth={o.liquidity:.0f}{flag}")


def cmd_check(args) -> None:
    cfg = load_config()
    model = Model.from_artifact(cfg.model_path)
    a, b = _split_players(args.match)
    event_date = date.fromisoformat(args.date) if args.date else None
    print(f"{args.tour.upper()}  {a} v {b}")
    with _client(cfg, args.demo) as client:
        result = evaluate_match(client, model, cfg, args.tour, a, b,
                                surface=args.surface, best_of=args.best_of, event_date=event_date)
    _print(result)
    if result.status == "alert":
        conn = storage.connect(cfg.db_path)
        storage.init_db(conn)
        opp_id = log_opportunity(conn, result.opportunity, force=args.force)
        print(f"  logged id {opp_id}" if opp_id else "  duplicate -- not logged (use --force)")
        conn.close()


def cmd_scan(args) -> None:
    cfg = load_config()
    model = Model.from_artifact(cfg.model_path)
    conn = storage.connect(cfg.db_path)
    storage.init_db(conn)
    with _client(cfg, args.demo) as client:
        for tour in args.tour:
            print(f"=== {tour.upper()} ===")
            if getattr(cfg.series, tour.lower(), None) is None:
                print("  no series configured for this tour")
                continue
            alerts = 0
            for result in scan_series(client, model, cfg, tour):
                if result.status == "alert":
                    alerts += 1
                    _print(result)
                    log_opportunity(conn, result.opportunity, force=args.force)
            print(f"  {alerts} alert(s)")
    conn.close()


def _tier_of(event: dict) -> str:
    """Coarse tournament tier from the event's competition string (no Kalshi tier field)."""
    label = ((event.get("product_metadata") or {}).get("competition") or event.get("title", "")).lower()
    if any(name in label for name in _SLAM_SURFACE):
        return "Grand Slam"
    if any(m in label for m in _MASTERS_1000):
        return "Masters 1000"
    return "Other"


def _measure(client, markets):
    """Yield (spread|None, depth_at_yes_ask) for each market that has a Yes quote (read-only)."""
    for m in markets:
        ob = client.get_orderbook(m.ticker)
        quotes = reconstruct_asks(ob)
        if quotes.yes_ask is not None:
            yield spread(quotes), depth_at_ask(ob, "yes", quotes.yes_ask)


def collect_liquidity(client, cfg, tour: str) -> list[tuple]:
    """Read-only sweep -> [(tier, spread|None, depth)] across a tour's open H2H markets PLUS any
    outright FINAL (an outright event down to exactly two active contracts, mirroring
    engine.scan_outright_finals). No model, no DB. The core of `dry-run`, split out to be testable."""
    records: list[tuple] = []
    series = getattr(cfg.series, tour.lower(), None)
    if series is not None:
        for event in client.get_events(series, status="open"):
            tier = _tier_of(event)
            for sp, dep in _measure(client, client.get_markets(event_ticker=event["event_ticker"])):
                records.append((tier, sp, dep))
    outright = getattr(cfg.series, f"{tour.lower()}_outright", None)
    if outright is not None:
        for event in client.get_events(outright, status="open"):
            active = [m for m in client.get_markets(event_ticker=event["event_ticker"]) if m.status == "active"]
            if len(active) != 2:
                continue  # only a 2-active outright (a final) is a tradeable head-to-head
            tier = _tier_of(event)
            for sp, dep in _measure(client, active):
                records.append((tier, sp, dep))
    return records


def _print_distribution(label: str, records: list[tuple], indent: str = "") -> None:
    depths = sorted(d for _t, _s, d in records)
    spreads = sorted(s for _t, s, _d in records if s is not None)
    print(f"{indent}=== {label} ({len(depths)} market(s) with quotes) ===")
    if not depths:
        print(f"{indent}  no open markets with quotes")
        return
    pct = lambda xs, p: xs[min(len(xs) - 1, int(p * len(xs)))]
    print(f"{indent}  depth   p10={pct(depths, .1):.0f}  median={statistics.median(depths):.0f}  p90={pct(depths, .9):.0f}")
    if spreads:  # a thin/one-sided book contributes depth but no spread
        print(f"{indent}  spread  p10={pct(spreads, .1):.3f}  median={statistics.median(spreads):.3f}  p90={pct(spreads, .9):.3f}")
    else:
        print(f"{indent}  spread  n/a (no two-sided books)")


def cmd_dry_run(args) -> None:
    """Read-only: spread + top-of-book depth distribution across open markets (H2H + outright
    finals), segmented per-tour and per-tier -- to calibrate min_liquidity/max_spread (no model,
    no DB). Run at the August Masters main draw to set the gate from the LIQUID distribution."""
    cfg = load_config()
    with _client(cfg, args.demo) as client:
        for tour in args.tour:
            if getattr(cfg.series, tour.lower(), None) is None:
                print(f"{tour.upper()}: no series configured")
                continue
            records = collect_liquidity(client, cfg, tour)
            _print_distribution(tour.upper(), records)
            for tier in sorted({r[0] for r in records}):
                _print_distribution(f"{tour.upper()} · {tier}", [r for r in records if r[0] == tier], indent="  ")


def main() -> None:
    p = argparse.ArgumentParser(description="Matador Phase-3 edge scan (paper only; never places orders)")
    p.add_argument("--demo", action="store_true", help="use the demo base URL (default: Kalshi production, read-only)")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="evaluate one match")
    c.add_argument("match", help='"Player A v Player B"')
    c.add_argument("--tour", required=True)
    c.add_argument("--surface")
    c.add_argument("--best-of", type=int, dest="best_of")
    c.add_argument("--date")
    c.add_argument("--force", action="store_true")
    c.set_defaults(func=cmd_check)

    s = sub.add_parser("scan", help="scan a series' open matches")
    s.add_argument("--tour", action="append", required=True)
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_scan)

    d = sub.add_parser("dry-run", help="read-only spread/depth distribution to calibrate the liquidity gate")
    d.add_argument("--tour", action="append", required=True)
    d.set_defaults(func=cmd_dry_run)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
