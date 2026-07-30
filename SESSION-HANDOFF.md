# SESSION HANDOFF — 2026-07-28 → 2026-07-30

Written so context can be cleared without losing anything. Durable homes for each topic are noted;
this file is the index, not the record.

## TL;DR state (as of 2026-07-30 21:15 UTC)

| | |
|---|---|
| **Bot** | **DEPLOYED AND RUNNING** — DigitalOcean droplet `157.230.158.73` (SFO2, $6/mo) |
| **Repo + droplet** | both at `e131417` on `origin/main`, **353 tests passing**, tree clean |
| **Sample** | 15 opportunities / 15 distinct positions / 0 duplicates; **10 now have results** |
| **Owner workload** | **none** — outcomes auto-record from Kalshi; `/result` is an override only |
| **Model** | `p_model` FROZEN for the paper run. No demonstrated edge (`w*` = 0.00 vs the sharp close) |
| **Next real event** | **Monday 06:00 UTC** — refresh cron fires, DMs its outcome, and now attaches `matador.db` |

**Nothing is left to build.** The run is accumulating; read the gate ONCE at ≥200 sharp-referenced bets
across ≥12 ISO weeks, per the pre-registered protocol in DESIGN-DECISIONS.

## The 2026-07-30 session (most recent)

1. **Improvements batch — 9 of 11 do-now items applied**, in deadline order (`IMPROVEMENTS.md` marks
   each with what was actually built). Headlines: **sharp probability at ENTRY** now logged (the only
   irrecoverable item — without it a MET gate can't be separated from a Kalshi-vs-Pinnacle venue
   basis); **the go-live gate's boundaries are finally tested** (6 mutations, incl. gating on the
   *circular* Kalshi count, previously all passed); the **week-12 read protocol is pre-registered** at
   n=11 bets; the **heartbeat answers the four SSH-only questions** and its header degrades to
   `🚨 N PROBLEM(S)`; `matador.db` **offsites weekly**; a **dead-man's switch** ships opt-in.
2. **`/result` is now fully automatic** (backlog #10, deferral overturned when the owner said they would
   forget it). Outcomes record from Kalshi's settlement; the first live sweep wrote 10 outcomes
   correctly, the second correctly wrote 0.
3. **Kalshi settles tennis THREE ways** — `yes`, `no`, and `scalar` (a pair-splitting refund). Measured,
   not assumed: `scripts/probe_settlement.py` + a captured fixture. `scalar` = **the match was never
   played**, which corrected an earlier wrong write-up calling it a retirement path.
4. **A 4-agent adversarial review of the auto-recorder found 8 defects**, all fixed and
   mutation-verified — including a DM inverted for every `no` bet and a sweep with no liveness surface.
   Two reviewer claims were **refuted** rather than acted on.
5. **Doc traps fixed** — DO backups confirmed **weekly / 4-week** (so the real RPO is 7 days, not 1),
   the stale deploy-key teardown step removed, and the two-poller **409 wedge** warning added.

## The 2026-07-28/29 session

1. **Deployed to DigitalOcean.** Hetzner was abandoned mid-setup after their US prices turned out to
   be ~$20.49/mo vs DO's $6. Full runbook: `DEPLOY.md` (signup → teardown). Kalshi verified reachable
   from SFO2 (HTTP 200), refresh cron installed and test-executed, reboot survival verified with 4
   pending captures restored from the DB.
2. **Fixed a silently broken weekly refresh** — upstream moved to Git LFS and the fetch had been
   overwriting good archives with 131-byte pointer stubs. Plus a future-dated-row guard (one WTA row
   was dated 2029).
3. **Measured the layoff question** → decay deliberately NOT built; `staleness` logged instead with
   pre-registered criteria. See DESIGN-DECISIONS "Layoff / inactivity".
4. **Found v1's first-order defect**: the model over-rates market underdogs by +4.3pp (7 SEs,
   vig-free). Structural under-dispersion. `shrinkage_n0` → 0 and `min_price` → 0.20 shipped as
   mitigation; the bias is **gated, not fixed**. See DESIGN-DECISIONS "Underdog over-rating".
5. **A duplicate-logging bug, twice.** Found live, fixed incompletely, then caught by review. Dedup is
   now on the **economic position**. See DESIGN-DECISIONS "Dedup identity".
6. **Added `/preview`** — `/check` that writes nothing, so the engine can be demoed mid-run without
   contaminating the sample.
