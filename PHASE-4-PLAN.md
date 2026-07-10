# Phase 4 Plan — Telegram Alerts (PARKED, not yet built)

> **Status (2026-07-10):** Phases 1–3 shipped and pushed (`main` @ `6e46e6b`, 153 tests). This is the
> approved, researched plan for **Phase 4**, held for later execution per the user. Nothing here is
> built yet. To resume: implement the Staging steps below (start with the `market_player` enabler).
> Delete this file once Phase 4 is committed. Companion context lives in the auto-loaded build-status
> memory and in `MASTER-PROMPT.md` (Phase-4 spec) / `DESIGN-DECISIONS.md`.

## Context

**Why:** Phase 3 built the edge engine that turns a live Kalshi match into a logged `prematch_value`
opportunity. Phase 4 is the **delivery layer**: an always-on Telegram bot that lets the user trigger
the engine on-demand and receive formatted **VALUE ALERTs**, feeding the forward-CLV paper-testing that
is the go-live bar.

**What it does:** long-polls Telegram for commands; on `/check` or `/scan` it hits Kalshi (read-only),
runs the Phase-3 engine, replies with a formatted alert (or a friendly abstain), and logs qualifying
opportunities to SQLite. **PAPER ONLY — never places orders. On-demand only — never polls Kalshi on a
timer** (the bot long-polls *Telegram* for commands; Kalshi is hit only when a command arrives).

**Decisions (confirmed):** (1) add a `market_player` field so alerts can say `BUY YES "Sinner wins"`
and the log is self-describing; (2) ship `/check`, `/scan`, `/recent`, `/help` — defer `/result` +
`/stats` (CLV) to Phase 5, `/settings` to later, `/watch`/in-play to v2; (3) reads hit Kalshi
**production** (read-only) by default, `--demo` toggle (mirrors `scripts/scan.py`).

## Approach

Mirror the existing split (pure logic in `matador/`, thin entrypoint in `scripts/`). Keep a
**PTB-free, dependency-injected sync core** that's fully unit-testable; the async Telegram handlers are
thin wrappers that offload the blocking core via `asyncio.to_thread`. Reuse the engine — do **not**
reimplement edge/resolve/log.

### New / changed files
- **`matador/alerts.py`** (new, pure — no PTB, no I/O): the formatters.
  - `format_alert(opp, opp_id, bankroll) -> str` → the mandated template:
    ```
    🎾 VALUE ALERT — {tour} · {event}
    {match} · pre-match
    BUY {SIDE} "{market_player} wins" @ {price*100:.0f}¢  ({market_ticker}, depth ~${liquidity*price:.0f})
    Model {p_model:.1%} | Market {price*100:.0f}¢ | Net edge {net_edge:+.1%} (after fee)
    Stake ${suggested_stake:.0f} → {contracts} contracts (¼-Kelly on net edge, bankroll ${bankroll:,.0f})
    opp #{opp_id}
    ⚠️ Large edge — check for late news (injury/withdrawal)   # only if opp.flagged
    ```
    (Round `(R2)` and the deferred limit-price `≤55¢` line are omitted — not in the data; show the
    honest computed depth. Verify the ladder size unit against one live book — drop `*price` if sizes
    are already dollars.)
  - `format_abstain(reason) -> str`: reason→friendly-text map with prefix matching for parameterized
    reasons (`insufficient_history(...)`, `error:*`, `unknown_tour(*)`) + a `f"Abstained: {reason}"`
    fallback. Covers all engine abstain reasons (empty_book, unresolved_market/_player,
    no_series_for_tour, no_edge, one_sided_book, spread_too_wide, insufficient_liquidity, stale_ratings).
  - `format_recent(rows) -> str` and `format_scan(alerts, abstain_tally, bankroll) -> str`.
