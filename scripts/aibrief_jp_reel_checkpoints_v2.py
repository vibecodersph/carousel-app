#!/usr/bin/env python3
"""Dual-platform AI Brief JP Reel checkpoint collection and rendering."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import reel_scheduler
from scripts import run_aibrief_jp_reel_checkpoints as legacy


PLATFORMS = ("instagram", "facebook")
TERMINAL_STATUSES = {
    "RECORDED",
    "MISSED_CHECKPOINT",
    "NOT_PUBLISHED",
    "MEDIA_ID_MISSING",
}
INDEPENDENT_INSTAGRAM_METRICS = tuple(
    name
    for name in reel_scheduler.INSTAGRAM_INSIGHT_REQUEST_METRIC_KEYS
    if name not in {"facebook_views", "crossposted_views"}
)


@dataclass
class PlatformState:
    platform: str
    row: dict[str, Any] | None
    status: str
    selected: sqlite3.Row | None
    available: dict[str, sqlite3.Row]
    published_at: datetime | None
    reference_at: datetime | None


def configured_independent_start(root: Path, override: str) -> datetime:
    def parse_required_timezone(value: object, source: str) -> datetime:
        text = str(value or "").strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if (
            parsed is None
            or parsed.tzinfo is None
            or parsed.utcoffset() is None
        ):
            raise SystemExit(
                f"{source} must be a timezone-aware ISO timestamp: {text!r}"
            )
        return parsed.astimezone(timezone.utc)

    if override:
        return parse_required_timezone(override, "--independent-start-at")
    channel_path = root / "channels" / legacy.CHANNEL / "channel.json"
    try:
        channel_data = json.loads(channel_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        raise SystemExit(
            f"cannot load independent-upload cutover from {channel_path}: {exc}"
        ) from exc
    publishing = (
        channel_data.get("publishing")
        if isinstance(channel_data, dict)
        and isinstance(channel_data.get("publishing"), dict)
        else {}
    )
    facebook = (
        publishing.get("facebook_reels")
        if isinstance(publishing.get("facebook_reels"), dict)
        else {}
    )
    value = str(facebook.get("mirror_start_at") or "").strip()
    return parse_required_timezone(
        value,
        "publishing.facebook_reels.mirror_start_at",
    )


def load_reel_map(
    connection: sqlite3.Connection,
    *,
    include_experiments: bool,
) -> dict[tuple[str, str], dict[str, Any]]:
    rows = connection.execute(
        "SELECT * FROM reels WHERE channel_id=? ORDER BY scheduled_at, content_hash",
        (legacy.CHANNEL,),
    ).fetchall()
    output: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        item = dict(row)
        if include_experiments:
            item["trial_experiment"] = legacy.load_trial_experiment(connection, item)
        key = (str(item.get("channel_id") or ""), str(item.get("content_hash") or ""))
        output[key] = item
    return output


def published_at(row: Mapping[str, Any] | None) -> datetime | None:
    if not row or str(row.get("status") or "") != "published":
        return None
    return legacy.parse_datetime(row.get("published_at"))


def has_media_id(row: Mapping[str, Any] | None) -> bool:
    return bool(row and str(row.get("media_id") or "").strip())


def unit_times(unit: Mapping[str, Any], field: str) -> list[datetime]:
    values: list[datetime] = []
    for platform in PLATFORMS:
        row = unit.get(platform)
        if not isinstance(row, Mapping):
            continue
        parsed = (
            published_at(row)
            if field == "published_at"
            else legacy.parse_datetime(row.get(field))
        )
        if parsed is not None:
            values.append(parsed)
    return values


def classify_mode(
    instagram: Mapping[str, Any] | None,
    facebook: Mapping[str, Any] | None,
    independent_start: datetime | None,
) -> str:
    if published_at(facebook) is not None and has_media_id(facebook):
        return "independent_dual_upload"
    if independent_start is None:
        return "legacy_crosspost"
    actual_candidates = [
        actual
        for row in (instagram, facebook)
        if row is not None
        if (actual := published_at(row)) is not None
    ]
    if actual_candidates:
        return (
            "independent_dual_upload"
            if min(actual_candidates) >= independent_start
            else "legacy_crosspost"
        )
    scheduled_candidates = [
        scheduled
        for row in (instagram, facebook)
        if row is not None
        if (scheduled := legacy.parse_datetime(row.get("scheduled_at"))) is not None
    ]
    return (
        "independent_dual_upload"
        if scheduled_candidates and min(scheduled_candidates) >= independent_start
        else "legacy_crosspost"
    )


def load_recent_units(
    instagram_connection: sqlite3.Connection,
    facebook_connection: sqlite3.Connection,
    *,
    as_of: datetime,
    lookback_hours: float,
    independent_start: datetime | None,
) -> list[dict[str, Any]]:
    instagram_rows = load_reel_map(
        instagram_connection,
        include_experiments=True,
    )
    facebook_rows = load_reel_map(
        facebook_connection,
        include_experiments=False,
    )
    units: list[dict[str, Any]] = []
    for key in set(instagram_rows) | set(facebook_rows):
        instagram = instagram_rows.get(key)
        facebook = facebook_rows.get(key)
        actual_times = [
            value
            for row in (instagram, facebook)
            if (value := published_at(row)) is not None
        ]
        if not actual_times:
            continue
        if not any(
            0 <= legacy.age_hours(as_of, value) <= lookback_hours
            for value in actual_times
        ):
            continue
        units.append(
            {
                "channel_id": key[0],
                "content_hash": key[1],
                "instagram": instagram,
                "facebook": facebook,
                "mode": classify_mode(instagram, facebook, independent_start),
            }
        )
    units.sort(
        key=lambda unit: min(unit_times(unit, "published_at")),
        reverse=True,
    )
    return units


def expected_platforms(unit: Mapping[str, Any]) -> tuple[str, ...]:
    return (
        PLATFORMS
        if unit.get("mode") == "independent_dual_upload"
        else ("instagram",)
    )


def anchor_at(unit: Mapping[str, Any]) -> datetime:
    values = unit_times(unit, "published_at")
    if not values:
        raise ValueError("logical Reel has no actual publication timestamp")
    return min(values)


def output_path(
    out_dir: Path,
    unit: Mapping[str, Any],
    checkpoint: legacy.Checkpoint,
) -> Path:
    local = anchor_at(unit).astimezone(legacy.JST)
    identity = f"{local:%H%M}_{str(unit.get('content_hash') or '')[:12]}"
    return out_dir / local.date().isoformat() / identity / f"{checkpoint.key}.v2.md"


def snapshot_metrics(
    row: sqlite3.Row | None,
    platform: str,
) -> dict[str, int | float]:
    if row is None:
        return {}
    metrics = reel_scheduler.latest_insight_metrics(row)
    if platform == "facebook":
        raw_payload = reel_scheduler.parse_raw_insight_payload(row)
        if isinstance(raw_payload, dict):
            metrics.update(reel_scheduler.facebook_reel_insight_metrics(raw_payload))
    return metrics


def valid_snapshot(platform: str, metrics: Mapping[str, Any]) -> bool:
    required = legacy.CORE_METRICS if platform == "instagram" else ("views",)
    return all(
        isinstance(metrics.get(name), (int, float))
        and not isinstance(metrics.get(name), bool)
        and float(metrics[name]) >= 0
        for name in required
    )


def select_snapshot(
    rows: Sequence[sqlite3.Row],
    *,
    platform: str,
    actual_published_at: datetime,
    checkpoint: legacy.Checkpoint,
    as_of: datetime,
) -> sqlite3.Row | None:
    candidates: list[tuple[datetime, int, sqlite3.Row]] = []
    for row in rows:
        captured_at = legacy.parse_datetime(row["captured_at"])
        if captured_at is None or captured_at > as_of:
            continue
        observed_age = legacy.age_hours(captured_at, actual_published_at)
        if not checkpoint.minimum_hours <= observed_age <= checkpoint.maximum_hours:
            continue
        if not valid_snapshot(platform, snapshot_metrics(row, platform)):
            continue
        candidates.append((captured_at, int(row["insight_id"]), row))
    return min(candidates, key=lambda item: (item[0], item[1]))[2] if candidates else None


def selected_snapshots(
    rows: Sequence[sqlite3.Row],
    *,
    platform: str,
    actual_published_at: datetime,
    as_of: datetime,
) -> dict[str, sqlite3.Row]:
    selected: dict[str, sqlite3.Row] = {}
    for checkpoint in legacy.CHECKPOINTS:
        row = select_snapshot(
            rows,
            platform=platform,
            actual_published_at=actual_published_at,
            checkpoint=checkpoint,
            as_of=as_of,
        )
        if row is not None:
            selected[checkpoint.key] = row
    return selected


def reference_at(unit: Mapping[str, Any], platform: str) -> datetime | None:
    row = unit.get(platform)
    if isinstance(row, Mapping):
        actual = published_at(row)
        if actual is not None:
            return actual
        scheduled = legacy.parse_datetime(row.get("scheduled_at"))
        if scheduled is not None:
            return scheduled
    actual = unit_times(unit, "published_at")
    if actual:
        return min(actual)
    scheduled = unit_times(unit, "scheduled_at")
    return min(scheduled) if scheduled else None


def evaluate_state(
    *,
    unit: Mapping[str, Any],
    platform: str,
    checkpoint: legacy.Checkpoint,
    connection: sqlite3.Connection,
    as_of: datetime,
) -> PlatformState:
    raw_row = unit.get(platform)
    row = dict(raw_row) if isinstance(raw_row, Mapping) else None
    actual = published_at(row)
    reference = actual or reference_at(unit, platform)
    if actual is None or not has_media_id(row):
        reference_age = (
            legacy.age_hours(as_of, reference) if reference is not None else None
        )
        if reference_age is None or reference_age <= checkpoint.maximum_hours:
            status = "NOT_STARTED"
        elif row is not None and str(row.get("status") or "") == "published":
            status = "MEDIA_ID_MISSING"
        else:
            status = "NOT_PUBLISHED"
        return PlatformState(
            platform,
            row,
            status,
            None,
            {},
            actual,
            reference,
        )

    rows = legacy.load_snapshot_rows(connection, row)
    available = selected_snapshots(
        rows,
        platform=platform,
        actual_published_at=actual,
        as_of=as_of,
    )
    selected = available.get(checkpoint.key)
    current_age = legacy.age_hours(as_of, actual)
    if selected is not None:
        status = "RECORDED"
    elif current_age < checkpoint.minimum_hours:
        status = "NOT_STARTED"
    elif current_age <= checkpoint.maximum_hours:
        status = "DUE"
    else:
        status = "MISSED_CHECKPOINT"
    return PlatformState(
        platform,
        row,
        status,
        selected,
        available,
        actual,
        reference,
    )


def evaluate_states(
    unit: Mapping[str, Any],
    checkpoint: legacy.Checkpoint,
    *,
    instagram_connection: sqlite3.Connection,
    facebook_connection: sqlite3.Connection,
    as_of: datetime,
) -> dict[str, PlatformState]:
    connections = {
        "instagram": instagram_connection,
        "facebook": facebook_connection,
    }
    return {
        platform: evaluate_state(
            unit=unit,
            platform=platform,
            checkpoint=checkpoint,
            connection=connections[platform],
            as_of=as_of,
        )
        for platform in expected_platforms(unit)
    }


def terminal(states: Mapping[str, PlatformState]) -> bool:
    return bool(states) and all(
        state.status in TERMINAL_STATUSES for state in states.values()
    )


def report_status(
    states: Mapping[str, PlatformState],
    checkpoint: legacy.Checkpoint,
) -> str:
    recorded = sum(state.status == "RECORDED" for state in states.values())
    if recorded == len(states):
        return checkpoint.stage
    if recorded:
        return "PARTIAL_CHECKPOINT"
    return "MISSED_CHECKPOINT"


def platform_permalink(platform: str, value: object) -> str:
    permalink = str(value or "").strip()
    if platform == "facebook" and permalink.startswith("/"):
        return f"https://www.facebook.com{permalink}"
    return permalink


def metric_rows(
    platform: str,
    mode: str,
) -> tuple[tuple[str, str, int, str], ...]:
    if platform == "facebook":
        return (
            ("views", "Facebook total plays", 0, ""),
            ("reach", "Facebook unique media viewers", 0, ""),
            ("blue_reels_play_count", "Facebook initial plays", 0, ""),
            ("fb_reels_replay_count", "Facebook replays", 0, ""),
            ("post_video_avg_time_watched", "Facebook average watch", 0, " ms"),
            ("post_video_view_time", "Facebook total watch time", 0, " ms"),
            ("likes", "Facebook reactions", 0, ""),
            ("comments", "Facebook comments", 0, ""),
            ("shares", "Facebook shares", 0, ""),
            ("total_interactions", "Facebook interactions", 0, ""),
        )
    rows = [
        ("views", "Instagram views", 0, ""),
        ("reach", "Instagram reach", 0, ""),
        ("total_views", "Meta all-surface views (diagnostic)", 0, ""),
        ("reels_skip_rate", "Instagram first-3s skip", 1, "%"),
        ("ig_reels_avg_watch_time", "Instagram average watch", 0, " ms"),
        ("likes", "Instagram likes", 0, ""),
        ("comments", "Instagram comments", 0, ""),
        ("saved", "Instagram saves", 0, ""),
        ("shares", "Instagram shares", 0, ""),
        ("total_interactions", "Instagram interactions", 0, ""),
    ]
    if mode == "legacy_crosspost":
        rows.extend(
            (
                ("facebook_views", "Legacy Facebook views from IG object", 0, ""),
                ("crossposted_views", "Legacy IG + Facebook crossposted views", 0, ""),
            )
        )
    return tuple(rows)


def render_platform_section(
    *,
    platform: str,
    state: PlatformState,
    checkpoint: legacy.Checkpoint,
    mode: str,
) -> list[str]:
    label = "Instagram" if platform == "instagram" else "Facebook"
    lines = [f"## {label} snapshot", ""]
    if state.status != "RECORDED" or state.selected is None:
        return lines + [
            f"- Checkpoint status: `{state.status}`",
            "- No core-valid platform snapshot is attached to this checkpoint.",
            "",
        ]
    current = snapshot_metrics(state.selected, platform)
    prior_key = legacy.previous_checkpoint_key(checkpoint, state.available)
    prior_row = state.available.get(prior_key) if prior_key else None
    prior = snapshot_metrics(prior_row, platform)
    captured_at = legacy.parse_datetime(state.selected["captured_at"])
    prior_captured_at = (
        legacy.parse_datetime(prior_row["captured_at"])
        if prior_row is not None
        else None
    )
    observed_age = (
        legacy.age_hours(captured_at, state.published_at)
        if captured_at is not None and state.published_at is not None
        else None
    )
    prior_age = (
        legacy.age_hours(prior_captured_at, state.published_at)
        if prior_captured_at is not None and state.published_at is not None
        else None
    )
    comparison = (
        f"{legacy.CHECKPOINT_BY_KEY[prior_key].label} at {prior_age:.2f}h"
        if prior_key and prior_age is not None
        else "NOT_AVAILABLE"
    )
    lines.extend(
        [
            "- Checkpoint status: `RECORDED`",
            (
                f"- Captured: {captured_at.astimezone(legacy.JST).isoformat(timespec='seconds')}"
                if captured_at is not None
                else "- Captured: unavailable"
            ),
            (
                f"- Actual observed age: **{observed_age:.2f}h**"
                if observed_age is not None
                else "- Actual observed age: unavailable"
            ),
            f"- Compared with: {comparison}",
            "",
            "| Signal | Value | Change from previous platform checkpoint |",
            "| --- | ---: | ---: |",
        ]
    )
    for name, label, decimals, suffix in metric_rows(platform, mode):
        value = legacy.metric(current, name)
        prior_value = legacy.metric(prior, name)
        delta_suffix = " pp" if name == "reels_skip_rate" else suffix
        lines.append(
            f"| {label} | {legacy.format_number(value, decimals=decimals, suffix=suffix)} | "
            f"{legacy.format_delta(value, prior_value, decimals=decimals, suffix=delta_suffix)} |"
        )
    lines.extend(
        [
            "",
            (
                "- Optional API warnings: "
                + ("; ".join(legacy.optional_metric_errors(state.selected)) or "none")
            ),
            "",
        ]
    )
    return lines


def canonical_row(unit: Mapping[str, Any]) -> Mapping[str, Any]:
    instagram = unit.get("instagram")
    if isinstance(instagram, Mapping):
        return instagram
    facebook = unit.get("facebook")
    return facebook if isinstance(facebook, Mapping) else {}


def render_checkpoint(
    *,
    unit: Mapping[str, Any],
    checkpoint: legacy.Checkpoint,
    states: Mapping[str, PlatformState],
) -> str:
    mode = str(unit.get("mode") or "")
    canonical = canonical_row(unit)
    instagram = unit.get("instagram")
    experiment = (
        dict(instagram.get("trial_experiment") or {})
        if isinstance(instagram, Mapping)
        and isinstance(instagram.get("trial_experiment"), Mapping)
        else {}
    )
    instagram_state = states.get("instagram")
    instagram_captured_at = (
        legacy.parse_datetime(instagram_state.selected["captured_at"])
        if instagram_state is not None and instagram_state.selected is not None
        else None
    )
    trial_enabled = legacy.trial_launch_enabled(canonical)
    trial_phase = (
        legacy.trial_phase_at(
            instagram_captured_at,
            item=canonical,
            experiment=experiment,
        )
        if instagram_captured_at is not None or not trial_enabled
        else "UNAVAILABLE"
    )
    graduation_strategy = str(
        legacy.mapping_value(canonical, "trial_graduation_strategy") or ""
    ).strip()
    experiment_id = str(experiment.get("experiment_id") or "").strip()
    experiment_case = str(experiment.get("case_type") or "").strip()
    experiment_state = str(experiment.get("state") or "").strip()
    asset_family = str(experiment.get("asset_family_id") or "").strip()
    parent_media_id = str(experiment.get("parent_media_id") or "").strip()
    baseline_hook = str(experiment.get("baseline_hook") or "").strip()
    variant_hook = str(experiment.get("variant_hook") or "").strip()
    lines = [
        f"# Reel checkpoint v2 — {checkpoint.label}",
        "",
        f"- Status: `{report_status(states, checkpoint)}`",
        "- Report version: `2`",
        f"- Distribution mode: `{mode}`",
        f"- Title: {legacy.markdown_inline(canonical.get('title'))}",
        f"- Logical identity: `{str(unit.get('content_hash') or '')}`",
        (
            "- First platform publication: "
            + anchor_at(unit).astimezone(legacy.JST).isoformat(timespec="seconds")
        ),
        (
            f"- Accepted window per platform: {checkpoint.minimum_hours:g}–"
            f"{checkpoint.maximum_hours:g}h after that platform's actual publication"
        ),
        "",
        "## Deliveries",
        "",
        "| Platform | Publication status | Checkpoint status | Media ID | Link | Published |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for platform in expected_platforms(unit):
        state = states[platform]
        row = state.row or {}
        permalink = platform_permalink(platform, row.get("permalink"))
        link = f"[Open]({permalink})" if permalink else "Unavailable"
        actual = (
            state.published_at.astimezone(legacy.JST).isoformat(timespec="seconds")
            if state.published_at is not None
            else "Unavailable"
        )
        media_id = (
            f"`{legacy.markdown_inline(row.get('media_id'))}`"
            if row.get("media_id")
            else "Unavailable"
        )
        lines.append(
            "| "
            + " | ".join(
                (
                    "Instagram" if platform == "instagram" else "Facebook",
                    legacy.markdown_inline(row.get("status") or "missing"),
                    f"`{state.status}`",
                    media_id,
                    link,
                    actual,
                )
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Distribution cohort",
            "",
            f"- Launch type: `{'TRIAL_REEL' if trial_enabled else 'REGULAR_REEL'}`",
            f"- Cohort: `{legacy.trial_distribution_cohort(canonical, experiment)}`",
            f"- Trial phase at Instagram capture: `{trial_phase}`",
            (
                f"- Trial graduation strategy: `{graduation_strategy}`"
                if trial_enabled and graduation_strategy
                else "- Trial graduation strategy: not applicable"
            ),
            (
                f"- Experiment ID: `{legacy.markdown_inline(experiment_id)}`"
                if experiment_id
                else "- Experiment ID: not registered"
            ),
            (
                f"- Experiment case: `{legacy.markdown_inline(experiment_case)}`"
                if experiment_case
                else "- Experiment case: unavailable"
            ),
            (
                f"- Experiment state: `{legacy.markdown_inline(experiment_state)}`"
                if experiment_state
                else "- Experiment state: unavailable"
            ),
            (
                f"- Asset family: `{legacy.markdown_inline(asset_family)}`"
                if asset_family
                else "- Asset family: unavailable"
            ),
            (
                f"- Parent media: `{legacy.markdown_inline(parent_media_id)}`"
                if parent_media_id
                else "- Parent media: unavailable"
            ),
            (
                f"- Baseline hook: {legacy.markdown_inline(baseline_hook)}"
                if baseline_hook
                else "- Baseline hook: unavailable"
            ),
            (
                f"- Trial hook: {legacy.markdown_inline(variant_hook)}"
                if variant_hook
                else "- Trial hook: unavailable"
            ),
            "",
        ]
    )
    for platform in expected_platforms(unit):
        lines.extend(
            render_platform_section(
                platform=platform,
                state=states[platform],
                checkpoint=checkpoint,
                mode=mode,
            )
        )
    lines.extend(["## Content-wide roll-up", ""])
    if mode == "independent_dual_upload":
        instagram_state = states.get("instagram")
        facebook_state = states.get("facebook")
        instagram_views = legacy.metric(
            snapshot_metrics(
                instagram_state.selected if instagram_state else None,
                "instagram",
            ),
            "views",
        )
        facebook_plays = legacy.metric(
            snapshot_metrics(
                facebook_state.selected if facebook_state else None,
                "facebook",
            ),
            "views",
        )
        if instagram_views is not None and facebook_plays is not None:
            combined = float(instagram_views) + float(facebook_plays)
            lines.append(
                f"- Combined non-unique plays: **{legacy.format_number(combined)}** "
                f"({legacy.format_number(instagram_views)} Instagram + "
                f"{legacy.format_number(facebook_plays)} Facebook)."
            )
        else:
            lines.append(
                "- Combined non-unique plays: unavailable until both platform play counts are recorded."
            )
        lines.extend(
            [
                "- This is a sum of platform play events, not unique viewers; Meta defines the measures separately.",
                "- Reach is not summed because cross-platform identity overlap is unknown.",
                "- Instagram all-surface/crosspost fields are not added to the independent Facebook object.",
            ]
        )
    else:
        instagram_metrics = snapshot_metrics(states["instagram"].selected, "instagram")
        crossposted = legacy.metric(instagram_metrics, "crossposted_views")
        lines.extend(
            [
                (
                    f"- Legacy crossposted views: **{legacy.format_number(crossposted)}**."
                    if crossposted is not None
                    else "- Legacy crossposted views: unavailable."
                ),
                "- `crossposted_views` is already the Instagram + Facebook aggregate and is never added to Instagram views.",
            ]
        )
    reference_row = next(
        (
            state.selected
            for platform in expected_platforms(unit)
            if (state := states[platform]).selected is not None
        ),
        None,
    )
    if reference_row is not None:
        item = reel_scheduler.build_reel_insight_export_item(reference_row)
        lines.extend(
            [
                "",
                "## Hook evidence",
                "",
                f"- Title hook: **{legacy.markdown_inline(reel_scheduler.item_hook(item))}**",
                f"- Spoken opening: “{legacy.markdown_inline(legacy.opening_excerpt(item))}”",
            ]
        )
    lines.extend(
        [
            "",
            "## Data quality",
            "",
            "- Platform snapshots use each platform's own media ID and actual publication clock.",
            "- Missing and unavailable values are not converted to zero.",
            "- A later lifetime value is never substituted for a missed checkpoint.",
            "- This is a cumulative observational snapshot, not proof that the hook caused the result.",
            f"- Next: {checkpoint.next_step}",
            "",
        ]
    )
    return "\n".join(lines)


def open_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def main_v2(args: Any) -> int:
    root = args.root.expanduser().resolve()
    instagram_db = (args.db or root / "state" / "reels.db").expanduser().resolve()
    facebook_db = (
        args.facebook_db or root / "state" / "facebook.db"
    ).expanduser().resolve()
    out_dir = (
        args.out_dir or root / "out" / "aibrief_jp_reel_learning"
    ).expanduser().resolve()
    for label, path in (("Instagram", instagram_db), ("Facebook", facebook_db)):
        if not path.is_file():
            raise SystemExit(f"{label} ledger database not found: {path}")
    if args.lookback_hours <= 0:
        raise SystemExit("--lookback-hours must be greater than zero")
    as_of = legacy.resolve_as_of(args.as_of)
    checkpoints = tuple(
        legacy.CHECKPOINT_BY_KEY[key]
        for key in (args.checkpoint or legacy.CHECKPOINT_BY_KEY)
    )
    independent_start = configured_independent_start(
        root,
        args.independent_start_at,
    )
    lock_dir = root / "state" / "reel_scheduler.lock"
    with legacy.directory_lock(lock_dir, args.lock_wait_seconds):
        instagram_connection = open_connection(instagram_db)
        facebook_connection = open_connection(facebook_db)
        try:
            units = load_recent_units(
                instagram_connection,
                facebook_connection,
                as_of=as_of,
                lookback_hours=args.lookback_hours,
                independent_start=independent_start,
            )
            work: list[tuple[dict[str, Any], legacy.Checkpoint]] = []
            due: dict[tuple[str, str], set[str]] = {}
            initial: dict[tuple[str, str], dict[str, PlatformState]] = {}
            for unit in units:
                for checkpoint in checkpoints:
                    path = output_path(out_dir, unit, checkpoint)
                    if path.exists():
                        continue
                    states = evaluate_states(
                        unit,
                        checkpoint,
                        instagram_connection=instagram_connection,
                        facebook_connection=facebook_connection,
                        as_of=as_of,
                    )
                    key = (str(unit["content_hash"]), checkpoint.key)
                    initial[key] = states
                    if terminal(states):
                        work.append((unit, checkpoint))
                    elif any(state.status == "DUE" for state in states.values()):
                        work.append((unit, checkpoint))
                        for platform, state in states.items():
                            if state.status == "DUE" and state.row is not None:
                                due.setdefault((platform, str(unit["mode"])), set()).add(
                                    str(state.row.get("media_id") or "")
                                )
            if args.dry_run:
                for unit, checkpoint in work:
                    states = initial[(str(unit["content_hash"]), checkpoint.key)]
                    statuses = " ".join(
                        f"{platform}={state.status}"
                        for platform, state in states.items()
                    )
                    print(
                        f"[aibrief-jp-checkpoints-v2] {checkpoint.key} "
                        f"hash={str(unit['content_hash'])[:12]} {statuses} "
                        f"path={output_path(out_dir, unit, checkpoint)}"
                    )
                print(
                    "[aibrief-jp-checkpoints-v2] dry-run summary "
                    f"recent_units={len(units)} work_items={len(work)} "
                    f"due_media={sum(len(values) for values in due.values())}"
                )
                return 0

            sync_failures = 0
            if due and not args.no_sync:
                instagram_connection.close()
                facebook_connection.close()
                for (platform, mode), media_ids in sorted(due.items()):
                    if platform == "instagram":
                        db_path = instagram_db
                        metrics = (
                            INDEPENDENT_INSTAGRAM_METRICS
                            if mode == "independent_dual_upload"
                            else reel_scheduler.INSTAGRAM_INSIGHT_REQUEST_METRIC_KEYS
                        )
                    else:
                        db_path = facebook_db
                        metrics = reel_scheduler.FACEBOOK_INSIGHT_REQUEST_METRIC_KEYS
                    rc = legacy.run_exact_sync(
                        root=root,
                        db_path=db_path,
                        media_ids=sorted(media_ids),
                        platform=platform,
                        metrics=metrics,
                    )
                    if rc != 0:
                        sync_failures += 1
                instagram_connection = open_connection(instagram_db)
                facebook_connection = open_connection(facebook_db)
            selection_as_of = as_of if args.as_of else datetime.now(timezone.utc)
            written = rendered = waiting = pending = 0
            for unit, checkpoint in work:
                states = evaluate_states(
                    unit,
                    checkpoint,
                    instagram_connection=instagram_connection,
                    facebook_connection=facebook_connection,
                    as_of=selection_as_of,
                )
                path = output_path(out_dir, unit, checkpoint)
                if terminal(states):
                    outcome = report_status(states, checkpoint)
                    if legacy.atomic_write_if_absent(
                        path,
                        render_checkpoint(
                            unit=unit,
                            checkpoint=checkpoint,
                            states=states,
                        ),
                    ):
                        written += 1
                    rendered += 1
                    print(
                        f"[aibrief-jp-checkpoints-v2] wrote {checkpoint.key} "
                        f"hash={str(unit['content_hash'])[:12]} "
                        f"status={outcome} path={path}"
                    )
                elif any(state.status == "DUE" for state in states.values()):
                    waiting += 1
                    statuses = " ".join(
                        f"{platform}={state.status}"
                        for platform, state in states.items()
                    )
                    print(
                        f"[aibrief-jp-checkpoints-v2] waiting {checkpoint.key} "
                        f"hash={str(unit['content_hash'])[:12]} {statuses}"
                    )
                else:
                    pending += 1
            print(
                "[aibrief-jp-checkpoints-v2] summary "
                f"recent_units={len(units)} work_items={len(work)} "
                f"rendered={rendered} waiting={waiting} pending={pending} "
                f"written={written} sync_failures={sync_failures}"
            )
            return 1 if waiting or sync_failures else 0
        finally:
            instagram_connection.close()
            facebook_connection.close()