7. **Added refresh alerting** — model freshness in the daily heartbeat (per-tour, ages off the
   OLDEST) plus an outcome DM from `scripts/weekly_refresh.sh` distinguishing OK / REJECTED / FAILED.
8. **Two adversarial multi-agent reviews** (Fable 5): a defect review (21 findings → 7 fixes) and an
   improvements pass (34 ideas → `IMPROVEMENTS.md`).
9. **Investigated the owner's trading ideas** — `Bot Ideas.txt` and
   `Tennis Trading Engine Specification.docx`. Conclusion in DESIGN-DECISIONS "In-play
   mean-reversion"; the overreaction is real but not harvestable, and the promising extreme-price
   result turned out to be look-ahead bias.

## Where each thing lives (durable)

| Topic | Home |
|---|---|
| Deploy / rebuild the host | `DEPLOY.md` |
| Every settled decision + rationale | `DESIGN-DECISIONS.md` |
| Status, changelog, command policy | `README.md` |
| Improvement backlog (9 of 11 do-now APPLIED / 4 after-gate / 7 rejected) | `IMPROVEMENTS.md` |
| In-play research tool + its pre-registration | `scripts/inplay_overreaction.py` |
| **How Kalshi settles (re-runnable)** | `scripts/probe_settlement.py` + `tests/fixtures/market_finalized_sample.json` |
| **Kalshi API reference incl. settlement fields** | `RESEARCH-KALSHI.md` §1 |
| Cross-session facts | `~/.claude/projects/.../memory/` (`build-status`, `go-live-prerequisites`, `underdog-over-rating`, `layoff-decay-preregistered`, `verify-your-own-work`, `unattended-needs-a-liveness-surface`, `automate-dont-nag`) |

## OPEN — pick up here

**Nothing is blocked and nothing is half-finished.** Everything below is either DONE (kept for the
record) or a deliberate deferral. The two things needing the OWNER, not a session:

| Action | Why it matters |
|---|---|
| **Arm the dead-man's switch** — create a healthchecks.io check (~26h grace), add `HEALTHCHECK_URL` to the droplet's `secrets/.env`, restart | It ships INERT. It is the only mechanism that detects the two-poller **409 wedge**, where running `scripts/bot.py` locally on the prod token silently kills the droplet's poller while the container still looks healthy |
| **Watch Monday 06:00 UTC** | First live test of the weekly `matador.db` offsite — expect a `.db` attached to the refresh DM alongside the outcome text |

### 1. ~~A live defect, currently untriggered~~ — **FIXED + DEPLOYED 2026-07-29**
The cross-anchor `prior["id"]` `TypeError` in `run_check`/`run_scan`. Both sites now go through
`bot._prior_position_id`, which mirrors `log_opportunity`'s key; parametrized test over all 9 ordered
pairs of `{scan, check(A,B), check(B,A)}` (306 tests). It never fired live — re-verified: no
`TypeError` in 72h of droplet logs, and all 11 rows carry the scan anchor.

Pushed and deployed (`69cf933`), verified live: no `TypeError`, migration applied, sample intact.
`/check` is no longer defect-armed but stays in "Avoid" below for the separate owner-timing reason.

### 2. The improvements backlog — **9 of 11 do-now items APPLIED (2026-07-29/30)**
`IMPROVEMENTS.md` (each done item struck through with what was actually built). Applied: #1 prior-id,
**#8 sharp-at-entry** (the irrecoverable one — now collecting), **#7 the gate mutations**, #3 the
pre-registered read protocol, #4 offsiting `matador.db`, #2 the self-sufficient heartbeat, #6 the doc
traps, **#10 auto-recorded settlement**, #11 the dead-man's switch.

**Only #5 and #9 remain, both by choice:**
- **#5** refresh shrink-guard + degradation warnings + a build stamp in `model.json` — safe (warn-only
  and metadata; verified `Model.from_artifact` ignores unknown top-level keys, so a stamp can't change
  a prediction).
- ~~**#6** doc traps~~ — **DONE.** Backups confirmed in the DO console as usage-based, **weekly, 4-week
  retention** (so `DEPLOY.md:54`'s "daily/~14-day" was the wrong side of the contradiction; the real
  7-day RPO is now stated). Stale deploy-key teardown step removed. The two-poller 409 warning added to
  DEPLOY §8 + the README policy.
