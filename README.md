# Tennis Betting Alert Bot

A personal project to build a **tennis value-alert bot** for **Kalshi** (the CFTC-regulated
event-contracts exchange): it watches ATP/WTA top-tier matches, reprices them with a fair-value
model, and sends a **Telegram alert with a suggested stake (in contracts)** when a Kalshi
contract is mispriced beyond my threshold. **I place every trade manually** — the bot never
places orders.

## Status

**Phase 2 (the model) complete — 123 tests passing.** Surface-weighted match Elo → a per-tour,
per-format **fitted** logistic scale → calibrated `p_model` with an abstain gate; the calibration
harness (reliability curve / Brier / log-loss) beats coin-flip on held-out seasons (ATP Brier
0.2181, WTA 0.2176). The model artifact (`data/model.json`) is **per tour** (separate ATP/WTA
ratings + name index + scales). Phase 1 data plumbing (Kalshi client + RSA-PSS auth, match→ticker
resolution, name-resolution join, storage) is done. Next: **Phase 3** (edge + staking engine).
**v1 = pre-match value alerts only** (in-play mean-reversion pilot = v2).

_Last updated: 2026-07-07_

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
| [`CLAUDE.md`](./CLAUDE.md) | Auto-loaded by every session in this repo: scope, hard rules (secrets, no auto-bet), GitHub remote, settled design facts, and the Karpathy dev principles. |

## Recommended build setup

- **Mode:** start in **Plan mode** — the master prompt is plan-first and won't write code until
  you approve the Phase 1 plan. Review edits in normal mode for correctness-critical parts
  (Kalshi auth/signing, edge & staking math); auto-accept is fine for boilerplate.
- **Model:** **Opus 4.8** for planning + correctness-critical code; **Sonnet 5** for the routine
  implementation (Telegram handlers, config, SQLite, tests) to save cost. **Skip Fable 5** — overkill.
- **Effort:** `xhigh` for implementation (the coding sweet spot); `max` only when stuck.

## Next step

**Phase 3 — edge + staking engine:** net-of-fee edge + ¼-Kelly sizing (scaffolded in
`matador/edge.py`) + the liquidity gate + config. See `MASTER-PROMPT.md` Phase 3. Develop
against the **Kalshi demo environment** first. Note: validation to date shows no pre-match edge
vs sharp closing lines (Kalshi inconclusive), so Phase 3–6 build toward **forward CLV
paper-testing** — the real go-live test — not live money.

## Changelog

- **2026-07-07 — Validation harness + market-edge findings.** Added `matador/backtest.py` +
  `scripts/backtest_vs_bookmaker.py` / `backtest_vs_kalshi.py`. Finding: `p_model` does **not** beat
  the sharp bookmaker close (Brier-optimal blend weight 0 on it; −10.6% flat-stake ROI over ~5.8k
  held-out 2025–26 matches); vs Kalshi's own pre-match line the available sample is too small/noisy
  to conclude (inconclusive). **No pre-match edge demonstrated yet → do not bet real money;** the
  go-live bar is forward CLV paper-testing. See DESIGN-DECISIONS.md "Validation findings".
- **2026-07-07 — Phase 2 (the model) built + calibrated; 123 tests.** Surface-weighted match Elo
  → a **fitted per-format logistic scale** (per tour × best-of, fit by minimizing log-loss on the
  walk-forward train split; <200-sample formats fall back to 400): ATP Bo3≈526 / Bo5≈404, WTA
  Bo3≈475 (WTA Bo5 falls back). The fixed /400 now applies **only** to the Elo rating-update
  expectation, not `p_model`. Walk-forward calibration on the held-out last two seasons beats
  coin-flip: ATP Brier 0.2181 / log-loss 0.6244, WTA 0.2176 / 0.6232. The model artifact
  (`data/model.json`) is now **per tour** so an ATP name can't resolve to a WTA player. Added
  `matador/edge.py` (net-of-fee edge + ¼-Kelly scaffold) and **cold-start shrinkage** (`n0=10`;
  tempers thin-player overconfidence — n0=0 to be re-evaluated in Phase 6 via CLV). **Data-source swap:**
  Sackmann's `tennis_atp`/`tennis_wta` went private mid-2025 — both ATP and WTA now from
  **LuckyLoser91/TennisCourtLog** (live weekly; prep via `scripts/prepare_matches.py`);
  TML-Database kept as the v2 reference for real ids + serve stats.
- **2026-07-06 — Evaluated `Research/` material; adopted two Phase-2/6 refinements.** Reviewed the
  two "viable strategy" screenshots (Polymarket weather bots; YES+NO<$1 arb), the "AI trading desk"
  repo list, and `tennis_bot_spec.md` + `tennis_edge.py`. Verdicts: weather bots = architecture
  validation only; the arb is **not viable** on Kalshi (per-side fees exceed the gap that only
  works on fee-free Polymarket, and it needs the automated execution we've ruled out); none of the
  five repos are relevant (crypto/execution tooling — ccxt has no Kalshi; `rtk-ai/rtk` is a token
  compressor, not a trading framework). Kept `tennis_edge.py` as the **v2** serve/return Markov
  reference (`reference/tennis_edge.py` — not wired into v1) and folded two improvements into the
  docs: (1) **Shin de-vig** the tennis-data.co.uk proxy odds before the edge-vs-prices backtest;
  (2) **Brier / log-loss / reliability-curve** calibration acceptance for the Elo model.
- **2026-07-02 — Pre-build design review (multi-agent).** Ran an adversarial 5-lens review
  before building; **49 findings confirmed**. Fixed 2 must-fix contradictions inline (v1
  Elo→p_model logistic; name-resolution + abstain gate) plus core staking math (net-of-fee Kelly,
  stake/price caps, No-side, empty-book); captured the rest as phase-tagged action items in
  `MASTER-PROMPT.md`; full report archived in `PRE-BUILD-REVIEW.md`.
- **2026-07-02 — GitHub repo + tooling set up.** Created the private repo
  **devalkeralia/Project-Matador** and pushed all commits; consolidated all credentials into the
  gitignored `secrets/` dir (Kalshi key id + RSA key, GitHub PAT); installed & configured `gh`
  (auth + git credential helper, so pushes just work); added the **Karpathy development
  principles** and the GitHub remote to `CLAUDE.md`.
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
