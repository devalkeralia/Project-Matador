# IMPROVEMENTS — prioritised backlog (generated 2026-07-29)

Produced by an adversarial multi-agent improvements pass (Fable 5, 5 lenses -> ruthless prioritiser):
**34 raw ideas -> 11 do-now, 4 after-gate, 2 needs-data, 7 rejected.**

**Status 2026-07-30: 9 of the 11 do-now items are APPLIED** (#1, #2, #3, #4, #6, #7, #8, #10, #11 —
each struck through below with what was actually done). Remaining: **#5** (small, safe) and **#9**
(deferred by choice — it is needed *at* the week-12 read and is just as cheap then).

The rest of this file is the original review artifact. Prune it before it becomes project doctrine.

## The constraint that tags every item

`p_model` is FROZEN for the duration of the paper run. Any change to the win-probability path, the
Elo build, calibration, the artifact, or config that alters which side/price is selected invalidates
the accumulated sample and restarts the 12 weeks. Hence: **do-now** = safe during the live run (ops,
logging, reporting, tests, docs, offline analysis); **after-gate** = touches p_model/selection;
**needs-data** = blocked on a source we do not have.

## Headline

The single most valuable item is logging the sharp (Pinnacle, Shin-devigged) fair probability at
ENTRY, because it is the only thing on this list that is irrecoverable if skipped: without it a
positive week-12 gate cannot be separated from a standing Kalshi-vs-Pinnacle venue basis (Kalshi
simply trading 2-3c below Pinnacle on the favorites the min_price 0.20 gate makes us buy), which
would look identical to skill and would authorize real money on an artifact. With it,
mean(sharp_close - sharp_entry) is a selection-clean measure of the line moving toward us after we
committed. Two cheaper things must go first because the sample is at risk today: the reproduced
prior-id TypeError (one reversed-order /check arms it, after which EVERY 8-hourly scheduled scan
dies mid-sweep and is swallowed as a log line nobody reads - and that scan is the mechanism that
keeps owner timing out of the sample), and the four tests that kill seven verified surviving
mutations in the code that decides go-live and guards the closing line.

---

## DO NOW — safe during the live run (11)

### 1. ~~Fix the prior-id lookup after a cross-anchor dedup~~ — **DONE 2026-07-29**  _[small]_

**Fixed.** Both sites now use `bot._prior_position_id`, which mirrors `log_opportunity`'s key
(no-opponent fallback included) and is None-safe; guarded by a parametrized test over all 9 ordered
pairs of `{scan, /check A B, /check B A}` — the 4 cross-anchor pairs reproduced the `TypeError`
before the fix. Never fired live (all 11 rows carry the scan anchor). Rationale recorded in
DESIGN-DECISIONS "Dedup identity" → Corollary. Original entry below.


**What.** run_check and run_scan still resolve the prior row with last_opportunity(conn, opp.market_ticker,
opp.side) after log_opportunity returns None, but the dedup key is now the economic position. When
the prior row was logged under the other anchor that lookup returns None and prior["id"] raises
TypeError. Resolve via storage.last_position(conn, opp.event_ticker, engine.backed_player(opp)) with
a None-safe fallback (~3 lines each), and add one parametrized regression test over sequences of
{run_scan, run_check(A,B), run_check(B,A)} in both orders asserting after each step: exactly one
opportunities row, a reply that names the prior id, no exception. Note in the docstring that the
H2H-vs-outright double-count is deliberately OUT of scope (documented deferral) so a future session
does not 'fix' the deferral by accident.

**Why.** Reproduced against the repo (/tmp/repro_prior.py: first=1, dedup returns None, last_opportunity
returns None, TypeError). Two live paths: (A) /check with the players typed in the other order gets
no reply at all; (B) worse, once one reversed /check has logged a standing edge, every subsequent
scheduled scan raises mid-sweep, scheduled_scan_job catches it and returns, and the whole cycle's
alerts are lost silently until that match closes. That suppresses the systematic scan - the
mechanism that removes owner-timing bias from the paper sample - for days per occurrence. The dedup
fix's own tests call log_opportunity directly and never exercise the bot wrappers, which is exactly
why this survived.

**Where.** `/home/dkeralia/projects/java/dev/ClaudeProjects/Tennis Betting/matador/bot.py:192 and :214; fix uses last_position in /home/dkeralia/projects/java/dev/ClaudeProjects/Tennis Betting/matador/storage.py and backed_player in /home/dkeralia/projects/java/dev/ClaudeProjects/Tennis Betting/matador/engine.py; test in /home/dkeralia/projects/java/dev/ClaudeProjects/Tennis Betting/tests/test_bot.py`

### 2. ~~Make the daily heartbeat answer the four questions that currently require SSH~~ — **DONE 2026-07-29**

**Done.** All four, plus the a/m/s/x legend, plus the header degrades to `🚨 Matador — N PROBLEM(S)` when
anything warns (a body full of failures under an "OK" first line is how a silent outage survives 84
daily DMs). Credits survive the per-job sharp clients via `sharp.last_requests_remaining()`. Original entry below.


