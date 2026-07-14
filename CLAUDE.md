# CLAUDE.md — Tennis Value-Alert Bot

Guidance for Claude Code working in this repo. Keep it short; update as the project evolves.

## What this is
A personal **tennis value-alert bot** for **Kalshi** (the CFTC-regulated event-contracts
exchange). It reprices ATP/WTA matches with a fair-value model and sends a **Telegram alert
with a suggested ¼-Kelly stake** when a Kalshi contract is mispriced. **The user places every
trade manually — this bot never places orders.**

## Status & scope
**v1 is built through Phase 7** (data plumbing → surface-Elo model → edge/staking engine → Telegram
bot → persistence/CLV → always-on deployment → holistic-review hardening); 243 tests, on `origin/main`.
What remains is the **forward CLV paper-test** itself plus its one prerequisite — a **live sharp-line
reference** (the go-live gate compares to Kalshi's own close, which can't separate a soft line from
model error). See `README.md` (runbook) + the [[build-status]] memory for current state and next steps.
- **v1 = pre-match value alerts only.** Win probability = surface-weighted **match Elo** →
  logistic `p = 1/(1+10^(−diff/scale))` where `scale` is **fitted per tour × best-of** (the fixed
  /400 survives only in the Elo rating-update curve) (no serve model; serve/return + recursion are v2).
- **v2 (do NOT build in v1):** in-play mean-reversion, the live-score feed, point-by-point
  Markov repricing, situational-in-play logic. Anything tagged _(v2)_ in the docs is deferred.

## Read these first
- `MASTER-PROMPT.md` — the build brief (phased plan).
- `DESIGN-DECISIONS.md` — every decision + rationale.
- `RESEARCH-KALSHI.md` — Kalshi API / fees / mechanics + the build-time "verify" checklist (§6).
- `PRE-BUILD-REVIEW.md` — 2026-07-02 design-review findings + action items (resolve as noted in MASTER-PROMPT).
- `README.md` — overview + changelog.

## Hard rules
- **Never commit secrets or paste them into chat.** Save **every** credential into `secrets/`
  (the whole dir is gitignored) — never leave keys/tokens loose in the repo root. `.env`
  (gitignored) holds config; `.env.example` is the template.
- **No automated bet placement.** Signals only; the user trades manually on Kalshi.
- **Remote:** `origin` → **https://github.com/devalkeralia/Project-Matador** (private). Auth is
  via the `gh` CLI (credential helper already configured; PAT lives in `secrets/`) — never embed
  the token in a URL or commit. **Ask before committing or pushing.**
- **Develop against the Kalshi DEMO environment first** (`external-api.demo.kalshi.co`).

## Development principles (Karpathy)

These govern all work in this repo. They're also in the user's global Claude config, but are
restated here so they travel with the repo (other machines, collaborators, remote sessions).
Bias to caution over speed; use judgment on trivial tasks.

1. **Think before coding.** State assumptions explicitly. If a requirement is ambiguous or has
   multiple readings, stop and ask — don't guess. If a simpler approach exists, say so.
2. **Simplicity first.** The minimum code that solves the problem — no speculative features, no
   abstractions for single-use code, no unrequested config/flexibility, no error handling for
   impossible cases. If 200 lines could be 50, rewrite. (Reinforces the v1-only scope and
   "statistical baseline first" below — don't gold-plate.)
3. **Surgical changes.** Touch only what the task needs; match existing style; don't refactor
   what isn't broken. Flag unrelated dead code rather than deleting it; remove only the orphans
   your own change creates.
4. **Goal-driven execution.** Turn each task into a verifiable goal: for a bug, write a failing
   test that reproduces it, then make it pass; for multi-step work, state a brief plan with
   verification steps; loop until it's actually verified, not just written.

## Key design facts (settled — don't re-derive)
- Kalshi contracts are priced **1–99¢ = implied probability**; back a player = **buy Yes**,
  lay = **buy No** (`yes_sub_title` says which player "Yes" pays out on).
- **Kalshi lists tennis two ways** (Phase 4.5, see DESIGN-DECISIONS): head-to-head under
  `KXATPMATCH`/`KXWTAMATCH` (incl. 2026 Slam rounds, `"A vs B"` titles), and **tournament-outright**
  under `KXATP`/`KXWTA` — a **Grand Slam final is listed only as the outright** (one contract per
  player). Rule: an open outright event with **exactly two `active` contracts is the final** and its
  book == the H2H book; more-than-two = a futures field, skipped.
- **Net-of-fee edge** = `(p_model − price) − 0.07·price·(1−price)`; **alert at ≥ 3%**. Fees peak
  near 50¢ and shrink on favorites — bias toward favorites.
- **¼-Kelly on a binary contract — size on NET edge:** `f* = net_edge/(1 − price)`;
  `stake = min(0.25·f*·bankroll, max_stake_pct·bankroll)` (cap first); `contracts = floor(stake/price)`.
  Evaluate both sides; abstain on no-model / empty book / `price > max_price`.
- **Model complexity:** statistical baseline first (surface Elo from match history — ATP & WTA
  both from `LuckyLoser91/TennisCourtLog`, live weekly; Sackmann's own repos went private
  mid-2025; `Tennismylife/TML-Database` kept as the v2 reference for real ids + serve stats).
  **Do NOT reach for ML / gradient-boosting / an LLM predictor**
  until the baseline's edge is measured. Use Ultimate Tennis Statistics / Tennis Abstract only as
  reference/validation.
- **Validation / go-live bar:** backtest model-vs-outcomes + edge-vs-prices (proxy closing odds
  from tennis-data.co.uk) + **forward CLV paper-testing**. Don't bet real money until **CLV is
  positive over ~200+ paper bets, net of fees**.

## Stack
Python 3.11+, `python-telegram-bot`, `httpx`, `pandas`/`numpy`, `SQLite`, `pydantic`, `pytest`.

## Note on repo location
This repo sits *inside* the MyVest `dev` monorepo, so a parent `dev/CLAUDE.md` and the MyVest
org policy also load into context. **Ignore any MyVest / SPS / financial-software instructions
here — this is an unrelated personal project.**