- **#9** the three week-12 analyses in `clv_report` — deferred on purpose, equally cheap at the read.
- ~~**#10** auto-record settlement~~ — **DONE 2026-07-30**, deferral overturned when the owner said they
  would forget `/result` (which makes the gate unreadable or selectively biased — worse than a cruder
  metric). The pre-req immediately earned its keep: Kalshi settles tennis **three** ways, and
  `result='scalar'` (3.6% ATP / 2.2% WTA) splits the pair across **0.15–0.85** rather than paying one
  side out. A second pass established WHY, correcting an earlier wrong guess of "retirement": the match
  was **never played** (16/17 absent from our archive; the one present scored `W/O`; the rules need a
  winner "after a ball has been played"), so Kalshi refunds both sides at the prevailing price. Those
  auto-record as `void` — fully hands-free. Reproduce with `scripts/probe_settlement.py`.
  A 4-agent adversarial review of the first cut found 8 defects (inverted payoff in the DM, a DM
  instructing an unparseable command, a failed send silencing a case for good, no liveness surface, a
  TOCTOU vs the owner's `/result`, and a suite that passed with the sweep disabled) — all fixed,
  mutation-verified. The ROI co-gate is now a **paper ROI at alert prices** — amendment
  recorded in DESIGN-DECISIONS before the read, as the pre-registration requires.
- ~~**#11** dead-man's switch~~ — **DONE 2026-07-30**, owner accepted the tradeoff. Ships INERT: set
  `HEALTHCHECK_URL` in the droplet's `secrets/.env` (healthchecks.io, ~26h grace) to arm it. It is the
  only mechanism that catches the two-poller 409 wedge.

### 3. Deferred model levers — AFTER the gate reads only
Ranked in `IMPROVEMENTS.md`: K floor → surface cold-start → recency-weighted Elo (conditional on the
K floor) → direct recalibration (near-dead-end). All touch `p_model`, so all must wait.

### 4. The owner's ideas, evaluated
- **Trading the price path** (Bot Ideas 1/3/6): overreaction real, not harvestable. Dead unless depth
  and friction change. The one untested piece is achievable DEPTH.
- **Hedging** (2): already settled — per-side fees exceed the arb gap.
- **Momentum weighting** (4): on the roadmap as recency-weighted Elo, but the stated motivation
  (boost low-n players) runs against measured evidence — thin players are the worst segment (−17.9%).
- **ML/neural net** (5): argued against by the very result that unlocked its gate. `w*` = 0.00 means a
  richer function of the same features reconstructs the price. ML needs *different* data (v2
  serve/return), not a different function.
- **`Tennis Trading Engine Specification.docx`**: philosophy already implemented; blocked on data.
  Engine 6 (psychology) is unobtainable at any price, Engine 3 nearly so, Engine 4 (35 inputs) needs
  a paid live feed — yet Engines 4+6 carry 30% of its trade score. Its central inconsistency: Engine
  8 needs a calibrated probability that Engines 1–6 never produce (a weighted 0–100 score can't be
  subtracted from a market price). Salvage: Engine 1's serve/return features (obtainable from
  TML-Database, already the scoped v2 path) and Engine 9's exit logic *if* in-play ever proves viable.

## During the run — command policy

| Safe | Automatic now | Avoid |
|---|---|---|
| `/preview` `/find` `/stats` `/recent` `/notes` | `/close` `/result` | `/check` `/scan` |

**`/result` is no longer needed at all** (2026-07-30): outcomes auto-record from Kalshi's settlement and
DM a `🧾`. Matches that were never played auto-record as `void`. It survives purely as an OVERRIDE; the
only thing escalated to a human is a settlement value never seen before. `/check` and `/scan` log owner-timed opportunities — that alone is why they
stay in "Avoid"; use `/preview` to inspect the engine instead.

**Watch:** Monday's `logs/refresh.log` (`latest` must advance, no `rejected` warning), the daily
heartbeat DM (its absence is the only outage signal), and `sharp_coverage` in `/stats` (below 0.5 the
gate can't bind).

**Do not** change `p_model` mid-run, and do not stop early on a favourable `/stats` read — the CI is
meant to be read once at ≥200 bets across ≥12 ISO weeks.

## Standing working agreements

- Ask before `git commit`/`push`; plan approval does not count.
- Omit the Co-Authored-By trailer in this repo.
- **Verify your own work**: revert a fix to prove its test fails; never quote prices/specs from
  recall; after finding a broken invariant grep every *writer* of that key; and check agent claims
  against live state — this session's reviews were wrong in both directions.
