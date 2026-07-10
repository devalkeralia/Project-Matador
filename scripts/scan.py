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

PROD_BASE = "https://external-api.kalshi.com/trade-api/v2"


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


def cmd_dry_run(args) -> None:
    """Read-only: spread + top-of-book depth distribution across open markets (no model, no DB)."""
    cfg = load_config()
    with _client(cfg, args.demo) as client:
        for tour in args.tour:
            series = getattr(cfg.series, tour.lower(), None)
            if series is None:
                print(f"{tour.upper()}: no series configured")
                continue
            spreads, depths = [], []
            for event in client.get_events(series, status="open"):
                for m in client.get_markets(event_ticker=event["event_ticker"]):
                    ob = client.get_orderbook(m.ticker)
                    quotes = reconstruct_asks(ob)
                    sp = spread(quotes)
                    if sp is not None:
                        spreads.append(sp)
                    if quotes.yes_ask is not None:
                        depths.append(depth_at_ask(ob, "yes", quotes.yes_ask))
            print(f"=== {tour.upper()} ({len(depths)} markets with quotes) ===")
            if not depths:
                print("  no open markets with quotes")
                continue
            depths.sort()
            pct = lambda xs, p: xs[min(len(xs) - 1, int(p * len(xs)))]
            print(f"  depth   p10={pct(depths, .1):.0f}  median={statistics.median(depths):.0f}  p90={pct(depths, .9):.0f}")
            if spreads:  # a thin/one-sided book contributes depth but no spread
                spreads.sort()
                print(f"  spread  p10={pct(spreads, .1):.3f}  median={statistics.median(spreads):.3f}  p90={pct(spreads, .9):.3f}")
            else:
                print("  spread  n/a (no two-sided books)")


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
