from dataclasses import dataclass


def _to_float(value: str | None) -> float | None:
    return float(value) if value not in (None, "") else None


@dataclass(frozen=True)
class Market:
    """A single Kalshi tennis market -- one player's "will they win" contract.

    Kalshi lists two mirrored markets per match (one per player), each with its own
    yes_bid/yes_ask/no_bid/no_ask already populated -- no reconstruction needed for
    top-of-book quotes. reconstruct_asks (in client.py) is for the raw orderbook, which
    only reports resting bids per side.
    """

    ticker: str
    event_ticker: str
    status: str
    yes_bid: float | None
    yes_ask: float | None
    no_bid: float | None
    no_ask: float | None
    last_price: float | None
    yes_sub_title: str
    no_sub_title: str
    liquidity: float | None
    volume: float | None
    open_interest: float | None
    occurrence_datetime: str | None

    @classmethod
    def from_api(cls, raw: dict) -> "Market":
        return cls(
            ticker=raw["ticker"],
            event_ticker=raw["event_ticker"],
            status=raw["status"],
            yes_bid=_to_float(raw.get("yes_bid_dollars")),
            yes_ask=_to_float(raw.get("yes_ask_dollars")),
            no_bid=_to_float(raw.get("no_bid_dollars")),
            no_ask=_to_float(raw.get("no_ask_dollars")),
            last_price=_to_float(raw.get("last_price_dollars")),
            yes_sub_title=raw.get("yes_sub_title", ""),
            no_sub_title=raw.get("no_sub_title", ""),
            liquidity=_to_float(raw.get("liquidity_dollars")),
            volume=_to_float(raw.get("volume_fp")),
            open_interest=_to_float(raw.get("open_interest_fp")),
            occurrence_datetime=raw.get("occurrence_datetime") or None,
        )
