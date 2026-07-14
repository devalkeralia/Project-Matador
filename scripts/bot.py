"""Phase-4 launcher: run the Telegram value-alert bot. PAPER ONLY -- never places orders.

Reads Kalshi PRODUCTION market data (public, read-only) by default; --demo uses the demo base
from config. Long-polls Telegram for /check, /scan, /recent, /help. Requires TELEGRAM_TOKEN and
TELEGRAM_CHAT_ID in secrets/.env (the token is never printed).

    .venv/bin/python scripts/bot.py           # production reads
    .venv/bin/python scripts/bot.py --demo    # demo reads
"""
import argparse
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from matador.bot import build_application  # noqa: E402
from matador.config import load_config, load_secrets  # noqa: E402
from matador.model.artifact import Model  # noqa: E402

LOG_DIR = Path("logs")
LOG_FILE = LOG_DIR / "matador.log"
_MAX_BYTES = 5 * 1024 * 1024   # ~5 MB per file
_BACKUP_COUNT = 5              # keep 5 rotated files (~30 MB total)

log = logging.getLogger("matador.bot.launcher")


def _configure_logging() -> None:
    """File + console logging for the always-on run: a console handler (always) plus a rotating file
    handler (so an unattended multi-week paper-test can't fill the disk). The file handler is
    best-effort: if logs/ isn't writable (e.g. a root-owned Docker bind mount) we DEGRADE to
    console-only rather than crash the process into a restart loop. Docker's json-file driver
    captures stdout, so logs are never lost. Chatty libraries are pinned to WARNING. Called once,
    before the app is built."""
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()  # idempotent: a re-run replaces handlers rather than stacking them
    root.addHandler(console)
    try:
        LOG_DIR.mkdir(exist_ok=True)  # logs/ is gitignored
        file_handler = RotatingFileHandler(LOG_FILE, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT)
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError as exc:  # unwritable mount / read-only fs -> console-only, don't crash-loop
        root.warning("file logging disabled (%s); logging to console only", exc)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.INFO)
    logging.getLogger("matador").setLevel(logging.INFO)


def main() -> None:
    p = argparse.ArgumentParser(description="Matador Telegram value-alert bot (paper only; never places orders)")
    p.add_argument("--demo", action="store_true", help="use the demo base URL (default: Kalshi production, read-only)")
    args = p.parse_args()

    _configure_logging()

    cfg = load_config()
    secrets = load_secrets()
    if not secrets.telegram_token or not secrets.telegram_chat_id:
        raise SystemExit("TELEGRAM_TOKEN and TELEGRAM_CHAT_ID must be set in secrets/.env")

    model = Model.from_artifact(cfg.model_path)
    app = build_application(secrets.telegram_token, cfg, model, secrets.telegram_chat_id, demo=args.demo)
    log.info("Matador bot up (%s reads); polling Telegram. Ctrl-C to stop.", "DEMO" if args.demo else "PROD")
    app.run_polling()


if __name__ == "__main__":
    main()
