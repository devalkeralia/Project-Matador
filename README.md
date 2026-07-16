# Tennis Betting Alert Bot

A personal project to build a **tennis value-alert bot** for **Kalshi** (the CFTC-regulated
event-contracts exchange): it watches ATP/WTA top-tier matches, reprices them with a fair-value
model, and sends a **Telegram alert with a suggested stake (in contracts)** when a Kalshi
contract is mispriced beyond my threshold. **I place every trade manually** — the bot never
places orders.

## Status

**v1 is built end-to-end (Phases 1–7) and twice review-hardened — 266 tests passing, on `origin/main`.** An always-on bot
(`matador/bot.py` + `scripts/bot.py`, `python-telegram-bot`) that long-polls Telegram and, on
`/check`/`/scan`, runs the Phase-3 engine against live Kalshi (read-only) and replies with a
formatted **VALUE ALERT** + ¼-Kelly stake — or a **self-explaining no-value breakdown** (prices,
model probability, per-side edge math) — logging qualifying paper opportunities. `/find` lists open
matches ranked by model strength; **`/close`** captures the closing line (manual + auto-scheduled at
match start), **`/result`** records outcomes, **`/stats`** reports hit rate, P&L, and **closing-line
value with a cluster-bootstrap 95% CI — the go-live metric**; also `/recent`, `/notes`, `/help`.
On-demand only for `/check`; owner-chat-gated; **never places orders**. Phases 1–3
(data plumbing; per-tour surface-Elo model → fitted logistic → calibrated `p_model`, ATP Brier 0.2175
/ WTA 0.2165; net-of-fee edge + ¼-Kelly staking engine) are done. **Phase 6 infrastructure + a
holistic review-hardening pass + the sharp-line reference (Fable-5 review-hardened) are built:** always-on Docker
deployment, a scheduled systematic scan (unbiased sampling), a postponement-aware **fail-closed**
closing-line capture, an offline `clv_report.py`, and a **hardened go-live gate** now bound to
**beating the SHARP (Pinnacle, via the-odds-api) closing line** — the de-circularized edge test —
with ISO-week BCa CI, realized-ROI + capture-health + sharp-coverage co-gates, thin-player abstain,
and flat paper stakes. What remains is the paper run itself (live-verified at the August Masters when
odds post). **v1 = pre-match value alerts only** (in-play mean-reversion pilot = v2).

_Last updated: 2026-07-16_

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

The infrastructure, the hardened go-live gate, and the **sharp-line reference** are all built
(Phases 1–7). What remains is the **forward CLV paper-test itself** — run the bot through live
tournaments and accumulate the sample, then read the go-live gate (now bound to beating Pinnacle's
close). See the **Phase-6 forward-CLV paper-test runbook** below for the protocol + the August-Masters
live-verification, and [`DESIGN-DECISIONS.md`](./DESIGN-DECISIONS.md) **"Open items & deferred work"**
for deferred model levers and monitored gaps. To run the bot: put `TELEGRAM_TOKEN` + `TELEGRAM_CHAT_ID`
in `secrets/.env` and the odds-api key in `secrets/odds_api_key.txt`, then
`.venv/bin/python scripts/bot.py` (or `docker compose up -d`).

## Run as a service

The forward-CLV paper-test needs the bot running **unattended for weeks** so the scheduled scan and
auto-capture accumulate a sample. Run it with Docker Compose (paper only — it still never places
orders):

```bash
mkdir -p data logs                 # pre-create the writable mounts so they're owned by YOU, not root
docker compose up -d --build       # start (production Kalshi reads, read-only)
docker compose logs -f matador     # follow logs (also written to logs/matador.log, rotated)
docker compose down                # stop
```

> **Create `data/` and `logs/` first** (the `mkdir` above). If you let Docker auto-create a missing
> bind-mount source it makes it **root-owned**, and the non-root (uid 1000) container can't write it —
> the DB write fails (and file logging falls back to console-only). On a box where your user isn't uid
> 1000, run as yourself instead: uncomment `user:` in `docker-compose.yml` and start with
> `UID=$(id -u) GID=$(id -g) docker compose up -d`.

