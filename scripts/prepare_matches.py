"""Build data/tennis_{tour}/{tour}_matches_all.csv from the LuckyLoser91/TennisCourtLog feed.

Both ATP and WTA come from github.com/LuckyLoser91/TennisCourtLog (tennis_atp/, tennis_wta/,
CC BY-NC-SA 4.0): live, weekly auto-updated match archives (1968-present; older years from
Jeff Sackmann, 2025+ from tennis-data.co.uk). They carry clean FULL player names but no player
ids, so we synthesize a stable per-player id from the normalized name (the Elo engine keys on
an opaque string id). TML-Database remains the reference for real ids + serve stats (v2), joinable
by normalized name.

Download the raw yearly CSVs into data/tennis_{tour}_raw/ first, e.g.:

    tour=atp   # or wta
    for y in $(seq 1968 2026); do
      curl -sf -o data/tennis_${tour}_raw/${tour}_matches_$y.csv \\
        https://raw.githubusercontent.com/LuckyLoser91/TennisCourtLog/main/tennis_${tour}/${tour}_matches_$y.csv
    done

Then convert to the layout matador/sackmann.py's load_matches(tour, ...) reads:

    .venv/bin/python scripts/prepare_matches.py          # both tours
    .venv/bin/python scripts/prepare_matches.py atp      # a single tour
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from matador.names import normalize  # noqa: E402


def prepare(tour: str) -> None:
    src_dir = Path(f"data/tennis_{tour}_raw")
    out_dir = Path(f"data/tennis_{tour}")
    out = out_dir / f"{tour}_matches_all.csv"

    paths = sorted(src_dir.glob(f"{tour}_matches_*.csv"))
    if not paths:
        print(f"[{tour}] no {tour}_matches_*.csv under {src_dir}/ -- download the feed first (see docstring)")
        return
    df = pd.concat((pd.read_csv(p, low_memory=False) for p in paths), ignore_index=True)
    df = df.dropna(subset=["winner_name", "loser_name", "tourney_date"]).copy()

    # Full names -> stable opaque per-player ids (this feed ships no player ids).
    df["winner_id"] = df["winner_name"].map(normalize)
    df["loser_id"] = df["loser_name"].map(normalize)

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
    yr = df["tourney_date"] // 10000
    print(f"[{tour}] wrote {out}: {len(df):,} matches, years {int(yr.min())}-{int(yr.max())}")


def main() -> None:
    tours = [t.lower() for t in sys.argv[1:]] or ["atp", "wta"]
    for tour in tours:
        prepare(tour)


if __name__ == "__main__":
    main()