**What.** Four additions to _heartbeat_text / scheduled_scan_job: (1) bets awaiting /result - ids and age for
rows whose occurrence_datetime is >12h past with result IS NULL, cap the listed ids at ~5; (2) flag
pending rows with NO occurrence_datetime - schedule_pending_captures (bot.py:633) skips them
silently, so they never auto-capture, never DM a miss, and sit in the pending count forever until
the owner runs '/close <id> pre'; (3) stash {finished_at, ok/failed, n_new} in bot_data on every
scheduled-scan cycle INCLUDING the except path and render 'last scan 2h ago: ok, 0 new' or 'last
scan FAILED 2h ago', honestly reporting 'no scan yet since restart' after a restart; (4) carry
SharpOddsClient's already-parsed x-requests-remaining onto the client and surface it when it drops
below ~100. Also spell out the cryptic 'captures 3a/1m/0s/2x' legend once.

**Why.** Three silent failures, all read-only to fix. The scheduled scan can fail every cycle for weeks
(Kalshi 403 from the droplet IP, TLS, schema drift) while the heartbeat still says 'Matador OK - N
opps', indistinguishable from a quiet market. The gate hard-requires roi is not None, which needs a
/result entry per bet; a few forgotten weeks makes the week-12 read unreadable or biased toward
whichever results the owner bothered to enter, and a paper fill price is not reconstructable later
the way a match result is. Odds-api credit exhaustion is the one real expiry here: fetch_h2h raises,
sharp_fair_for_opp swallows it, sharp_close stays NULL, and the only symptom is sharp_coverage
sagging below the 0.5 co-gate weeks later, by which time those bets are permanently sharp-less.

**Where.** `/home/dkeralia/projects/java/dev/ClaudeProjects/Tennis Betting/matador/bot.py _heartbeat_text (~775) and scheduled_scan_job (~684); /home/dkeralia/projects/java/dev/ClaudeProjects/Tennis Betting/matador/storage.py (new query beside pending_captures ~243); /home/dkeralia/projects/java/dev/ClaudeProjects/Tennis Betting/matador/sharp.py fetch_h2h (~109)`

### 3. ~~Pre-register the week-12 read protocol and the stopping rule~~ — **DONE 2026-07-29**

**Done.** DESIGN-DECISIONS "Week-12 read protocol — PRE-REGISTERED", written at n=11 bets, plus the
K-floor-supersedes-recency cross-reference. Original entry below.


**What.** One dated section in DESIGN-DECISIONS, written BEFORE more bets accrue, fixing: the gate is read
ONCE at week >=12 with n_sharp>=200 exactly as clv.summarize computes it (no substitute metric);
/stats peeks are monitoring and cannot trigger an early go-live; the closed segment list that may be
looked at but cannot gate (price band, experience, event/tier, staleness - all exploratory except
the already pre-registered layoff-decay criteria); the stopping rule (sharp CLV <=0 AND
informational Kalshi CLV <=0 on a full sample -> STOP, no lever cascade; Kalshi positive but the
sharp gate missed -> exactly ONE lever, the K floor, with its pass bar written down first; sharp
coverage <0.5 -> ops failure, 'no answer', fix and extend, not 'answer is no'); a ban on extending
the run to hunt a positive segment; that the clv_report robustness numbers are veto-only (they can
block a go-live, never authorize one); and the caveats the reader must carry (closes are captured at
T-5min of SCHEDULED start, so late-starting matches understate late drift, and the missed-capture
set is non-random). Include an explicit 'any deviation must be written down before the read' escape
hatch. While there, add the one-line cross-reference from the Open-items 'first lever is recency
Elo' text (~line 540) to the 2026-07-27 K-floor evidence (lines 325-334) that supersedes it.

**Why.** DESIGN-DECISIONS has flagged optional-stopping / segment-mining as open and unresolved since
2026-07-14, and the gate CI is recomputable at every /stats call. Twelve weeks of watching a number
hover near a 1.5c bar is exactly the environment where a lucky peek green-lights real money. It
costs a paragraph, and it is only worth anything if written before the sample matures. The cross-
reference stops a future session spending the post-gate window on the lever with the weaker
evidence.

**Where.** `/home/dkeralia/projects/java/dev/ClaudeProjects/Tennis Betting/DESIGN-DECISIONS.md (next to the go-live gate ~506, the optional-stopping bullet ~561, the Open-items lever text ~540)`

### 4. ~~Offsite the paper sample weekly~~ — **DONE 2026-07-29**

**Done**, with one upgrade on the spec: the snapshot goes through SQLite's online-backup API rather
than reading the file, because the bot runs in WAL mode — verified that a plain `read_bytes()` of
`matador.db` doesn't even carry the table, let alone the recent rows. Original entry below.


**What.** After refresh_notify sends its outcome text, POST data/matador.db (20 KB today) to Telegram
sendDocument with the existing token/chat_id, best-effort under the same never-fail-the-cron
handling as the text message.

**Why.** DEPLOY.md itself calls matador.db the project's only clean forward instrument that cannot be
reconstructed, yet every copy lives inside one DO account (droplet plus a DO backup) plus a manual
scp that depends on the owner remembering. A billing lapse, a compromise, or an accidental destroy
past retention loses the entire 12-week sample. Six lines, no new credentials or infrastructure, and
it lands a genuinely independent copy in a chat the owner already reads. Worst case is one torn WAL-
mode copy in some week.

