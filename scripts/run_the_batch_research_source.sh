#!/bin/zsh
set -u

ROOT="/Users/aiagent/GitHub/carousel-app"
NODE="/opt/homebrew/bin/node"
LOG_DIR="$ROOT/out/logs"
LOCK_DIR="$ROOT/state/the_batch_research_source.lock"
LOCK_PID="$LOCK_DIR/pid"
LOCK_STARTED="$LOCK_DIR/started_at"

mkdir -p "$LOG_DIR" "$ROOT/state"

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  existing_pid=""
  if [ -f "$LOCK_PID" ]; then
    existing_pid="$(cat "$LOCK_PID" 2>/dev/null || true)"
  fi
  if [ -n "$existing_pid" ] && kill -0 "$existing_pid" 2>/dev/null; then
    echo "[$(/bin/date -u '+%Y-%m-%dT%H:%M:%SZ')] The Batch research source already running as pid=$existing_pid; skipping"
    exit 0
  fi
  rm -f "$LOCK_PID" "$LOCK_STARTED"
  if ! rmdir "$LOCK_DIR" 2>/dev/null; then
    echo "[$(/bin/date -u '+%Y-%m-%dT%H:%M:%SZ')] The Batch research source lock is stale but not removable; skipping"
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

PROVIDER="${THE_BATCH_RESEARCH_PROVIDER:-local}"
ISSUE_PROVIDER="${THE_BATCH_ISSUE_PROVIDER:-gemini}"
CARDS="${THE_BATCH_RESEARCH_CARDS:-5}"
ISSUE_CARDS="${THE_BATCH_ISSUE_CARDS:-5}"
LOOKBACK_DAYS="${THE_BATCH_RESEARCH_LOOKBACK_DAYS:-10}"

echo "[$(/bin/date -u '+%Y-%m-%dT%H:%M:%SZ')] The Batch research source run start"
"$NODE" research_idea_generator/cli.ts run \
  --sources the_batch \
  --the-batch-live \
  --days "$LOOKBACK_DAYS" \
  --provider "$PROVIDER" \
  --cards "$CARDS" \
  --out out/research_idea_generator/the_batch/insight_cards.json \
  --report out/research_idea_generator/the_batch/report.md \
  --carousel-out out/research_idea_generator/the_batch/carousel_briefs.json \
  --runs-dir out/research_idea_generator/runs/the_batch \
  --no-archive
exit_code=$?
if [ "$exit_code" -ne 0 ]; then
  echo "[$(/bin/date -u '+%Y-%m-%dT%H:%M:%SZ')] The Batch research source run exit=$exit_code"
  exit "$exit_code"
fi

echo "[$(/bin/date -u '+%Y-%m-%dT%H:%M:%SZ')] The Batch issue brief run start"
"$NODE" research_idea_generator/cli.ts the-batch-issue \
  --the-batch-live \
  --days "$LOOKBACK_DAYS" \
  --provider "$ISSUE_PROVIDER" \
  --cards "$ISSUE_CARDS" \
  --cover-candidates-out out/research_idea_generator/the_batch/issue_cover_candidates.json \
  --carousel-out out/research_idea_generator/the_batch/issue_carousel_briefs.json \
  --source-items-out out/research_idea_generator/the_batch/issue_source_items.json \
  --report out/research_idea_generator/the_batch/issue_report.md \
  --runs-dir out/research_idea_generator/runs/the_batch_issue
issue_exit=$?
echo "[$(/bin/date -u '+%Y-%m-%dT%H:%M:%SZ')] The Batch issue brief run exit=$issue_exit"
exit "$issue_exit"
