"""Phase-4 launcher: run the Telegram value-alert bot. PAPER ONLY -- never places orders.

Reads Kalshi PRODUCTION market data (public, read-only) by default; --demo uses the demo base
from config. Long-polls Telegram for /check, /scan, /recent, /help. Requires TELEGRAM_TOKEN and
TELEGRAM_CHAT_ID in secrets/.env (the token is never printed).

    .venv/bin/python scripts/bot.py           # production reads
    .venv/bin/python scripts/bot.py --demo    # demo reads
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from matador.bot import build_application  # noqa: E402
from matador.config import load_config, load_secrets  # noqa: E402
from matador.model.artifact import Model  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description="Matador Telegram value-alert bot (paper only; never places orders)")
    p.add_argument("--demo", action="store_true", help="use the demo base URL (default: Kalshi production, read-only)")
    args = p.parse_args()

    cfg = load_config()
    secrets = load_secrets()
    if not secrets.telegram_token or not secrets.telegram_chat_id:
        raise SystemExit("TELEGRAM_TOKEN and TELEGRAM_CHAT_ID must be set in secrets/.env")

    model = Model.from_artifact(cfg.model_path)
    app = build_application(secrets.telegram_token, cfg, model, secrets.telegram_chat_id, demo=args.demo)
    print(f"Matador bot up ({'DEMO' if args.demo else 'PROD'} reads); polling Telegram. Ctrl-C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
