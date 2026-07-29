"""DM the weekly-refresh outcome to Telegram, so a silent failure can't hide in a log file.

The refresh is the highest-consequence silent failure in the system: if the upstream feed moves,
`prepare_matches.py` warns and KEEPS the old archives, the bot keeps running happily, and the model
simply stops advancing for the rest of the paper test. That warning goes to logs/refresh.log, which
nobody reads. This sends the outcome to Telegram instead.

Called by scripts/weekly_refresh.sh, which runs it UNCONDITIONALLY (even when the refresh failed --
especially then). Usage:

    python scripts/refresh_notify.py <refresh_exit_code>
"""
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from matador.config import load_config, load_secrets  # noqa: E402

LOG = Path("logs/refresh.log")
_TAIL_BYTES = 20_000   # enough for one run's output


def _data_through(model_path: str) -> dict[str, str]:
    """{tour: 'YYYY-MM-DD'} from the model artifact, or {} if unavailable."""
    try:
        art = json.loads(Path(model_path).read_text())
    except (OSError, ValueError):
        return {}
    out = {}
    for tour, section in art.get("tours", {}).items():
        stamp = section.get("data_through")
        if stamp:
            out[tour] = date(stamp // 10000, (stamp // 100) % 100, stamp % 100).isoformat()
    return out


RUN_MARKER = "===== refresh "   # written by weekly_refresh.sh at the start of each run


def current_run_slice(log_tail: str) -> str:
    """Only THIS run's output. logs/refresh.log is append-only across the whole 12-week test, so
    scanning the raw tail meant one genuine rejection in week 2 produced a false FROZEN alarm every
    Monday thereafter -- and a boy-who-cried-wolf alarm is worse than none, because the real one
    stops being read. Slice from the last run marker; if absent, fall back to the whole tail (better
    a false alarm than a missed freeze)."""
    idx = log_tail.rfind(RUN_MARKER)
    return log_tail[idx:] if idx != -1 else log_tail


def build_message(exit_code: int, data_through: dict[str, str], log_tail: str) -> str:
    """The DM text. Pure so it can be tested without a network or a real log.

    Three outcomes worth distinguishing, because they need different responses:
      - FAILED: the model was not updated at all -> investigate now
      - REJECTED/FROZEN: the fetch ran and exited 0, but nothing usable arrived and the good
        archives were deliberately kept -> the model is FROZEN. Exit code CANNOT detect this: the
        fetch exits 0 BY DESIGN when it protects existing data.
      - OK: report the dates so a stalled-but-'successful' feed is still visible week over week
    """
    run = current_run_slice(log_tail)
    # Both warnings prepare_matches can emit: a changed/LFS body, and nothing-fetched-at-all.
    rejected = "rejected as unusable" in run or "the model is FROZEN" in run
    dates = ", ".join(f"{t.upper()} {d}" for t, d in sorted(data_through.items())) or "unknown"

    if exit_code != 0:
        return (f"🛑 Weekly refresh FAILED (exit {exit_code}) — the model was NOT updated.\n"
                f"Data still through: {dates}\n"
                f"Check logs/refresh.log on the droplet.")
    if rejected:
        return (f"⚠️ Weekly refresh: upstream files REJECTED as unusable — the feed has likely moved "
                f"(Git-LFS or a schema change). Existing archives were kept, so the model is FROZEN.\n"
                f"Data through: {dates}\n"
                f"Check logs/refresh.log — this is the failure that went unnoticed for weeks before "
                f"2026-07-27.")
    return f"🔄 Weekly refresh OK — model data through {dates}."


def main() -> None:
    exit_code = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    cfg = load_config()
    secrets = load_secrets()
    tail = ""
    if LOG.exists():
        with LOG.open("rb") as fh:                     # tail only: the log grows all season
            fh.seek(max(0, LOG.stat().st_size - _TAIL_BYTES))
            tail = fh.read().decode("utf-8", "replace")

    text = build_message(exit_code, _data_through(cfg.model_path), tail)
    print(f"[notify] {text.splitlines()[0]}")
    if not secrets.telegram_token or not secrets.telegram_chat_id:
        print("[notify] no TELEGRAM_TOKEN/CHAT_ID -- printed only, not sent")
        return
    try:
        r = httpx.post(f"https://api.telegram.org/bot{secrets.telegram_token}/sendMessage",
                       json={"chat_id": secrets.telegram_chat_id, "text": text}, timeout=20.0)
        print(f"[notify] telegram HTTP {r.status_code}")
    except httpx.HTTPError as exc:                     # never fail the cron over a notification
        print(f"[notify] send failed: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
