#!/bin/zsh
set -u

ROOT="/Users/aiagent/GitHub/carousel-app"
UV="/opt/homebrew/bin/uv"
LOG_DIR="$ROOT/out/logs"
LOCK_DIR="$ROOT/state/reel_scheduler.lock"

mkdir -p "$LOG_DIR" "$ROOT/state"

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "[$(/bin/date -u '+%Y-%m-%dT%H:%M:%SZ')] reel scheduler already running; skipping"
  exit 0
fi

cleanup() {
  rmdir "$LOCK_DIR" 2>/dev/null || true
}
trap cleanup EXIT

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export UV_CACHE_DIR="$ROOT/state/uv-cache"
cd "$ROOT" || exit 1

echo "[$(/bin/date -u '+%Y-%m-%dT%H:%M:%SZ')] reel scheduler run start"
"$UV" run python scripts/sync_aibrief_facebook_queue.py
facebook_sync_exit=$?
if [ "$facebook_sync_exit" -ne 0 ]; then
  echo "[$(/bin/date -u '+%Y-%m-%dT%H:%M:%SZ')] aibrief Facebook queue sync exit=$facebook_sync_exit"
fi

"$UV" run python reel_scheduler.py run-due --upload-r2
instagram_exit=$?

facebook_exit=0
if [ "$facebook_sync_exit" -eq 0 ]; then
  "$UV" run python reel_scheduler.py run-due --platform facebook --channel aibrief_jp
  facebook_exit=$?
fi

"$UV" run python reel_scheduler.py report --out out/reel_report.html
report_exit=$?
if [ "$report_exit" -ne 0 ]; then
  echo "[$(/bin/date -u '+%Y-%m-%dT%H:%M:%SZ')] reel scheduler report exit=$report_exit"
fi
echo "[$(/bin/date -u '+%Y-%m-%dT%H:%M:%SZ')] reel scheduler run instagram_exit=$instagram_exit facebook_sync_exit=$facebook_sync_exit facebook_exit=$facebook_exit"
if [ "$instagram_exit" -ne 0 ] || [ "$facebook_sync_exit" -ne 0 ] || [ "$facebook_exit" -ne 0 ]; then
  exit 1
fi
exit 0
