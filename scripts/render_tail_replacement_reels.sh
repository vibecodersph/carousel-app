#!/usr/bin/env bash
set -euo pipefail

PACK="out/aibrief_jp_growth_sprint_2026-07-03/tail_replacement_reel_briefs.json"
OUT_ROOT="out/aibrief_jp_growth_sprint_2026-07-03/rendered_tail_replacements"
CHANNEL="aibrief_jp"
LIMIT="3"
NO_MUSIC=""
IDS=()

usage() {
  cat <<'EOF'
Usage: scripts/render_tail_replacement_reels.sh [options]

Render local review videos from tail_replacement_reel_briefs.json.
This is render-only: it does not enqueue, publish, skip, or reschedule anything.
These renders are experimental local_text_reel mockups, not the canonical
long-form YouTube/podcast auto-cut Reel pipeline.

Options:
  --pack PATH       Brief pack JSON path.
  --out-root PATH   Output directory root.
  --channel ID      Channel id for branding.
  --limit N         Render the first N briefs by replacementPriority. Default: 3.
  --all             Render all briefs.
  --id BRIEF_ID     Render a specific brief id. May be repeated.
  --no-music        Render without music.
  -h, --help        Show this help.
EOF
}

while (($#)); do
  case "$1" in
    --pack)
      PACK="$2"
      shift 2
      ;;
    --out-root)
      OUT_ROOT="$2"
      shift 2
      ;;
    --channel)
      CHANNEL="$2"
      shift 2
      ;;
    --limit)
      LIMIT="$2"
      shift 2
      ;;
    --all)
      LIMIT=""
      shift
      ;;
    --id)
      IDS+=("$2")
      shift 2
      ;;
    --no-music)
      NO_MUSIC="1"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ ! -f "$PACK" ]]; then
  echo "Brief pack not found: $PACK" >&2
  exit 1
fi

mkdir -p "$OUT_ROOT/_briefs"

select_filter='.briefs | sort_by(.replacementPriority)'
if ((${#IDS[@]})); then
  id_json="$(printf '%s\n' "${IDS[@]}" | jq -R . | jq -s .)"
  select_filter=".briefs | map(select(.id as \$id | $id_json | index(\$id))) | sort_by(.replacementPriority)"
elif [[ -n "$LIMIT" ]]; then
  select_filter=".briefs | sort_by(.replacementPriority) | .[:$LIMIT]"
fi

ids=()
while IFS= read -r id; do
  [[ -n "$id" ]] && ids+=("$id")
done < <(jq -r "$select_filter | .[].id" "$PACK")

if ((${#ids[@]} == 0)); then
  echo "No briefs selected." >&2
  exit 1
fi

echo "[tail-render] selected ${#ids[@]} brief(s)"
for id in "${ids[@]}"; do
  priority="$(jq -r --arg id "$id" '.briefs[] | select(.id == $id) | .replacementPriority' "$PACK")"
  slug="$(printf '%02d_%s' "$priority" "$id")"
  brief_path="$OUT_ROOT/_briefs/$slug.json"
  out_dir="$OUT_ROOT/$slug"
  jq --arg id "$id" '.briefs[] | select(.id == $id)' "$PACK" > "$brief_path"

  echo "[tail-render] rendering $slug"
  if [[ -n "$NO_MUSIC" ]]; then
    uv run python build_text_reel.py --brief "$brief_path" --channel "$CHANNEL" --out-dir "$out_dir" --no-music
  else
    uv run python build_text_reel.py --brief "$brief_path" --channel "$CHANNEL" --out-dir "$out_dir"
  fi
done

echo "[tail-render] wrote assets under $OUT_ROOT"
