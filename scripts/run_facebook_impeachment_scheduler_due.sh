#!/bin/zsh
set -u

ROOT="/Users/aiagent/GitHub/carousel-app"
UV="/opt/homebrew/bin/uv"
LOG_DIR="$ROOT/out/logs"
LOCK_DIR="$ROOT/state/facebook_impeachment_scheduler.lock"

CHANNEL="${FACEBOOK_IMPEACHMENT_CHANNEL:-vibecodersph}"
OUTPUTS_ROOT="${FACEBOOK_IMPEACHMENT_OUTPUTS_ROOT:-/Users/aiagent/GitHub/reel-app/outputs/impeachments_news}"
DB="${FACEBOOK_IMPEACHMENT_DB:-$ROOT/state/facebook_impeachment.db}"
OUT_DIR="${FACEBOOK_IMPEACHMENT_OUT_DIR:-$ROOT/out/reel_schedules/facebook_impeachment}"
REPORT_OUT="${FACEBOOK_IMPEACHMENT_REPORT_OUT:-$ROOT/out/facebook_impeachment_report.html}"
QUEUE_MODE="${FACEBOOK_IMPEACHMENT_QUEUE_MODE:-reshuffle}"

mkdir -p "$LOG_DIR" "$ROOT/state" "$OUT_DIR"

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "[$(/bin/date -u '+%Y-%m-%dT%H:%M:%SZ')] facebook impeachment scheduler already running; skipping"
  exit 0
fi

cleanup() {
  rmdir "$LOCK_DIR" 2>/dev/null || true
}
trap cleanup EXIT

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export UV_CACHE_DIR="$ROOT/state/uv-cache"
cd "$ROOT" || exit 1

echo "[$(/bin/date -u '+%Y-%m-%dT%H:%M:%SZ')] facebook impeachment scheduler run start"
echo "[$(/bin/date -u '+%Y-%m-%dT%H:%M:%SZ')] channel=$CHANNEL outputs=$OUTPUTS_ROOT db=$DB"
echo "[$(/bin/date -u '+%Y-%m-%dT%H:%M:%SZ')] queue_mode=$QUEUE_MODE reshuffle_only_if_new=1"

queue_exit=0
if [ -d "$OUTPUTS_ROOT" ]; then
  "$UV" run python reel_scheduler.py queue-outputs "$OUTPUTS_ROOT" \
    --platform facebook \
    --channel "$CHANNEL" \
    --db "$DB" \
    --out-dir "$OUT_DIR" \
    --report-out "$REPORT_OUT" \
    --mode "$QUEUE_MODE" \
    --reshuffle-only-if-new
  queue_exit=$?
else
  echo "[$(/bin/date -u '+%Y-%m-%dT%H:%M:%SZ')] outputs root missing; queue step skipped"
fi

"$UV" run python reel_scheduler.py run-due \
  --platform facebook \
  --channel "$CHANNEL" \
  --db "$DB"
publish_exit=$?

"$UV" run python reel_scheduler.py report \
  --platform facebook \
  --channel "$CHANNEL" \
  --db "$DB" \
  --out "$REPORT_OUT"
report_exit=$?
if [ "$report_exit" -ne 0 ]; then
  echo "[$(/bin/date -u '+%Y-%m-%dT%H:%M:%SZ')] facebook impeachment report exit=$report_exit"
fi

echo "[$(/bin/date -u '+%Y-%m-%dT%H:%M:%SZ')] facebook impeachment scheduler queue_exit=$queue_exit publish_exit=$publish_exit"
if [ "$queue_exit" -ne 0 ]; then
  exit "$queue_exit"
fi
exit "$publish_exit"
