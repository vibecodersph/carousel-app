#!/bin/zsh
set -u

ROOT="/Users/aiagent/GitHub/carousel-app"
NODE="/opt/homebrew/bin/node"
LOG_DIR="$ROOT/out/logs"
LOCK_DIR="$ROOT/state/ai_news_research_source.lock"
LOCK_PID="$LOCK_DIR/pid"
LOCK_STARTED="$LOCK_DIR/started_at"

mkdir -p "$LOG_DIR" "$ROOT/state"

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  existing_pid=""
  if [ -f "$LOCK_PID" ]; then
    existing_pid="$(cat "$LOCK_PID" 2>/dev/null || true)"
  fi
  if [ -n "$existing_pid" ] && kill -0 "$existing_pid" 2>/dev/null; then
    echo "[$(/bin/date -u '+%Y-%m-%dT%H:%M:%SZ')] AINews research source already running as pid=$existing_pid; skipping"
    exit 0
  fi
  rm -f "$LOCK_PID" "$LOCK_STARTED"
  if ! rmdir "$LOCK_DIR" 2>/dev/null; then
    echo "[$(/bin/date -u '+%Y-%m-%dT%H:%M:%SZ')] AINews research source lock is stale but not removable; skipping"
    exit 1
  fi
  mkdir "$LOCK_DIR" || exit 1
fi
echo "$$" > "$LOCK_PID"
/bin/date -u '+%Y-%m-%dT%H:%M:%SZ' > "$LOCK_STARTED"

cleanup() {
  rm -f "$LOCK_PID" "$LOCK_STARTED"
  rmdir "$LOCK_DIR" 2>/dev/null || true
}
trap cleanup EXIT

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export TZ="Asia/Tokyo"
cd "$ROOT" || exit 1

PROVIDER="${AI_NEWS_ISSUE_PROVIDER:-gemini}"
CARDS="${AI_NEWS_ISSUE_CARDS:-5}"
LOOKBACK_DAYS="${AI_NEWS_LOOKBACK_DAYS:-10}"
ISSUE_URL="${AI_NEWS_ISSUE_URL:-}"

echo "[$(/bin/date -u '+%Y-%m-%dT%H:%M:%SZ')] AINews issue brief run start"
ARGS=(
  research_idea_generator/cli.ts ai-news-issue
  --ai-news-live
  --days "$LOOKBACK_DAYS"
  --provider "$PROVIDER"
  --cards "$CARDS"
  --cover-candidates-out out/research_idea_generator/ai_news/issue_cover_candidates.json
  --carousel-out out/research_idea_generator/ai_news/issue_carousel_briefs.json
  --source-items-out out/research_idea_generator/ai_news/issue_source_items.json
  --report out/research_idea_generator/ai_news/issue_report.md
  --runs-dir out/research_idea_generator/runs/ai_news_issue
)
if [ -n "$ISSUE_URL" ]; then
  ARGS+=(--ai-news-issue-url "$ISSUE_URL")
fi

"$NODE" "${ARGS[@]}"
issue_exit=$?
echo "[$(/bin/date -u '+%Y-%m-%dT%H:%M:%SZ')] AINews issue brief run exit=$issue_exit"
exit "$issue_exit"
