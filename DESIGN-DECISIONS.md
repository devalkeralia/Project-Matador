# Design Decisions

_Last updated: 2026-07-02 · Phase: design/planning · Venue: **Kalshi** (pivoted from Betfair 2026-07-02)_

Record of every decision made during the design interview, with rationale. Update this file
as decisions change or new ones are made. See [`RESEARCH-KALSHI.md`](./RESEARCH-KALSHI.md) for
the sourced research behind the Kalshi-specific decisions.

## Decisions at a glance

| Decision | Choice |
|----------|--------|
| Edge (priority) | **Pre-match model-vs-market value (primary)** + in-play mean-reversion (**liquidity-gated pilot**) + situational reads |
| v1 scope | **Pre-match value alerts only**; in-play pilot + live-score feed = **v2** |
| Market | **Kalshi** — event-contracts exchange (buy Yes / buy No) |
| Data | Kalshi API (prices/book) + live-score feed (api-tennis.com) + Jeff Sackmann datasets (model) + UTS/Tennis Abstract (reference) |
| Coverage | ATP + WTA top events (Grand Slams + Masters/1000), **gated on live liquidity** |
| Cadence | On-demand (I trigger checks; no continuous polling) |
| Runtime | Always-on Telegram bot |
| Alerts | Telegram, ¼-Kelly stake, **net-of-fee edge ≥ 3%** |
| Tracking | SQLite log of opportunities + results + CLV |
| Model complexity | Statistical baseline first (surface Elo + Markov); ML/LLM staged & evidence-gated |
| Validation | Backtest model vs outcomes **and** edge vs historical prices (proxy odds); forward CLV paper-testing |
| Bankroll | $2,000 (placeholder — set in config; Kalshi is USD) |

> **Revisitable defaults:** strategy priority, coverage handling, and model-data sourcing were
> **defaulted to the recommended options** while away from keyboard. They're adopted but easy to
> flip — say the word and the docs update without a rewrite.

> **v1 scope (locked):** the first build is **pre-match value alerts only** — the backtestable
> edge, needing no paid live-score feed. In v1 the model's win probability comes from **surface
> Elo** directly; the point-by-point Markov repricing, the live-score feed, and all in-play +
> situational-in-play logic are **v2**.

## Rationale

**Market — Kalshi (replaces Betfair).** Kalshi is a CFTC-regulated **event-contracts**
exchange, not a betting exchange. Contracts are binary, priced 1¢–99¢, settling to $1.00/$0.00,
so **the price is the implied probability directly** (54¢ ≈ 54%) — cleaner than decimal odds
(no overround to strip). "Back a player" = **buy Yes**; "lay" = **buy No**. Trades are placed
manually on Kalshi; the bot only signals.

**Edge — priority reordered because of Kalshi's in-play reality.** Research (see RESEARCH doc §3)
found that while Kalshi tennis markets *do* trade continuously in-play, **in-play liquidity is
thin and concentrated on Grand Slams**, and Kalshi's live price tracks fast feeds so it **may
not overreact** the way sportsbook/exchange lines do — the exact inefficiency in-play
mean-reversion needs. So:
- **Primary: pre-match model-vs-market value** — compare the model's win probability to the
  pre-match contract price on liquid markets. Robust on Kalshi.
- **Pilot: in-play mean-reversion** — keep it, but only on deep (Grand Slam / marquee) matches,
  gated by live liquidity, and validate empirically that the price actually overreacts before
  trusting it.
- **Situational reads** (fatigue, etc.) refine or flag either path.

**Coverage — broad, gated on live liquidity.** Watch ATP/WTA top events (Grand Slams +
Masters/1000), but **only alert when the market's live depth/spread clears a threshold** —
Kalshi's regular-tour markets are often too thin to trade, and a liquidity gate avoids alerts
on untradeable markets while keeping reach broad.

**Fees replace commission — and they favor favorites.** Kalshi taker fee ≈
`0.07 × price × (1−price)` per contract, peaking ~1.75¢ at 50¢ and collapsing to ~0.3–0.9¢ at
0.85–0.95. Near 50/50 this eats a large chunk of a 3–5% edge; on favorites it's negligible.
So the bot measures edge **net of fees** and biases toward higher-priced (favorite) contracts.

**Staking — ¼-Kelly (adapted to contracts).** Conservative; standard for value betting.
Alert threshold: **net edge ≥ 3%** (moderate).

**Cadence — on-demand.** No continuous API polling; I ask the bot to check a match (or scan
liquid top-tier matches) when I want. Optional per-match watch for hands-off triggering.

**Runtime — always-on Telegram bot.** Listens for commands (`/check`, `/scan`); best fit for
"ask it to look at the current score." Small VPS or always-on machine.

**Tracking — SQLite + CLV.** Log every opportunity, price taken vs closing price, hit rate,
P&L. **Closing-line value is the primary proof the edge is real** — doubly important now that
the flagship in-play edge is unproven on Kalshi.

