# Master Prompt — Tennis Value-Alert Bot (Kalshi)

> **How to use:** paste everything below the line as the opening message of a fresh Claude
> Code session in the project directory. It's a build brief — Claude will plan, then implement
> in phases. Fill the `<<...>>` placeholders first. Build on Opus 4.8; use `xhigh` effort for
> implementation. See `RESEARCH-KALSHI.md` in this folder for the sourced facts behind the
> Kalshi mechanics, fees, and data-source choices.

---

## Role & objective
You are building a **tennis betting *alert* bot** (not an auto-trader) for **Kalshi**, the
CFTC-regulated event-contracts exchange. It watches ATP/WTA top-tier matches, reprices them
with a fair-value model, and sends me a **Telegram alert with a suggested stake** when a
contract is mispriced beyond my threshold. **I place every trade manually on Kalshi.** The bot
never places orders.

**Edge priority:**
1. **Primary — pre-match model-vs-market value:** the model's win probability vs the pre-match
   contract price on liquid markets.
2. **Pilot — in-play mean-reversion:** a favorite drops a set / gets broken and the live price
   overreacts. On Kalshi this is *liquidity-constrained and unproven* (thin in-play depth; the
   live price may already reprice efficiently), so treat it as an opportunistic pilot on deep
   Grand Slam / marquee matches, gated by liquidity, and validate empirically before trusting it.
3. Situational reads (fatigue, etc.) refine or flag either path.

