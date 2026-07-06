from pathlib import Path

import pandas as pd

# Acquisition (not automated -- run once, refresh cadence is a Phase 2 concern):
#   git clone --depth 1 https://github.com/JeffSackmann/tennis_atp.git data/tennis_atp
#   git clone --depth 1 https://github.com/JeffSackmann/tennis_wta.git data/tennis_wta


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

    matches = pd.concat((pd.read_csv(path) for path in paths), ignore_index=True)
    matches["tourney_date"] = pd.to_datetime(matches["tourney_date"].astype(str), format="%Y%m%d")
    return matches
