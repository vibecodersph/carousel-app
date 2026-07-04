#!/bin/zsh
set -eu

ROOT="/Users/aiagent/GitHub/carousel-app"
UV="/opt/homebrew/bin/uv"
PLATFORM="${AIBRIEF_PLATFORM:-instagram}"
CHANNEL="${AIBRIEF_CHANNEL:-aibrief_jp}"
DB="${AIBRIEF_REFLOW_DB:-$ROOT/state/reels.db}"
START_AT="${AIBRIEF_REFLOW_START:-2026-07-04}"
LOG_DIR="$ROOT/out/logs"
BACKUP_DIR="$ROOT/state/backups"
LOCK_DIR="$ROOT/state/reel_scheduler.lock"
APPLY=0

usage() {
  printf '%s\n' \
    "Usage: scripts/apply_aibrief_4day_reflow.sh [--apply] [--start-at YYYY-MM-DD]" \
    "" \
    "Dry-run is the default. --apply backs up the live DB before mutating the queue." \
    "Environment overrides: AIBRIEF_PLATFORM, AIBRIEF_CHANNEL, AIBRIEF_REFLOW_DB, AIBRIEF_REFLOW_START"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --apply)
      APPLY=1
      shift
      ;;
    --dry-run)
      APPLY=0
      shift
      ;;
    --start-at)
      if [ "$#" -lt 2 ]; then
        echo "[aibrief-4day] --start-at requires a value" >&2
        exit 2
      fi
      START_AT="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      START_AT="$1"
      shift
      ;;
  esac
done

mkdir -p "$LOG_DIR" "$BACKUP_DIR" "$ROOT/state"
STAMP="$(TZ=Asia/Tokyo /bin/date '+%Y%m%d-%H%M%S-JST')"
LOG_FILE="$LOG_DIR/aibrief_4day_reflow_$STAMP.log"

exec > >(tee -a "$LOG_FILE") 2>&1

if [ ! -f "$DB" ]; then
  echo "[aibrief-4day] DB not found: $DB"
  exit 1
fi

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "[aibrief-4day] reel scheduler lock exists; refusing to reflow while scheduler is running"
  exit 1
fi

cleanup() {
  rmdir "$LOCK_DIR" 2>/dev/null || true
}
trap cleanup EXIT

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export UV_CACHE_DIR="$ROOT/state/uv-cache"
cd "$ROOT" || exit 1

echo "[aibrief-4day] log: $LOG_FILE"
echo "[aibrief-4day] platform=$PLATFORM channel=$CHANNEL db=$DB start_at=$START_AT"

cmd=("$UV" run python reel_scheduler.py reflow-queue --platform "$PLATFORM" --channel "$CHANNEL" --db "$DB" "$START_AT")

if [ "$APPLY" -eq 1 ]; then
  BACKUP="$BACKUP_DIR/reels-before-aibrief-4day-$STAMP.db"
  cp "$DB" "$BACKUP"
  echo "[aibrief-4day] backed up DB: $BACKUP"
  cmd+=(--apply)
else
  echo "[aibrief-4day] dry-run only; no live queue mutation will be made"
fi

"${cmd[@]}"

if [ "$APPLY" -ne 1 ]; then
  echo "[aibrief-4day] dry-run complete. Re-run with --apply to update the live scheduler ledger."
  exit 0
fi

"$UV" run python reel_scheduler.py report --out out/reel_report.html

AIBRIEF_VERIFY_DB="$DB" AIBRIEF_VERIFY_CHANNEL="$CHANNEL" "$UV" run python - <<'PY'
import os
import sqlite3
from pathlib import Path

path = Path(os.environ["AIBRIEF_VERIFY_DB"]).resolve()
channel = os.environ["AIBRIEF_VERIFY_CHANNEL"]
conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
try:
    print("[aibrief-4day] scheduled rows by date:")
    for day, count in conn.execute(
        """
        select substr(scheduled_at, 1, 10) as day, count(*)
        from reels
        where channel_id = ? and status = 'scheduled'
        group by day
        order by day
        limit 12
        """,
        [channel],
    ):
        print(f"{day}|{count}")
finally:
    conn.close()
PY

echo "[aibrief-4day] apply complete"