**Where.** `/home/dkeralia/projects/java/dev/ClaudeProjects/Tennis Betting/scripts/refresh_notify.py main() (~101)`

### 5. Harden the weekly refresh against silent degradation, and stamp what was built  _[small]_

**What.** Three surgical additions. (1) In fetch(), before write_bytes, compare the downloaded body's data-row
count to the existing year file's; for past years treat a material shrink as outcome='rejected',
keep the old file, and print the existing 'rejected as unusable' WARNING (refresh_notify's keyword
scan then DMs it), naming the override in the message (delete the local year file and re-run --fetch
to accept an intentional upstream correction). (2) In prepare(), after building the combined frame,
print WARNINGs when for the current year the share of rows failing canonical_surface exceeds a few
percent, or fewer than ~70% of the year's player ids were seen in any prior year (measured healthy
baseline: ATP 87%, WTA 92%); extend refresh_notify's keyword list to catch them. Warn-only - drop no
rows beyond what prepare() already drops. (3) In build_ratings.py main(), add a top-level 'build'
key: built_at, git HEAD sha (best-effort), the full Elo parameter set actually used - critically
k_num/k_shift/k_pow - and sha256 of each {tour}_matches_all.csv input.

**Why.** (1) Verified by reading _is_usable_csv: it requires only a header plus one data row, so a truncated
body silently replaces the 2,807-row atp_matches_2019.csv and deletes a season of Elo history - the
LFS-overwrite class one notch subtler, with nothing downstream to flag it. (2) These are the
degradations that still look like a valid CSV: a renamed surface value makes canonical_surface
return None and freezes surface Elo while overall Elo advances; a name-format regression upstream
mints new synthesized player ids and splits every career into a fresh ~1500 entity while
data_through happily advances - p_model degrades while looking alive, which is worse than a freeze.
(3) The week-12 verdict must be read against the model the sample was collected under, and the
K-factor triple lives only in gitignored config.yaml with no record of the code commit or the
inputs; Model.from_artifact ignores unknown top-level keys, so extra metadata cannot change a
prediction.

**Where.** `/home/dkeralia/projects/java/dev/ClaudeProjects/Tennis Betting/scripts/prepare_matches.py fetch() (~88) and prepare() (~141); /home/dkeralia/projects/java/dev/ClaudeProjects/Tennis Betting/scripts/refresh_notify.py build_message(); /home/dkeralia/projects/java/dev/ClaudeProjects/Tennis Betting/scripts/build_ratings.py main() (~98)`

### 6. ~~Fix three read-under-stress doc traps~~ — **DONE 2026-07-29**

**Done, all three.** (1) The backup contradiction is resolved by CHECKING rather than picking a variant:
the owner confirmed the DO console reads usage-based, **weekly, 4-week retention**, so DEPLOY.md's
"daily, ~14-day" line was the wrong one — corrected, and the real 7-day RPO is now spelled out with its
consequence (prefer the weekly `matador.db` DM for sample recovery; the DO backup is for rebuilding the
box). (2) The stale "remove the deploy key" teardown step is gone — step 3 clones over HTTPS and there
has never been a key to remove. (3) The two-poller 409 warning is now in DEPLOY §8 and the README
command policy, named as the likeliest self-inflicted outage, pointing at the dead-man's switch as the
only mechanism that detects it. Original entry below.


**What.** (1) DEPLOY.md line 54 says backups are 'USAGE-BASED, daily, ~14-day retention' while the very next
paragraph and the step-1 table say weekly frequency / retention in weeks / '4 weeks' / '$0.04/GiB,
weekly' - check the DO console and make them agree rather than inventing a third variant. (2) Step 9
item 3 says 'Remove the deploy key from the GitHub repo', but step 3 clones over HTTPS and
explicitly states 'no deploy key and no credential on the server at all' - delete the stale step.
(3) Add one warning to the README during-run policy and DEPLOY step 8: never run scripts/bot.py
locally with the production TELEGRAM_TOKEN while the droplet bot is up - two long-pollers on one
token 409-conflict and the droplet bot goes silent, the exact wedged-long-poll failure the heartbeat
docstring names but the runbook never warns about.

