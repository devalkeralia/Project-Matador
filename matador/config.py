from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class SeriesConfig(BaseModel):
    atp: str = "KXATPMATCH"
    wta: str | None = None  # unconfirmed until scripts/probe.py discovers it
    # Tournament-winner (outright) series. Kalshi lists a Grand Slam final only here, not as an
    # H2H market; once the field is down to two, the outright collapses to a head-to-head.
    atp_outright: str | None = "KXATP"
    wta_outright: str | None = "KXWTA"


class EloConfig(BaseModel):
    """v1 surface-weighted match-Elo hyperparameters (see DESIGN-DECISIONS.md)."""

    initial_rating: float = 1500.0
    k_num: float = 250.0   # K-factor K = k_num / (n + k_shift)^k_pow (538/Sackmann decay)
    k_shift: float = 5.0
    k_pow: float = 0.4
    surface_weight: float = 0.3  # blend surface_weight*surface_elo + (1-w)*overall_elo; tuned on held-out log-loss (0.7 over-weighted the noisier per-surface elo)
    shrinkage_n0: float = 10.0  # cold-start shrinkage: thin ratings keep n/(n+n0) of their deviation from initial; n0=10 fit on held-out (removes ~75% of thin-favorite overconfidence at ~0.4% log-loss cost; larger n0 under-values breakouts)
    max_staleness_days: int = 365  # abstain if a player's ratings are older than this
    # The per-format logistic scales are FITTED per tour by scripts/build_ratings.py and
    # stored in data/model.json (not configured here).

    @field_validator("surface_weight")
    @classmethod
    def _weight_range(cls, v: float) -> float:
        if not (0 <= v <= 1):
            raise ValueError("surface_weight must be in [0, 1]")
        return v

    @field_validator("k_num")
    @classmethod
    def _k_num_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("k_num must be > 0")
        return v

    @field_validator("k_shift")
    @classmethod
    def _k_shift_positive(cls, v: float) -> float:
        if v <= 0:  # n + k_shift is the K-factor divisor at n=0; must stay > 0
            raise ValueError("k_shift must be > 0")
        return v

    @field_validator("k_pow", "max_staleness_days", "shrinkage_n0")
    @classmethod
    def _nonnegative(cls, v: float) -> float:
        if v < 0:
            raise ValueError("must be >= 0")
        return v


