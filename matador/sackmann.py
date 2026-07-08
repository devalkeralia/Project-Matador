from pathlib import Path

import pandas as pd

# Data sources (Jeff Sackmann's tennis_atp/tennis_wta repos went private mid-2025):
#   ATP & WTA -> LuckyLoser91/TennisCourtLog (tennis_atp/, tennis_wta/): live, weekly
#          auto-updated, 1968-2026, full names (ids synthesized from the normalized name).
#          scripts/prepare_matches.py writes data/tennis_{tour}/{tour}_matches_all.csv.
#   TML-Database (data/tml_atp/) is kept as the v2 reference for real ids + serve stats
#          (join by normalized name); it froze at 2026-01, so it is NOT the live ATP source.
# Both land as {tour}_matches_*.csv below; this loader is source-agnostic.


def load_matches(tour: str, data_dir: str | Path, years: list[int] | None = None) -> pd.DataFrame:
    """Load Sackmann's {tour}_matches_{year}.csv files from {data_dir}/tennis_{tour}/.

    Concatenates the requested years (or all available years, if none given) into one
    DataFrame with tourney_date parsed as a real date. Purely local/network-free -- the
    CSVs must already be present.
    """
    tour_dir = Path(data_dir) / f"tennis_{tour}"

    if years is not None:
        paths = [tour_dir / f"{tour}_matches_{year}.csv" for year in years]
        missing = [p for p in paths if not p.exists()]
        if missing:
            raise FileNotFoundError(f"missing Sackmann files: {missing}")
    else:
        paths = sorted(tour_dir.glob(f"{tour}_matches_*.csv"))
        if not paths:
            raise FileNotFoundError(f"no {tour}_matches_*.csv files found under {tour_dir}")

    matches = pd.concat((pd.read_csv(path, low_memory=False) for path in paths), ignore_index=True)
    matches["tourney_date"] = pd.to_datetime(matches["tourney_date"].astype(str), format="%Y%m%d")
    return matches