**Mount contract — nothing sensitive is baked into the image.** The Dockerfile copies only source
(`matador/`, `scripts/`, `reference/`) and `pip install`s the package; `.dockerignore` keeps
secrets/config/data out of the build context. At runtime `docker-compose.yml` bind-mounts:

| Host path | In container | Mode | Holds |
|-----------|-------------|------|-------|
| `secrets/` | `/app/secrets` | read-only | Kalshi key + `TELEGRAM_TOKEN`/`TELEGRAM_CHAT_ID` |
| `config.yaml` | `/app/config.yaml` | read-only | bankroll + gate thresholds |
| `data/` | `/app/data` | writable | `model.json` + the SQLite opportunity/outcome log |
| `logs/` | `/app/logs` | writable | rotating `matador.log` |

`restart: unless-stopped` brings the bot back after a crash or host reboot; PTB reconnects its
long-poll on transient network drops (health check: send `/help` and confirm a reply). No custom
watchdog in v1.

**Weekly model refresh** (LuckyLoser91 updates weekly) — a host cron; a **restart is safe** because
pending closing-line captures are rebuilt from the DB on startup, so none are lost:

```bash
# crontab -e  (Mondays 06:00) — run from the repo root. --fetch PULLS the latest LuckyLoser91 feed
# (without it the cron just re-processes stale CSVs and the model freezes for the whole test).
0 6 * * 1  cd /path/to/Tennis\ Betting && .venv/bin/python scripts/prepare_matches.py --fetch && \
           .venv/bin/python scripts/build_ratings.py && docker compose restart matador
```

**No-Docker alternative (systemd):** create `/etc/systemd/system/matador.service` with
`WorkingDirectory=/path/to/Tennis Betting`, `ExecStart=/path/to/.venv/bin/python scripts/bot.py`,
`Restart=always`, `User=<you>`, then `systemctl enable --now matador`. Same weekly-refresh cron, with
`systemctl restart matador` instead of `docker compose restart`.

## Phase-6 forward-CLV paper-test runbook

The binding go-live gate. **Start at the August Masters main draw** (Toronto/Cincinnati) — liquid
markets where the gate thresholds are meaningful.

> **The gate is bound to beating the SHARP close** (Pinnacle via the-odds-api, Shin-devigged),
> de-circularizing it — CLV vs Kalshi's *own* close can't separate "Kalshi was soft (real edge)" from
> "our model was wrong and the close corrected away from us". The sharp fair prob is captured alongside
> the Kalshi mid at close; go-live binds on the sharp CLV track. **Live-verify at the August Masters**
> when odds post: confirm the exact `tennis_*` slugs via `GET /v4/sports`, dry-run `sharp_fair_prob` on
> a few live opps (a yes- AND a no-side), watch `sharp_coverage` in `/stats`, and add `names.ALIASES`
> for any systematic name misses.

1. **Recalibrate the liquidity gate first:** `.venv/bin/python scripts/scan.py dry-run --tour atp --tour wta`
   (read-only). It prints the depth/spread distribution per tour **and per tier** (H2H + outright
   finals). Set `min_liquidity`/`max_spread` in `config.yaml` from the observed **liquid**
   (Slam/Masters) distribution — the current values are interim, from a thin post-Wimbledon slate.
   During the paper-test **err loose** (you're measuring, not trading — don't starve the sample).
2. **Run it unattended** (see "Run as a service" for the one-time `mkdir -p data logs` + `docker
   compose up -d --build`). The scheduled scan (`scan_interval_hours: 8`)
   sweeps on a timer so the sample isn't biased by when *you* happen to check; the auto-capture
   snapshots each match's closing line a few minutes before start (postponement-aware). You get a DM
   only when an alert fires.
