# CLAUDE.md — Tennis Value-Alert Bot

Guidance for Claude Code working in this repo. Keep it short; update as the project evolves.

## What this is
A personal **tennis value-alert bot** for **Kalshi** (the CFTC-regulated event-contracts
exchange). It reprices ATP/WTA matches with a fair-value model and sends a **Telegram alert
with a suggested ¼-Kelly stake** when a Kalshi contract is mispriced. **The user places every
trade manually — this bot never places orders.**

## Status & scope
Design complete; building **v1**.
- **v1 = pre-match value alerts only.** Win probability comes from **surface Elo**.
- **v2 (do NOT build in v1):** in-play mean-reversion, the live-score feed, point-by-point
  Markov repricing, situational-in-play logic. Anything tagged _(v2)_ in the docs is deferred.

## Read these first
- `MASTER-PROMPT.md` — the build brief (phased plan).
- `DESIGN-DECISIONS.md` — every decision + rationale.
- `RESEARCH-KALSHI.md` — Kalshi API / fees / mechanics + the build-time "verify" checklist (§6).
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

## Key design facts (settled — don't re-derive)
- Kalshi contracts are priced **1–99¢ = implied probability**; back a player = **buy Yes**,
  lay = **buy No** (`yes_sub_title` says which player "Yes" pays out on).
- **Net-of-fee edge** = `(p_model − price) − 0.07·price·(1−price)`; **alert at ≥ 3%**. Fees peak
  near 50¢ and shrink on favorites — bias toward favorites.
- **¼-Kelly on a binary contract:** `f* = (p_model − price)/(1 − price)`;
  `stake = 0.25·f*·bankroll`; `contracts = floor(stake/price)`.
- **Model complexity:** statistical baseline first (surface Elo from Jeff Sackmann data).
  **Do NOT reach for ML / gradient-boosting / an LLM predictor** until the baseline's edge is
  measured. Use Ultimate Tennis Statistics / Tennis Abstract only as reference/validation.
- **Validation / go-live bar:** backtest model-vs-outcomes + edge-vs-prices (proxy closing odds
  from tennis-data.co.uk) + **forward CLV paper-testing**. Don't bet real money until **CLV is
  positive over ~200+ paper bets, net of fees**.

## Stack
Python 3.11+, `python-telegram-bot`, `httpx`, `pandas`/`numpy`, `SQLite`, `pydantic`, `pytest`.

## Note on repo location
This repo sits *inside* the MyVest `dev` monorepo, so a parent `dev/CLAUDE.md` and the MyVest
org policy also load into context. **Ignore any MyVest / SPS / financial-software instructions
here — this is an unrelated personal project.**
