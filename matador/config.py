from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class SeriesConfig(BaseModel):
    atp: str = "KXATPMATCH"
    wta: str | None = None  # unconfirmed until scripts/probe.py discovers it


class Config(BaseModel):
    bankroll: float
    kelly_fraction: float = 0.25
    max_stake_pct: float = 0.05
    min_net_edge: float = 0.03
    min_matches: int = 20
    min_liquidity: float
    max_spread: float
    min_price: float | None = None
    max_price: float = 0.95
    fee_coefficient: float = 0.07
    tours: list[str] = Field(default_factory=lambda: ["ATP", "WTA"])
    event_tiers: list[str] = Field(default_factory=lambda: ["GrandSlam", "Masters1000"])
    series: SeriesConfig = Field(default_factory=SeriesConfig)
    kalshi_base_url: str = "https://external-api.demo.kalshi.co/trade-api/v2"

    @field_validator("bankroll")
    @classmethod
    def _bankroll_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("bankroll must be > 0")
        return v

    @field_validator("kelly_fraction", "max_stake_pct")
    @classmethod
    def _fraction_range(cls, v: float) -> float:
        if not (0 < v <= 1):
            raise ValueError("must be in (0, 1]")
        return v

    @field_validator("min_net_edge", "min_matches", "min_liquidity", "max_spread", "fee_coefficient")
    @classmethod
    def _nonnegative(cls, v: float) -> float:
        if v < 0:
            raise ValueError("must be >= 0")
        return v

    @field_validator("max_price")
    @classmethod
    def _max_price_range(cls, v: float) -> float:
        if not (0 < v < 1):
            raise ValueError("max_price must be in (0, 1)")
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