**v1 scope:** build **pre-match value alerts only**. In v1 the model's win probability comes from
**surface Elo** directly (the point-by-point Markov repricing is the *in-play* mechanism). The
**live-score feed (data source #2) and every in-play step are v2 — skip them in v1**. Items
tagged _(v2)_ below are deferred.

## Kalshi contract basics (know these before coding)
- Binary contracts priced **1¢–99¢**, settling to **$1.00 / $0.00**. **Price = implied
  probability** (54¢ ≈ 54%) — no overround to strip.
- Backing a player = **buy Yes**; laying = **buy No**. `yes_sub_title` on the market says which
  player "Yes" pays out on — use it to pick the side.
- Fees (taker) ≈ `0.07 × contracts × price × (1−price)`, peaking ~1.75¢/contract at 50¢ and
  collapsing to ~0.3–0.9¢ on favorites. **Always compute edge net of fees; bias toward
  fee-efficient favorite prices.**

## User flow (on-demand — do NOT poll continuously)
1. I message the bot, e.g. `/check Sinner v Medvedev` or `/scan` (liquid live/upcoming top-tier
   matches).
2. The bot resolves the match to its **Kalshi market ticker** (pull the series' open events,
   match on title — no name search; don't parse tickers) and fetches **price + order book +
   liquidity** from Kalshi.
3. For in-play, it fetches the **current score** from the live-score feed (Kalshi doesn't expose
   granular score).
4. It reprices with the fair-value model, computes **net-of-fee edge**, checks the **liquidity
   gate**, and (for the in-play pilot) applies mean-reversion + situational filters.
5. If it qualifies, it replies with a formatted alert: side (buy Yes/No), price, net edge, and a
   ¼-Kelly stake in **contracts**.
6. It logs the opportunity to SQLite. I trade manually and can later record the fill + result.

## Tech stack
- **Python 3.11+**, `python-telegram-bot` (long-polling), `httpx`/`requests`, `cryptography`
  (RSA-PSS signing for Kalshi auth), `pandas`, `numpy`, `SQLite` (`sqlalchemy` or stdlib),
  `pydantic` for config, `pytest`.
- Runs as an **always-on process** on a small VPS. Config via `config.yaml` + `.env` for secrets.

## Data sources
1. **Kalshi API** — contract prices, order book, liquidity (the market being judged).
   - Auth: RSA-PSS/SHA-256 signing; headers `KALSHI-ACCESS-KEY` / `-TIMESTAMP` (ms) /
     `-SIGNATURE`; sign `timestamp+METHOD+path` (path includes `/trade-api/v2`, excludes query).
   - REST base `https://external-api.kalshi.com/trade-api/v2` (demo `external-api.demo.kalshi.co`
     — build/test against demo first). WebSocket `wss://external-api-ws.kalshi.com/trade-api/ws/v2`
     (channels `orderbook_delta`, `ticker`, `trade`) for live price following if needed.
   - Endpoints: `GET /markets` (filter `series_ticker=KXATPMATCH`+`mve_filter=exclude`,
     `event_ticker`, `status`, `tickers`), `GET /markets/{ticker}`,
     `GET /markets/{ticker}/orderbook`. **The order book returns bids only per side —
     reconstruct the Yes ask as `1 − best No bid`.**
   - Tennis series: `KXATPMATCH` (ATP match winner), WTA equivalent, `KXATPEXACTMATCH` (set
     score). Map a match by pulling open events and matching `title`/`yes_sub_title`.
2. **Live-score feed** _(v2 — skip in v1)_ — current point/game/set state + server for the Markov model.
   Recommended **api-tennis.com** (`get_livescore`: `event_serve`, `event_game_result`, per-set
   games, `pointbypoint`). Cheap/derived, ~2–5s latency — fine for on-demand checks.
3. **Jeff Sackmann datasets** (`tennis_atp`, `tennis_wta`; optionally `TML-Database` for same-day
   results) — model inputs (surface Elo, serve/return rates). Load once, refresh periodically.
4. **Ultimate Tennis Statistics + Tennis Abstract** — **reference/validation only** (no API;
   NonCommercial license). Mirror UTS's feature design; sanity-check computed Elo/probabilities.

## Fair-value model (the "true" probability)
1. **Pre-match:** surface-weighted Elo per player from Sackmann history → per-player
   serve-point-win probability on the match surface.
2. **In-play** _(v2)_**:** point-by-point Markov model → `P(player wins match | current score)` from the
   current points/games/sets/server (from the live-score feed) via the standard recursive
   game→set→match formulas.
3. **Live blending** _(v2)_**:** update serve-point-win probabilities from actual hold performance so far,
   shrunk toward the pre-match prior (prior dominates early).
4. Output: `p_model` = model win probability for the player in question.

## Edge & trigger logic
- `price` = the side's ask in dollars (Yes ask reconstructed = `1 − best No bid`), used directly
  as the market-implied probability.
- **Net edge** = `(p_model − price) − 0.07 × price × (1 − price)`.
- Evaluate both sides (buy Yes to back the `yes_sub_title` player; buy No for the opponent) and
  take the side with positive net edge, if any.
- **Alert if:** `net_edge ≥ min_net_edge` (default 3%) AND the **liquidity gate** passes
  (`liquidity_dollars`/book depth ≥ `min_liquidity` and spread ≤ `max_spread`) AND
  `price ≥ min_price` (favorite bias, optional) AND — for the **in-play pilot only** —
  mean-reversion (candidate recently lost price ground: dropped a set / broken) + situational
  filters agree.

## Staking (¼-Kelly on a binary contract)
- `f* = (p_model − price) / (1 − price)`
- `stake = kelly_fraction × f* × bankroll` (default `kelly_fraction = 0.25`)
- `contracts = floor(stake / price)`; cap `stake` at `max_stake_pct` of bankroll.
- No alert if `f* ≤ 0` or net edge fails.

## Telegram commands
- `/check <player A> v <player B>` — resolve to the Kalshi market ticker and evaluate now.
- `/scan` — evaluate liquid live/upcoming ATP/WTA top-tier markets; alert on any that qualify.
- `/watch <match>` *(optional, later)* — auto-alert if an in-play mean-reversion trigger fires on
  that market (low-frequency polling only while watching).
- `/result <opp_id> <win|loss> <fill_price> [contracts]` — record how a trade went.
- `/stats` — recent opportunities, hit rate, P&L, and **closing-line value (CLV)**.
- `/settings` — view/update thresholds and bankroll.

Alert format (example):
```
🎾 VALUE ALERT — ATP · Wimbledon
Sinner vs Medvedev (R2) · pre-match
BUY YES "Sinner wins" @ 54¢  (Kalshi KXATPMATCH-…, ~$3.1k ≤55¢)
Model 60.0% | Market 54¢ | Net edge +4.3% (after fee)
Stake $65 → 120 contracts (¼-Kelly, bankroll $2,000)
opp #1043
```
(In-play pilot alerts add a trigger note, e.g. `· in-play: lost set 1`.)

## Persistence (SQLite)
- `opportunities`: id, ts, tour, event, match, market_ticker, side (yes/no), price, p_model,
  net_edge, suggested_stake, contracts, liquidity, trigger_reason (prematch_value /
  inplay_meanrev / situational), score_state.
- `outcomes`: opp_id (FK), fill_price, contracts_filled, closing_price, result, pnl, **clv**.
- CLV = did I beat the closing price? Primary proof the edge is real — especially important
  since the in-play edge is unproven on Kalshi.

## Config (`config.yaml`)
`bankroll`, `kelly_fraction: 0.25`, `min_net_edge: 0.03`, `min_liquidity`, `max_spread`,
`min_price` (favorite bias, optional), `fee_coefficient: 0.07`, `tours: [ATP, WTA]`,
`event_tiers: [GrandSlam, Masters1000]`, `series: {atp: KXATPMATCH, wta: <WTA ticker>}`.
Secrets in `.env` (gitignored; template in `.env.example`): `KALSHI_KEY_ID`,
`KALSHI_PRIVATE_KEY_PATH` (path to the gitignored `.pem`), `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`.
`LIVESCORE_API_KEY` is **v2 only** (not needed for v1).

## Guardrails / non-goals
- **No automated order placement — ever.** Signals only; I execute manually on Kalshi.
- Always compute edge **net of Kalshi fees**; prefer fee-efficient favorite prices.
- Do **not** poll continuously — on-demand `/check`/`/scan` only, plus optional per-match
  `/watch`.
- Responsible-gambling posture: hard stake cap, no alert without positive net edge, honest
  CLV/P&L tracking.

## Build in phases — plan first, confirm, then implement each
1. **Data plumbing:** Kalshi auth (RSA signing) + market/order-book fetch + match→ticker
   mapping; live-score client _(v2)_; load Sackmann data; storage layer. Use the **Kalshi demo
   environment** for development.
2. **Model:** surface Elo + serve model + point-by-point in-play win probability — **the
   statistical baseline; do NOT reach for gradient-boosting/NN or an LLM predictor** until the
   baseline is measured (see DESIGN-DECISIONS "Model complexity"). **Validate against historical
   matches** (and spot-check vs UTS/Tennis Abstract) before trusting it.
3. **Edge + staking engine** (net-of-fee edge, liquidity gate, binary-contract Kelly) + config.
4. **Telegram bot:** commands, ticker resolution, alert formatting, on-demand flow.
5. **Persistence + CLV/stats** tracking.
6. **Backtest / paper-trade** *before* real money (see DESIGN-DECISIONS "Validation &
   backtesting"): (a) **model vs outcomes** on Sackmann history (calibration, no lookahead);
   (b) **edge vs prices** — replay against historical closing odds (tennis-data.co.uk as a
   proxy, since Kalshi's own price history is shallow) to check the discrepancy is real edge net
   of fees; (c) **forward CLV paper-testing** log-only against live Kalshi prices (the
   Kalshi-specific proof). The **in-play pilot is not backtestable** (no historical in-play tick
   paths) — validate it only by capturing your own live data going forward. **Go-live gate:
positive CLV over ~200+ paper bets, net of fees, before real money.**

## Verify at build (Kalshi's surface changes — confirm these)
- Live base URLs (`external-api.kalshi.com` vs legacy `api.kalshi.com`).
- Order-placement endpoint path (migrating ~June 2026) — only if reading positions/fills.
- Fee coefficient `0.07` + maker-fee market list (re-pull the current fee-schedule PDF).
- Response field names/types (`*_dollars`, `*_fp`).
- Which player is the **Yes** side per market (`yes_sub_title`).
- Live-score provider fields (api-tennis.com `get_livescore` serving + current-game points).

Start by restating your understanding, listing assumptions/risks, and proposing the phase-1 file
structure. Do not write code until I approve the plan.

## Placeholders to fill before use
- `<<bankroll>>` — real bankroll figure (default $2,000).
- `<<Kalshi Key ID + RSA private key>>` — from Kalshi account settings (demo first).
- `<<live-score API key>>` — api-tennis.com (or chosen provider).
- `<<telegram token + chat id>>` — from BotFather.
- `<<min_liquidity / max_spread / min_price>>` — your liquidity gate + favorite bias.
- `<<WTA series ticker>>` — confirm from Kalshi.