3. **Record outcomes** with `/result <opp_id> <win|loss> <fill> [contracts]` as matches settle.
4. **Read the verdict** weekly: `/stats` (with a `Captures: N auto / N manual / N missed` health line)
   and `.venv/bin/python scripts/clv_report.py` (segments net CLV by tour / price band / flag / week).
   - **Watch:** capture health (a high `missed` count = a thin/biased sample — investigate before
     trusting the number); `n` → 200; day-clusters → 30; the net-CLV 95% CI lower bound.
   - **MET** (`/stats` shows *Go-live gate: ✅ MET*): the **SHARP** CLV (entry vs Pinnacle's close),
     net-of-fee, ISO-**week**-clustered **BCa** 95% CI lower bound `> min_effect_size` (0.015), `≥ 200`
     sharp-referenced bets, `≥ 12` week-clusters, **sharp-coverage ≥ 50%**, **realized net-ROI ≥ 0**,
     **missed-capture ≤ 30%**. (Kalshi-close CLV is informational.) Only then consider real money.
   - **Flat** (enough sample, CI straddles/below the effect size): the model has no Kalshi edge. The
     first documented lever is **recency / time-decayed Elo** (see DESIGN-DECISIONS "Open items") —
     *measure first; don't build it pre-emptively.*

## Changelog

- **2026-07-16 — P7-E review-hardening (independent Fable-5 review → fixes); 266 tests.** A fresh-model
  adversarial review found the sharp *math* sound but 6 capture-path/gate-composition defects; fixed all
  before the (unbackfillable) paper run:
  - **Gate is now Pinnacle-only** — a soft-book `consensus` reference is captured + reported but can no
    longer satisfy go-live (it was pooled into the binding CI, a partial re-circularization).
  - **Too-early capture guard** — a batch `/close` no longer snapshots a far-future match's line as its
    "close" (it skips rows > 60 min from start, leaving them pending); the binding metric keeps its
    late-information content.
  - **Idempotent, non-destructive capture** — an already-captured row is never re-written (a re-fired
    auto job can't overwrite a good `sharp_close` with NULL on a transient odds-API hiccup; a late
    re-`/close` can't relabel a clean row "missed").
  - **Sharp attempted even on a thin Kalshi book** (recorded `sharp_only`), so a one-sided Kalshi book
    no longer censors an independently-available Pinnacle line and bias the sample upward.
  - **Batch robustness** — a null team name can't kill a whole board's sharp refs; sharp fetch failures
    are negative-cached so a rate-limited API can't cascade rows past the capture window.
  - **Visibility** — `_sharp_client` warns (not silent) when the key is missing/unreadable; the daily
    heartbeat reports sharp coverage + pinnacle/consensus counts.
- **2026-07-16 — Sharp-line reference (de-circularizes the go-live gate); 257 tests.** The gate no
  longer measures CLV vs Kalshi's *own* close (circular — can't tell a soft line from model error);
  it now gates on **beating the SHARP closing line** (Pinnacle via [the-odds-api](https://the-odds-api.com),
  Shin-devigged). New `matador/sharp.py` (`SharpOddsClient` + a pure `sharp_fair_prob` reusing
  `devig_shin` + the `surname_key(canonical_key(...))` pair-match idiom); the auto/manual close-capture
  now records the sharp fair prob (`sharp_close`/`sharp_source`) alongside the Kalshi mid, best-effort
  (a sharp miss never disturbs the Kalshi capture). `clv.summarize` runs a parallel **sharp CLV** track
  and binds `go_live` on it — sharp-CLV BCa CI lower bound `> min_effect_size`, ≥ 200 sharp-referenced
  bets, ≥ 12 weeks, **sharp-coverage ≥ 50%**, realized net-ROI ≥ 0, missed-capture ≤ 30% — with the
  Kalshi-close CLV demoted to informational. Pinnacle-first with a **consensus-median fallback**
  (config `sharp_consensus_fallback`); coverage ≈ our liquid universe (Slams/Masters/500s), so an
  uncovered/unmatched match simply has no sharp ref (fail-safe). Persist the `opponent` name for a
  robust full-pair match. Key lives in `secrets/odds_api_key.txt` (free tier). Live client verified
  end-to-end; the full name-match/de-vig path live-verifies at the August Masters when odds post.
- **2026-07-14 — Holistic review-hardening (15-agent whole-project review → fixes); 243 tests.** A
  full-project review flagged that the paper-test, as built, could green-light real money on noise.
  Fixes:
  - **Go-live gate hardened** (`clv.summarize`): clusters by **ISO week** (day was too fine — intra-
    tournament correlation was shrinking the CI), a **BCa** bootstrap interval (not percentile) for the
    skewed CLV, plus two co-gates — **realized net-ROI ≥ 0** and a **max missed-capture rate** — and a
    higher effect-size bar (0.015, sized to cover slippage + the per-order round-up fee + the ask-vs-mid
    basis). The TRUE go-live path is now unit-tested.
  - **Closing-line capture is fail-closed:** if a match's scheduled start is unknown it's marked *missed*
    rather than risk snapshotting an in-play price (Kalshi trades tennis in-play, so `status` alone can't
    tell); grace tightened to 5 min; `/close <id> pre` lets the owner confirm a genuinely-pre-match untimed
    market; auto-capture retries a transient Kalshi error instead of silently losing the datapoint.
  - **Weekly model refresh actually fetches** (`prepare_matches.py --fetch`) — the documented cron had been
    re-processing stale CSVs, freezing the model. Player ids now fold hyphen/space variants (one player was
    split across two Elo entities). Calibration unchanged (ATP 0.2175 / WTA 0.2165).
  - **Model/sizing tightened:** thin players (< `thin_matches`) now **abstain** (their Elo is overconfident
    and they were the worst bets vs the sharp close) rather than being haircut-and-alerted; optional **flat
    paper stakes** (CLV is stake-independent — don't prime Kelly-sized real bets on an unvalidated model);
    an **aggregate open-exposure** warning.
  - **Ops:** SQLite WAL + busy_timeout, a **daily heartbeat DM** (a silent outage otherwise looks like "no
    edge"), and a PTB error handler.
  - **Known prerequisite (not yet built):** the go-live gate compares our entry to Kalshi's *own* close,
    which can't distinguish a genuinely soft Kalshi line from model error. A **live sharp-line reference**
    (e.g. Pinnacle via an odds API, Shin-devigged, gated on beating *that*) is required before the gate can
    authorize real money — staged pending an odds-provider + API key.
- **2026-07-14 — Phase 6 infrastructure (run the forward-CLV paper-test unattended); 237 tests.**
  Everything needed to accumulate a trustworthy CLV sample over weeks without babysitting:
  - **Always-on deployment:** `Dockerfile` + `docker-compose.yml` (`restart: unless-stopped`, secrets/
    config mounted **read-only**, nothing baked into the image), rotating file logging, and a PTB error
    handler so a transient job failure logs instead of silently dying. Weekly host-cron model refresh +
    `docker compose restart` is safe — pending captures rebuild from the DB on startup. systemd
    alternative documented. (See **"Run as a service"**.)
  - **Scheduled systematic scan** (`scan_interval_hours`, default 8h) — sweeps on a timer to remove
    owner-timing selection bias from the sample; `max_instances:1`+`coalesce` guard overlap; quiet
    unless an alert fires.
  - **Postponement-aware capture:** the auto-capture job treats the live Kalshi market as the single
    source of truth for match time — if a match is pushed back it re-arms instead of false-missing;
    a past-due row at startup fires an immediate re-check rather than being marked missed.
  - **CLV analysis depth:** `scripts/clv_report.py` segments net CLV by tour / price band / flag / ISO
    week (reusing `clv.summarize`), and `/stats` gained a capture-health line.
  - **Proxy-backtest fidelity:** `backtest_vs_bookmaker.py` now Shin-devigs the proxy odds, excludes
    retirements/walkovers, and segments by tier — the no-edge verdict holds (w*≈0, ROI ≈ −11%, negative
    across the liquid Slam/Masters tiers too).
  - **Liquidity-gate recalibration tooling:** `scan.py dry-run` now sweeps outright finals too and
    segments per tour and per tier — run it at the August Masters to set the gate from the liquid
    distribution (see the **Phase-6 runbook**). Model still has **no demonstrated pre-match edge** —
    do not bet real money until the gate is MET.
- **2026-07-13 — Review hardening (multi-agent review → fixes); 220 tests.** An 8-lens adversarial
  review (42 agents) surfaced 31 verified issues; fixed the correctness / data-integrity / go-live-gate
  ones before forward paper-testing:
  - **Go-live gate is now NET of fees + a minimum effect size** (was gross CLV — could green-light a
    fee-losing strategy). `clv.summarize` subtracts each bet's entry fee and clusters the bootstrap by
    **trading day** (event-clustering was inert), requiring ≥30 day-clusters alongside ≥200 bets.
  - **CLV measured on the closing MID vs the objective logged alert price** (fills feed P&L only) —
    removes half-spread / entry-basis bias.
  - **Closing-line capture hardened:** same-side mid; refuses when the market isn't active or we're
    past scheduled start (marks *missed*, never fabricates); auto-capture fires a few minutes BEFORE
    start and skips long-past — stops in-play/settled prices leaking the outcome into CLV.
  - **`resolve_player`** requires an exact full-name match on same-surname collisions (fixed a *live*
    bug — "Xiyu Wang" resolving to "Xinyu Wang") and abstains otherwise.
  - **Thin players** flagged + Kelly-haircut, with CLV and calibration reports **segmented by
    experience bucket** (exposes the 20–50-match overconfidence the aggregate hid).
  - Surface **fallback to overall Elo** when a player has no history on the match surface (was a
    phantom-1500 blend); exact **round-up Kalshi fee** in realized P&L; **`min_price` floor (0.10)**
    blocks deep longshots; contracts-floor off-by-one; **`void`** result state; migration completeness
    for pre-Phase-3 DBs. Model refreshed — calibration holds (ATP Brier 0.2175 / WTA 0.2165).
  - *Deferred (documented):* the Slam H2H/outright double-count (day-clustering already mitigates the
    CI risk); a full uncertainty-aware Kelly beyond the thin haircut.
- **2026-07-13 — Phase 5 (persistence + CLV); 208 tests.** Turns the opportunity log into a
  measurable track record for the forward-CLV go-live test. New commands: **`/close [opp_id]`**
  snapshots the same-side closing line (a live read near match start — candlestick backfill is too
  flaky; also **auto-scheduled** one-shot per opp via the PTB JobQueue, reconciled across restarts),
  **`/result <opp_id> <win|loss> <fill> [contracts]`** records the outcome + net-of-fee P&L,
  **`/stats`** reports hit rate, P&L/ROI, and **mean CLV with a cluster-bootstrap (by event) 95% CI**
  + the go-live gate (CI lower bound > 0, ≥ 200 bets). New pure `matador/clv.py` (CLV / net-P&L /
  bootstrap / summarize); `storage` gained `closing_captured_at`/`closing_source`, an **idempotent
  column migration**, an upsert `record_outcome` (merges the two-phase close/result writes), and
  `settled_bets`/`pending_captures` joins. Dep bumped to `python-telegram-bot[job-queue]` (APScheduler;
  httpx still 0.28). Also did the pre-work: **model refreshed** (Wimbledon results; ATP Brier 0.2175
  / WTA 0.2164) and **interim liquidity gate** set (`min_liquidity 500` / `max_spread 0.03`, to be
  recalibrated on the August Masters). Paper only — still no order path. Verified live: real
  closing-line capture → result → stats end-to-end.
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
