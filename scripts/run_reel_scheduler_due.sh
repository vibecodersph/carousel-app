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
"$UV" run python reel_scheduler.py run-due --upload-r2
exit_code=$?
"$UV" run python reel_scheduler.py report --out out/reel_report.html
report_exit=$?
if [ "$report_exit" -ne 0 ]; then
  echo "[$(/bin/date -u '+%Y-%m-%dT%H:%M:%SZ')] reel scheduler report exit=$report_exit"
fi
echo "[$(/bin/date -u '+%Y-%m-%dT%H:%M:%SZ')] reel scheduler run exit=$exit_code"
exit $exit_code