- **`matador/bot.py`** (new): PTB app + testable sync cores + async handlers.
  - **Sync cores (PTB-free, testable):** `run_check(client, model, cfg, conn, tour, a, b) -> str`
    (evaluate_match → if alert log_opportunity[dedup] + format_alert, showing the prior id via
    `last_opportunity` on dedup; else format_abstain); `run_scan(client, model, cfg, conn, tours) -> str`
    (per configured tour with a series, iterate `scan_series`, log alerts, tally abstains → format_scan);
    `run_recent(conn, n) -> str`.
  - `parse_check_args(text, default_tour) -> tuple[a,b,tour] | None` — non-raising replacement for
    `scan.py._split_players` (peels a trailing `atp`/`wta`, splits on `r"\s+vs?\.?\s+"`); returns None
    on failure so the handler replies a usage string.
  - `is_authorized(chat_id, allowed) -> bool` — tiny pure guard (testable).
  - Per-command resource wrappers (run in the worker thread): open a fresh `KalshiClient` (PROD via
    `_client(cfg, demo)`, no signer — reads are public) **and** a fresh `storage.connect(cfg.db_path)`
    + `init_db` **inside the thread** (sidesteps sqlite thread-affinity), call the core, close both.
  - Async handlers `cmd_check/cmd_scan/cmd_recent/cmd_help`: chat-auth check → `await
    asyncio.to_thread(job, ...)` → `_reply_chunked` (≤4096 chars). `cfg`, `model`, `demo`,
    `default_tour="atp"` live in `application.bot_data` (model loaded once, shared read-only).
- **`scripts/bot.py`** (new, thin launcher): `main()` — `load_config` + `load_secrets`; **fail fast** if
  `telegram_token`/`telegram_chat_id` unset (never print the token); `chat_id=int(...)`; load Model once;
  `ApplicationBuilder().token(...).build()`; register the 4 `CommandHandler`s with
  `filters.Chat(chat_id=...)` (belt-and-suspenders with the in-handler `is_authorized` check);
  `app.run_polling()`. `--demo` argparse flag (default = production reads).
- **`matador/engine.py`** (small, the confirmed enabler): add `market_player: str` to `Opportunity`
  (set from `resolution.yes_sub_title` in `evaluate_resolution`) + include it in the `log_opportunity`
  insert.
- **`matador/storage.py`**: add a nullable `market_player TEXT` column to the opportunities `SCHEMA`
  and to `_OPPORTUNITY_COLUMNS` (`CREATE TABLE IF NOT EXISTS` + nullable = existing rows stay valid).

### Reused unchanged
`matador/engine.py` (`evaluate_match`, `scan_series`, `log_opportunity`), `matador/model/artifact.py`
(`Model.from_artifact`), `matador/kalshi/client.py` (`KalshiClient`, sync), `matador/config.py`
(`load_config`/`load_secrets`, `Secrets.telegram_token`/`telegram_chat_id`), `matador/storage.py`
(`connect`/`init_db`/`recent_opportunities`/`last_opportunity`), and `scripts/scan.py`'s `PROD_BASE`
/`_client` pattern.

### Sync ↔ async bridge (the core foot-gun)
PTB v21 handlers run on one asyncio loop; the engine + `KalshiClient` are blocking (incl. `time.sleep`
429/503 backoff). Every handler offloads its blocking job with `await asyncio.to_thread(...)` so the
bot stays responsive. `/scan` offloads the **whole** sweep in one `to_thread` call. Each command opens
its own `KalshiClient` + sqlite connection **inside** the worker thread (thread-safe); `Model` is loaded
once at startup and shared read-only.

### Dependency
Add `"python-telegram-bot>=21.9"` to `[project].dependencies` in `pyproject.toml`; install via
`.venv/bin/pip install -e ".[dev]"`. **httpx-compat check:** PTB pins an httpx range; httpx-0.28 support
is in PTB 21.9+/22.x. After install, run `.venv/bin/pip show python-telegram-bot httpx` + `pip check`
and re-run the full suite to confirm httpx was **not** downgraded from 0.28 (which would risk the
engine/client tests). If it conflicts, bump the pin to the PTB release whose metadata allows
`httpx==0.28.*`. Base install only (no extras — long-polling needs none).

