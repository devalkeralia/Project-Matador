# Tennis Betting Alert Bot

A personal project to build a **tennis value-alert bot** for **Kalshi** (the CFTC-regulated
event-contracts exchange): it watches ATP/WTA top-tier matches, reprices them with a fair-value
model, and sends a **Telegram alert with a suggested stake (in contracts)** when a Kalshi
contract is mispriced beyond my threshold. **I place every trade manually** — the bot never
places orders.

## Status

**Phase: Design / planning.** No code written yet. Decisions locked, master prompt ready.
**v1 = pre-match value alerts only** (in-play mean-reversion pilot = v2).

_Last updated: 2026-07-02_

## What this is

An *alerting/signal* system, not an auto-trader. The bot compares the model's win probability
to the Kalshi contract price and flags mispricings. Edge priority:

1. **Primary — pre-match model-vs-market value** on liquid markets.
2. **Pilot — in-play mean-reversion** (a favorite drops a set and the live price overreacts),
   kept as a liquidity-gated experiment because Kalshi's in-play liquidity is thin and its live
   price may not overreact like a sportsbook's (see `RESEARCH-KALSHI.md`).
3. Situational reads (fatigue, etc.) refine either path.

I trade the signal manually on Kalshi.

## Documents

| File | What it is |
|------|------------|
| [`MASTER-PROMPT.md`](./MASTER-PROMPT.md) | The build brief — paste as the first message of a fresh Claude Code session to build the app. |
| [`DESIGN-DECISIONS.md`](./DESIGN-DECISIONS.md) | Every decision + rationale; the four-layer data stack; the fair-value model + contract math; open questions; interview record. |
| [`RESEARCH-KALSHI.md`](./RESEARCH-KALSHI.md) | Sourced research: Kalshi API/fees/mechanics, the in-play viability verdict, live-score API comparison, and the UTS assessment — with URLs and a build-time verify checklist. |

## Recommended build setup

- **Model:** Claude Opus 4.8 (`claude-opus-4-8`).
- **Effort:** `max` for planning/design, drop to `xhigh` once implementing code.

## Next step

Start the build: open a fresh session, paste `MASTER-PROMPT.md`, and have Claude draft the
**Phase 1** plan (data plumbing) for approval before any code is written. Develop against the
**Kalshi demo environment** first.

## Changelog

- **2026-07-02 — v1 scope set.** First build = **pre-match value alerts only** (no paid
  live-score feed); in-play mean-reversion pilot deferred to **v2**. Go-live bar: positive CLV
  over ~200+ paper bets. (Kalshi account exists; RSA keys still to generate.)
- **2026-07-02 — Model & validation approach recorded.** Added two decisions to
  `DESIGN-DECISIONS.md`: (1) **model complexity** — statistical baseline (Elo + Markov) first,
  ML/LLM staged and evidence-gated; (2) **validation & backtesting** — model-vs-outcomes plus
  edge-vs-prices (proxy closing odds from tennis-data.co.uk), forward CLV paper-testing, and the
  in-play-not-backtestable caveat.
- **2026-07-02 — Venue pivot: Betfair → Kalshi.** Reworked the design for Kalshi's
  event-contract mechanics (buy Yes/No, price = implied probability, per-contract fees).
  Research found in-play mean-reversion is *marginal* on Kalshi (thin in-play liquidity + a live
  price that may not overreact), so **pre-match model-vs-market value is now the primary edge**
  and in-play is a liquidity-gated pilot. Added a **live-score feed** (api-tennis.com) as a new
  data source (Kalshi gives prices, not granular scores) and folded in **Ultimate Tennis
  Statistics** as a model reference. Strategy/coverage/model-data decisions were defaulted to the
  recommended options (away from keyboard) — flagged as revisitable in `DESIGN-DECISIONS.md`.
- **2026-07-02 — Initial design.** Interview + first master prompt (originally Betfair-based).
