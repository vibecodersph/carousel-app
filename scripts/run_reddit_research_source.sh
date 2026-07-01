#!/bin/zsh
set -u

ROOT="/Users/aiagent/GitHub/carousel-app"
NODE="/opt/homebrew/bin/node"
LOG_DIR="$ROOT/out/logs"
LOCK_DIR="$ROOT/state/reddit_research_source.lock"

mkdir -p "$LOG_DIR" "$ROOT/state"

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "[$(/bin/date -u '+%Y-%m-%dT%H:%M:%SZ')] reddit research source already running; skipping"
  exit 0
fi

cleanup() {
  rmdir "$LOCK_DIR" 2>/dev/null || true
}
trap cleanup EXIT

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
cd "$ROOT" || exit 1

echo "[$(/bin/date -u '+%Y-%m-%dT%H:%M:%SZ')] reddit research source run start"
"$NODE" research_idea_generator/redditCollectorCli.ts collect \
  --max-subreddits 2 \
  --limit-per-listing 5 \
  --max-items 60 \
  --rss-delay-ms 10000
exit_code=$?
echo "[$(/bin/date -u '+%Y-%m-%dT%H:%M:%SZ')] reddit research source run exit=$exit_code"
exit $exit_code