**Why.** All three bite under stress. (1) is the document the owner acts from during a disaster: believing
the RPO is 1 day when it is 7 changes whether restoring is acceptable versus rebuilding and how much
sample to expect to have lost - and the contradiction means one of the two statements does not
describe what was actually enabled. (2) sends the owner hunting for a key that does not exist during
teardown. (3) is the likeliest self-inflicted outage: the natural debugging instinct ('let me run
the bot locally and poke it') silently takes down the droplet's poller mid-sample.

**Where.** `/home/dkeralia/projects/java/dev/ClaudeProjects/Tennis Betting/DEPLOY.md lines 54-64, the Backups table row (~103), step 9 item 3 (~333), step 8; /home/dkeralia/projects/java/dev/ClaudeProjects/Tennis Betting/README.md during-run command policy (~235)`

### 7. ~~Kill the surviving mutations in the gate and the closing-line guards~~ — **DONE 2026-07-29**

**Done — and the count was SIX, not seven.** Re-ran every mutation at runtime: the `sharp_coverage`
co-gate was already covered. The other six all passed 306/306 and now each kill at least one test
(the two timing ones kill three each). The gate matrix hinges on a base case that genuinely returns
`go_live=True` with the two tracks DIVERGING (n_sharp 210 vs n_clv 240) — every prior gate test had
them aliased, which is exactly why "count the circular Kalshi rows" was undetectable. Timing
constants are pinned ABSOLUTELY, since a test written as `start + CAPTURE_LATE_GRACE + 1min` scales
with the constant and stays green when it widens. Original entry below.


**What.** Mutation-tested at runtime (module substitution; no repo files touched) - all seven survive 297/297.
Four tests close them. (a) ONE near-miss gate matrix in tests/test_clv.py where the two CLV tracks
DIVERGE: base ~210 pinnacle rows plus ~30 kalshi-only rows over 12 weeks with VARIED CLVs, then
variants asserting go_live is False for 199 pinnacle bets (Kalshi count above 200), mean sharp CLV
~+0.8c (positive but under the 1.5c bar), the same bets packed into 11 weeks, and closes with zero
recorded fills (roi None). That kills four co-gate mutations plus the one that gates on the circular
Kalshi count. (b) A BCa test on right-skewed data across >=6 clusters: assert the interval differs
from the plain percentile and pin the numeric interval for the fixed seed (a golden value catches
any sign or transcription change in z0 / the acceleration); state in the docstring that it cannot
catch a conceptual error. (c) Boundary tests for CAPTURE_LATE_GRACE using the already-injectable
now=: start+4min captures, start+6min marks missed:late. (d) One realistic reschedule test: stored
start T, live start T-30min, now=T-5min -> action captured, reason too_late, stored time corrected,
no closing_price written; companion assertion for T+30min -> rescheduled. While in these files fix
the comments that teach the wrong spec: tests/test_clv.py:43 says 'min_effect 0.005, 30 clusters'
(actual 0.015 and 12 ISO weeks) and tests/test_engine.py:166 says 'same (market_ticker, side) ->
deduped', which re-teaches the retired dedup identity that caused the live 2026-07-29 double-log.

**Why.** Verified, not asserted. Gate: deleting the 200-bet floor, weakening the effect bar to >0, deleting
the 12-week floor, letting roi=None pass, and gating on the Kalshi count all pass the full suite,
because in every gate test the two tracks are perfectly aliased (n_clv == n_sharp, all values far
from the 1.5c boundary) or sharp_ci is None. go_live is the single boolean the whole run exists to
produce, read once with real money on the line. BCa: replacing clv._NORM with a garbage stub
(inv_cdf=-99, cdf=0.999) still passes 297/297 - every existing call hits the <4-cluster or
degenerate fallback, so the bias correction and jackknife acceleration that produce the number
authorizing real money are dead code under test, and BCa exists precisely because CLV is right-
skewed. Timing: grace->12h and epsilon->1 day both survive, because the only 'late' test uses a 2020
date and the reschedule tests move starts by months. A widened grace, or an ignored 30-minute-
earlier restart on a reordered court, records an in-play price as the close - a partially-resolved,
systematically informed 'close' is poison in the binding metric and invisible until the gate is
read.

**Where.** `/home/dkeralia/projects/java/dev/ClaudeProjects/Tennis Betting/matador/clv.py:82-94 (BCa) and :195-202 (gate); /home/dkeralia/projects/java/dev/ClaudeProjects/Tennis Betting/matador/bot.py:279 and :331 (guards; constants at :92 and :94); tests in /home/dkeralia/projects/java/dev/ClaudeProjects/Tennis Betting/tests/test_clv.py and /home/dkeralia/projects/java/dev/ClaudeProjects/Tennis Betting/tests/test_bot.py`

### 8. ~~Log the sharp fair probability at ENTRY~~ — **DONE 2026-07-29**

**Done.** `opportunities.sharp_entry` / `sharp_entry_source`, filled by `bot._sharp_entry_job` strictly
after the DM. Guarded by a test asserting the DM happens FIRST and that a failing fill can't touch the
alert path (proved by reordering the calls and watching it fail), plus a migration test on a
pre-existing populated DB. Original entry below.


**What.** Two nullable columns on opportunities (sharp_entry, sharp_entry_source) added via the existing
_MIGRATIONS pattern, filled by reusing sharp.sharp_fair_for_opp with the batch cache. Keep it
strictly post-decision and OUT of the alert-latency path: after a scan cycle has logged its alerts
and sent its DMs, fill the columns for the rows just written (one cached tournament fetch per alert-
bearing cycle). Wrap it exactly like the capture-side fetch so it can never raise, delay, or
suppress an alert. Add a test asserting the alert decision is identical with the sharp client
absent.

**Why.** This is the one confound the current schema cannot rule out. A MET gate could mean nothing more than
'Kalshi trades 2-3c below Pinnacle fair on the favorites we buy' - a venue-level basis, constant
from entry to close, indistinguishable today from forecasting skill. With the entry snapshot,
mean(sharp_close - sharp_entry) is the sharp line moving toward us AFTER we committed (skill) and
mean(sharp_entry - entry_price) is the standing basis. It must be collected live: the-odds-api
historical odds is not on the free tier, so no week-12 session can reconstruct it. Rows logged
before the change simply lack the field and the decomposition runs on the covered subset, which 12
weeks supplies plenty of.

**Where.** `/home/dkeralia/projects/java/dev/ClaudeProjects/Tennis Betting/matador/bot.py (after the alert-logging path in run_scan / scheduled_scan_job, ~175-215); /home/dkeralia/projects/java/dev/ClaudeProjects/Tennis Betting/matador/storage.py (_OPPORTUNITY_COLUMNS + _MIGRATIONS); reusing sharp_fair_for_opp in /home/dkeralia/projects/java/dev/ClaudeProjects/Tennis Betting/matador/sharp.py`

### 9. Add the three analyses the week-12 read actually needs to clv_report  _[medium]_

**What.** All offline and read-only, all from columns already stored. (1) Edge-vs-CLV gradient: mean sharp net
CLV binned by the model's claimed net_edge (3-5%, 5-8%, 8%+) with n and a CI per bin, plus a rank
correlation. (2) Concentration and drop-one-week: the max single-week share of bets (warn above
~25%) and the BCa lower bound recomputed with the largest week dropped. (3) An 'event' axis in
segment_summaries using the existing event column, plus one header sentence stating that
sharp._SUFFIX_BY_KEYWORD covers only Slams / Masters / big 500s, so the verdict applies to that
universe only. The binding gate in clv.summarize is NOT touched.

**Why.** (1) is the cheapest test of whether a positive mean is MODEL-driven: if the model has signal, bets
it claimed 8% edge on should beat the close by more than bets at 3%; a flat or inverted gradient
says the positive mean comes from the venue basis, the price-band mix, or the min_price gate, and
the model adds nothing. It is invisible in every current report. (2) at the 12-cluster minimum a
cluster bootstrap undercovers, and one Masters fortnight holding a third of the bets can dominate
the resampling distribution, so the CI can look tight for reasons unrelated to evidence; if the BCa
lower bound clears 0.015 but the drop-Cincinnati rerun does not, nothing today would surface that
disagreement. (3) the 250-tier has no Pinnacle reference, so the binding sample is structurally a
big-tournament sample; a MET gate authorizes money only in the tier it measured and the report
should say so.

**Where.** `/home/dkeralia/projects/java/dev/ClaudeProjects/Tennis Betting/scripts/clv_report.py (segment_summaries ~47, main ~85), reusing bootstrap_mean_ci at /home/dkeralia/projects/java/dev/ClaudeProjects/Tennis Betting/matador/clv.py:57; coverage map at /home/dkeralia/projects/java/dev/ClaudeProjects/Tennis Betting/matador/sharp.py:32-61`

### 10. ~~Auto-record settlement from Kalshi~~ — **DONE 2026-07-30** (owner asked; deferral overturned)

**Done.** Held back as the only unattended writer to the live sample, then built when the owner said
plainly they would forget `/result` — which makes the gate unreadable or selectively biased, worse than
a slightly cruder metric.

**The pre-req paid off immediately.** Verified against 2,000 live finalized markets: Kalshi settles
tennis THREE ways, not two. `result='scalar'` (~3%) is a PARTIAL settlement where the mirrored pair
splits the dollar (0.75/0.25 observed) — its retirement path. The naive "not yes -> loss" mapping would
have booked **-100% on a bet that returned 25c or 75c a contract**. Also `status` is `finalized`, not
`settled`. Scalars are left `result` NULL (not `void`, which `summarize` skips — NULL still contributes
CLV) and DM'd once for a human call. All three guards are mutation-tested. See DESIGN-DECISIONS
"Auto-recorded settlement" for the `scalar` table and the recorded ROI-co-gate amendment. Original entry below.

**What.** Kalshi's market object carries result (yes/no) once settled and the Market dataclass currently drops
it - parse it. Then one scheduled job beside _auto_capture_job that, for captured-but-unresulted
rows some hours after start, fetches the market and records win/loss at the logged alert price and
logged contract count (exactly the values the owner would type for a log-only paper bet). Record
ONLY unambiguous settled yes/no markets, never overwrite an existing result, DM every auto-recorded
result so the owner can correct it, and keep /result as the override for real fills. Verify Kalshi's
actual settlement behaviour for retirements and walkovers first, and record in DESIGN-DECISIONS that
the ROI co-gate is now a paper ROI at alert price.

**Why.** The gate hard-requires realized net-ROI >= 0, and roi is None - the gate permanently unreadable -
unless the owner manually types /result with a fill price for every one of 200+ bets over 12 weeks.
That is the largest recurring operating cost of the paper run. Ranked last of the code items because
it is the only one that writes to the live sample automatically: a retirement Kalshi settles rather
than refunds would enter P&L as a real bet where /result would have voided it, which is why the
three guards above are not optional.

**Where.** `/home/dkeralia/projects/java/dev/ClaudeProjects/Tennis Betting/matador/kalshi/market.py (Market dataclass + from_api, lines 9-45: parse result); /home/dkeralia/projects/java/dev/ClaudeProjects/Tennis Betting/matador/bot.py (new job beside _auto_capture_job, reusing run_result / record_outcome)`

### 11. ~~Dead-man's switch on the heartbeat~~ — **DONE 2026-07-29** (owner ACCEPTED the tradeoff)

**Done.** `bot.ping_dead_man_switch` + `HEALTHCHECK_URL` in `secrets/.env`, opt-in (unset = no ping, no
network call). The deciding argument was not general liveness but one specific failure: running
`scripts/bot.py` locally on the prod token 409s the droplet's poller, and that outage is invisible to
the container check AND to the refresh DM (which runs in its own `docker compose run --rm` container
and would still report "refresh OK"). Pinging strictly AFTER a successful DM is what makes the wedge
detectable, and that ordering is pinned by a test. Original entry below.


**What.** At the end of heartbeat_job, after the DM sends, one best-effort httpx GET to a healthchecks.io
check URL (free tier; URL in secrets/.env plus .env.example) with the check's grace set to ~26h,
wrapped with a short timeout so a healthchecks outage can never break the DM. Ping only AFTER the DM
succeeds so the check measures the real end-to-end path.

**Why.** The current liveness design requires a human to notice the ABSENCE of a routine message every day
for 84 days, and the failure the heartbeat exists to catch (droplet dead, container crash-looping, a
token conflict wedging the long-poll) is precisely the failure that also stops the heartbeat. The
weekly refresh DM does not cover it: refresh_notify runs via 'docker compose run --rm', so it would
still say 'Weekly refresh OK' with the bot container wedged - actively misleading. Every silent day
costs sample; a week unnoticed costs a week of the 12. Honest tradeoff: this is the only item that
grows the system's surface (a fifth external dependency and one more URL-shaped secret) - if the
owner prefers a boring box, decline it and accept 1-7 day detection latency.

**Where.** `/home/dkeralia/projects/java/dev/ClaudeProjects/Tennis Betting/matador/bot.py heartbeat_job (~790); optionally /home/dkeralia/projects/java/dev/ClaudeProjects/Tennis Betting/scripts/weekly_refresh.sh; new HEALTHCHECK_URL in secrets/.env and .env.example`

---

## AFTER THE GATE READS — touches p_model, must wait (4)

### 1. K floor on the Elo K-factor

**What.** KFactor.__call__ returns max(self.floor, self.num / (n + self.shift) ** self.pow), with floor=0
keeping today's behaviour bit-for-bit. Sweep floor in {15, 20, 25, 30, 35} (K(n)=250/(n+5)^0.4 is
~22.6 at n=400 and ~17.2 at n=800, so 20-35 binds from n~560 and n~120 respectively). The ANALYSIS
half is runnable offline NOW, before the gate reads, provided the pass bar is written into DESIGN-
DECISIONS first (decay-style pre-registration, so it cannot be re-fitted post hoc) and nothing under
data/ is rewritten - a locally rebuilt model.json would break /preview parity on this machine. Ship
nothing mid-run; bundle nothing else with it.

