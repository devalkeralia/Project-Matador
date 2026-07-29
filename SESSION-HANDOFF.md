# SESSION HANDOFF — 2026-07-28/29

Written so this session's context can be cleared without losing anything. Durable homes for each
topic are noted; this file is the index, not the record.

## TL;DR state

| | |
|---|---|
| **Bot** | **DEPLOYED AND RUNNING** — DigitalOcean droplet `157.230.158.73` (SFO2, $6/mo) |
| **Repo** | `origin/main`, 297 tests passing |
| **Sample** | accumulating; was 8 rows / 8 distinct positions / 0 duplicates at last check |
| **Next real event** | **Monday 06:00 UTC** — refresh cron fires and DMs its outcome |
| **Model** | `p_model` FROZEN for the paper run. No demonstrated edge (`w*` = 0.00 vs the sharp close) |

## What happened this session

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
| Improvement backlog (11 do-now / 4 after-gate / 7 rejected) | `IMPROVEMENTS.md` |
| In-play research tool + its pre-registration | `scripts/inplay_overreaction.py` |
| Cross-session facts | `~/.claude/projects/.../memory/` (`build-status`, `go-live-prerequisites`, `underdog-over-rating`, `layoff-decay-preregistered`, `verify-your-own-work`) |

## OPEN — pick up here

### 1. ~~A live defect, currently untriggered~~ — **FIXED 2026-07-29, awaiting deploy**
The cross-anchor `prior["id"]` `TypeError` in `run_check`/`run_scan`. Both sites now go through
`bot._prior_position_id`, which mirrors `log_opportunity`'s key; parametrized test over all 9 ordered
pairs of `{scan, check(A,B), check(B,A)}` (306 tests). It never fired live — re-verified: no
`TypeError` in 72h of droplet logs, and all 11 rows carry the scan anchor.

**Still to do: push + redeploy.** The droplet is at `5a10f21` and pulls from `origin/main`, so the fix
is not live until `git pull && docker compose build && docker compose up -d` on the box. The
`/check` mitigation can then drop its defect rider — but `/check` stays in "Avoid" below for the
separate owner-timing reason.

### 2. The improvements backlog
`IMPROVEMENTS.md`. Not applied, deliberately — prune before it becomes doctrine. Top items after the
defect above: heartbeat answering the four questions that currently need SSH; pre-registering the
week-12 read protocol and stopping rule; logging the sharp fair probability **at entry** (irrecoverable
if skipped — without it a positive gate can't be separated from a standing Kalshi-vs-Pinnacle venue
basis); offsiting `matador.db` weekly.

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

| Safe | Required | Avoid |
|---|---|---|
| `/preview` `/find` `/stats` `/recent` `/notes` | `/result` `/close` | `/check` `/scan` |

`/result` is not optional: the gate hard-requires realized net-ROI ≥ 0, so missing results make the
week-12 read unreadable. `/check` and `/scan` log owner-timed opportunities — that alone is why they
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