## Fair-value model approach (market-agnostic — carried over unchanged)

1. **Pre-match (v1):** surface-weighted **match Elo** per player (from Sackmann history) →
   match-win probability **directly** via the logistic `p = 1/(1 + 10^((Elo_opp − Elo_self)/400))`;
   no serve model in v1. (Steps 2–3 — serve/return Elo + game→set→match recursion — are the
   **v2** in-play mechanism.)
2. **In-play:** point-by-point Markov model → `P(win match | current score)` from the current
   points/games/sets/server, via standard game→set→match recursion. The current score comes
   from the **live-score feed** (Kalshi does not expose granular score via API).
3. **Live blending:** update serve-point-win probabilities from actual hold performance so far
   this match, shrunk toward the pre-match prior (prior dominates early).
4. **Edge (contract semantics):**
   - `price` = Yes ask in dollars (reconstructed = `1 − best No bid`), used directly as the
     market-implied probability.
   - **Net edge** = `(p_model − price) − 0.07 × price × (1 − price)`.
   - Alert when **net edge ≥ 3%** AND live liquidity/spread clears the gate AND (for the pilot)
     the in-play mean-reversion + situational filters agree.
5. **Staking (¼-Kelly on a binary contract):** size on the **net-of-fee** edge —
   `f* = net_edge / (1 − price)`; `stake = min(¼ × f* × bankroll, max_stake_pct × bankroll)`
   (cap first); `contracts = floor(stake / price)`. Evaluate both sides (No side:
   `price = no_ask = 1 − best Yes bid`, `p = 1 − p_model`); skip an empty book or `price > max_price`.
   - Worked example: p_model 0.60, Yes ask 0.54, bankroll $2,000 → net edge ≈ 4.3% →
     f* = 0.0426/0.46 = 0.093 → ¼-Kelly 0.023 → stake ≈ $46 → 85 contracts (fee ≈ $1.48).
     At price 0.90 the fee term is ~0.6%, so the same gross edge survives far better — the
     favorite bias in action. (Sizing on gross edge would over-stake ~28%.)

## Model complexity — statistical baseline first; ML/LLM staged and evidence-gated

**Decision:** start with the transparent statistical model (surface-weighted Elo →
point-by-point Markov). Do **not** open with gradient-boosted trees or a neural net, and do
**not** use an LLM as the core win-probability predictor.

**Why:**
- **The target is beating the Kalshi price, not raw accuracy.** The market already prices in
  ranking, surface, form, H2H, and obvious playstyle matchups; a kitchen-sink model tends to
  *reconstruct the price* and find no edge. Edge exists only where the model **systematically
  disagrees** with the price and is right — unknowable until edge is measured against a baseline.
- **Transparency.** When the model disagrees with the price you must tell **edge from bug**;
  Elo is inspectable, a neural net isn't.
- **Overfitting.** ~tens of thousands of tour matches is a small dataset; complex models find
  spurious patterns that die live.
- **Kalshi is a thin, retail market** — likely soft enough that the Elo baseline surfaces
  inefficiency without ML; if it's efficient, ML won't beat it either. Baseline first either way.

**Signal notes on "all those variables":** surface form ✓ (Elo core), fatigue ✓ (situational),
serve/return ✓ (serve/return Elo). **H2H** is weaker than intuition suggests (small, stale
samples; Elo captures most) — a small adjustment at most. **Playstyle matchups** are appealing
but hard to quantify and largely priced in — an overfitting trap, not a day-one feature.

**Staged path (each stage gated on evidence):**
1. Elo + Markov baseline + the measurement harness.
2. Measure CLV vs the closing line, net of fees.
3. *Only if* near break-even: add features one at a time via logistic regression / gradient
   boosting with **time-aware** cross-validation; each earns its place by improving
   **out-of-sample CLV**, not backtest accuracy.
4. *Optional, later, narrow:* an LLM to parse **unstructured** signals (injury news, withdrawal
   risk, weather) as a **veto/flag**, never the core predictor.

UTS's Tennis Crystal Ball (~60-feature NN) is a **benchmark** to validate against — and a
reminder that a good predictor still isn't automatically market-beating.

## Validation & backtesting

**Two distinct tests — don't conflate them:**
1. **Model calibration** — replay historical matches using only pre-match data (no lookahead);
   compare model probabilities to actual results. Needs **results only** (Sackmann). Necessary
   but **not** sufficient.
2. **Edge / strategy** — replay history and ask *"what would the bot have flagged (model vs
   price), and would those bets have made money net of fees?"* Needs historical **prices**, not
   just results. **This is what proves we're reading the discrepancy correctly.**

> **The trap:** an accurate model that *agrees* with the price has zero edge. Always validate
> against **prices** (did the bets beat the close / win net of fees), never just "did the model
> pick the winner."