class Config(BaseModel):
    bankroll: float
    kelly_fraction: float = 0.25
    max_stake_pct: float = 0.05
    min_net_edge: float = 0.03
    min_matches: int = 20
    min_liquidity: float
    max_spread: float
    min_price: float | None = 0.10  # favorite floor: don't back a side priced below this (deep longshots -- unreliable Elo tails, ballooning contract counts). Tunable.
    max_price: float = 0.95
    fee_coefficient: float = 0.07
    adverse_gap: float = 0.15  # flag alerts whose net edge exceeds this for manual "recent news?" scrutiny (late injury/withdrawal the Elo can't see)
    thin_matches: int = 50          # a player with fewer prior matches is "thin" (overconfident Elo): ABSTAIN from alerting on them, and segment CLV/calibration by this. Set == min_matches to disable the thin-abstain.
    thin_kelly_haircut: float = 0.5  # (superseded: thin players now abstain rather than being haircut-and-alerted; kept for a possible future re-enable)
    paper_flat_stake: float | None = None  # if set, suggest this FLAT $ stake instead of Kelly (CLV is stake-independent; avoids priming Kelly-sized real bets on an unvalidated p_model). None = Kelly.
    min_effect_size: float = 0.015   # go-live gate: NET-CLV 95% CI lower bound must exceed this. ~1.5c, sized to cover real slippage + Kalshi's per-order round-up fee + the ask-vs-mid entry basis (a 0.5c bar was below those). Tunable.
    min_clv_clusters: int = 12       # go-live gate: require at least this many independent ISO-WEEK clusters (alongside >= 200 bets); weeks (not days) are the correlation unit
    max_missed_capture_rate: float = 0.30  # go-live gate: refuse if more than this fraction of closing-line captures were missed (a thin/biased sample)
    scan_interval_hours: float | None = None  # scheduled systematic /scan cadence in hours (removes owner-timing selection bias); None disables the timer
    scan_announce: bool = False      # DM the owner on every scheduled scan; default only DMs when an alert fires (avoids ping fatigue)
    heartbeat_hours: float | None = 24.0  # daily liveness DM (scans/pending/missed/exposure) so a silent outage is visible; None disables
    max_open_exposure_pct: float = 0.20  # warn when total open (unsettled) suggested stake exceeds this fraction of bankroll (correlated same-day alerts have no per-alert cap)
    # Sharp-line reference (the-odds-api -> Pinnacle) -- the binding go-live gate is "beat the sharp CLOSE".
    odds_api_key_path: str | None = "secrets/odds_api_key.txt"  # None (or a missing/empty file) disables the sharp track -> go-live can't pass (no real money without a sharp reference)
    odds_api_base_url: str = "https://api.the-odds-api.com/v4"
    odds_region: str = "eu"                 # Pinnacle is listed under the EU region
    sharp_consensus_fallback: bool = True   # when Pinnacle isn't quoting a covered match, use the median of the other EU books (tagged sharp_source='consensus') rather than starve the sample
    min_sharp_coverage: float = 0.5         # go-live gate: require this fraction of closed bets to have a sharp reference (else the sample is biased toward efficient big matches)
    tours: list[str] = Field(default_factory=lambda: ["ATP", "WTA"])
    series: SeriesConfig = Field(default_factory=SeriesConfig)
    elo: EloConfig = Field(default_factory=EloConfig)
    kalshi_base_url: str = "https://external-api.demo.kalshi.co/trade-api/v2"
    model_path: str = "data/model.json"   # artifact written by scripts/build_ratings.py
    db_path: str = "data/matador.db"       # SQLite opportunity/outcome log

    @field_validator("bankroll")
    @classmethod
    def _bankroll_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("bankroll must be > 0")
        return v

    @field_validator("kelly_fraction", "max_stake_pct", "thin_kelly_haircut", "max_open_exposure_pct")
    @classmethod
    def _fraction_range(cls, v: float) -> float:
        if not (0 < v <= 1):
            raise ValueError("must be in (0, 1]")
        return v

    @field_validator("min_net_edge", "min_matches", "min_liquidity", "max_spread", "fee_coefficient",
                     "adverse_gap", "thin_matches", "min_effect_size", "min_clv_clusters")
    @classmethod
    def _nonnegative(cls, v: float) -> float:
        if v < 0:
            raise ValueError("must be >= 0")
        return v

    @field_validator("max_missed_capture_rate", "min_sharp_coverage")
    @classmethod
    def _rate_range(cls, v: float) -> float:
        if not (0 <= v <= 1):
            raise ValueError("must be in [0, 1]")
        return v

    @field_validator("max_price")
    @classmethod
    def _max_price_range(cls, v: float) -> float:
        if not (0 < v < 1):
            raise ValueError("max_price must be in (0, 1)")
        return v

    @field_validator("scan_interval_hours", "heartbeat_hours", "paper_flat_stake")
    @classmethod
    def _positive_or_none(cls, v: float | None) -> float | None:
        if v is not None and v <= 0:  # a period/stake must be positive when set; omit (None) to disable
            raise ValueError("must be > 0 (or omit)")
        return v

    @model_validator(mode="after")
    def _min_price_below_max(self) -> "Config":
        if self.min_price is not None and not (0 < self.min_price < self.max_price):
            raise ValueError("min_price must be in (0, max_price)")
        return self


class Secrets(BaseSettings):
    model_config = SettingsConfigDict(env_file="secrets/.env", extra="ignore")

    kalshi_key_id: str
    kalshi_private_key_path: str
    telegram_token: str | None = None
    telegram_chat_id: str | None = None


def load_config(path: str | Path = "config.yaml") -> Config:
    raw = yaml.safe_load(Path(path).read_text())
    return Config.model_validate(raw or {})


def load_secrets() -> Secrets:
    return Secrets()
