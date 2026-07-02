#!/bin/zsh
set -u

ROOT="/Users/aiagent/GitHub/carousel-app"
NODE="/opt/homebrew/bin/node"
LOG_DIR="$ROOT/out/logs"
LOCK_DIR="$ROOT/state/the_batch_research_source.lock"

mkdir -p "$LOG_DIR" "$ROOT/state"

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "[$(/bin/date -u '+%Y-%m-%dT%H:%M:%SZ')] The Batch research source already running; skipping"
  exit 0
fi

cleanup() {
  rmdir "$LOCK_DIR" 2>/dev/null || true
}
trap cleanup EXIT

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
cd "$ROOT" || exit 1

PROVIDER="${THE_BATCH_RESEARCH_PROVIDER:-local}"
CARDS="${THE_BATCH_RESEARCH_CARDS:-5}"
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
  --runs-dir out/research_idea_generator/runs/the_batch
exit_code=$?
echo "[$(/bin/date -u '+%Y-%m-%dT%H:%M:%SZ')] The Batch research source run exit=$exit_code"
exit $exit_code
