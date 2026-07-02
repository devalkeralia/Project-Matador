# Research — Kalshi, Live-Score Feeds & Data Sources

_Compiled 2026-07-02. Preserves the web research behind the Betfair→Kalshi pivot. Starred
(\*) items must be re-verified at build time — Kalshi's API surface and fee schedule change._

---

## 1. Kalshi market mechanics & API

Kalshi is a **CFTC-regulated event-contracts exchange**. Contracts are **binary**, priced in
whole cents **1¢–99¢**, settling to **$1.00** (correct side) or **$0.00** (wrong side). The
price *is* the implied probability (54¢ ≈ 54%). Every market has a **Yes** and a
complementary **No** side (Yes at X¢ ⇔ No at (100−X)¢). Backing a player = **buy Yes**;
laying = **buy No**. You can exit early by selling at the live price.

**Auth.** RSA-PSS / SHA-256 request signing (no password/bearer flow). Generate a Key ID + RSA
private key in account settings (private key shown once). Every authed request sends:
`KALSHI-ACCESS-KEY` (Key ID), `KALSHI-ACCESS-TIMESTAMP` (ms), `KALSHI-ACCESS-SIGNATURE`. Sign
the string `timestamp + HTTP_METHOD + path`, where `path` **includes** `/trade-api/v2` and
**excludes** the query string. Public market data reads generally don't need auth; orders and
positions do.

**REST base\*:** `https://external-api.kalshi.com/trade-api/v2`
**Demo/paper base\*:** `https://external-api.demo.kalshi.co/trade-api/v2` (a paper environment
exists — test there first).

Key read endpoints:
- `GET /markets` — filters: `series_ticker` (needs `mve_filter=exclude`), `event_ticker`,
  `status` (`unopened|open|paused|closed|settled`), `tickers` (comma-sep); `limit` (max 1000)
  + `cursor`. Per-market fields include `ticker`, `event_ticker`, `yes_bid_dollars`,
  `yes_ask_dollars`, `no_bid_dollars`, `last_price_dollars`, `volume_fp`, `volume_24h_fp`,
  `open_interest_fp`, `liquidity_dollars`, `status`, `yes_sub_title`, `no_sub_title`.
  **There is no free-text/name search** — filter by ticker/metadata only.
- `GET /markets/{ticker}` — single market.
- `GET /markets/{ticker}/orderbook?depth=` — **returns bids only for each side**. In a binary
  market a *Yes ask* at X ⇔ a *No bid* at (1−X), so **reconstruct the Yes ask side from the No
  bids** (best Yes ask = 100¢ − best No bid).
- `GET /events`, `GET /events/{event_ticker}`, `GET /series/{series_ticker}`.
- Order placement (write path)\*: historically `POST /portfolio/orders` /
  `DELETE /portfolio/orders/{id}`; **migrating to `/portfolio/events/orders` ~June 2026** —
  confirm the live path before coding the order flow. (We don't auto-place orders, so this is
  only relevant if the bot ever reads positions/fills.)

**WebSocket:** `wss://external-api-ws.kalshi.com/trade-api/ws/v2` (demo
`wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2`); same RSA-signed headers on the
handshake. Public channels: **`orderbook_delta`** (initial `orderbook_snapshot` then
incremental deltas — maintain a local book), **`ticker`** (top-of-book + last trade),
**`trade`** (executions). Private: `fill`, `market_positions`, `user_orders`.

**Tickers — series → event → market.** Fields `series_ticker` / `event_ticker` / `ticker`.
Tennis series: **`KXATPMATCH`** (ATP singles match winner), the **WTA equivalent**, and
**`KXATPEXACTMATCH`** (exact set score). Event tickers encode date + surname abbreviations
(e.g. `KXATPMATCH-26MAR14ZVESIN`), **but Kalshi warns against parsing tickers**. To map
"Sinner vs Medvedev" → market: `GET /events?series_ticker=KXATPMATCH&status=open`, match on
the human-readable `title` / `yes_sub_title` / `no_sub_title` + date, then
`GET /markets?event_ticker=…` for the exact `ticker`. **`yes_sub_title` tells you which player
"Yes" pays out on** — this determines buy Yes vs buy No to back a given player.

> Field-naming note: 2026 responses use dollar-string fields (`yes_bid_dollars`,
> `last_price_dollars`) and fixed-point `_fp` quantities; older code assumed integer-cent
> fields. Verify field names/types at build.

---

## 2. Kalshi fees & edge impact

**Taker fee (per order):** `fee = roundup( 0.07 × C × P × (1 − P) )`, rounded **up to the next
cent**, where C = contracts, P = price in dollars. Per-contract that's `0.07 × P × (1−P)` — a
parabola peaking at P = 0.50.