**Data reality on Kalshi:** tennis launched ~2025, so Kalshi price history is shallow. Handle
with two complementary approaches:
- **Proxy-market backtest (for depth):** historical **closing odds** from tennis-data.co.uk
  (Pinnacle/Bet365, ~20 yrs). Test whether the model beats a market's closing line over
  thousands of matches. Kalshi ≠ Pinnacle, so it's a proxy — but if the model can't beat
  Pinnacle's close it won't beat anything; if it can, that's promising.
- **Forward CLV paper-testing (Kalshi-specific):** run the bot **log-only** against live Kalshi
  prices — record would-be bets + price, then compare to the **closing price** (CLV) and the
  outcome. The honest Kalshi validation; already supported by the SQLite `outcomes`/CLV schema
  + Phase 6.

**CLV is the north star:** beating the closing line is the leading indicator of real edge, well
before P&L is statistically significant.

**Go-live bar:** don't bet real money until **CLV is convincingly positive over ~200+ forward
paper (log-only) bets, net of fees.**

**In-play caveat:** pre-match value **is** backtestable (historical closing odds exist); the
**in-play mean-reversion pilot is not** (it would need historical in-play tick paths, which
nobody publishes for Kalshi). Validate the pilot only by **capturing your own live in-play data
going forward** and paper-testing — another reason it's a pilot, not the core.

## Data sources (four layers)

1. **Kalshi API** — contract prices, order book, liquidity (the market being judged). RSA-signed
   auth; REST + WebSocket. Order book returns bids only (reconstruct the ask side). Map a match
   to its market by pulling the series' (`KXATPMATCH` / WTA) open events and matching on the
   title — no name-search endpoint. See RESEARCH doc §1.
2. **Live-score feed (NEW)** — current point/game/set state + server for the Markov model, since
   Kalshi provides prices, not granular scores. **Recommended: api-tennis.com** (~$40/mo; serving
   + current-game points + point-by-point; REST fits on-demand). Alternatives: SportDevs (free
   tier for prototyping), BetsAPI, GoalServe. Note: cheap feeds are derived, ~2–5s latency, no
   SLA — fine for on-demand checks, not latency-sensitive edges.
3. **Jeff Sackmann datasets** — model inputs (surface Elo, serve/return rates). Consider
   `Tennismylife/TML-Database` for same-day results.
4. **Ultimate Tennis Statistics + Tennis Abstract** — **reference/validation only** (no API;
   scrape-averse; NonCommercial license). Use to mirror UTS's feature design and sanity-check the
   hand-rolled Elo/probabilities. Tennis Abstract publishes Sackmann's own surface-Elo reports.

## Open questions (resolve during build)

- **In-play depth (top empirical unknown).** No public per-match in-play order-book-depth figure
  exists for Kalshi tennis. Pilot with small size on deep Grand Slam matches to measure real
  fills/spreads, and test whether the live price actually overreacts after a lost set.
- **Live-score provider.** Confirm api-tennis.com's `get_livescore` exposes serving +
  current-game points as needed (SportDevs' free tier for prototyping — verify its fields).
- **Kalshi tennis coverage breadth.** Confirm which events/tours Kalshi actually lists at any
  given time (it skews to Slams/marquee) — drives the liquidity gate's practical reach.
- **Licensing for any commercial use.** Sackmann data + UTS outputs are NonCommercial — personal
  real-money use is fine; a monetized product would need legal review / a licensed feed.
- **Kalshi API keys.** Account exists; **generate the RSA key pair** in Kalshi settings before
  any API calls (develop against the demo env first). Prerequisite, not a design blocker.
- **Bankroll figure.** Placeholder $2,000 — set the real figure in config.
- **Build-time API verification.** Base URLs, order endpoint, fee coefficient, field names — see
  RESEARCH doc §6.

## Non-goals / guardrails

- **No automated bet placement — ever.** Signals only; I execute manually on Kalshi.
- Always compute edge **net of Kalshi fees**; bias toward fee-efficient (favorite) prices.
- No continuous API polling — on-demand only.
- Responsible-gambling posture: hard stake cap, no alert without positive net edge, honest
  CLV/P&L tracking.

## Interview record

The design was elicited through a structured interview. Answers given:

- **Edge / how to spot mispricing:** In-play mean-reversion, Model vs market, Situational angles
  — later **reprioritized** (model-vs-market primary, in-play a pilot) once Kalshi's in-play
  liquidity was researched.
- **Market:** Betfair → **switched to Kalshi** (user places trades on Kalshi).
- **Data feed:** None yet → recommended stack: Kalshi API + api-tennis.com live scores + Sackmann
  + UTS/Tennis Abstract reference.
- **Coverage:** ATP, WTA, top events only → refined to **liquidity-gated** broad coverage.
- **Runtime:** Always-on Telegram bot.
- **Tracking:** Log opportunities, results & CLV.
- **Staking aggression:** ¼-Kelly.
- **Alert edge threshold:** ≥ 3% (now measured **net of fees**).
- **Bankroll:** not specified → $2,000 placeholder.
- **Strategy / coverage / model-data:** defaulted to recommended options (away from keyboard) —
  flagged as revisitable.
