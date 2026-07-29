#!/usr/bin/env python3
"""Capture immutable +1h, +3h, +24h, +72h, and +7d analyses for AI Brief JP Reels.

The runner is intentionally narrow:

* it looks only at published ``aibrief_jp`` Reels;
* it syncs only the exact media ids with a checkpoint currently due;
* it freezes the first core-valid insight snapshot inside each age window; and
* it writes one Markdown file per Reel/checkpoint without changing the queue.

Run it from a local Codex Scheduled task. Repeated runs are safe: an existing
checkpoint file is immutable, and a snapshot written before a crash is reused
on the next run instead of causing a second Graph request.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import reel_scheduler  # noqa: E402


CHANNEL = "aibrief_jp"
JST = ZoneInfo("Asia/Tokyo")
CORE_METRICS = (
    "views",
    "reach",
    "likes",
    "comments",
    "saved",
    "shares",
    "total_interactions",
)
DISPLAY_METRICS = (
    *CORE_METRICS,
    "total_views",
    "ig_reels_video_view_total_time",
    "ig_reels_avg_watch_time",
    "reels_skip_rate",
    "facebook_views",
    "crossposted_views",
)
TRIAL_EXPERIMENT_COLUMNS = (
    "experiment_id",
    "case_type",
    "parent_content_hash",
    "parent_media_id",
    "asset_family_id",
    "baseline_hook",
    "variant_hook",
    "changed_variables_json",
    "state",
    "decision",
    "decision_reason",
    "displaced_content_hash",
    "scheduled_at",
    "published_at",
    "decision_at",
    "graduated_at",
    "stopped_at",
)


@dataclass(frozen=True)
class Checkpoint:
    key: str
    label: str
    target_hours: float
    minimum_hours: float
    maximum_hours: float
    stage: str
    next_step: str


CHECKPOINTS = (
    Checkpoint(
        key="01h",
        label="+1h",
        target_hours=1.0,
        minimum_hours=1.0,
        maximum_hours=2.0,
        stage="EARLY_OBSERVATION",
        next_step="Recheck at +3h. Do not call this a winner or loser.",
    ),
    Checkpoint(
        key="03h",
        label="+3h",
        target_hours=3.0,
        minimum_hours=3.0,
        maximum_hours=4.5,
        stage="EARLY_TRAJECTORY",
        next_step="Recheck at +24h. Do not extrapolate the current pace.",
    ),
    Checkpoint(
        key="24h",
        label="+24h",
        target_hours=24.0,
        minimum_hours=24.0,
        maximum_hours=28.0,
        stage="PROVISIONAL_24H",
        next_step="Recheck at +72h before making a Trial graduation decision.",
    ),
    Checkpoint(
        key="72h",
        label="+72h",
        target_hours=72.0,
        minimum_hours=72.0,
        maximum_hours=76.0,
        stage="DECISION_READY_72H",
        next_step=(
            "For a Trial Reel, record a manual graduate or stop decision. "
            "Keep every Reel launched as Trial outside the regular baseline."
        ),
    ),
    Checkpoint(
        key="7d",
        label="+7d",
        target_hours=168.0,
        minimum_hours=168.0,
        maximum_hours=192.0,
        stage="MATURE_7D",
        next_step=(
            "Use this as mature descriptive evidence. Require several comparable "
            "posts before treating a pattern as repeatable."
        ),
    ),
)
CHECKPOINT_BY_KEY = {checkpoint.key: checkpoint for checkpoint in CHECKPOINTS}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Record per-Reel aibrief_jp analyses at +1h, +3h, +24h, +72h, and +7d"
        )
    )
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--facebook-db", type=Path, default=None)
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument(
        "--report-version",
        type=int,
        choices=(1, 2),
        default=2,
        help="Write legacy v1 files or dual-platform v2 files (default: 2)",
    )
    parser.add_argument(
        "--independent-start-at",
        default="",
        help=(
            "Override the independent Facebook-upload cutover timestamp; "
            "defaults to publishing.facebook_reels.mirror_start_at"
        ),
    )
    parser.add_argument(
        "--checkpoint",
        action="append",
        choices=tuple(CHECKPOINT_BY_KEY),
        help="Process only this checkpoint; repeat to select more than one",
    )
    parser.add_argument(
        "--lookback-hours",
        type=float,
        default=240.0,
        help="Inspect Reels published within this many hours (default: 240)",
    )
    parser.add_argument(
        "--as-of",
        default="",
        help="Timezone-aware ISO timestamp for deterministic checks/tests (default: now)",
    )
    parser.add_argument(
        "--no-sync",
        action="store_true",
        help="Never call Graph; render only already-recorded snapshots and missed windows",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show due/render/missed work without syncing or writing files",
    )
    parser.add_argument("--lock-wait-seconds", type=int, default=60)
    return parser


def parse_datetime(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def resolve_as_of(value: str) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    parsed = parse_datetime(value)
    if parsed is None:
        raise SystemExit(f"invalid --as-of timestamp: {value!r}")
    return parsed


def mapping_value(item: Mapping[str, Any], key: str, default: Any = None) -> Any:
    """Read sqlite.Row and ordinary mappings through one defensive interface."""
    try:
        return item[key]
    except (IndexError, KeyError):
        return default


def table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    if table not in {"reels", "trial_experiments"}:
        raise ValueError(f"unsupported table: {table}")
    return {
        str(row["name"])
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }


def load_trial_experiment(
    connection: sqlite3.Connection,
    reel: Mapping[str, Any],
) -> dict[str, Any]:
    """Return normalized experiment metadata when the optional table exists."""
    columns = table_columns(connection, "trial_experiments")
    if not {"content_hash", "channel_id"}.issubset(columns):
        return {}
    selected = [name for name in TRIAL_EXPERIMENT_COLUMNS if name in columns]
    if not selected:
        return {}
    row = connection.execute(
        f"SELECT {', '.join(selected)} FROM trial_experiments "
        "WHERE content_hash=? AND channel_id=? LIMIT 1",
        (
            str(mapping_value(reel, "content_hash") or ""),
            str(mapping_value(reel, "channel_id") or ""),
        ),
    ).fetchone()
    return {name: row[name] for name in selected} if row is not None else {}


def trial_launch_enabled(item: Mapping[str, Any]) -> bool:
    value = mapping_value(item, "trial_reel", 0)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def experiment_is_graduated(experiment: Mapping[str, Any]) -> bool:
    return bool(str(experiment.get("graduated_at") or "").strip()) or (
        str(experiment.get("state") or "").strip().lower() == "graduated"
    )


def trial_distribution_cohort(
    item: Mapping[str, Any],
    experiment: Mapping[str, Any],
) -> str:
    if not trial_launch_enabled(item):
        return "REGULAR"
    if experiment_is_graduated(experiment):
        return "GRADUATED_TRIAL"
    state = str(experiment.get("state") or "").strip().lower()
    if state in {"stopped", "failed"}:
        return "CLOSED_TRIAL"
    return "TRIAL_ACTIVE"


def trial_phase_at(
    captured_at: datetime | None,
    *,
    item: Mapping[str, Any],
    experiment: Mapping[str, Any],
) -> str:
    if not trial_launch_enabled(item):
        return "NOT_TRIAL"
    graduated_at = parse_datetime(experiment.get("graduated_at"))
    if graduated_at is not None and captured_at is not None:
        return (
            "POST_GRADUATION"
            if captured_at >= graduated_at
            else "PRE_GRADUATION"
        )
    if experiment_is_graduated(experiment):
        return "GRADUATION_TIME_UNKNOWN"
    return "PRE_GRADUATION"


@contextmanager
def directory_lock(lock_dir: Path, wait_seconds: int) -> Iterator[None]:
    if wait_seconds < 0:
        raise SystemExit("--lock-wait-seconds must be non-negative")
    deadline = time.monotonic() + wait_seconds
    lock_dir.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            lock_dir.mkdir()
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise SystemExit(
                    f"[aibrief-jp-checkpoints] lock remained busy for {wait_seconds}s: {lock_dir}"
                )
            time.sleep(min(5.0, max(0.1, deadline - time.monotonic())))
    try:
        yield
    finally:
        try:
            lock_dir.rmdir()
        except FileNotFoundError:
            pass


def age_hours(captured_at: datetime, published_at: datetime) -> float:
    return (captured_at - published_at).total_seconds() / 3600.0


def checkpoint_path(out_dir: Path, reel: Mapping[str, Any], checkpoint: Checkpoint) -> Path:
    published_at = parse_datetime(reel.get("published_at"))
    if published_at is None:
        raise ValueError("published Reel is missing a valid published_at")
    local = published_at.astimezone(JST)
    content_hash = str(reel.get("content_hash") or "")
    identity = f"{local:%H%M}_{content_hash[:12]}"
    return out_dir / local.date().isoformat() / identity / f"{checkpoint.key}.md"


def load_recent_reels(
    connection: sqlite3.Connection,
    *,
    as_of: datetime,
    lookback_hours: float,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT *
        FROM reels
        WHERE channel_id=?
          AND status='published'
          AND media_id IS NOT NULL
          AND media_id != ''
        ORDER BY published_at DESC, content_hash
        """,
        (CHANNEL,),
    ).fetchall()
    reels: list[dict[str, Any]] = []
    for row in rows:
        reel = dict(row)
        published_at = parse_datetime(reel.get("published_at"))
        if published_at is None:
            continue
        current_age = age_hours(as_of, published_at)
        if 0 <= current_age <= lookback_hours:
            reel["current_age_hours"] = current_age
            reel["trial_experiment"] = load_trial_experiment(connection, reel)
            reels.append(reel)
    return reels


