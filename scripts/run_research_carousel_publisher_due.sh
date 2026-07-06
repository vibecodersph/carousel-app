#!/bin/zsh
set -u

ROOT="/Users/aiagent/GitHub/carousel-app"
UV="/opt/homebrew/bin/uv"
NPM="/opt/homebrew/bin/npm"
LOG_DIR="$ROOT/out/logs"
LOCK_DIR="$ROOT/state/research_carousel_publisher.lock"

mkdir -p "$LOG_DIR" "$ROOT/state"

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "[$(/bin/date -u '+%Y-%m-%dT%H:%M:%SZ')] research carousel publisher already running; skipping"
  exit 0
fi

cleanup() {
  rmdir "$LOCK_DIR" 2>/dev/null || true
}
trap cleanup EXIT

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export TZ="Asia/Tokyo"
export UV_CACHE_DIR="$ROOT/state/uv-cache"
cd "$ROOT" || exit 1

echo "[$(/bin/date -u '+%Y-%m-%dT%H:%M:%SZ')] research carousel publisher run start"
"$NPM" run ideas:scan-briefs
scan_exit=$?
if [ "$scan_exit" -ne 0 ]; then
  echo "[$(/bin/date -u '+%Y-%m-%dT%H:%M:%SZ')] research queue scan exit=$scan_exit"
  exit "$scan_exit"
fi

"$UV" run python research_carousel_queue_renderer.py --channel aibrief_jp --limit 1 --generate-images --localize-copy --cover-style aibrief-study --cover-template auto
render_exit=$?
if [ "$render_exit" -ne 0 ]; then
  echo "[$(/bin/date -u '+%Y-%m-%dT%H:%M:%SZ')] research carousel render exit=$render_exit"
  exit "$render_exit"
fi

"$UV" run python research_carousel_publisher.py --channel aibrief_jp
publish_exit=$?
echo "[$(/bin/date -u '+%Y-%m-%dT%H:%M:%SZ')] research carousel publisher run exit=$publish_exit"
exit "$publish_exit"