**Expected value.** The only deferred lever with direct measured in-repo evidence. Restricted to fresh players, implied-
Elo error by experience runs -32.7 (n 20-100) -> +16.0 (500-800) -> +123.6 at n>=800 with 9/9
players positive (Djokovic +116, Wawrinka +133, Venus +315, Kvitova +341), while the active elite
are UNDER-rated (Medvedev -26, Sabalenka -28, de Minaur -72): a bidirectional failure to track
change, ~4x the layoff slice's +25/+29 Elo. It is also the actual Venus fix, since her error is flat
in idle so no decay touches it. Highest EV per unit effort on the board - ~3 lines plus a rerun of
an already-proven sweep protocol. Honest ceiling: the n>=800 tail is ~2.5% of held-out matches, so
it cannot close an ~11-point ROI gap; it matters only if forward CLV lands near-positive.

**Validation / what would falsify it.** Per tour: calibration.walk_forward with the candidate KFactor, fit_scale on TRAIN only (<=2024),
evaluate on held-out 2025+. Pass bar: held-out Brier AND log-loss both improve versus floor=0 with a
consistent sign across all four splits (ATP/WTA x 2025/2026), and the optimal floor is stable across
tours rather than a one-cell spike; the implied-Elo-error-by-experience table must show the n>=800
fresh-player error shrinking materially AND the elite under-rating moving toward zero (both
directions - the finding is bidirectional); backtest_vs_bookmaker.py with train-only scales must not
degrade flat-stake ROI or worsen the underdog-bias-by-price table. Falsifiers: aggregate held-out
metrics flat or worse (the 140-row / 9-player tail does not generalize); improvement confined to
retired-era rating chains with no effect on 2025-26 predictions; optimum floor unstable between
tours or years (unidentified, like the layoff tau); or Brier improves while the price-conditional
bias table worsens (error moved, not removed). Code at matador/model/elo.py:39-49; harness in
matador/model/calibration.py.

