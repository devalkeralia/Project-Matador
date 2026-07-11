# Tennis Betting Alert Bot

A personal project to build a **tennis value-alert bot** for **Kalshi** (the CFTC-regulated
event-contracts exchange): it watches ATP/WTA top-tier matches, reprices them with a fair-value
model, and sends a **Telegram alert with a suggested stake (in contracts)** when a Kalshi
contract is mispriced beyond my threshold. **I place every trade manually** — the bot never
places orders.

## Status

**Phase 4 + 4.5 (Telegram value-alert bot, with Grand Slam markets) built — 189 tests passing.** An always-on bot
(`matador/bot.py` + `scripts/bot.py`, `python-telegram-bot`) that long-polls Telegram and, on
`/check`/`/scan`, runs the Phase-3 engine against live Kalshi (read-only) and replies with a
formatted **VALUE ALERT** + ¼-Kelly stake — or a **self-explaining no-value breakdown** (prices,
model probability, per-side edge math) — logging qualifying paper opportunities for CLV. `/find`
lists open matches (one per line) with the checkable ones ranked by model strength; `/notes`
explains how to read a message; also `/recent` and `/help`. On-demand only (never polls Kalshi on
a timer); owner-chat-gated; **never places orders**. Phases 1–3 (data plumbing; per-tour surface-Elo model →
fitted logistic → calibrated `p_model`, ATP Brier 0.2181 / WTA 0.2176; net-of-fee edge + ¼-Kelly
staking engine) are done. Next: **Phase 5** (persist outcomes + CLV/stats), building toward
**forward CLV paper-testing** (the go-live bar). **v1 = pre-match value alerts only** (in-play
mean-reversion pilot = v2).

_Last updated: 2026-07-10_

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

**Phase 5 — persistence + CLV/stats:** add `/result` + `/stats` and the closing-line-value
pipeline (`record_outcome` + the stored `occurrence_datetime`/`event_ticker` are the hooks), then
run **forward CLV paper-testing** — the real go-live test, not live money. Before relying on the
liquidity gate, calibrate `min_liquidity`/`max_spread` with `scan.py dry-run` on a liquid slate —
the current placeholders were set from a thin field. To run the bot: put `TELEGRAM_TOKEN` +
`TELEGRAM_CHAT_ID` in `secrets/.env`, then `.venv/bin/python scripts/bot.py`. See
[`DESIGN-DECISIONS.md`](./DESIGN-DECISIONS.md) **"Open items & deferred work"** for the full
list of deferred features, monitored gaps, and the validation gate.

## Changelog

- **2026-07-10 — Phase 4.5 (Grand Slam markets, /find, /notes, self-explaining /check); 189 tests.**
  Additions on top of Phase 4. (0) **`/find [atp|wta]`** — lists open matches (H2H + outright
  finals, one per line) and ranks the model-priceable ones by Elo strength (a rankings proxy; we
  don't ingest official rankings), so you can see what's checkable at a glance (`engine.list_open_matches`
  + `alerts.format_find`). **`/notes`** — a how-to-read-the-messages guide (linked as a footnote on
  every `/check` reply). (1) **Grand Slam final support:** Kalshi lists a Slam final only as a tournament-
  **outright** market (series `KXATP`/`KXWTA`, one contract per player — "will X win the title"),
  not a head-to-head; once the field is down to two, the outright collapses to an H2H, so
  `client.resolve_outright_final` + `engine.scan_outright_finals` price it. Gate: an open outright
  event with **exactly two contracts still `active`** is the final; a full-field futures market
  (many active) is skipped. `/check` falls back to the outright series when there's no H2H market;
  `/scan` sweeps finals too. (Per-round Slam matches were already handled — 2026 uses `A vs B`
  titles under `KXATPMATCH`; the 2025 `"X or Y advances?"` format is a documented, monitored gap.)
  (3) **Self-explaining /check:** when a market was priced but no alert fired, the reply now walks
  through the reasoning — market prices (+ implied %), order-book depth, the model's probability, and
  the per-side edge math (gap → fee → net edge vs the +3% bar) ending in a plain-English verdict (via
  `engine.Diagnostics` + `alerts.format_no_alert`). Verified live against **both** Wimbledon 2026
  finals (ATP Sinner–Zverev, WTA Muchova–Noskova), which also **confirms** the outright series
  `KXATP`/`KXWTA`.
- **2026-07-10 — Phase 4 (Telegram value-alert bot) built; 174 tests.** `matador/bot.py`
  (PTB-free testable sync cores — `run_check`/`run_scan`/`run_recent` + pure helpers — wrapped by
  thin async handlers that offload the blocking engine via `asyncio.to_thread`) + `scripts/bot.py`
  launcher: long-polls Telegram for `/check`, `/scan`, `/recent`, `/help` (`/start` alias), runs
  the Phase-3 engine against live Kalshi (read-only; production by default, `--demo` toggle), and
  replies with a formatted VALUE ALERT + ¼-Kelly stake (or a friendly abstain), logging qualifying
  **paper** opportunities (deduped). `matador/alerts.py` holds the pure formatters. Added a
  `market_player` field (engine + storage) so alerts read `BUY YES "Sinner wins"` and the log is
  self-describing. Owner-chat-gated (`filters.Chat` + in-handler check); on-demand only (polls
  Telegram, never Kalshi on a timer); **never places orders** (no signer, no order endpoint
  reachable). Added `python-telegram-bot>=21.9` — httpx stays 0.28.x. `/result`+`/stats`+CLV
  deferred to Phase 5. (Supersedes the parked plan `PHASE-4-PLAN.md`, now removed.)
- **2026-07-10 — Phase 3 (edge + staking engine) built; 152 tests.** `matador/engine.py` wires
  resolve → `p_model` → net-of-fee edge → ¼-Kelly stake → liquidity/spread gate → deduped
  `prematch_value` opportunity (reusing `edge.py` + `Model.predict`); `matador/tournament.py`
  derives surface/best-of from Kalshi `product_metadata.competition`; `scripts/scan.py` adds
  `check`/`scan`/`dry-run` reading Kalshi **production** market data (read-only, retry/backoff on
  429). Paper only — never places orders. The opportunities log carries CLV fields (event_ticker,
  occurrence_datetime, flagged) that Phase 5/6 needs to fetch closing lines. Verified live:
  correctly resolves markets and abstains on unmodellable players; alert/log path + model
  orientation unit-tested (pre-commit fan-out review applied).
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
