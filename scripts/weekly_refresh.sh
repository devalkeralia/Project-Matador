#!/usr/bin/env bash
# Weekly model refresh for the always-on paper test. Installed as a Monday 06:00 UTC cron:
#
#   0 6 * * 1 /home/matador/matador/scripts/weekly_refresh.sh >> /home/matador/matador/logs/refresh.log 2>&1
#
# A script rather than a monster crontab line so it is version-controlled and testable, and so the
# notification can run UNCONDITIONALLY. The refresh steps are &&-chained (no point building ratings
# from a failed fetch), but the notify step must fire even -- especially -- when they fail, otherwise
# the one outcome you need to hear about is the one that stays silent.
#
# --fetch is mandatory: without it the cron re-processes stale CSVs and the model silently freezes.
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
DOCKER=${DOCKER:-/usr/bin/docker}          # cron has a minimal PATH
RUN="$DOCKER compose run --rm --entrypoint python matador"

echo "===== refresh $(date -u +%FT%TZ) ====="

rc=0
$RUN scripts/prepare_matches.py --fetch && $RUN scripts/build_ratings.py || rc=$?

if [ "$rc" -eq 0 ]; then
    # Restart so the bot loads the new artifact. Safe: pending closing-line captures are rebuilt
    # from the DB on startup, so none are lost.
    $DOCKER compose restart matador || rc=$?
fi

# ALWAYS notify -- a failed refresh is precisely what must not go unreported.
$RUN scripts/refresh_notify.py "$rc" || echo "[warn] notify step itself failed"

echo "===== refresh done (exit $rc) ====="
exit "$rc"