### 2. Surface cold-start: shrink the surface term on its own count

**What.** In blended_rating, shrink the surface component by the per-surface match count instead of letting a
1-2-match surface Elo enter the blend at full surface_weight (the documented deferred fix). Requires
persisting surface counts in the artifact - dropped at build time today - plus an artifact-version
bump so an old model.json fails loudly rather than silently blending without counts. A separate,
unbundled change.

**Expected value.** A documented, bounded defect: a 1-2-match surface rating can swing p_model ~2-3pp, worst on clay and
grass - which is the low-price band where the underdog bias is already worst (+7.6pp at 15-25c), so
it plausibly feeds the TAIL of the first-order defect rather than its bulk. Cheap and unblocked, but
expect a small effect: it cannot move the aggregate +4.3pp / 7-SE bias.

**Validation / what would falsify it.** Same four-split train-only protocol as the K floor: held-out Brier and log-loss improve with
consistent sign across ATP/WTA x 2025/2026, and the clay/grass price-conditional bias table
improves. Falsified if the gain is confined to matches the min_price 0.20 gate already excludes, if
the aggregate degrades, or if Brier improves while the bias table is unchanged. Ship on the bar,
never on plausibility. Code at matador/model/probability.py:15-38 plus the artifact schema in
matador/model/elo.py.