def load_snapshot_rows(
    connection: sqlite3.Connection,
    reel: Mapping[str, Any],
) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT
          r.*,
          i.id AS insight_id,
          i.captured_at,
          i.views,
          i.total_views,
          i.reach,
          i.likes,
          i.total_likes,
          i.comments,
          i.total_comments,
          i.saved,
          i.shares,
          i.total_interactions,
          i.ig_reels_video_view_total_time,
          i.ig_reels_avg_watch_time,
          i.reels_skip_rate,
          i.clips_replays_count,
          i.facebook_views,
          i.crossposted_views,
          i.follows,
          i.raw
        FROM reels AS r
        JOIN insights AS i
          ON i.content_hash=r.content_hash
         AND i.channel_id=r.channel_id
         AND i.media_id=r.media_id
        WHERE r.content_hash=?
          AND r.channel_id=?
          AND r.media_id=?
          AND r.status='published'
        ORDER BY i.captured_at, i.id
        """,
        (
            str(reel.get("content_hash") or ""),
            str(reel.get("channel_id") or ""),
            str(reel.get("media_id") or ""),
        ),
    ).fetchall()


def valid_core_metrics(metrics: Mapping[str, Any]) -> bool:
    return all(
        isinstance(metrics.get(name), (int, float))
        and not isinstance(metrics.get(name), bool)
        and float(metrics[name]) >= 0
        for name in CORE_METRICS
    )


def snapshot_age(row: sqlite3.Row, published_at: datetime) -> float | None:
    captured_at = parse_datetime(row["captured_at"])
    if captured_at is None:
        return None
    return age_hours(captured_at, published_at)


def select_checkpoint_snapshot(
    rows: Sequence[sqlite3.Row],
    *,
    published_at: datetime,
    checkpoint: Checkpoint,
    as_of: datetime | None = None,
) -> sqlite3.Row | None:
    candidates: list[tuple[datetime, int, sqlite3.Row]] = []
    for row in rows:
        captured_at = parse_datetime(row["captured_at"])
        if captured_at is None:
            continue
        if as_of is not None and captured_at > as_of:
            continue
        observed_age = age_hours(captured_at, published_at)
        if not checkpoint.minimum_hours <= observed_age <= checkpoint.maximum_hours:
            continue
        metrics = reel_scheduler.latest_insight_metrics(row)
        if not valid_core_metrics(metrics):
            continue
        candidates.append((captured_at, int(row["insight_id"]), row))
    if not candidates:
        return None
    return min(candidates, key=lambda item: (item[0], item[1]))[2]


def selected_snapshots(
    rows: Sequence[sqlite3.Row], published_at: datetime, *, as_of: datetime | None = None
) -> dict[str, sqlite3.Row]:
    selected: dict[str, sqlite3.Row] = {}
    for checkpoint in CHECKPOINTS:
        row = select_checkpoint_snapshot(
            rows,
            published_at=published_at,
            checkpoint=checkpoint,
            as_of=as_of,
        )
        if row is not None:
            selected[checkpoint.key] = row
    return selected


def metric(metrics: Mapping[str, Any], name: str) -> int | float | None:
    value = metrics.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def format_number(value: Any, *, decimals: int = 0, suffix: str = "") -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "N/A"
    if decimals:
        return f"{float(value):,.{decimals}f}{suffix}"
    return f"{float(value):,.0f}{suffix}"


def format_delta(current: Any, previous: Any, *, decimals: int = 0, suffix: str = "") -> str:
    if any(
        isinstance(value, bool) or not isinstance(value, (int, float))
        for value in (current, previous)
    ):
        return "N/A"
    difference = float(current) - float(previous)
    if decimals:
        return f"{difference:+,.{decimals}f}{suffix}"
    return f"{difference:+,.0f}{suffix}"


def per_thousand(numerator: Any, denominator: Any) -> float | None:
    if any(
        isinstance(value, bool) or not isinstance(value, (int, float))
        for value in (numerator, denominator)
    ):
        return None
    if float(denominator) <= 0:
        return None
    return 1000.0 * float(numerator) / float(denominator)


def optional_metric_errors(row: sqlite3.Row) -> list[str]:
    raw = str(row["raw"] or "").strip()
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return ["raw insight payload is not valid JSON"]
    errors = payload.get("optional_metric_errors") if isinstance(payload, dict) else None
    return [str(error) for error in errors] if isinstance(errors, list) else []


def markdown_inline(value: object) -> str:
    return (
        str(value or "")
        .replace("\r", " ")
        .replace("\n", " ")
        .replace("|", "\\|")
        .strip()
    )


def opening_excerpt(item: Mapping[str, Any], max_chars: int = 180) -> str:
    transcript = reel_scheduler.item_transcript(dict(item)).strip()
    if not transcript:
        return "Transcript unavailable"
    compact = " ".join(transcript.split())
    if len(compact) > max_chars:
        compact = compact[: max_chars - 1].rstrip() + "…"
    return compact


def duration_seconds(item: Mapping[str, Any]) -> float | None:
    segment = reel_scheduler.item_segment(dict(item))
    value = segment.get("duration")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def diagnostic_ratio(metrics: Mapping[str, Any], duration: float | None) -> float | None:
    average_ms = metric(metrics, "ig_reels_avg_watch_time")
    if average_ms is None or duration is None or duration <= 0:
        return None
    return 100.0 * float(average_ms) / (1000.0 * duration)


def previous_checkpoint_key(checkpoint: Checkpoint, available: Mapping[str, sqlite3.Row]) -> str | None:
    if checkpoint.key == "03h" and "01h" in available:
        return "01h"
    if checkpoint.key == "24h":
        if "03h" in available:
            return "03h"
        if "01h" in available:
            return "01h"
    if checkpoint.key == "72h":
        if "24h" in available:
            return "24h"
        if "03h" in available:
            return "03h"
        if "01h" in available:
            return "01h"
    if checkpoint.key == "7d":
        for key in ("72h", "24h", "03h", "01h"):
            if key in available:
                return key
    return None


def observation_lines(
    checkpoint: Checkpoint,
    metrics: Mapping[str, Any],
    previous_metrics: Mapping[str, Any] | None,
    *,
    observed_age: float,
    previous_age: float | None,
    duration: float | None,
) -> list[str]:
    views = metric(metrics, "views")
    reach = metric(metrics, "reach")
    interactions = metric(metrics, "total_interactions")
    saved = metric(metrics, "saved")
    shares = metric(metrics, "shares")
    save_share_total = (
        float(saved) + float(shares)
        if saved is not None and shares is not None
        else None
    )
    quality_rate = per_thousand(save_share_total, reach)
    skip_rate = metric(metrics, "reels_skip_rate")
    average_ms = metric(metrics, "ig_reels_avg_watch_time")
    watch_ratio = diagnostic_ratio(metrics, duration)

    if previous_metrics is not None and previous_age is not None:
        distribution = (
            f"Since the previous checkpoint ({previous_age:.2f}h), Instagram reach moved "
            f"{format_delta(reach, metric(previous_metrics, 'reach'))} and views moved "
            f"{format_delta(views, metric(previous_metrics, 'views'))}."
        )
    else:
        distribution = (
            f"At {observed_age:.2f}h, Instagram recorded {format_number(reach)} reach "
            f"from {format_number(views)} views."
        )

    if skip_rate is None and average_ms is None:
        retention = "Retention diagnostics were unavailable for this snapshot."
    else:
        parts: list[str] = []
        if skip_rate is not None:
            parts.append(f"first-3-second skip was {format_number(skip_rate, decimals=1, suffix='%')}")
        if average_ms is not None:
            parts.append(f"average watch was {float(average_ms) / 1000.0:.2f}s")
        if watch_ratio is not None:
            parts.append(f"average-watch/duration was {watch_ratio:.1f}%")
        retention = "Retention diagnostic: " + "; ".join(parts) + "."

    engagement = (
        f"The reached cohort produced {format_number(interactions)} total interaction(s) and "
        f"{format_number(save_share_total)} saves + shares"
        + (
            f" ({quality_rate:.1f} per 1,000 reached)."
            if quality_rate is not None
            else "; the per-reach rate is unavailable."
        )
    )

    if checkpoint.key == "7d":
        hypothesis = (
            "Mature read: use this result as descriptive evidence, but require "
            "several comparable posts before treating a format or series as repeatable."
        )
    elif checkpoint.key == "72h":
        hypothesis = (
            "Decision checkpoint: compare a Trial only with the Trial cohort and its "
            "registered parent. Do not add a Reel launched as Trial to the regular baseline, "
            "even after graduation."
        )
    elif checkpoint.key == "24h":
        if skip_rate is not None and float(skip_rate) >= 65:
            hypothesis = (
                "Candidate next test: keep the topic and payoff, but state the consequence "
                "more directly in the first three seconds."
            )
        elif skip_rate is not None and float(skip_rate) <= 45 and quality_rate is not None and quality_rate >= 10:
            hypothesis = (
                "Candidate next test: preserve this opening structure and retest it on one "
                "closely related topic before scaling the pattern."
            )
        else:
            hypothesis = (
                "Candidate next test: keep the topic and change only one opening element in "
                "a future Reel so the result remains interpretable."
            )
    elif checkpoint.key == "03h":
        hypothesis = "Current read: trajectory only; wait for the +24h checkpoint."
    else:
        hypothesis = "Current read: baseline observation only; wait for the +3h checkpoint."
    return [distribution, retention, engagement, hypothesis]


def render_checkpoint(
    *,
    checkpoint: Checkpoint,
    row: sqlite3.Row,
    available: Mapping[str, sqlite3.Row],
    trial_experiment: Mapping[str, Any] | None = None,
) -> str:
    experiment = dict(trial_experiment or {})
    item = reel_scheduler.build_reel_insight_export_item(row)
    metrics = reel_scheduler.latest_insight_metrics(row)
    published_at = parse_datetime(row["published_at"])
    captured_at = parse_datetime(row["captured_at"])
    if published_at is None or captured_at is None:
        raise ValueError("selected checkpoint row has an invalid timestamp")
    observed_age = age_hours(captured_at, published_at)
    previous_key = previous_checkpoint_key(checkpoint, available)
    previous_row = available.get(previous_key) if previous_key else None
    previous_metrics = (
        reel_scheduler.latest_insight_metrics(previous_row)
        if previous_row is not None
        else None
    )
    previous_age = (
        snapshot_age(previous_row, published_at) if previous_row is not None else None
    )
    duration = duration_seconds(item)
    watch_ratio = diagnostic_ratio(metrics, duration)
    reach = metric(metrics, "reach")
    save_share_total = (
        float(metric(metrics, "saved")) + float(metric(metrics, "shares"))
        if metric(metrics, "saved") is not None and metric(metrics, "shares") is not None
        else None
    )
    quality_rate = per_thousand(save_share_total, reach)
    errors = optional_metric_errors(row)
    missing_optional = [name for name in DISPLAY_METRICS if metric(metrics, name) is None]
    permalink = str(row["permalink"] or "").strip()
    reel_link = f"[Open Reel]({permalink})" if permalink else "Unavailable"
    comparison_label = (
        f"{CHECKPOINT_BY_KEY[previous_key].label} at {previous_age:.2f}h"
        if previous_key and previous_age is not None
        else "NOT_AVAILABLE"
    )
    trial_enabled = trial_launch_enabled(row)
    distribution_cohort = trial_distribution_cohort(row, experiment)
    trial_phase = trial_phase_at(
        captured_at,
        item=row,
        experiment=experiment,
    )
    experiment_id = str(experiment.get("experiment_id") or "").strip()
    experiment_state = str(experiment.get("state") or "").strip()
    experiment_case = str(experiment.get("case_type") or "").strip()
    asset_family = str(experiment.get("asset_family_id") or "").strip()
    parent_media_id = str(experiment.get("parent_media_id") or "").strip()
    baseline_hook = str(experiment.get("baseline_hook") or "").strip()
    variant_hook = str(experiment.get("variant_hook") or "").strip()
    graduation_strategy = str(
        mapping_value(row, "trial_graduation_strategy") or ""
    ).strip()

    lines = [
        f"# Reel checkpoint — {checkpoint.label}",
        "",
        f"- Status: `{checkpoint.stage}`",
        f"- Title: {markdown_inline(row['title'])}",
        f"- Reel: {reel_link}",
        f"- Identity: `{str(row['content_hash'])[:12]}` / `{row['media_id']}`",
        f"- Published: {published_at.astimezone(JST).isoformat(timespec='seconds')}",
        f"- Captured: {captured_at.astimezone(JST).isoformat(timespec='seconds')}",
        f"- Actual observed age: **{observed_age:.2f}h**",
        (
            f"- Accepted window: {checkpoint.minimum_hours:g}–{checkpoint.maximum_hours:g}h "
            "after actual publication"
        ),
        f"- Compared with: {comparison_label}",
        "",
        "## Distribution cohort",
        "",
        f"- Launch type: `{'TRIAL_REEL' if trial_enabled else 'REGULAR_REEL'}`",
        f"- Cohort: `{distribution_cohort}`",
        f"- Trial phase at capture: `{trial_phase}`",
        (
            f"- Trial graduation strategy: `{graduation_strategy}`"
            if trial_enabled and graduation_strategy
            else "- Trial graduation strategy: not applicable"
        ),
        f"- Experiment ID: `{experiment_id}`" if experiment_id else "- Experiment ID: not registered",
        f"- Experiment case: `{experiment_case}`" if experiment_case else "- Experiment case: unavailable",
        f"- Experiment state: `{experiment_state}`" if experiment_state else "- Experiment state: unavailable",
        f"- Asset family: `{asset_family}`" if asset_family else "- Asset family: unavailable",
        f"- Parent media: `{parent_media_id}`" if parent_media_id else "- Parent media: unavailable",
        f"- Baseline hook: {markdown_inline(baseline_hook)}" if baseline_hook else "- Baseline hook: unavailable",
        f"- Trial hook: {markdown_inline(variant_hook)}" if variant_hook else "- Trial hook: unavailable",
        "",
        "## Snapshot",
        "",
        "| Signal | Value | Change from previous checkpoint |",
        "| --- | ---: | ---: |",
    ]
    metric_labels = (
        ("views", "Instagram views", 0, ""),
        ("reach", "Instagram reach", 0, ""),
        ("total_views", "Meta all-surface views", 0, ""),
        ("reels_skip_rate", "First-3s skip", 1, "%"),
        ("ig_reels_avg_watch_time", "Average watch", 0, " ms"),
        ("likes", "Instagram likes", 0, ""),
        ("comments", "Instagram comments", 0, ""),
        ("saved", "Instagram saves", 0, ""),
        ("shares", "Instagram shares", 0, ""),
        ("total_interactions", "Instagram total interactions", 0, ""),
        ("facebook_views", "Facebook views", 0, ""),
        ("crossposted_views", "IG + Facebook crossposted views", 0, ""),
    )
    for name, label, decimals, suffix in metric_labels:
        value = metric(metrics, name)
        previous_value = metric(previous_metrics or {}, name)
        delta_suffix = " pp" if name == "reels_skip_rate" else suffix
        lines.append(
            f"| {label} | {format_number(value, decimals=decimals, suffix=suffix)} | "
            f"{format_delta(value, previous_value, decimals=decimals, suffix=delta_suffix)} |"
        )
    lines.extend(
        [
            f"| Save + share / 1,000 reached | {format_number(quality_rate, decimals=1)} | — |",
            (
                "| Average watch / estimated duration | "
                f"{format_number(watch_ratio, decimals=1, suffix='%')} | — |"
            ),
            "",
            "The average-watch/duration value is a diagnostic ratio, not a completion rate or retention curve.",
            "Instagram, Facebook, crossposted, and Meta all-surface views can overlap and are never added together.",
            "",
            "## Hook evidence",
            "",
            f"- Title hook: **{markdown_inline(reel_scheduler.item_hook(item))}**",
            f"- Spoken opening: “{markdown_inline(opening_excerpt(item))}”",
            "",
            "## Read",
            "",
        ]
    )
    for observation in observation_lines(
        checkpoint,
        metrics,
        previous_metrics,
        observed_age=observed_age,
        previous_age=previous_age,
        duration=duration,
    ):
        lines.append(f"- {observation}")
    lines.extend(
        [
            f"- Next: {checkpoint.next_step}",
            "",
            "## Data quality",
            "",
            f"- Missing or unavailable fields: {', '.join(missing_optional) or 'none'}",
            f"- Optional API warnings: {'; '.join(errors) or 'none'}",
            "- This is a cumulative observational snapshot, not proof that the hook caused the result.",
            "",
        ]
    )
    return "\n".join(lines)


def render_missed(
    reel: Mapping[str, Any],
    checkpoint: Checkpoint,
    *,
    trial_experiment: Mapping[str, Any] | None = None,
) -> str:
    published_at = parse_datetime(reel.get("published_at"))
    if published_at is None:
        raise ValueError("published Reel is missing a valid published_at")
    permalink = str(reel.get("permalink") or "").strip()
    reel_link = f"[Open Reel]({permalink})" if permalink else "Unavailable"
    experiment = dict(trial_experiment or {})
    trial_enabled = trial_launch_enabled(reel)
    experiment_id = str(experiment.get("experiment_id") or "").strip()
    experiment_state = str(experiment.get("state") or "").strip()
    return "\n".join(
        [
            f"# Reel checkpoint — {checkpoint.label}",
            "",
            "- Status: `MISSED_CHECKPOINT`",
            f"- Title: {markdown_inline(reel.get('title'))}",
            f"- Reel: {reel_link}",
            (
                f"- Identity: `{str(reel.get('content_hash') or '')[:12]}` / "
                f"`{str(reel.get('media_id') or '')}`"
            ),
            f"- Published: {published_at.astimezone(JST).isoformat(timespec='seconds')}",
            (
                f"- Required window: {checkpoint.minimum_hours:g}–{checkpoint.maximum_hours:g}h "
                "after actual publication"
            ),
            f"- Launch type: `{'TRIAL_REEL' if trial_enabled else 'REGULAR_REEL'}`",
            f"- Cohort: `{trial_distribution_cohort(reel, experiment)}`",
            (
                f"- Experiment ID: `{experiment_id}`"
                if experiment_id
                else "- Experiment ID: not registered"
            ),
            (
                f"- Experiment state: `{experiment_state}`"
                if experiment_state
                else "- Experiment state: unavailable"
            ),
            "",
            "No core-valid insight snapshot was stored inside this checkpoint window. "
            "A later lifetime value was not substituted or relabeled.",
            "",
        ]
    )


def atomic_write_if_absent(path: Path, content: str) -> bool:
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp_path.write_text(content, encoding="utf-8")
        if path.exists():
            return False
        os.replace(temp_path, path)
        return True
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def run_exact_sync(
    *,
    root: Path,
    db_path: Path,
    media_ids: Sequence[str],
    platform: str = "instagram",
    metrics: Sequence[str] | None = None,
) -> int:
    command = [
        sys.executable,
        str(root / "reel_scheduler.py"),
        "sync-insights",
        "--platform",
        platform,
        "--channel",
        CHANNEL,
        "--db",
        str(db_path),
    ]
    if metrics is not None:
        command.extend(["--metrics", ",".join(metrics)])
    for media_id in sorted(set(media_ids)):
        command.extend(["--media-id", media_id])
    print(f"[aibrief-jp-checkpoints] run: {' '.join(command)}", flush=True)
    return subprocess.run(command, cwd=root, check=False).returncode


def describe_work(
    reel: Mapping[str, Any], checkpoint: Checkpoint, action: str, path: Path
) -> str:
    return (
        f"{action} {checkpoint.key} media_id={reel.get('media_id')} "
        f"age={float(reel.get('current_age_hours') or 0):.2f}h path={path}"
    )


def main_v1(args: argparse.Namespace) -> int:
    root = args.root.expanduser().resolve()
    db_path = (args.db or root / "state" / "reels.db").expanduser().resolve()
    out_dir = (
        args.out_dir or root / "out" / "aibrief_jp_reel_learning"
    ).expanduser().resolve()
    if not db_path.is_file():
        raise SystemExit(f"ledger database not found: {db_path}")
    if args.lookback_hours <= 0:
        raise SystemExit("--lookback-hours must be greater than zero")
    as_of = resolve_as_of(args.as_of)
    checkpoints = tuple(
        CHECKPOINT_BY_KEY[key] for key in (args.checkpoint or CHECKPOINT_BY_KEY)
    )

    lock_dir = root / "state" / "reel_scheduler.lock"
    with directory_lock(lock_dir, args.lock_wait_seconds):
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        try:
            reels = load_recent_reels(
                connection,
                as_of=as_of,
                lookback_hours=args.lookback_hours,
            )
            due_media_ids: set[str] = set()
            work: dict[tuple[str, str], str] = {}
            for reel in reels:
                published_at = parse_datetime(reel.get("published_at"))
                if published_at is None:
                    continue
                rows = load_snapshot_rows(connection, reel)
                available = selected_snapshots(rows, published_at, as_of=as_of)
                for checkpoint in checkpoints:
                    path = checkpoint_path(out_dir, reel, checkpoint)
                    if path.exists():
                        continue
                    key = (str(reel["content_hash"]), checkpoint.key)
                    if checkpoint.key in available:
                        work[key] = "render"
                    elif checkpoint.minimum_hours <= reel["current_age_hours"] <= checkpoint.maximum_hours:
                        work[key] = "due"
                        due_media_ids.add(str(reel["media_id"]))
                    elif reel["current_age_hours"] > checkpoint.maximum_hours:
                        work[key] = "missed"

            if args.dry_run:
                for reel in reels:
                    for checkpoint in checkpoints:
                        action = work.get((str(reel["content_hash"]), checkpoint.key))
                        if action:
                            print(
                                "[aibrief-jp-checkpoints] "
                                + describe_work(
                                    reel,
                                    checkpoint,
                                    action,
                                    checkpoint_path(out_dir, reel, checkpoint),
                                )
                            )
                print(
                    "[aibrief-jp-checkpoints] dry-run summary "
                    f"recent_reels={len(reels)} work_items={len(work)} "
                    f"due_media={len(due_media_ids)}"
                )
                return 0

            sync_rc = 0
            if due_media_ids and not args.no_sync:
                connection.close()
                sync_rc = run_exact_sync(
                    root=root,
                    db_path=db_path,
                    media_ids=sorted(due_media_ids),
                )
                connection = sqlite3.connect(db_path)
                connection.row_factory = sqlite3.Row

            # A live Graph snapshot is captured a few seconds after the run's
            # initial ``as_of`` value. Accept it immediately. An explicit
            # ``--as-of`` remains frozen for deterministic historical checks.
            selection_as_of = (
                as_of if args.as_of else datetime.now(timezone.utc)
            )

            written = rendered = missed = waiting = 0
            for reel in reels:
                published_at = parse_datetime(reel.get("published_at"))
                if published_at is None:
                    continue
                rows = load_snapshot_rows(connection, reel)
                available = selected_snapshots(
                    rows,
                    published_at,
                    as_of=selection_as_of,
                )
                for checkpoint in checkpoints:
                    key = (str(reel["content_hash"]), checkpoint.key)
                    action = work.get(key)
                    if action is None:
                        continue
                    path = checkpoint_path(out_dir, reel, checkpoint)
                    selected = available.get(checkpoint.key)
                    if selected is not None:
                        content = render_checkpoint(
                            checkpoint=checkpoint,
                            row=selected,
                            available=available,
                            trial_experiment=reel.get("trial_experiment"),
                        )
                        if atomic_write_if_absent(path, content):
                            written += 1
                        rendered += 1
                        print(
                            "[aibrief-jp-checkpoints] "
                            + describe_work(reel, checkpoint, "recorded", path)
                        )
                    elif action == "missed":
                        if atomic_write_if_absent(
                            path,
                            render_missed(
                                reel,
                                checkpoint,
                                trial_experiment=reel.get("trial_experiment"),
                            ),
                        ):
                            written += 1
                        missed += 1
                        print(
                            "[aibrief-jp-checkpoints] "
                            + describe_work(reel, checkpoint, "missed", path)
                        )
                    else:
                        waiting += 1
                        print(
                            "[aibrief-jp-checkpoints] no core-valid snapshot yet "
                            f"checkpoint={checkpoint.key} media_id={reel['media_id']}"
                        )
            print(
                "[aibrief-jp-checkpoints] summary "
                f"recent_reels={len(reels)} due_media={len(due_media_ids)} "
                f"rendered={rendered} missed={missed} waiting={waiting} written={written}"
            )
            if waiting:
                return 1
            return 0 if sync_rc == 0 or not due_media_ids else sync_rc
        finally:
            connection.close()


def main() -> int:
    """Dispatch to immutable legacy reports or the dual-platform v2 runner."""
    args = build_parser().parse_args()
    report_version_explicit = any(
        value == "--report-version" or value.startswith("--report-version=")
        for value in sys.argv[1:]
    )
    legacy_custom_db = (
        args.db is not None
        and args.facebook_db is None
        and not report_version_explicit
    )
    if args.report_version == 1 or legacy_custom_db:
        return main_v1(args)
    if __name__ == "__main__":
        sys.modules.setdefault(
            "scripts.run_aibrief_jp_reel_checkpoints",
            sys.modules[__name__],
        )
    from scripts import aibrief_jp_reel_checkpoints_v2

    return aibrief_jp_reel_checkpoints_v2.main_v2(args)


if __name__ == "__main__":
    raise SystemExit(main())