| Price P | Fee/contract |
|--------|--------------|
| 0.50 | ~1.75¢ |
| 0.40 / 0.60 | ~1.68¢ |
| 0.30 / 0.70 | ~1.47¢ |
| 0.20 / 0.80 | ~1.12¢ |
| 0.10 / 0.90 | ~0.63¢ |
| 0.05 / 0.95 | ~0.33¢ |

- **Maker fee** = 25% of taker, only in markets that carry maker fees (resting orders are
  otherwise free). **INX/NASDAQ-100** markets use 0.035; **sports/tennis use the general
  0.07\***. No separate settlement fee in the current schedule.
- **Edge impact:** near 50/50, the ~1.75¢/contract entry fee eats **~34–57% of a 3–5¢ edge**
  on entry alone, doubled if you round-trip (buy then sell) instead of holding to settlement.
  Near the extremes it collapses (~0.3–0.9¢). **Implication: favorites (high prices) are far
  more fee-efficient; the bot should measure edge NET of fee and bias toward higher-priced
  contracts.** The round-up penalizes tiny orders — batch fills.

---

## 3. In-play viability verdict (the finding that reshaped the strategy)

**Do Kalshi tennis contracts trade continuously in-play? YES (high confidence).** Kalshi's
market-lifecycle docs show markets stay `active`/tradeable through play and close only when
the outcome is determined; the only interruptions are exchange-wide (Thu 03:00–05:00 ET
maintenance, rare pauses). No per-market freeze at match start. Wimbledon 2026 coverage
explicitly describes buying a match contract, watching a set develop, and selling at a profit
before the match ends.

**Is there usable in-play DEPTH? Thin / uncertain (medium-low confidence).** Liquidity
concentrates **pre-match and on Grand Slams / tour finals / marquee matches**; regular ATP/WTA
and Challenger markets are markedly thinner than the same match at a sportsbook. Launch-era
(Feb 2025) volume was ~$100k/week across *all* pro matches (stale, likely higher now); the
2026 Wimbledon *outright* winner market showed ~$7M cumulative (not per-match, not in-play).
**No public per-match in-play order-book-depth figure exists** — the biggest evidence gap.

**Does Kalshi's live price overreact (the mean-reversion premise)? Unverified — possibly not.**
Kalshi's in-play pricing tracks fast data feeds ("as fast as sportsbooks") and is set by an
order book, so it may already reprice a lost set efficiently rather than lag it — eroding the
exact inefficiency the strategy targets. App instability during live play was also reported.

**VERDICT:** In-play tennis on Kalshi is **mechanically viable but the in-play mean-reversion
strategy is MARGINAL** and likely **not scalable systematically** at current liquidity. Worth
**piloting small on deep Grand Slam matches** to measure real fills/spreads and to test
whether the price actually overreacts — not a reliable core edge. → Strategy reprioritized:
**pre-match model-vs-market value is primary; in-play is a liquidity-gated pilot.**

---

## 4. Live-score data feed (new requirement)