### 3. Recency-weighted (time-decayed) Elo - conditional on the K floor passing

**What.** Down-weight matches by age in the rating build (the UTS-style time decay). Attempt ONLY if the K
floor passed its validation AND forward CLV came back near-positive; it then answers the follow-on
question 'does more forgetting help mid-career players too?'.

**Expected value.** Deliberately ranked below the K floor despite the older Open-items text calling it the first lever,
because a floored K already IS exponential recency-weighting for high-n players: constant-K Elo
forgets old results geometrically, decaying-K Elo converges to a career average, so the floor
restores forgetting exactly where the measurement says it is missing. Recency Elo has NO direct in-
repo measurement, and the Option-1 evaluation already stamped better-Elo refinements low-ceiling at
w*=0.000 (the sharp close subsumes the model). Real but unquantified expected value, which is why it
goes third rather than first.

**Validation / what would falsify it.** Same four-split train-only bar, plus a decay-form sweep. Falsified if no half-life gives a
consistent-sign held-out improvement, or if the optimum is unstable across tours/years - the layoff
work already showed this parameter family is hard to identify (three lenses gave three incompatible
fits). If the K floor FAILED its bar, do not attempt this at all: that outcome says the model class
is the problem and the pre-registered stopping rule fires. Code path: matador/model/elo.py build.

### 4. Direct under-dispersion recalibration - near-dead-end, one cheap honest experiment

**What.** Fit a more flexible OUTCOME-ONLY link (a two-parameter diff->p mapping with heavier tails than the
logistic) on <=2024 data and test whether the price-conditional bias table on 2025-26 shrinks.
Prerequisite, and the only part worth doing early: download pre-2025 seasons from tennis-data.co.uk
(backtest_vs_bookmaker.py is already parameterized by year and caches into gitignored data/odds/),
which creates the pre-2025 market-referenced fit window that today does not exist. Everything else
in this family is foreclosed by existing measurements: a global temperature IS the fitted scale
(already log-loss-optimal, and the bias survives it); any market-conditional fit on 2025-26 burns
the only clean test window; and with w*=0.000 a p=f(p_model, p_market) blend collapses to 'use the
market price', which produces zero alerts by construction.

**Expected value.** Low - probably nothing. Included so the post-gate window is not spent re-deriving why the +4.3pp
defect cannot be recalibrated away. The controlling precedent is the n0 sweep: better calibration
bought slightly WORSE ROI (-11.8% vs -11.0%), exactly as w*=0.000 predicts. The real fix for
structural under-dispersion is the v2 serve/return model, which is a project decision, not a lever.

**Validation / what would falsify it.** Fitted on <=2024 only, tested on 2025-26: the vig-free underdog-bias-by-price table must shrink
materially at 15-35c AND flat-stake ROI must not degrade. Falsified - and then abandoned, not
iterated - if Brier improves while the price-conditional bias is untouched, which is the expected
outcome since the bias is conditional on a variable the model never sees. Note the older seasons'
AvgW/AvgL consensus is a softer reference than a Pinnacle close and name-join coverage degrades, so
treat cross-era magnitudes as directional. Code at matador/model/probability.py:41-45 and
matador/model/calibration.py:94-107.

---

## BLOCKED ON EXTERNAL DATA (2)

- **Challenger / qualifying (and ITF for WTA) ingestion into the rating stream** — No live source. Verified read-only: LuckyLoser91/TennisCourtLog publishes only main-draw
{tour}_matches_{year}.csv (no qual_chall files); Sackmann's repos, which had them, went private
mid-2025; TML-Database froze 2026-01 so it cannot feed a weekly refresh. Largest absolute upside on
the board but for COVERAGE, not sharpness (~40% of a 250-tier slate is unmodellable, and 'idle'
really means 'absent from the main-draw dataset', so a match-fit Challenger player registers as a
90-day layoff and pollutes the staleness instrument). The source hunt is cheap and can start any
time; the build work cannot. It would also rebuild every rating - the fullest possible clock restart
- and needs a level-mixing decision (Challenger wins probably carry lower K), with name-join quality
worst exactly where sub-tour data lives.

