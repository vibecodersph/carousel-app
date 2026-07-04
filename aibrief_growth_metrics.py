#!/usr/bin/env python3
"""Summarize AI Brief JP growth sprint metrics.

The sprint queue is intentionally manual-approval only. This helper reads the
queue plus `metrics_log.csv` and prints the next measurement/action state.
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import datetime
import json
import re
import sqlite3
from pathlib import Path
from typing import Any


DEFAULT_ROOT = Path("out/aibrief_jp_growth_sprint_2026-07-03")
DEFAULT_QUEUE = DEFAULT_ROOT / "growth_publish_queue.json"
DEFAULT_METRICS = DEFAULT_ROOT / "metrics_log.csv"
DEFAULT_DB = Path("state/reels.db")
DEFAULT_SCHEDULE_REPORT = DEFAULT_ROOT / "manual_vs_live_schedule_report.md"
DEFAULT_REPLACEMENT_REPORT = DEFAULT_ROOT / "manual_replacement_candidates.md"
DEFAULT_TAIL_RISK_REPORT = DEFAULT_ROOT / "post_reflow_tail_risk_report.md"
DEFAULT_TAIL_REPLACEMENT_PLAN = DEFAULT_ROOT / "tail_diversity_replacement_plan.md"
DEFAULT_SOURCE_BACKLOG = DEFAULT_ROOT / "auto_cut_source_backlog_2026-07-03.json"
DEFAULT_SOURCE_INTAKE_REPORT = DEFAULT_ROOT / "auto_cut_source_intake_report.md"
DEFAULT_MANUAL_WATCH_APPROVAL_QUEUE = DEFAULT_ROOT / "fresh_manual_watch_approval_queue_2026-07-03.json"
DEFAULT_REEL_OUTPUTS = Path("/Users/aiagent/GitHub/reel-app/outputs")
DEFAULT_SOURCE_FRESHNESS_CUTOFF = "2026-03-03"
DEFAULT_PUBLISHED_METRICS_SINCE = "2026-07-03"
FOLLOW_CTA = "毎日のAI開発ニュースはフォローでチェック"
OLD_SAVE_CTA = "気になったら保存して"
TOP_LEVEL_PATH_KEYS = (
    "followUpReelsPath",
    "hookBankPath",
    "planPath",
    "operatorChecklistPath",
    "postingAssetsPath",
    "metricsLogPath",
    "day1LaunchPacketPath",
    "day2ReelPacketPath",
    "day3CareerReelPacketPath",
    "day4ScienceLaunchPacketPath",
    "day6ProductSafetyReelPacketPath",
    "day6ProductSafetyReelBriefPath",
    "day6ProductSafetyReelManifestPath",
    "day6ProductSafetyReelPath",
    "day6ProductSafetyReelContactSheetPath",
    "day6ProductSafetyReelDryRunReportPath",
    "platformGuidancePath",
    "captionRefreshPreviewPath",
    "queueGrowthAuditPath",
    "alternateSourcePreviewPath",
    "schedulerApprovalPlanPath",
    "manualVsLiveScheduleReportPath",
    "manualReplacementCandidateReportPath",
    "cadence4PerDayPlanPath",
    "cadence4PerDayApplyHelperPath",
    "postReflowTailRiskReportPath",
    "autoCutSourceBacklogPath",
    "autoCutSourceBacklogJsonPath",
    "autoCutSourceIntakeReportPath",
    "tailDiversityReplacementPlanPath",
    "tailReplacementBriefsPath",
    "tailReplacementBriefsSummaryPath",
    "tailReplacementRenderHelperPath",
    "tailReplacementRenderedReviewPath",
    "tailReplacementRenderedReviewRoot",
    "chromeRecentPublicSignalsPath",
    "nextReelReplacementApprovalPacketPath",
    "profileSnapshotPath",
)
ITEM_PATH_KEYS = (
    "manifestPath",
    "contactSheetPath",
    "dryRunReportPath",
    "launchPacketPath",
    "reelLaunchPacketPath",
    "reelBriefPath",
    "renderedReelManifestPath",
    "renderedReelPath",
    "renderedReelContactSheetPath",
    "reelDryRunReportPath",
)


def int_value(value: object) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(float(text.replace(",", "")))
    except ValueError:
        return None


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_metrics(path: Path) -> dict[str, dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return {row["post_id"]: row for row in csv.DictReader(handle) if row.get("post_id")}


def iso_datetime(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def post_id_date(post_id: str) -> str | None:
    match = re.search(r"(20\d{6})$", post_id)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%d").date().isoformat()
    except ValueError:
        return None


def parse_source_date(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d{8}", text):
        try:
            return datetime.strptime(text, "%Y%m%d").date().isoformat()
        except ValueError:
            return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            return datetime.fromisoformat(text).date().isoformat()
        except ValueError:
            return None
    parsed = iso_datetime(text)
    if parsed is not None:
        return parsed.date().isoformat()
    return None


def table_cell(value: object, *, max_len: int = 120) -> str:
    text = str(value or "").replace("\n", " ").replace("|", "/").strip()
    text = re.sub(r"\s+", " ", text)
    if len(text) > max_len:
        return f"{text[: max_len - 3]}..."
    return text


def short_time(value: str) -> str:
    parsed = iso_datetime(value)
    if parsed is None:
        return table_cell(value, max_len=16)
    return parsed.strftime("%H:%M")


def nearest_gap_minutes(manual_items: list[dict[str, Any]], live_rows: list[dict[str, str]]) -> str:
    manual_times = [iso_datetime(item.get("recommendedPublishAt")) for item in manual_items]
    live_times = [iso_datetime(row.get("scheduled_at")) for row in live_rows]
    gaps: list[int] = []
    for manual_time in manual_times:
        if manual_time is None:
            continue
        for live_time in live_times:
            if live_time is None:
                continue
            gaps.append(round(abs((manual_time - live_time).total_seconds()) / 60))
    if not gaps:
        return "-"
    return str(min(gaps))


def metric(row: dict[str, str], key: str) -> int | None:
    return int_value(row.get(key))


def follower_delta(row: dict[str, str], horizon: str) -> int | None:
    before = metric(row, "followers_before")
    after = metric(row, f"followers_{horizon}")
    if before is None or after is None:
        return None
    return after - before


def follows_24h(row: dict[str, str]) -> int | None:
    direct = metric(row, "follows_24h")
    if direct is not None:
        return direct
    return follower_delta(row, "24h")


def ratio_per_1000(numerator: int | None, denominator: int | None) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return round(numerator * 1000 / denominator, 2)


def recommendation(row: dict[str, str]) -> str:
    posted = bool(str(row.get("posted_at_jst") or "").strip())
    if not posted:
        return "not posted yet"

    reach = metric(row, "reach_24h")
    follows = follows_24h(row)
    saves = metric(row, "saves_24h") or 0
    shares = metric(row, "shares_24h") or 0

    if follows is None and reach is None:
        return "waiting for 24h metrics"
    if follows is not None and follows >= 5:
        return "repeat this angle with a follow-up Reel"
    if saves + shares >= 10:
        return "repeat this topic; saves/shares are strong"
    if reach is not None and reach < 150:
        return "rewrite hook/cover before repeating"
    if follows == 0 and saves < 3:
        return "pause this format; test a stronger human-stakes hook"
    return "watch 72h metrics before deciding"


def summarize(queue_path: Path, metrics_path: Path) -> str:
    queue = read_json(queue_path)
    rows = read_metrics(metrics_path)
    lines = [
        f"AI Brief JP growth sprint",
        f"Baseline: {queue.get('baseline', {}).get('followers')} followers",
        f"Goal: {queue.get('goal', {}).get('targetFollowers')} followers",
        "",
        "| Post | Status | 24h follows | 24h reach | Follows / 1k reach | Saves+shares | Recommendation |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for item in queue.get("items", []):
        if not isinstance(item, dict):
            continue
        post_id = str(item.get("id") or "")
        row = rows.get(post_id, {})
        follows = follows_24h(row)
        reach = metric(row, "reach_24h")
        saves_shares = (metric(row, "saves_24h") or 0) + (metric(row, "shares_24h") or 0)
        ratio = ratio_per_1000(follows, reach)
        lines.append(
            "| {hook} | {status} | {follows} | {reach} | {ratio} | {saves_shares} | {rec} |".format(
                hook=str(item.get("hook") or post_id),
                status=str(item.get("status") or ""),
                follows="" if follows is None else follows,
                reach="" if reach is None else reach,
                ratio="" if ratio is None else ratio,
                saves_shares=saves_shares if row else "",
                rec=recommendation(row),
            )
        )
    return "\n".join(lines)


def read_live_schedule(db_path: Path, channel_id: str, dates: Iterable[str]) -> dict[str, list[dict[str, str]]]:
    wanted_dates = sorted({date for date in dates if date})
    if not wanted_dates:
        return {}
    placeholders = ",".join("?" for _ in wanted_dates)
    uri = f"file:{db_path.as_posix()}?mode=ro"
    query = f"""
        select content_hash, scheduled_at, source_video, title, status, caption
        from reels
        where channel_id = ?
          and status in ('scheduled', 'publish_previewed', 'publishing')
          and substr(scheduled_at, 1, 10) in ({placeholders})
        order by scheduled_at
    """
    rows_by_date: dict[str, list[dict[str, str]]] = defaultdict(list)
    with sqlite3.connect(uri, uri=True) as conn:
        for content_hash, scheduled_at, source_video, title, status, caption in conn.execute(query, [channel_id, *wanted_dates]):
            parsed_at = iso_datetime(scheduled_at)
            date = parsed_at.date().isoformat() if parsed_at else str(scheduled_at or "")[:10]
            rows_by_date[date].append(
                {
                    "content_hash": str(content_hash or ""),
                    "scheduled_at": str(scheduled_at or ""),
                    "source_video": str(source_video or ""),
                    "title": str(title or ""),
                    "status": str(status or ""),
                    "caption": str(caption or ""),
                }
            )
    return rows_by_date


def read_live_source_counts(db_path: Path, channel_id: str) -> Counter[str]:
    uri = f"file:{db_path.as_posix()}?mode=ro"
    counts: Counter[str] = Counter()
    with sqlite3.connect(uri, uri=True) as conn:
        for source_video, count in conn.execute(
            """
            select coalesce(source_video, 'unknown'), count(*)
            from reels
            where channel_id = ?
              and status in ('scheduled', 'publish_previewed', 'publishing')
            group by coalesce(source_video, 'unknown')
            """,
            [channel_id],
        ):
            counts[str(source_video or "unknown")] = int(count)
    return counts


def read_queued_rows(db_path: Path, channel_id: str) -> list[dict[str, str]]:
    uri = f"file:{db_path.as_posix()}?mode=ro"
    rows: list[dict[str, str]] = []
    with sqlite3.connect(uri, uri=True) as conn:
        for content_hash, scheduled_at, source_video, title, status in conn.execute(
            """
            select content_hash, scheduled_at, source_video, title, status
            from reels
            where channel_id = ?
              and status in ('scheduled', 'publish_previewed', 'publishing')
              and scheduled_at is not null
            order by scheduled_at
            """,
            [channel_id],
        ):
            rows.append(
                {
                    "content_hash": str(content_hash or ""),
                    "scheduled_at": str(scheduled_at or ""),
                    "source_video": str(source_video or "unknown"),
                    "title": str(title or ""),
                    "status": str(status or ""),
                }
            )
    return rows


def source_run_summary(rows: list[dict[str, str]]) -> dict[str, Any]:
    longest_source = ""
    longest_count = 0
    current_source = ""
    current_count = 0
    for row in rows:
        source = row.get("source_video") or "unknown"
        if source == current_source:
            current_count += 1
        else:
            current_source = source
            current_count = 1
        if current_count > longest_count:
            longest_source = current_source
            longest_count = current_count
    return {"source_video": longest_source, "count": longest_count}


def source_diversity_gap(top_count: int, total_count: int, max_share: float) -> int:
    if top_count <= 0 or max_share <= 0:
        return 0
    needed_total = int(top_count / max_share)
    if top_count / max_share > needed_total:
        needed_total += 1
    return max(0, needed_total - total_count)


def render_tail_risk_report(
    db_path: Path,
    channel_id: str,
    *,
    target_daily_slots: int = 4,
    refill_days: int = 7,
    source_share_limit: float = 0.35,
) -> str:
    rows = read_queued_rows(db_path, channel_id)
    source_counts = Counter(row["source_video"] or "unknown" for row in rows)
    day_counts = Counter(str(row["scheduled_at"])[:10] for row in rows if row.get("scheduled_at"))
    total = len(rows)
    top_source, top_count = source_counts.most_common(1)[0] if source_counts else ("-", 0)
    diversity_gap = source_diversity_gap(top_count, total, source_share_limit)
    refill_target = target_daily_slots * refill_days
    fresh_rows_target = max(refill_target, diversity_gap)
    dates = sorted(day_counts)
    first_day = dates[0] if dates else "-"
    last_day = dates[-1] if dates else "-"
    tail_days = dates[-5:]
    longest_run = source_run_summary(rows)

    lines = [
        "# Post-Reflow Tail Risk Report",
        "",
        "This report is read-only. It does not publish, enqueue, skip, or reschedule anything.",
        "",
        f"- Live DB: `{db_path}`",
        f"- Channel: `{channel_id}`",
        f"- Queued rows: {total}",
        f"- Window: `{first_day}` to `{last_day}`",
        f"- Target cadence: {target_daily_slots} Reels/day",
        f"- Refill buffer target: {refill_days} days / {refill_target} fresh rows",
        f"- Top source: `{top_source}` ({top_count}/{total}, {round(top_count * 100 / total, 1) if total else 0}%)",
        f"- Longest same-source run: `{longest_run.get('source_video') or '-'}` x {longest_run.get('count') or 0}",
        "",
        "## Recommendation",
        "",
    ]
    if total == 0:
        lines.append("- Queue is empty. Add fresh approved source inventory before relying on the scheduler.")
    else:
        lines.extend(
            [
                f"- Prepare at least **{fresh_rows_target}** fresh approved Reels before the tail date.",
                f"- Make those rows come from at least **{max(4, min(fresh_rows_target, 7))}** distinct source stories, with no more than **{max(2, fresh_rows_target // 4)}** from any one source.",
                f"- Prioritize human-stakes AI stories over more `{top_source}`/Claude-only clips unless metrics prove that source is converting follows.",
                "- Keep captions unchanged for the no-forced-CTA cadence test; solve conversion through stronger topics, hooks, and source variety first.",
            ]
        )
        if diversity_gap:
            lines.append(
                f"- Source diversity floor: add at least **{diversity_gap}** non-`{top_source}` rows to bring the top-source share under {int(source_share_limit * 100)}%."
            )

    lines.extend(
        [
            "",
            "## Daily Counts",
            "",
            "| Date | Queued posts |",
            "|---|---:|",
        ]
    )
    for date in dates:
        lines.append(f"| {date} | {day_counts[date]} |")

    lines.extend(
        [
            "",
            "## Source Mix",
            "",
            "| Source | Queued posts | Share |",
            "|---|---:|---:|",
        ]
    )
    for source, count in source_counts.most_common():
        share = round(count * 100 / total, 1) if total else 0
        lines.append(f"| {source} | {count} | {share}% |")

    lines.extend(
        [
            "",
            "## Tail Sample",
            "",
            "| Scheduled | Source | Hash | Title |",
            "|---|---|---|---|",
        ]
    )
    for row in rows:
        if str(row["scheduled_at"])[:10] not in tail_days:
            continue
        lines.append(
            "| {scheduled} | {source} | {hash} | {title} |".format(
                scheduled=table_cell(row["scheduled_at"], max_len=32),
                source=table_cell(row["source_video"], max_len=16),
                hash=table_cell(row["content_hash"][:10], max_len=12),
                title=table_cell(row["title"], max_len=100),
            )
        )

    return "\n".join(lines)


def write_tail_risk_report(db_path: Path, channel_id: str, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_tail_risk_report(db_path, channel_id), encoding="utf-8")


def score_tail_replacement_candidate(row: dict[str, str], top_source: str) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    source = row.get("source_video") or "unknown"
    title = row.get("title") or ""
    if source == top_source:
        score += 5
        reasons.append(f"overrepresented source `{source}`")
    if "Claude" in title or "Anthropic" in title:
        score += 2
        reasons.append("Claude/Anthropic-heavy topic")
    if "AI" in title and ("仕事" in title or "生産性" in title or "効率" in title):
        score += 1
        reasons.append("generic AI-work framing")
    return score, reasons


def render_tail_replacement_plan(
    db_path: Path,
    channel_id: str,
    *,
    start_date: str = "2026-07-15",
    source_share_limit: float = 0.25,
) -> str:
    rows = read_queued_rows(db_path, channel_id)
    total = len(rows)
    source_counts = Counter(row["source_video"] or "unknown" for row in rows)
    top_source, top_count = source_counts.most_common(1)[0] if source_counts else ("-", 0)
    target_top_count = int(total * source_share_limit)
    replacements_needed = max(0, top_count - target_top_count)
    tail_rows = [row for row in rows if str(row.get("scheduled_at") or "")[:10] >= start_date]
    scored: list[tuple[int, str, dict[str, str], list[str]]] = []
    for row in tail_rows:
        score, reasons = score_tail_replacement_candidate(row, top_source)
        if score <= 0:
            continue
        scored.append((score, str(row.get("scheduled_at") or ""), row, reasons))
    scored.sort(key=lambda item: (-item[0], item[1]))

    candidate_count = len(scored)
    selected_count = min(candidate_count, replacements_needed or candidate_count)
    lines = [
        "# Tail Diversity Replacement Plan",
        "",
        "This report is read-only. It identifies rows to replace or regenerate, but does not skip, delete, enqueue, publish, or reschedule anything.",
        "",
        f"- Live DB: `{db_path}`",
        f"- Channel: `{channel_id}`",
        f"- Scope start date: `{start_date}`",
        f"- Queued rows: {total}",
        f"- Top source: `{top_source}` ({top_count}/{total}, {round(top_count * 100 / total, 1) if total else 0}%)",
        f"- Target top-source cap: <= {int(source_share_limit * 100)}% ({target_top_count}/{total})",
        f"- Replacements needed to hit cap without adding new rows: {replacements_needed}",
        f"- Candidate rows in scope: {candidate_count}",
        "",
        "## Decision",
        "",
    ]
    if replacements_needed == 0:
        lines.append("- Source share is already within the target cap. Keep monitoring after adding new inventory.")
    elif candidate_count < replacements_needed:
        lines.extend(
            [
                f"- Replace/regenerate all **{candidate_count}** candidate rows in this tail window.",
                f"- This still leaves **{replacements_needed - candidate_count}** additional non-`{top_source}` replacements or fresh additions needed outside this window.",
            ]
        )
    else:
        lines.append(f"- Replace/regenerate the first **{selected_count}** rows below.")
    lines.extend(
        [
            "- Replacement stories should be fresh, non-duplicate, human-stakes AI stories.",
            "- Keep the no-forced-CTA caption format; this plan is about content diversity and hook strength.",
            "",
            "## Candidate Rows",
            "",
            "| Priority | Scheduled | Source | Hash | Title | Reasons | Replacement Direction |",
            "|---:|---|---|---|---|---|---|",
        ]
    )
    replacement_directions = [
        "AI career / hiring / founder tradeoff",
        "AI product safety / user agency",
        "AI research with practical builder consequence",
        "Japanese builder workflow / local market angle",
        "AI regulation / platform policy implication",
        "Open-source model/tool adoption story",
        "Failure story: what teams should not automate",
    ]
    for index, (score, _, row, reasons) in enumerate(scored, start=1):
        direction = replacement_directions[(index - 1) % len(replacement_directions)]
        marker = index if index <= selected_count else f"{index} (backup)"
        lines.append(
            "| {priority} | {scheduled} | {source} | {hash} | {title} | {reasons} | {direction} |".format(
                priority=marker,
                scheduled=table_cell(row.get("scheduled_at"), max_len=32),
                source=table_cell(row.get("source_video"), max_len=16),
                hash=table_cell((row.get("content_hash") or "")[:10], max_len=12),
                title=table_cell(row.get("title"), max_len=100),
                reasons=table_cell("; ".join(reasons), max_len=140),
                direction=direction,
            )
        )
    lines.extend(
        [
            "",
            "## Replacement Acceptance Criteria",
            "",
            "- No more than 2 replacement rows from the same new source story in this tail window.",
            "- At least 5 distinct source stories across the replacement set.",
            "- Each replacement must include a named company/tool/person/paper and a human consequence in the first line.",
            "- Avoid adding more Claude/Anthropic-only clips unless the 24h metrics show clear follow conversion.",
        ]
    )
    return "\n".join(lines)


def write_tail_replacement_plan(db_path: Path, channel_id: str, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_tail_replacement_plan(db_path, channel_id), encoding="utf-8")


def ready_reel_count(source_dir: Path, channel_id: str) -> int:
    return sum(1 for _ in source_dir.glob(f"clips/*/reel.*.{channel_id}.mp4"))


def dry_run_candidate_count(source_dir: Path) -> int:
    path = source_dir / "candidates.json"
    if not path.exists():
        return 0
    try:
        candidates = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return 0
    clips = candidates.get("clips")
    if not isinstance(clips, list):
        return 0
    return len(clips)


def output_metadata(source_dir: Path) -> dict[str, str]:
    path = source_dir / "metadata.json"
    if not path.exists():
        return {}
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return {
        "title": str(metadata.get("title") or ""),
        "channel": str(metadata.get("channel") or metadata.get("uploader") or ""),
        "url": str(metadata.get("webpage_url") or metadata.get("original_url") or metadata.get("url") or ""),
        "upload_date": parse_source_date(metadata.get("upload_date") or metadata.get("uploadDate") or metadata.get("release_date")) or "",
    }


def source_output_inventory(outputs_root: Path, channel_id: str) -> dict[str, dict[str, Any]]:
    inventory: dict[str, dict[str, Any]] = {}
    if not outputs_root.exists():
        return inventory
    for source_dir in sorted(path for path in outputs_root.iterdir() if path.is_dir()):
        source_id = source_dir.name
        metadata = output_metadata(source_dir)
        inventory[source_id] = {
            "source_id": source_id,
            "path": str(source_dir),
            "ready_clips": ready_reel_count(source_dir, channel_id),
            "dry_run_candidates": dry_run_candidate_count(source_dir),
            "title": metadata.get("title", ""),
            "channel": metadata.get("channel", ""),
            "url": metadata.get("url", ""),
            "upload_date": metadata.get("upload_date", ""),
        }
    return inventory


def render_source_intake_report(backlog_path: Path, outputs_root: Path, channel_id: str) -> str:
    backlog = read_json(backlog_path)
    sources = [source for source in backlog.get("sources", []) if isinstance(source, dict)]
    inventory = source_output_inventory(outputs_root, channel_id)
    backlog_ids = {str(source.get("youtubeId") or "") for source in sources}
    freshness_cutoff = parse_source_date(backlog.get("freshnessCutoff") or DEFAULT_SOURCE_FRESHNESS_CUTOFF) or DEFAULT_SOURCE_FRESHNESS_CUTOFF
    backlog_rows: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
    for source in sorted(sources, key=lambda item: int(item.get("priority") or 0)):
        source_id = str(source.get("youtubeId") or "")
        backlog_rows.append((source, inventory.get(source_id)))

    processed = [(source, item) for source, item in backlog_rows if item and int(item.get("ready_clips") or 0) > 0]
    dry_run_only = [
        (source, item)
        for source, item in backlog_rows
        if item and int(item.get("ready_clips") or 0) == 0 and int(item.get("dry_run_candidates") or 0) > 0
    ]
    first_batch_missing_ready = [
        source
        for source, item in backlog_rows
        if source.get("batch") == "process_first" and not (item and int(item.get("ready_clips") or 0) > 0)
    ]
    first_batch_missing_outputs = [
        source
        for source, item in backlog_rows
        if source.get("batch") == "process_first" and not item
    ]
    missing_outputs = [source for source, item in backlog_rows if not item]
    total_ready = sum(int(item.get("ready_clips") or 0) for _, item in processed)
    target = int((backlog.get("targets") or {}).get("freshApprovedRows") or 28)

    lines = [
        "# Auto-Cut Source Intake Report",
        "",
        "This report is read-only. It compares the recommended source backlog with local `reel-app` outputs; it does not download, cut, queue, publish, skip, delete, or reschedule anything.",
        "",
        f"- Backlog: `{backlog_path}`",
        f"- Outputs root: `{outputs_root}`",
        f"- Channel: `{channel_id}`",
        f"- Backlog sources: {len(sources)}",
        f"- Backlog sources with ready `{channel_id}` clips: {len(processed)}",
        f"- Backlog sources with dry-run candidates only: {len(dry_run_only)}",
        f"- Ready clips from backlog sources: {total_ready}/{target} target fresh rows",
        f"- First-batch sources still missing ready clips: {len(first_batch_missing_ready)}",
        f"- First-batch sources not started in outputs: {len(first_batch_missing_outputs)}",
        "",
        "## Backlog Status",
        "",
        "| Priority | Batch | YouTube id | Source | Suggested cap | Ready clips | Dry-run candidates | Status |",
        "|---:|---|---|---|---:|---:|---:|---|",
    ]
    for source, item in backlog_rows:
        ready = int(item.get("ready_clips") or 0) if item else 0
        candidates = int(item.get("dry_run_candidates") or 0) if item else 0
        if ready:
            status = "ready_for_review"
        elif candidates:
            status = "dry_run_only"
        elif item:
            status = "metadata_only"
        else:
            status = "missing_from_outputs"
        lines.append(
            "| {priority} | {batch} | `{youtube_id}` | {title} - {channel} | {cap} | {ready} | {candidates} | {status} |".format(
                priority=source.get("priority") or "",
                batch=table_cell(source.get("batch"), max_len=18),
                youtube_id=table_cell(source.get("youtubeId"), max_len=40),
                title=table_cell(source.get("title"), max_len=80),
                channel=table_cell(source.get("channel"), max_len=40),
                cap=source.get("suggestedCap") or "",
                ready=ready,
                candidates=candidates,
                status=status,
            )
        )

    stale_non_backlog_outputs = [
        item
        for source_id, item in sorted(inventory.items())
        if source_id not in backlog_ids
        and item.get("upload_date")
        and str(item.get("upload_date")) < freshness_cutoff
    ]
    lines.extend(
        [
            "",
            "## Existing Non-Backlog Outputs",
            "",
            f"Stale non-backlog outputs older than `{freshness_cutoff}` are suppressed from this fresh-source report. Suppressed stale output folders: {len(stale_non_backlog_outputs)}.",
            "",
            "| Source id | Ready clips | Source |",
            "|---|---:|---|",
        ]
    )
    for source_id, item in sorted(inventory.items()):
        if source_id in backlog_ids:
            continue
        upload_date = str(item.get("upload_date") or "")
        if upload_date and upload_date < freshness_cutoff:
            continue
        lines.append(
            "| `{source_id}` | {ready} | {title} - {channel} |".format(
                source_id=table_cell(source_id, max_len=16),
                ready=item.get("ready_clips") or 0,
                title=table_cell(item.get("title"), max_len=90),
                channel=table_cell(item.get("channel"), max_len=40),
            )
        )

    lines.extend(["", "## Next Action", ""])
    if dry_run_only:
        dry_run_ids = ", ".join(f"`{source.get('youtubeId')}`" for source, _ in dry_run_only)
        lines.append(f"- Dry-run candidates exist but still need rights review, trim approval, rendering, and visual QA: {dry_run_ids}.")
    if first_batch_missing_outputs:
        missing_ids = ", ".join(f"`{source.get('youtubeId')}`" for source in first_batch_missing_outputs)
        lines.append(f"- Dry-run/process the first-batch source videos not started in `reel-app`: {missing_ids}.")
    if missing_outputs:
        missing_ids = ", ".join(f"`{source.get('youtubeId')}`" for source in missing_outputs)
        lines.append(f"- Backlog sources still not started in `reel-app`: {missing_ids}.")
    if not first_batch_missing_ready:
        lines.append("- First-batch source videos all have local clips; run visual QA and rights review before any queue mutation.")
    if total_ready < target:
        lines.append(f"- Need {target - total_ready} more approved clips from backlog sources to reach the {target}-row refill target.")
    lines.append("- Keep live DB changes gated behind explicit approval after source clips are reviewed.")
    return "\n".join(lines)


def write_source_intake_report(backlog_path: Path, outputs_root: Path, channel_id: str, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        render_source_intake_report(backlog_path, outputs_root, channel_id),
        encoding="utf-8",
    )


def manual_items_by_date(queue: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in queue.get("items", []):
        if not isinstance(item, dict):
            continue
        publish_at = iso_datetime(item.get("recommendedPublishAt"))
        if publish_at is None:
            continue
        by_date[publish_at.date().isoformat()].append(item)
    return by_date


def metrics_only_dates(queue: dict[str, Any], metrics: dict[str, dict[str, str]]) -> dict[str, list[str]]:
    item_ids = {str(item.get("id") or "") for item in queue.get("items", []) if isinstance(item, dict)}
    by_date: dict[str, list[str]] = defaultdict(list)
    for post_id in metrics:
        if post_id in item_ids:
            continue
        date = post_id_date(post_id)
        if date:
            by_date[date].append(post_id)
    return by_date


def date_decision(manual_count: int, live_count: int, metrics_only_count: int) -> str:
    if manual_count and live_count >= 3:
        return "approval needed: replace one live Reel, post as extra, or defer manual asset"
    if manual_count and live_count:
        return "approval needed: avoid unplanned stacking"
    if manual_count:
        return "open live slot; manual post still requires Instagram approval"
    if metrics_only_count:
        return "prepared asset is tracked in metrics but not in the manual queue"
    if live_count >= 3:
        return "live pipeline only; monitor metrics"
    return "no manual asset and sparse live schedule"


def render_schedule_report(queue_path: Path, metrics_path: Path, db_path: Path, channel_id: str) -> str:
    queue = read_json(queue_path)
    metrics = read_metrics(metrics_path)
    manual_by_date = manual_items_by_date(queue)
    metrics_only_by_date = metrics_only_dates(queue, metrics)
    dates = sorted(set(manual_by_date) | set(metrics_only_by_date))
    live_by_date = read_live_schedule(db_path, channel_id, dates)

    conflict_dates = [
        date
        for date in dates
        if manual_by_date.get(date) and len(live_by_date.get(date, [])) >= 3
    ]
    lines = [
        "# Manual vs Live Schedule Report",
        "",
        f"- Queue: `{queue_path}`",
        f"- Metrics: `{metrics_path}`",
        f"- Live DB: `{db_path}`",
        f"- Channel: `{channel_id}`",
        f"- Manual ready items: {sum(len(items) for items in manual_by_date.values())}",
        f"- Metrics-only prepared ids: {sum(len(ids) for ids in metrics_only_by_date.values())}",
        f"- Manual days with 3+ live scheduled Reels: {len(conflict_dates)}",
        "",
        "## Decision Table",
        "",
        "| Date | Manual ID | Status / Format | Manual Time | Live Slots | Nearest Gap Min | Live Sources | Decision |",
        "|---|---|---|---:|---|---:|---|---|",
    ]
    for date in dates:
        manual_items = manual_by_date.get(date, [])
        live_rows = live_by_date.get(date, [])
        metrics_only_ids = metrics_only_by_date.get(date, [])
        source_counts = Counter(row["source_video"] or "unknown" for row in live_rows)
        source_text = ", ".join(f"{source} x {count}" for source, count in source_counts.most_common()) or "-"
        live_slots = ", ".join(short_time(row["scheduled_at"]) for row in live_rows) or "-"
        if manual_items:
            manual_text = "; ".join(
                table_cell(item.get("id"), max_len=70) for item in manual_items
            )
            time_text = "; ".join(
                short_time(str(item.get("recommendedPublishAt") or "")) for item in manual_items
            )
            status_format = "; ".join(
                table_cell(
                    f"{item.get('status') or 'unknown'} / {item.get('format') or item.get('preferredFormat') or 'unknown'}",
                    max_len=50,
                )
                for item in manual_items
            )
        else:
            manual_text = "; ".join(metrics_only_ids) if metrics_only_ids else "-"
            time_text = "-"
            status_format = "metrics_only / draft" if metrics_only_ids else "-"
        lines.append(
            "| {date} | {manual} | {status_format} | {time} | {slots} | {gap} | {sources} | {decision} |".format(
                date=date,
                manual=manual_text,
                status_format=status_format,
                time=time_text,
                slots=f"{len(live_rows)} ({live_slots})",
                gap=nearest_gap_minutes(manual_items, live_rows),
                sources=table_cell(source_text, max_len=100),
                decision=date_decision(len(manual_items), len(live_rows), len(metrics_only_ids)),
            )
        )

    lines.extend(["", "## Live Rows By Manual Day", ""])
    for date in dates:
        live_rows = live_by_date.get(date, [])
        manual_items = manual_by_date.get(date, [])
        metrics_only_ids = metrics_only_by_date.get(date, [])
        lines.append(f"### {date}")
        if manual_items:
            for item in manual_items:
                lines.append(
                    f"- Manual asset: `{item.get('id')}` at `{item.get('recommendedPublishAt')}` - {table_cell(item.get('hook'), max_len=120)}"
                )
        for post_id in metrics_only_ids:
            lines.append(f"- Metrics-only prepared asset: `{post_id}`")
        if not live_rows:
            lines.append("- Live queue: no scheduled or previewed rows on this date.")
            lines.append("")
            continue
        lines.append("")
        lines.append("| Scheduled | Source | Status | Hash | Title |")
        lines.append("|---|---|---|---|---|")
        for row in live_rows:
            lines.append(
                "| {scheduled} | {source} | {status} | {hash} | {title} |".format(
                    scheduled=table_cell(row["scheduled_at"], max_len=32),
                    source=table_cell(row["source_video"], max_len=16),
                    status=table_cell(row["status"], max_len=18),
                    hash=table_cell(row["content_hash"][:10], max_len=12),
                    title=table_cell(row["title"], max_len=100),
                )
            )
        lines.append("")

    lines.extend(
        [
            "## Operating Recommendation",
            "",
            "1. Do not add a manual growth post to a date that already has three or more live Reels unless that extra post is explicitly approved.",
            "2. If a manual item is stronger than the weakest live Reel on the same date, approve a replace/postpone action before posting.",
            "3. Keep using the live scheduler for cadence, and keep captions unchanged for this no-forced-CTA cadence test.",
            "4. Treat metrics-only prepared assets as drafts until they are added to the manual queue or explicitly approved for posting.",
        ]
    )
    return "\n".join(lines)


def write_schedule_report(queue_path: Path, metrics_path: Path, db_path: Path, channel_id: str, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        render_schedule_report(queue_path, metrics_path, db_path, channel_id),
        encoding="utf-8",
    )


def row_gap_to_manual(row: dict[str, str], manual_items: list[dict[str, Any]]) -> int | None:
    scheduled_at = iso_datetime(row.get("scheduled_at"))
    if scheduled_at is None:
        return None
    gaps: list[int] = []
    for item in manual_items:
        manual_at = iso_datetime(item.get("recommendedPublishAt"))
        if manual_at is None:
            continue
        gaps.append(round(abs((manual_at - scheduled_at).total_seconds()) / 60))
    return min(gaps) if gaps else None


def replacement_score(
    row: dict[str, str],
    live_rows: list[dict[str, str]],
    manual_items: list[dict[str, Any]],
    queue_source_counts: Counter[str],
) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    status = row.get("status") or ""
    source = row.get("source_video") or "unknown"
    parsed_at = iso_datetime(row.get("scheduled_at"))

    if status == "publishing":
        return -100, ["in-flight publishing row; do not target"]
    if status == "publish_previewed":
        score -= 1
        reasons.append("already previewed; review carefully before touching")

    same_day_source_count = sum(1 for live_row in live_rows if (live_row.get("source_video") or "unknown") == source)
    if same_day_source_count > 1:
        score += 3
        reasons.append(f"same-day source repeat ({source} x{same_day_source_count})")

    total_queued = sum(queue_source_counts.values())
    global_source_count = queue_source_counts.get(source, 0)
    if total_queued and global_source_count / total_queued >= 0.25:
        score += 2
        reasons.append(f"globally overrepresented source ({global_source_count}/{total_queued})")

    gap = row_gap_to_manual(row, manual_items)
    all_gaps = [row_gap_to_manual(live_row, manual_items) for live_row in live_rows]
    known_gaps = [known_gap for known_gap in all_gaps if known_gap is not None]
    if gap is not None and known_gaps and gap == min(known_gaps):
        score += 2
        reasons.append(f"closest live row to manual post ({gap} min)")
    if parsed_at is not None and parsed_at.hour >= 18:
        score += 1
        reasons.append("same evening slot as manual post")

    return score, reasons


def best_replacement_candidate(
    live_rows: list[dict[str, str]],
    manual_items: list[dict[str, Any]],
    queue_source_counts: Counter[str],
) -> tuple[dict[str, str] | None, int, list[str]]:
    if not live_rows:
        return None, 0, []
    scored: list[tuple[int, str, dict[str, str], list[str]]] = []
    for row in live_rows:
        score, reasons = replacement_score(row, live_rows, manual_items, queue_source_counts)
        scheduled_at = row.get("scheduled_at") or ""
        scored.append((score, scheduled_at, row, reasons))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    score, _, row, reasons = scored[0]
    return row, score, reasons


def render_replacement_report(queue_path: Path, metrics_path: Path, db_path: Path, channel_id: str) -> str:
    queue = read_json(queue_path)
    metrics = read_metrics(metrics_path)
    manual_by_date = manual_items_by_date(queue)
    metrics_only_by_date = metrics_only_dates(queue, metrics)
    dates = sorted(set(manual_by_date) | set(metrics_only_by_date))
    live_by_date = read_live_schedule(db_path, channel_id, dates)
    source_counts = read_live_source_counts(db_path, channel_id)

    lines = [
        "# Manual Replacement Candidates",
        "",
        "This is an advisory report only. It does not skip, delete, publish, or reschedule anything.",
        "",
        f"- Queue: `{queue_path}`",
        f"- Metrics: `{metrics_path}`",
        f"- Live DB: `{db_path}`",
        f"- Channel: `{channel_id}`",
        f"- Live queued source counts: {dict(source_counts.most_common())}",
        "",
        "## Recommended Candidate Table",
        "",
        "| Date | Manual ID | Live Count | Candidate Hash | Candidate Time | Candidate Source | Score | Reasons | Approval Note |",
        "|---|---|---:|---|---:|---|---:|---|---|",
    ]
    for date in dates:
        manual_items = manual_by_date.get(date, [])
        metrics_only_ids = metrics_only_by_date.get(date, [])
        live_rows = live_by_date.get(date, [])
        if manual_items:
            manual_id = "; ".join(table_cell(item.get("id"), max_len=50) for item in manual_items)
            candidate, score, reasons = best_replacement_candidate(live_rows, manual_items, source_counts)
            if len(live_rows) >= 3:
                approval_note = "approve replace/postpone before posting manual asset"
            elif live_rows:
                approval_note = "optional only if avoiding any same-day stacking"
            else:
                approval_note = "no live row to replace"
        else:
            manual_id = "; ".join(metrics_only_ids) if metrics_only_ids else "-"
            candidate, score, reasons = None, 0, []
            approval_note = "draft only; add to manual queue or approve posting before targeting live rows"
        if candidate is None:
            lines.append(
                f"| {date} | {manual_id} | {len(live_rows)} | - | - | - | - | - | {approval_note} |"
            )
            continue
        lines.append(
            "| {date} | {manual} | {live_count} | {hash} | {time} | {source} | {score} | {reasons} | {note} |".format(
                date=date,
                manual=manual_id,
                live_count=len(live_rows),
                hash=table_cell(candidate.get("content_hash", "")[:10], max_len=12),
                time=short_time(candidate.get("scheduled_at", "")),
                source=table_cell(candidate.get("source_video"), max_len=16),
                score=score,
                reasons=table_cell("; ".join(reasons), max_len=140),
                note=approval_note,
            )
        )

    lines.extend(["", "## Candidate Details", ""])
    for date in dates:
        manual_items = manual_by_date.get(date, [])
        live_rows = live_by_date.get(date, [])
        if not manual_items:
            continue
        candidate, score, reasons = best_replacement_candidate(live_rows, manual_items, source_counts)
        lines.append(f"### {date}")
        for item in manual_items:
            lines.append(
                f"- Manual asset: `{item.get('id')}` ({item.get('format') or item.get('preferredFormat')}) at `{item.get('recommendedPublishAt')}` - {table_cell(item.get('hook'), max_len=120)}"
            )
        if candidate is None:
            lines.append("- Candidate: none; no live row on this date.")
            lines.append("")
            continue
        lines.extend(
            [
                f"- Candidate full hash: `{candidate.get('content_hash')}`",
                f"- Candidate scheduled: `{candidate.get('scheduled_at')}`",
                f"- Candidate status/source: `{candidate.get('status')}` / `{candidate.get('source_video')}`",
                f"- Candidate title: {table_cell(candidate.get('title'), max_len=160)}",
                f"- Score: {score}",
                f"- Reasons: {table_cell('; '.join(reasons), max_len=220)}",
                "",
            ]
        )

    lines.extend(
        [
            "## Scoring Notes",
            "",
            "- Higher scores mean a row is easier to justify replacing or postponing if a manual growth asset is approved.",
            "- The score favors same-day source repeats, globally overrepresented sources, rows closest to the manual post time, and evening collisions.",
            "- A `publishing` row is protected and should not be targeted.",
            "- A `publish_previewed` row is sensitive; re-check status before any approved action.",
            "- This report is not an instruction to mutate the live queue. Use it only as input to an explicit approval decision.",
        ]
    )
    return "\n".join(lines)


def write_replacement_report(queue_path: Path, metrics_path: Path, db_path: Path, channel_id: str, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        render_replacement_report(queue_path, metrics_path, db_path, channel_id),
        encoding="utf-8",
    )


def missing_queue_paths(queue_path: Path) -> list[str]:
    queue = read_json(queue_path)
    missing: list[str] = []
    for key in TOP_LEVEL_PATH_KEYS:
        value = queue.get(key)
        if value and not Path(str(value)).exists():
            missing.append(f"{key}: {value}")
    for item in queue.get("items", []):
        if not isinstance(item, dict):
            continue
        post_id = str(item.get("id") or "unknown")
        for key in ITEM_PATH_KEYS:
            value = item.get(key)
            if value and not Path(str(value)).exists():
                missing.append(f"{post_id}.{key}: {value}")
    return missing


def missing_metric_rows(queue_path: Path, metrics_path: Path) -> list[str]:
    if not metrics_path.exists():
        return [f"metrics file: {metrics_path}"]
    queue = read_json(queue_path)
    rows = read_metrics(metrics_path)
    missing: list[str] = []
    for item in queue.get("items", []):
        if not isinstance(item, dict):
            continue
        post_id = str(item.get("id") or "").strip()
        if post_id and post_id not in rows:
            missing.append(post_id)
    return missing


def metrics_shape_issues(metrics_path: Path) -> list[str]:
    if not metrics_path.exists():
        return [f"metrics file: {metrics_path}"]
    issues: list[str] = []
    with metrics_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration:
            return [f"metrics file is empty: {metrics_path}"]
        expected = len(header)
        for line_number, row in enumerate(reader, start=2):
            if len(row) != expected:
                post_id = row[0] if row else "unknown"
                issues.append(f"{post_id} line {line_number} has {len(row)} columns; expected {expected}")
    return issues


def published_metric_coverage_issues(
    db_path: Path,
    metrics_path: Path,
    channel_id: str,
    *,
    since_date: str | None = DEFAULT_PUBLISHED_METRICS_SINCE,
) -> list[str]:
    if not db_path.exists():
        return [f"scheduler db missing: {db_path}"]
    if not metrics_path.exists():
        return [f"metrics file: {metrics_path}"]

    try:
        metric_rows = list(read_metrics(metrics_path).values())
    except (KeyError, csv.Error) as exc:
        return [f"metrics file unreadable: {exc}"]

    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("pragma table_info(reels)")}
        required = {"content_hash", "channel_id", "status", "scheduled_at"}
        if not required.issubset(columns):
            missing = ", ".join(sorted(required - columns))
            return [f"scheduler db reels table missing columns: {missing}"]

        select_columns = [
            "content_hash",
            "scheduled_at",
            "source_video" if "source_video" in columns else "'' as source_video",
            "title" if "title" in columns else "'' as title",
            "published_at" if "published_at" in columns else "'' as published_at",
            "media_id" if "media_id" in columns else "'' as media_id",
            "permalink" if "permalink" in columns else "'' as permalink",
        ]
        order_by = "coalesce(published_at, scheduled_at)" if "published_at" in columns else "scheduled_at"
        where = ["channel_id = ?", "status = 'published'"]
        params: list[object] = [channel_id]
        parsed_since = parse_source_date(since_date)
        if parsed_since:
            where.append("date(scheduled_at) >= date(?)")
            params.append(parsed_since)
        rows = conn.execute(
            f"""
            select {", ".join(select_columns)}
            from reels
            where {" and ".join(where)}
            order by {order_by} desc
            """,
            params,
        ).fetchall()

    issues: list[str] = []
    for content_hash, scheduled_at, source_video, title, published_at, media_id, permalink in rows:
        hash_prefix = str(content_hash or "")[:12]
        media_id = str(media_id or "").strip()
        permalink = str(permalink or "").strip()

        matched = False
        for metric_row in metric_rows:
            if media_id and str(metric_row.get("instagram_media_id") or "").strip() == media_id:
                matched = True
                break
            if permalink and str(metric_row.get("permalink") or "").strip() == permalink:
                matched = True
                break
            if hash_prefix and hash_prefix in str(metric_row.get("post_id") or ""):
                matched = True
                break

        if not matched:
            label = media_id or permalink or hash_prefix or "unknown"
            when = published_at or scheduled_at or "unknown time"
            source = source_video or "unknown source"
            issues.append(f"{label} missing metrics row for published {channel_id} Reel ({source}, {when}, {title or 'untitled'})")
    return issues


def approval_queue_issues(path: Path) -> list[str]:
    if not path.exists():
        return [f"approval queue missing: {path}"]
    try:
        queue = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"approval queue invalid JSON: {exc}"]

    issues: list[str] = []
    if queue.get("schema_version") != "1.0":
        issues.append("approval queue schema_version must be 1.0")
    if queue.get("queue_type") != "instagram_dry_run_source_candidate_approval":
        issues.append("approval queue queue_type is invalid")
    if queue.get("dry_run_only") is not True:
        issues.append("approval queue dry_run_only must be true")
    if queue.get("requires_manual_approval") is not True:
        issues.append("approval queue requires_manual_approval must be true")
    freshness_cutoff = parse_source_date(queue.get("freshness_cutoff") or queue.get("freshnessCutoff") or DEFAULT_SOURCE_FRESHNESS_CUTOFF)
    if freshness_cutoff is None:
        issues.append("approval queue freshness_cutoff must be a valid date")
        freshness_cutoff = DEFAULT_SOURCE_FRESHNESS_CUTOFF

    gates = queue.get("side_effect_gates")
    if not isinstance(gates, dict):
        issues.append("approval queue side_effect_gates must be an object")
        gates = {}
    for gate in ("render", "replace_existing", "enqueue", "publish", "boost", "profile_edit", "follow_or_unfollow", "comment_or_dm"):
        if gates.get(gate) is not False:
            issues.append(f"approval queue side_effect_gates.{gate} must default false")

    candidates = queue.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        issues.append("approval queue candidates must be a non-empty list")
        return issues

    seen_ids: set[str] = set()
    top_ranks: list[int] = []
    for index, candidate in enumerate(candidates, start=1):
        label = f"candidate[{index}]"
        if not isinstance(candidate, dict):
            issues.append(f"{label} must be an object")
            continue

        candidate_id = str(candidate.get("candidate_id") or "")
        if not candidate_id:
            issues.append(f"{label}.candidate_id is required")
        elif candidate_id in seen_ids:
            issues.append(f"{candidate_id} is duplicated")
        seen_ids.add(candidate_id)

        source = candidate.get("source")
        if not isinstance(source, dict):
            issues.append(f"{candidate_id or label}.source must be an object")
            continue
        video_id = str(source.get("video_id") or "")
        upload_date = parse_source_date(candidate.get("upload_date") or source.get("upload_date") or source.get("published_at"))
        if upload_date is None:
            issues.append(f"{candidate_id or label}.upload_date is required")
        elif upload_date < freshness_cutoff:
            issues.append(f"{candidate_id or label}.upload_date {upload_date} is before freshness cutoff {freshness_cutoff}")
        if candidate.get("published_after_cutoff") is not True:
            issues.append(f"{candidate_id or label}.published_after_cutoff must be true")
        start_seconds = source.get("start_seconds")
        end_seconds = source.get("end_seconds")
        if not isinstance(start_seconds, int) or not isinstance(end_seconds, int):
            issues.append(f"{candidate_id or label}.source start_seconds/end_seconds must be integers")
            continue
        if end_seconds <= start_seconds:
            issues.append(f"{candidate_id or label}.source.end_seconds must be greater than start_seconds")
        expected_id = f"youtube:{video_id}:{start_seconds}-{end_seconds}"
        if candidate_id and candidate_id != expected_id:
            issues.append(f"{candidate_id}.candidate_id should be {expected_id}")
        url = str(source.get("url") or "")
        match = re.search(r"[?&]t=(\d+)s(?:&|$)", url)
        if not match or int(match.group(1)) != start_seconds:
            issues.append(f"{candidate_id or label}.source.url t= must match start_seconds")

        priority_group = candidate.get("priority_group")
        rank = candidate.get("rank")
        if priority_group == "top_five":
            if not isinstance(rank, int):
                issues.append(f"{candidate_id or label}.rank must be an integer for top_five")
            else:
                top_ranks.append(rank)
        elif priority_group == "second_pass":
            if rank is not None:
                issues.append(f"{candidate_id or label}.rank must be null for second_pass")
        else:
            issues.append(f"{candidate_id or label}.priority_group must be top_five or second_pass")

        approved = candidate.get("approved")
        approved_actions = candidate.get("approved_actions")
        approved_by = candidate.get("approved_by")
        approved_at = candidate.get("approved_at")
        if not isinstance(approved_actions, list):
            issues.append(f"{candidate_id or label}.approved_actions must be a list")
            approved_actions = []
        if approved is False:
            if approved_actions:
                issues.append(f"{candidate_id or label}.approved_actions must be empty while approved is false")
            if approved_by is not None or approved_at is not None:
                issues.append(f"{candidate_id or label}.approved_by/approved_at must be null while approved is false")
        elif approved is True:
            if not approved_by or not approved_at:
                issues.append(f"{candidate_id or label}.approved_by and approved_at are required when approved is true")
            for action in approved_actions:
                if gates.get(str(action)) is not True:
                    issues.append(f"{candidate_id or label}.approved_actions includes {action} but side_effect_gates.{action} is not true")
        else:
            issues.append(f"{candidate_id or label}.approved must be boolean")

        if candidate.get("review_status") not in {"pending_manual_watch", "manual_watch_passed", "manual_watch_rejected"}:
            issues.append(f"{candidate_id or label}.review_status is invalid")

    if sorted(top_ranks) != [1, 2, 3, 4, 5]:
        issues.append("top_five ranks must be unique integers 1 through 5")
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize AI Brief JP growth sprint metrics")
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    parser.add_argument("--metrics", type=Path, default=DEFAULT_METRICS)
    parser.add_argument("--validate", action="store_true", help="Check that queue artifact paths exist")
    parser.add_argument("--schedule-report", action="store_true", help="Write a manual-vs-live schedule report")
    parser.add_argument("--replacement-report", action="store_true", help="Write advisory manual replacement candidates")
    parser.add_argument("--tail-risk-report", action="store_true", help="Write post-reflow queue horizon/source risk report")
    parser.add_argument("--tail-replacement-plan", action="store_true", help="Write exact tail rows to replace/regenerate for source diversity")
    parser.add_argument("--source-intake-report", action="store_true", help="Write auto-cut source backlog intake status")
    parser.add_argument("--source-backlog", type=Path, default=DEFAULT_SOURCE_BACKLOG, help="Auto-cut source backlog JSON")
    parser.add_argument("--outputs-root", type=Path, default=DEFAULT_REEL_OUTPUTS, help="reel-app outputs directory")
    parser.add_argument("--approval-queue", type=Path, default=DEFAULT_MANUAL_WATCH_APPROVAL_QUEUE, help="Manual watch approval queue JSON")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="Reel scheduler ledger DB")
    parser.add_argument("--channel", default="aibrief_jp", help="Channel id for the live schedule report")
    parser.add_argument("--published-metrics-since", default=DEFAULT_PUBLISHED_METRICS_SINCE, help="Require metrics rows for published scheduler Reels scheduled on or after this date")
    parser.add_argument("--out", type=Path, help="Output path for report commands")
    args = parser.parse_args()
    if args.validate:
        missing_paths = missing_queue_paths(args.queue)
        missing_metrics = missing_metric_rows(args.queue, args.metrics)
        metric_shape = metrics_shape_issues(args.metrics)
        published_metrics = published_metric_coverage_issues(
            args.db,
            args.metrics,
            args.channel,
            since_date=args.published_metrics_since,
        )
        approval_issues = approval_queue_issues(args.approval_queue)
        if missing_paths or missing_metrics or metric_shape or published_metrics or approval_issues:
            print("Validation issues:")
            if missing_paths:
                print("Missing queue artifacts:")
                for path in missing_paths:
                    print(f"- {path}")
            if missing_metrics:
                print("Missing metrics rows:")
                for post_id in missing_metrics:
                    print(f"- {post_id}")
            if metric_shape:
                print("Metrics shape issues:")
                for issue in metric_shape:
                    print(f"- {issue}")
            if published_metrics:
                print("Published metrics coverage issues:")
                for issue in published_metrics:
                    print(f"- {issue}")
            if approval_issues:
                print("Approval queue issues:")
                for issue in approval_issues:
                    print(f"- {issue}")
            return 1
        print("All queue artifact paths, metrics rows, published metrics coverage, and approval queue checks passed.")
        return 0
    if args.schedule_report:
        out_path = args.out or DEFAULT_SCHEDULE_REPORT
        write_schedule_report(args.queue, args.metrics, args.db, args.channel, out_path)
        print(f"Wrote manual-vs-live schedule report: {out_path}")
        return 0
    if args.replacement_report:
        out_path = args.out or DEFAULT_REPLACEMENT_REPORT
        write_replacement_report(args.queue, args.metrics, args.db, args.channel, out_path)
        print(f"Wrote manual replacement candidate report: {out_path}")
        return 0
    if args.tail_risk_report:
        out_path = args.out or DEFAULT_TAIL_RISK_REPORT
        write_tail_risk_report(args.db, args.channel, out_path)
        print(f"Wrote post-reflow tail risk report: {out_path}")
        return 0
    if args.tail_replacement_plan:
        out_path = args.out or DEFAULT_TAIL_REPLACEMENT_PLAN
        write_tail_replacement_plan(args.db, args.channel, out_path)
        print(f"Wrote tail diversity replacement plan: {out_path}")
        return 0
    if args.source_intake_report:
        out_path = args.out or DEFAULT_SOURCE_INTAKE_REPORT
        write_source_intake_report(args.source_backlog, args.outputs_root, args.channel, out_path)
        print(f"Wrote auto-cut source intake report: {out_path}")
        return 0
    print(summarize(args.queue, args.metrics))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
