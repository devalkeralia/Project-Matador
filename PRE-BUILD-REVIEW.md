# Pre-Build Design Review — 2026-07-02

Adversarial multi-agent review of the design docs + master prompt **before** building v1.
5 independent lenses (contract math, v1 model soundness, edge realism on Kalshi, master-prompt
buildability, validation methodology) → each finding verified against the docs (default-reject)
→ synthesized. **79 findings raised → 49 confirmed.**

**Verdict:** not ready to build *as written* — 2 must-fix contradictions, load-bearing for the
first thing Phase 1 builds. Both are now **fixed inline**; the should-fix list is captured as
phase-tagged action items in `MASTER-PROMPT.md`. Buildable once the must-fixes were resolved.

## Must-fix (fixed inline)
1. **v1 `p_model` path was self-contradictory** across all four docs — "surface Elo directly"
   vs "Elo → serve-point-win probability" (which only yields a match prob via the v2 recursion),
   so v1 had *no* specified Elo→p_model path. **Resolved:** v1 uses the direct logistic
   `p = 1/(1+10^((Elo_opp−Elo_self)/400))` on surface-weighted match Elo; serve model + recursion
   explicitly marked v2 (incl. Phase 2).
2. **No name-resolution join or abstain policy** — Kalshi name → Sackmann Elo mapping undefined,
   and no rule for players with no/thin history (bot would crash, drop, or fabricate an edge from
   a default rating). **Resolved:** canonical-key join spec + a model-exists abstain gate
   (`min_matches` default 20 → emit no alert).

## Should-fix (action items in MASTER-PROMPT §"Pre-build review — action items")
Core staking math **fixed inline**: net-of-fee Kelly (gross over-staked ~28%), `max_stake_pct`
(0.05) + `max_price` (0.95) caps, explicit No-side math, empty-book guard.

Deferred to the noted build phase:
- **Model:** Elo K-factor (`250/(n+5)^0.4`), init 1500, surface-blend scheme/weights, carpet &
  indoor/outdoor mapping; **match format (Bo3/Bo5)** as an input (calibrate Bo3/Bo5 separately);
  data-refresh cadence + max-staleness guard.
- **Edge/liquidity:** gate on order-book **depth at target ask** (not summary `liquidity_dollars`),
  define `max_spread`, provisional defaults via dry-run; alert a **limit price** + log realizable
  fill; **injury/withdrawal adverse-selection guard**; (nice-to-have) aggregate exposure cap.
- **Validation:** fully define the **CLV pipeline** (`closing_price` = price at match **start**,
  not settlement; consistent basis; capture path; CLV gross + fees separate); **go-live rule** =
  bootstrap 95% CI lower bound > 0 (200 = floor), not `mean(clv)>0`; strict **no-lookahead** +
  train/eval split; **segment backtest** by tier/round, exclude RET/walkover; **reframe** "beat
  Pinnacle" — the binding gate is forward CLV vs the **Kalshi** close.
- **Consistency:** one secrets convention stated in both docs (`secrets/.env` + `secrets/*.pem`);
  `/settings` vs `config.yaml` precedence; derive `event_tiers` from tournament name or drop it.

## Build-time verify
Cap-before-contracts ordering; `liquidity_dollars` vs executable depth; whether the Kalshi
**demo env** exposes tennis (else use production public read-only for market data); which
tennis-data.co.uk columns are **closing** odds; read `fee_coefficient` from config; treat
`KXATPEXACTMATCH` as out of scope.

_Full per-finding detail is in the workflow transcript (run `wf_9ebd9008-209`)._