Kalshi provides **prices, not granular live scores** via its API, so the model needs a
separate feed for current match state (sets, games, current-game points, who's serving).
Truly official real-time point-by-point (Sportradar, Enetpulse) is enterprise-priced ($10k+/mo
or quote-only) and unnecessary here.

| Provider | Live granularity | Coverage | Price | Notes |
|---|---|---|---|---|
| **api-tennis.com** ✅ | Point-by-point (`event_serve`, `event_game_result`, per-set games, `pointbypoint`) | ATP/WTA/ITF + Slams | ~$40/mo, 14-day trial, 8k req/day | **Recommended.** Aggregator (derived, ~2–5s latency, no SLA); REST fits on-demand. |
| SportDevs | Live scores (verify point-level fields) | ATP/WTA | Free tier ~300 req/day | Good for free prototyping; confirm fields. |
| BetsAPI | In-play every 3–5s, point-level | Tennis inplay + odds | ~$1 trial, cheap monthly | Betting-oriented; handy if we also want odds. |
| GoalServe | Point-by-point ~every 5s | Slams/ATP/WTA/Challenger/ITF | $150/mo, 30-day trial | More reliable step-up. |
| Sportradar / Enetpulse / Entity | Official point-by-point | Full | Enterprise / quote ($10k+/mo) | Overkill. |
| SofaScore / Flashscore / LiveScore | (per-point on site) | — | — | **No official API; scraping violates ToS and is fragile — avoid.** |
| The Odds API | Odds only, **no score state** | — | — | Not usable for live state. |

**Constraint to note in design:** cheap aggregators are *derived/unofficial*, carry
**no latency SLA (~2–5s)**, and shouldn't be trusted for latency-sensitive live edges — fine
for on-demand state checks, not for beating a book on speed. **Recommendation: api-tennis.com**
(SportDevs free tier for prototyping).

---

## 5. Ultimate Tennis Statistics (UTS) assessment

**What it is:** the public face of an open-source project by Mileta Cekovic
(`github.com/mcekovic/tennis-crystal-ball`), itself built on **Jeff Sackmann's** open data.

**What it publishes (exactly the model's feature layer):** surface-specific **Elo**
(hard/clay/grass/carpet, indoor/outdoor, set/game, **service-game Elo**, **return-game Elo**,
tie-break Elo, peak Elo, last-52-week); **serve/return stats** (hold %, break %, BP
saved/converted, serve/return ratings, aces/DF, serve speed from 2021); **performance splits**;
**H2H**; **recent form**. It also ships a **neural-net win-probability predictor** (Tennis
Crystal Ball). It does **not** publish a fatigue/match-load metric — derive that yourself.

**Access:** **no API.** Options are (a) scrape the site — but it explicitly asks you **not to
crawl heavily** (runs on 1 CPU/2 GB); (b) a **frozen DB dump** (`mcekovic/uts-database` on
Docker Hub, data only through ~2019–2021 — historical only); (c) **self-host** the full stack
(PostgreSQL + Spring Boot + Groovy/Selenium loader).

**Licensing (important):** code is **Apache-2.0**, but the **data + derived Elo/prediction
outputs are CC BY-NC-SA 4.0 (NonCommercial + ShareAlike)** — and so is Sackmann's underlying
data. Personal real-money use is almost certainly fine; **a monetized/commercial product would
need legal review or a licensed commercial feed.** Switching from UTS to hand-rolled Sackmann
Elo does **not** escape the NC term (same base data).

**Recommendation:** **hand-roll surface Elo + serve/return from Sackmann's raw data** (fresh,
transparent, controllable, and it's the only way to get current ratings). Use **UTS as a design
reference** (mirror its feature set + surface-factor blending) and a **validation oracle**
(spot-check computed Elo/probabilities), with light, attributed reads only.
**Tennis Abstract** publishes Sackmann's own surface-Elo reports (a canonical reference without
scraping UTS); **`Tennismylife/TML-Database`** is a live-updated ATP mirror for same-day
results.

---

## 6. Build-time verification checklist (the starred items)

- [ ] Live base URLs — `external-api.kalshi.com` (REST) / `external-api-ws.kalshi.com` (WS) vs
  legacy `api.kalshi.com`.
- [ ] Order-placement endpoint path (post ~June 2026 migration to `/portfolio/events/orders`).
- [ ] Fee coefficient `0.07` + the maker-fee market list — re-pull the current fee-schedule PDF.
- [ ] Response field names/types (`*_dollars`, `*_fp`).
- [ ] Which player is the **Yes** side per tennis market (`yes_sub_title`).
- [ ] WebSocket channel names (watch for `_v2` variants).
- [ ] Live-score provider fields — confirm api-tennis.com `get_livescore` returns serving +
  current-game points as expected (and SportDevs' fields if used for prototyping).

---

## Sources

**Kalshi API / fees / mechanics**
- https://docs.kalshi.com/getting_started/api_keys
- https://docs.kalshi.com/api-reference/market/get-markets
- https://docs.kalshi.com/api-reference/market/get-market-orderbook
- https://docs.kalshi.com/getting_started/quick_start_market_data
- https://docs.kalshi.com/getting_started/terms
- https://docs.kalshi.com/websockets/websocket-connection
- https://docs.kalshi.com/websockets/orderbook-updates
- https://docs.kalshi.com/changelog
- https://help.kalshi.com/en/articles/13823854-kalshi-api
- https://help.kalshi.com/en/articles/13823805-fees
- https://kalshi.com/docs/kalshi-fee-schedule.pdf
- https://kalshi.com/markets/kxatpmatch

**Kalshi in-play / liquidity**
- https://docs.kalshi.com/getting_started/market_lifecycle
- https://docs.kalshi.com/getting_started/maintenance_and_pauses
- https://help.kalshi.com/en/articles/13823807-what-are-trading-hours
- https://nexteventhorizon.substack.com/p/now-you-can-bet-on-single-tennis-matches-kalshi
- https://lycheedata.com/guides/kalshi-volume
- https://www.si.com/prediction-markets/reviews/kalshi

**Live-score APIs**
- https://api-tennis.com/documentation
- https://www.sportdevs.com/tennis
- https://betsapi.com/c/tennis
- https://www.goalserve.com/en/sport-data-feeds/tennis-api/prices

**Ultimate Tennis Statistics / model data**
- https://www.ultimatetennisstatistics.com/about
- https://github.com/mcekovic/tennis-crystal-ball
- https://github.com/JeffSackmann/tennis_atp
- https://tennisabstract.com/reports/atp_elo_ratings.html
- https://github.com/Tennismylife/TML-Database