- **Separating age from career phase in the K-floor result** — No player birthdates in the feed. The +123.6 Elo high-n error rests on 140 rows / 9 players and
cannot be decomposed into 'the K decayed too far' versus 'these players genuinely declined with
age'. This is a caveat on after-gate lever 1 rather than a blocker for it - the four-split
consistency bar is the guard - but the mechanism stays ambiguous until a birthdate source exists.

---

## REJECTED as over-engineering or already-settled (7)

Recorded so they are not re-raised. `CLAUDE.md` mandates simplicity first: no abstractions for
single-use code, no unrequested configurability, no optimising a non-problem. Disagree with any of
these and it goes back on the list — but the reason should be a concrete pain, not tidiness.

- **Daily placebo arm: snapshot Kalshi mid vs sharp fair across ALL liquid covered markets**
  Superseded at a fraction of the cost. The sharp-at-entry decomposition answers the same confound
  for the sample that actually matters, using two nullable columns instead of a new table, a new
  daily job, and an offline analysis script. It also spends odds-api credits that must stay reserved
  for the BINDING close-captures - the one way an 'improvement' here could damage the live sample -
  and adds live surface area on a box that should stay boring. The unconditional baseline it would
  add is not worth that.

- **Dated weekly snapshots of the raw archives and model.json on the droplet**
  Archiving for its own sake. Reading the gate needs the logged alerts, and p_model is already
  stored per opportunity; the build stamp makes any week's build verifiable, and the fetch() shrink
  guard is what actually prevents the corruption these tarballs would insure against. Rebuilding
  week-N's exact model is not on the critical path for any decision the run produces.

- **Monthly manual cross-check of the feed against tennis-data.co.uk**
  Not an independent source - tennis-data.co.uk IS the upstream of the feed's own 2025+ rows, so it
  is a pipeline-integrity check dressed as a second opinion, and its tournament naming needs fuzzy
  matching or it cries wolf. A manual monthly chore that gates nothing will be forgotten by week 5.
  The automated player-continuity and surface-mappability warnings cover the realistic silent
  degradations at zero recurring cost.

- **Four-interval robustness block in clv_report (per-week t-interval plus tournament-clustered BCa)**
  Trimmed to the two numbers that can change a decision (single-week concentration, drop-the-
  largest-week lower bound). A per-week t-interval is largely redundant with leave-one-week-out, and
  re-clustering by tournament mostly just reduces the cluster count. Four intervals around one
  number invite cherry-picking the friendliest, which is the exact failure mode the pre-registration
  item exists to prevent.

- **Replicating the underdog-bias table across 2019-21 / 2022-24 eras as its own exercise**
  Already settled where it counts: the bias is +4.3pp at 7 SEs in the window the gate is read on,
  which is decisive for the stopping rule without a cross-era replication whose magnitudes would be
  directional anyway (softer AvgW/AvgL consensus, degrading name joins in older seasons). The
  genuinely useful part - downloading the pre-2025 odds to create a clean fit window - survives as
  the prerequisite inside after-gate lever 4.

- **Reimplementing the BCa z0 / acceleration inside the test as a reference computation**
  Replaced by a golden interval plus a differs-from-plain-percentile assertion, which kills the same
  sabotage in a third of the lines. A parallel reimplementation doubles the maintenance surface
  while catching only transcription errors - it cannot catch a conceptual error shared by both
  copies, so it buys confidence it does not actually provide.

- **Splitting the scan-status / credit-balance / overdue-result signals into separate mechanisms or a status surface**
  All four belong in the one message the owner already reads daily, computed from columns already
  stored. Adding a separate monitoring path, a metrics surface, or new configurability for a single-
  user bot on an idle 1-vCPU box is exactly the unrequested machinery the simplicity rule forbids.

---

## Honest caveat — what this backlog will NOT fix

None of this creates edge, and nothing here changes the fact that the model does not beat the sharp
close. The measured position is untouched: Brier-optimal blend weight against Pinnacle is 0.00,
flat-stake ROI is -11% over ~5.8k held-out matches, and the model over-rates the market underdog by
+4.3pp at 7 SEs from structural under-dispersion that an n0 sweep moved by 5% and that better
calibration made ROI slightly WORSE. The most likely week-12 outcome is still a not-met gate. What
this backlog buys is that the answer is worth reading: a not-met will mean 'no edge' rather than
'the scheduled scan died in week 3 and nobody saw it', and a met will be decomposable into venue
basis versus post-entry line drift, checkable for a real claimed-edge gradient, and attributable to
a named tier - instead of one number the owner has to take on faith. It also lowers the human cost
of actually finishing 12 weeks and stops the sample being silently corrupted or lost. The after-gate
levers are honest about their ceiling too: the K floor addresses ~2.5% of held-out matches, so even
a clean pass cannot close an 11-point ROI gap. If the paper test comes back flat, the correct
conclusion is that a results-only pre-match Elo cannot beat this market and the decision is v2
(serve/return, different data) or quit - not lever number three. Two further limits: the sharp-at-
entry decomposition covers only bets logged after it ships, and every trust improvement here is
downstream of sharp coverage clearing 0.5 - below that there is no answer to interpret at all.

