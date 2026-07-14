"""Build data/tennis_{tour}/{tour}_matches_all.csv from the LuckyLoser91/TennisCourtLog feed.

Both ATP and WTA come from github.com/LuckyLoser91/TennisCourtLog (tennis_atp/, tennis_wta/,
CC BY-NC-SA 4.0): live, weekly auto-updated match archives (1968-present; older years from
Jeff Sackmann, 2025+ from tennis-data.co.uk). They carry clean FULL player names but no player
ids, so we synthesize a stable per-player id from the normalized name (the Elo engine keys on
an opaque string id). TML-Database remains the reference for real ids + serve stats (v2).

    .venv/bin/python scripts/prepare_matches.py --fetch      # pull the latest feed, then build both tours
    .venv/bin/python scripts/prepare_matches.py              # rebuild from already-downloaded raw CSVs
    .venv/bin/python scripts/prepare_matches.py atp --fetch  # a single tour

The weekly model-refresh cron MUST pass --fetch (the whole point of LuckyLoser91 over the now-private
Sackmann repos is that it updates weekly); without it the model freezes on whatever's already on disk.
"""
import argparse
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import pandas as pd  # noqa: E402

from matador.names import normalize  # noqa: E402

RAW_URL = "https://raw.githubusercontent.com/LuckyLoser91/TennisCourtLog/main/tennis_{tour}/{tour}_matches_{year}.csv"


def _player_id(name: str) -> str:
    """Stable opaque per-player id from the full name. Folds spaces/hyphens/apostrophes/commas to a
    single '_' so spelling variants (e.g. 'Roger Vasselin' vs 'Roger-Vasselin') map to ONE id -- a
    plain normalize() kept the hyphen and split one player across two diluted Elo entities."""
    return re.sub(r"[\s'\-,]+", "_", normalize(name)).strip("_")


def fetch(tour: str, year_from: int, year_to: int) -> int:
    """Download the raw yearly CSVs from LuckyLoser91 into data/tennis_{tour}_raw/ (overwriting on
    success; a failed year leaves the existing file). Returns the count fetched."""
    src_dir = Path(f"data/tennis_{tour}_raw")
    src_dir.mkdir(parents=True, exist_ok=True)
    got = 0
    with httpx.Client(timeout=40.0, follow_redirects=True) as client:
        for year in range(year_from, year_to + 1):
            url = RAW_URL.format(tour=tour, year=year)
            for _ in range(3):
                try:
                    r = client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                    if r.status_code == 404:
                        break  # that year isn't published yet
                    if r.status_code == 200 and r.content:
                        (src_dir / f"{tour}_matches_{year}.csv").write_bytes(r.content)
                        got += 1
                        break
                except httpx.HTTPError:
                    pass
    print(f"[{tour}] fetched {got} year file(s) from LuckyLoser91")
    return got


def prepare(tour: str) -> None:
    src_dir = Path(f"data/tennis_{tour}_raw")
    out_dir = Path(f"data/tennis_{tour}")
    out = out_dir / f"{tour}_matches_all.csv"

    paths = sorted(src_dir.glob(f"{tour}_matches_*.csv"))
    if not paths:
        print(f"[{tour}] no {tour}_matches_*.csv under {src_dir}/ -- run with --fetch first")
        return
    df = pd.concat((pd.read_csv(p, low_memory=False) for p in paths), ignore_index=True)
    df = df.dropna(subset=["winner_name", "loser_name", "tourney_date"]).copy()

    # Full names -> stable opaque per-player ids (this feed ships no player ids).
    df["winner_id"] = df["winner_name"].map(_player_id)
    df["loser_id"] = df["loser_name"].map(_player_id)

    # tourney_date is ISO 'YYYY-MM-DD' (older, Sackmann-origin) or 'YYYY/M/D' (2025+,
    # tennis-data.co.uk). Parse ISO first, fall back to a general year-first parse, then emit
    # the YYYYMMDD int load_matches expects. Drop any unparseable rows.
    raw = df["tourney_date"].astype(str).str.strip()
    parsed = pd.to_datetime(raw, format="%Y-%m-%d", errors="coerce")
    rest = parsed.isna()
    parsed[rest] = pd.to_datetime(raw[rest], errors="coerce")
    df["tourney_date"] = parsed
    df = df.dropna(subset=["tourney_date"]).copy()
    df["tourney_date"] = df["tourney_date"].dt.strftime("%Y%m%d").astype(int)

    keep = ["tourney_date", "tourney_name", "surface", "round", "best_of",
            "winner_id", "winner_name", "loser_id", "loser_name", "score"]
    df = df[keep]

    out_dir.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    max_date = int(df["tourney_date"].max())
    yr = df["tourney_date"] // 10000
    # print the MAX date prominently so the cron log shows the feed actually advanced week-over-week
    print(f"[{tour}] wrote {out}: {len(df):,} matches, years {int(yr.min())}-{int(yr.max())}, latest {max_date}")


def main() -> None:
    p = argparse.ArgumentParser(description="Build match archives from the LuckyLoser91 feed")
    p.add_argument("tours", nargs="*", help="atp and/or wta (default: both)")
    p.add_argument("--fetch", action="store_true", help="download the latest raw CSVs before building")
    p.add_argument("--from", dest="year_from", type=int, default=1968)
    p.add_argument("--to", dest="year_to", type=int, default=date.today().year)
    args = p.parse_args()
    tours = [t.lower() for t in args.tours] or ["atp", "wta"]
    for tour in tours:
        if args.fetch:
            fetch(tour, args.year_from, args.year_to)
        prepare(tour)


if __name__ == "__main__":
    main()