### Guardrail (structural)
The bot imports/calls only read endpoints (`get_events`/`get_markets`/`get_orderbook`/`resolve_match`)
+ `storage.insert_opportunity` (paper). No order/trade endpoint is reachable; no signer is constructed;
no Kalshi timer/poll. Chat-auth restricts every command to the owner's `telegram_chat_id`.

## Testing
Reuse the `test_engine.py` patterns (copy the ~15 lines of `make_client`/`OrientedModel`/`LIQUID_BOOK`/
`make_cfg`/`_opp` helpers — no `tests/__init__.py`, so don't cross-import test modules).
- **`tests/test_alerts.py`** (pure): `format_alert` ¢/%/$ rendering, opp_id vs dedup note, flagged ⚠️
  line, Yes vs No phrasing, depth note; `format_abstain` every reason + prefix matches + fallback;
  `format_recent` empty/rows/flagged.
- **`tests/test_bot.py`** (sync cores via MockTransport client + fake model + `:memory:` sqlite):
  `run_check` alert (contains BUY/price/opp id + logs a row) / dedup (prior-id note, no 2nd row) /
  abstain paths (no row); `parse_check_args` good + garbage + trailing tour; `run_scan` alert-block +
  abstain tally + dedup; `run_recent`; `is_authorized` true/false.
- Async handlers + `main()` are **not** unit-tested (parity with `scan.py`), but add a lightweight
  `import matador.bot` smoke test (handlers/help build without a token) to catch wiring regressions
  after the PTB install. Update `tests/test_engine.py`/`test_storage.py` `_opp`/`make_opportunity` for
  the new `market_player` field.

## Deferred (out of Phase 4)
`/result` + `/stats` + CLV computation (Phase 5 — `record_outcome` already exists; `occurrence_datetime`
is the hook); `/settings` (later); `/watch` + in-play/live-score (v2); VPS deployment (user runs it);
the limit-price/VWAP `≤55¢` alert line (deferred Phase-3 feature); `--force` on `/check` (dedup note is
friendlier).

## Staging
1. `market_player` enabler (engine + storage) + update `_opp`/`make_opportunity`; suite green.
2. `matador/alerts.py` (format_alert/format_abstain) + `tests/test_alerts.py`.
3. `matador/bot.py` sync core (`run_check`, `parse_check_args`, `is_authorized`, `_check_job`) +
   `tests/test_bot.py`; add PTB to pyproject + install + verify httpx.
4. `scripts/bot.py` launcher (`/check` + chat-auth + run_polling) — **minimal end-to-end bot**.
5. `/scan` (`run_scan`/`format_scan`/`_scan_job` + handler + chunking) + tests.
6. `/recent` + `/help` (+ `/start` alias) + import/smoke test.

## Verification
- `.venv/bin/pip install -e ".[dev]"` → `pip show python-telegram-bot httpx` + `pip check` (httpx still 0.28.x).
- `.venv/bin/python -m pytest -q` — full suite (153 existing + new) green.
- Dry smoke: construct an `Opportunity` and print `format_alert(...)`, eyeball vs the template; `import matador.bot`.
- **Manual live run (needs real creds — cannot run headless here):** put a BotFather `TELEGRAM_TOKEN`
  + your `TELEGRAM_CHAT_ID` in `secrets/.env`; `.venv/bin/python scripts/bot.py` (prod reads; `--demo`
  for demo). From your chat: `/check Dimitrov v Berrettini atp`, `/scan`, `/recent`, `/help`. Confirm:
  a foreign chat gets no reply (auth); alerts match the template; rows land in `data/matador.db`
  (`sqlite3 data/matador.db "select id,match,market_player,side,price from opportunities order by id desc limit 5"`);
  re-sending `/check` shows the "already logged" dedup note; `/scan` + `/check` back-to-back both
  respond (event loop not blocked).
- Guardrail grep: no POST/order path in the new files; no signer constructed.
