#!/usr/bin/env python3
"""Build a deterministic, read-only AI Brief JP Trial Reel recommendation.

The selector never renders, schedules, changes a manifest, or publishes.  It
only reads the Instagram and optional Facebook ledgers and writes a JSON and
Markdown review packet containing dry-run argv for the existing scheduler
commands.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import quote
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import aibrief_jp_reach_analysis as reach_analysis


CHANNEL = "aibrief_jp"
JST = ZoneInfo("Asia/Tokyo")
POLICY_VERSION = "aibrief-trial-v1"
SCHEMA_VERSION = 1
WEEKLY_CAP = 2
MINIMUM_SPACING_HOURS = 48
PARENT_TARGET_LEAD_HOURS = 72
SCHEDULED_TARGET_LEAD_HOURS = 36
PARENT_MAX_AGE_DAYS = 45
SLOT_TOLERANCE_MINUTES = 15
SHORTLIST_SIZE = 3
PARENT_SHORTLIST_SIZE = 5
OBSERVATION_WINDOW_HOURS = 72
MAX_CONCURRENT_OBSERVATION_WINDOWS = 2
NONTERMINAL_STATES = {"scheduled", "publishing", "active"}
FACEBOOK_MUTABLE_STATUSES = {
    "new",
    "skipped",
    "scheduled",
    "publish_previewed",
}

# Alternating lanes with every lane represented once in every canonical slot
# over one eight-experiment cycle.
CYCLE: tuple[tuple[str, str], ...] = (
    ("successful_post_variant", "09"),
    ("scheduled_conversion", "18"),
    ("successful_post_variant", "13"),
    ("scheduled_conversion", "21"),
    ("successful_post_variant", "18"),
    ("scheduled_conversion", "09"),
    ("successful_post_variant", "21"),
    ("scheduled_conversion", "13"),
)
FORMAL_ID = re.compile(r"^TRIAL-V1-(\d{4})-[AB](09|13|18|21)-[0-9a-f]{8}$")
WINNER_PRIORITY = {
    "AUDIENCE_FIT_WINNER": 0,
    "COMPLETE_WINNER": 1,
    "DISTRIBUTION_WINNER": 2,
}


def parse_aware_datetime(value: Any, *, field: str) -> datetime:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO 8601 datetime: {text}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone offset: {text}")
    return parsed


def default_as_of() -> datetime:
    return datetime.now(JST).replace(microsecond=0)


def readonly_connection(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"ledger database not found: {resolved}")
    uri = f"file:{quote(str(resolved), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
        is not None
    )


def row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def load_ledger_state(
    db_path: Path,
    *,
    channel_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    with readonly_connection(db_path) as connection:
        reels = [
            row_dict(row)
            for row in connection.execute(
                "SELECT * FROM reels WHERE channel_id=? "
                "ORDER BY COALESCE(scheduled_at, published_at), content_hash",
                (channel_id,),
            ).fetchall()
        ]
        if table_exists(connection, "trial_experiments"):
            experiments = [
                row_dict(row)
                for row in connection.execute(
                    """
                    SELECT t.*, r.scheduled_at AS reel_scheduled_at,
                           r.published_at AS reel_published_at
                    FROM trial_experiments AS t
                    LEFT JOIN reels AS r
                      ON r.content_hash=t.content_hash AND r.channel_id=t.channel_id
                    WHERE t.channel_id=?
                    ORDER BY t.created_at, t.experiment_id
                    """,
                    (channel_id,),
                ).fetchall()
            ]
        else:
            experiments = []
    return reels, experiments


def load_facebook_statuses(
    facebook_db: Path | None,
    *,
    channel_id: str,
) -> dict[str, str]:
    if facebook_db is None or not facebook_db.expanduser().resolve().is_file():
        return {}
    with readonly_connection(facebook_db) as connection:
        if not table_exists(connection, "reels"):
            return {}
        return {
            str(row["content_hash"]): str(row["status"] or "")
            for row in connection.execute(
                "SELECT content_hash, status FROM reels WHERE channel_id=?",
                (channel_id,),
            ).fetchall()
        }


def experiment_time(experiment: Mapping[str, Any]) -> datetime | None:
    for key in (
        "published_at",
        "scheduled_at",
        "reel_published_at",
        "reel_scheduled_at",
    ):
        value = str(experiment.get(key) or "").strip()
        if not value:
            continue
        try:
            return parse_aware_datetime(value, field=key)
        except ValueError:
            continue
    return None


def nonterminal_observation_window_starts(
    experiments: Sequence[Mapping[str, Any]],
) -> list[datetime]:
    """Return launch times for nonterminal Trials with measurable windows."""
    starts: list[datetime] = []
    for experiment in experiments:
        if (
            str(experiment.get("state") or "").strip().lower()
            not in NONTERMINAL_STATES
        ):
            continue
        start = experiment_time(experiment)
        if start is not None:
            starts.append(start)
    return starts


def concurrent_observation_windows_at(
    candidate_launch: datetime,
    observation_window_starts: Sequence[datetime],
) -> int:
    """Count existing 72-hour observation windows open at a future launch."""
    window = timedelta(hours=OBSERVATION_WINDOW_HOURS)
    return sum(
        start <= candidate_launch < start + window
        for start in observation_window_starts
    )


def week_key(value: datetime) -> str:
    local = value.astimezone(JST)
    iso = local.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def cycle_for_ordinal(ordinal: int) -> dict[str, Any]:
    if ordinal < 0:
        raise ValueError("cycle ordinal must be non-negative")
    position = ordinal % len(CYCLE)
    lane, slot = CYCLE[position]
    return {
        "ordinal": ordinal,
        "position": position,
        "lane": lane,
        "target_slot": slot,
    }


def next_cycle_ordinal(experiments: Sequence[Mapping[str, Any]]) -> int:
    formal_ordinals: list[int] = []
    for experiment in experiments:
        match = FORMAL_ID.fullmatch(str(experiment.get("experiment_id") or ""))
        if match:
            formal_ordinals.append(int(match.group(1)))
    if formal_ordinals:
        return max(formal_ordinals) + 1
    # PILOT-000 already exercised the published-winner lane.  Start the formal
    # cycle at B18 so the two lanes continue alternating.
    return 1 if experiments else 0


def canonical_slot(value: datetime) -> str | None:
    local = value.astimezone(JST)
    minute = local.hour * 60 + local.minute
    candidates = {
        slot: abs(minute - int(slot) * 60)
        for slot in ("09", "13", "18", "21")
    }
    slot, distance = min(candidates.items(), key=lambda item: (item[1], item[0]))
    return slot if distance <= SLOT_TOLERANCE_MINUTES else None


def asset_family_id(row: Mapping[str, Any]) -> str:
    source = str(row.get("source_video") or "").strip()
    clip_name = Path(str(row.get("clip_dir") or "")).name
    if source and clip_name:
        return f"{source}/{clip_name}"
    return source or clip_name or str(row.get("content_hash") or "")


def file_readiness_reasons(row: Mapping[str, Any], *, published_parent: bool) -> list[str]:
    reasons: list[str] = []
    media_path = Path(str(row.get("media_path") or ""))
    clip_dir = Path(str(row.get("clip_dir") or ""))
    if not media_path.is_file():
        reasons.append("MISSING_MEDIA")
    if not clip_dir.is_dir():
        reasons.append("MISSING_CLIP_DIR")
    if published_parent:
        if not str(row.get("media_id") or "").strip():
            reasons.append("MISSING_MEDIA_ID")
        if clip_dir.is_dir():
            if not (clip_dir / "subtitles.ja.ass").is_file():
                reasons.append("MISSING_JA_SUBTITLES")
            if not (clip_dir / "notes.json").is_file():
                reasons.append("MISSING_NOTES")
            if not (clip_dir / "one_liners.json").is_file():
                reasons.append("MISSING_ONE_LINERS")
            source_root = clip_dir.parent.parent
            if not (source_root / "work" / "source.mp4").is_file():
                reasons.append("MISSING_SOURCE_MEDIA")
    else:
        manifest_path = Path(str(row.get("manifest_path") or ""))
        if not manifest_path.is_file():
            reasons.append("MISSING_MANIFEST")
    return reasons


def stable_selection_key(ordinal: int, content_hash: str) -> str:
    material = f"{POLICY_VERSION}|{ordinal}|{content_hash}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def formal_experiment_id(
    *,
    ordinal: int,
    lane: str,
    slot: str,
    content_hash: str,
) -> str:
    lane_code = "A" if lane == "successful_post_variant" else "B"
    suffix = content_hash[:8].lower()
    if re.fullmatch(r"[0-9a-f]{8}", suffix) is None:
        suffix = hashlib.sha256(content_hash.encode("utf-8")).hexdigest()[:8]
    return f"TRIAL-V1-{ordinal:04d}-{lane_code}{slot}-{suffix}"


def trial_capacity(
    experiments: Sequence[Mapping[str, Any]],
) -> tuple[Counter[str], list[datetime], set[str], set[str], set[str]]:
    weekly_counts: Counter[str] = Counter()
    trial_times: list[datetime] = []
    tested_parents: set[str] = set()
    tested_reels: set[str] = set()
    tested_families: set[str] = set()
    for experiment in experiments:
        when = experiment_time(experiment)
        if when is not None:
            weekly_counts[week_key(when)] += 1
            trial_times.append(when)
        parent = str(experiment.get("parent_content_hash") or "").strip()
        if parent:
            tested_parents.add(parent)
        content_hash = str(experiment.get("content_hash") or "").strip()
        if content_hash:
            tested_reels.add(content_hash)
        family = str(experiment.get("asset_family_id") or "").strip()
        if family:
            tested_families.add(family)
    return weekly_counts, trial_times, tested_parents, tested_reels, tested_families


def scheduled_exclusions(
    row: Mapping[str, Any],
    *,
    as_of: datetime,
    lead_hours: int,
    target_slot: str,
    weekly_counts: Mapping[str, int],
    trial_times: Sequence[datetime],
    tested_reels: set[str],
    tested_families: set[str],
    facebook_statuses: Mapping[str, str],
    observation_window_starts: Sequence[datetime] = (),
) -> tuple[list[str], datetime | None]:
    reasons: list[str] = []
    if str(row.get("status") or "") != "scheduled":
        reasons.append("NOT_SCHEDULED")
    if bool(row.get("trial_reel") or 0):
        reasons.append("ALREADY_TRIAL")
    content_hash = str(row.get("content_hash") or "")
    if content_hash in tested_reels:
        reasons.append("EXPERIMENT_ALREADY_LINKED")
    family = asset_family_id(row)
    if family in tested_families:
        reasons.append("ASSET_FAMILY_ALREADY_TESTED")
    scheduled_at: datetime | None = None
    try:
        scheduled_at = parse_aware_datetime(row.get("scheduled_at"), field="scheduled_at")
    except ValueError:
        reasons.append("INVALID_SCHEDULED_AT")
    if scheduled_at is not None:
        if scheduled_at < as_of + timedelta(hours=lead_hours):
            reasons.append("INSUFFICIENT_LEAD")
        if canonical_slot(scheduled_at) != target_slot:
            reasons.append("WRONG_CYCLE_SLOT")
        if (
            concurrent_observation_windows_at(
                scheduled_at,
                observation_window_starts,
            )
            >= MAX_CONCURRENT_OBSERVATION_WINDOWS
        ):
            reasons.append("OBSERVATION_WINDOW_CAP_REACHED")
        if weekly_counts.get(week_key(scheduled_at), 0) >= WEEKLY_CAP:
            reasons.append("WEEKLY_CAP_REACHED")
        local_date = scheduled_at.astimezone(JST).date()
        if any(value.astimezone(JST).date() == local_date for value in trial_times):
            reasons.append("TRIAL_ALREADY_ON_DATE")
        if any(
            abs((scheduled_at - value).total_seconds()) < MINIMUM_SPACING_HOURS * 3600
            for value in trial_times
        ):
            reasons.append("TRIAL_SPACING")
    reasons.extend(file_readiness_reasons(row, published_parent=False))
    facebook_status = facebook_statuses.get(content_hash)
    if facebook_status and facebook_status not in FACEBOOK_MUTABLE_STATUSES:
        reasons.append("FACEBOOK_ROW_IMMUTABLE")
    return sorted(set(reasons)), scheduled_at


def scheduled_shortlist(
    reels: Sequence[Mapping[str, Any]],
    *,
    as_of: datetime,
    ordinal: int,
    lead_hours: int,
    target_slot: str,
    weekly_counts: Mapping[str, int],
    trial_times: Sequence[datetime],
    tested_reels: set[str],
    tested_families: set[str],
    facebook_statuses: Mapping[str, str],
    observation_window_starts: Sequence[datetime] = (),
) -> tuple[list[dict[str, Any]], Counter[str]]:
    eligible: list[tuple[datetime, Mapping[str, Any]]] = []
    excluded: Counter[str] = Counter()
    for row in reels:
        if str(row.get("status") or "") != "scheduled":
            continue
        reasons, scheduled_at = scheduled_exclusions(
            row,
            as_of=as_of,
            lead_hours=lead_hours,
            target_slot=target_slot,
            weekly_counts=weekly_counts,
            trial_times=trial_times,
            tested_reels=tested_reels,
            tested_families=tested_families,
            facebook_statuses=facebook_statuses,
            observation_window_starts=observation_window_starts,
        )
        if reasons:
            excluded.update(reasons)
            continue
        assert scheduled_at is not None
        eligible.append((scheduled_at, row))
    first_three = sorted(
        eligible,
        key=lambda item: (item[0], str(item[1].get("content_hash") or "")),
    )[:SHORTLIST_SIZE]
    shortlist: list[dict[str, Any]] = []
    for scheduled_at, row in first_three:
        content_hash = str(row.get("content_hash") or "")
        key = stable_selection_key(ordinal, content_hash)
        shortlist.append(
            {
                "content_hash": content_hash,
                "title": str(row.get("title") or ""),
                "scheduled_at": scheduled_at.isoformat(),
                "canonical_slot": target_slot,
                "selection_key": key,
                "asset_family_id": asset_family_id(row),
                "media_path": str(row.get("media_path") or ""),
                "manifest_path": str(row.get("manifest_path") or ""),
                "facebook_status": facebook_statuses.get(content_hash, "not_present"),
                "notes_score_used_for_selection": False,
                "updated_at": str(row.get("updated_at") or ""),
            }
        )
    shortlist.sort(key=lambda item: (item["selection_key"], item["content_hash"]))
    for rank, item in enumerate(shortlist, start=1):
        item["rank"] = rank
        item["selected"] = rank == 1
    return shortlist, excluded


def published_parent_candidates(
    *,
    report_path: Path,
    db_path: Path,
    ledger_rows: Sequence[Mapping[str, Any]],
    channel_id: str,
    as_of: datetime,
    tested_parents: set[str],
    tested_families: set[str],
) -> tuple[list[dict[str, Any]], Counter[str]]:
    report = reach_analysis.load_report(report_path)
    loaded = reach_analysis.load_reels(report, db_path)
    regular = [item for item in loaded if not reach_analysis.truthy_flag(item.get("trial_reel"))]
    coverage = (
        sum(bool(item.get("snapshots")) for item in regular) / len(regular)
        if regular
        else 0.0
    )
    classified = [
        reach_analysis.classify_reel(item, coverage=coverage)
        for item in regular
    ]
    metadata = {
        str(row.get("content_hash") or ""): row
        for row in ledger_rows
        if str(row.get("status") or "") == "published"
    }
    candidates: list[dict[str, Any]] = []
    excluded: Counter[str] = Counter()
    for result in classified:
        content_hash = str(result.get("content_hash") or "")
        row = metadata.get(content_hash)
        reasons: list[str] = []
        if row is None:
            reasons.append("MISSING_LEDGER_METADATA")
        if content_hash in tested_parents:
            reasons.append("PARENT_ALREADY_TESTED")
        classification = str(result.get("classification") or "")
        if classification not in WINNER_PRIORITY:
            reasons.append("NOT_ELIGIBLE_WINNER")
        snapshot_age = result.get("snapshot_age_hours")
        if not isinstance(snapshot_age, (int, float)) or not 72 <= float(snapshot_age) <= 96:
            reasons.append("NO_STRICT_72_96_SNAPSHOT")
        if result.get("data_errors"):
            reasons.append("ANALYTICS_DATA_HOLD")
        published_at: datetime | None = None
        try:
            published_at = parse_aware_datetime(
                result.get("published_at"),
                field="published_at",
            )
        except ValueError:
            reasons.append("INVALID_PUBLISHED_AT")
        if published_at is not None:
            age = as_of - published_at
            if age < timedelta(hours=72):
                reasons.append("PARENT_NOT_MATURE")
            if age > timedelta(days=PARENT_MAX_AGE_DAYS):
                reasons.append("PARENT_TOO_OLD")
        if row is not None:
            family = asset_family_id(row)
            if family in tested_families:
                reasons.append("ASSET_FAMILY_ALREADY_TESTED")
            reasons.extend(file_readiness_reasons(row, published_parent=True))
        if reasons:
            excluded.update(sorted(set(reasons)))
            continue
        assert row is not None
        metrics = dict(result.get("metrics") or {})
        reach = float(metrics.get("reach") or 0)
        saves = int(metrics.get("saved") or 0)
        shares = int(metrics.get("shares") or 0)
        interactions = int(metrics.get("total_interactions") or 0)
        save_share_rate = 1000 * (saves + shares) / reach if reach > 0 else 0.0
        assert published_at is not None
        candidates.append(
            {
                "content_hash": content_hash,
                "media_id": str(result.get("media_id") or ""),
                "title": str(result.get("title") or ""),
                "published_at": published_at.isoformat(),
                "snapshot_captured_at": str(result.get("snapshot_captured_at") or ""),
                "snapshot_age_hours": float(snapshot_age),
                "classification": classification,
                "action": str(result.get("action") or ""),
                "metrics": {
                    "instagram_views": metrics.get("base_views"),
                    "instagram_reach": metrics.get("reach"),
                    "total_interactions": interactions,
                    "saved": saves,
                    "shares": shares,
                    "save_share_per_1000_reached": save_share_rate,
                },
                "asset_family_id": asset_family_id(row),
                "clip_dir": str(row.get("clip_dir") or ""),
                "media_path": str(row.get("media_path") or ""),
                "_sort": (
                    WINNER_PRIORITY[classification],
                    -save_share_rate,
                    -interactions,
                    -published_at.timestamp(),
                    content_hash,
                ),
            }
        )
    candidates.sort(key=lambda item: item["_sort"])
    for rank, item in enumerate(candidates[:PARENT_SHORTLIST_SIZE], start=1):
        item.pop("_sort", None)
        item["rank"] = rank
        item["selected"] = rank == 1
    return candidates[:PARENT_SHORTLIST_SIZE], excluded


def input_fingerprint(
    *,
    as_of: datetime,
    cycle: Mapping[str, Any],
    ledger_rows: Sequence[Mapping[str, Any]],
    experiments: Sequence[Mapping[str, Any]],
) -> str:
    payload = {
        "policy_version": POLICY_VERSION,
        "as_of": as_of.isoformat(),
        "cycle": dict(cycle),
        "reels": [
            {
                key: row.get(key)
                for key in (
                    "content_hash",
                    "status",
                    "scheduled_at",
                    "published_at",
                    "trial_reel",
                    "updated_at",
                )
            }
            for row in ledger_rows
        ],
        "experiments": [
            {
                key: experiment.get(key)
                for key in (
                    "experiment_id",
                    "content_hash",
                    "case_type",
                    "state",
                    "scheduled_at",
                    "published_at",
                )
            }
            for experiment in experiments
        ],
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_selection(
    *,
    db_path: Path,
    report_path: Path,
    facebook_db: Path | None,
    channel_id: str,
    as_of: datetime,
) -> dict[str, Any]:
    ledger_rows, experiments = load_ledger_state(db_path, channel_id=channel_id)
    facebook_statuses = load_facebook_statuses(facebook_db, channel_id=channel_id)
    ordinal = next_cycle_ordinal(experiments)
    cycle = cycle_for_ordinal(ordinal)
    (
        weekly_counts,
        trial_times,
        tested_parents,
        tested_reels,
        tested_families,
    ) = trial_capacity(experiments)
    observation_window_starts = nonterminal_observation_window_starts(experiments)
    lead_hours = (
        PARENT_TARGET_LEAD_HOURS
        if cycle["lane"] == "successful_post_variant"
        else SCHEDULED_TARGET_LEAD_HOURS
    )
    scheduled, scheduled_excluded = scheduled_shortlist(
        ledger_rows,
        as_of=as_of,
        ordinal=ordinal,
        lead_hours=lead_hours,
        target_slot=str(cycle["target_slot"]),
        weekly_counts=weekly_counts,
        trial_times=trial_times,
        tested_reels=tested_reels,
        tested_families=tested_families,
        facebook_statuses=facebook_statuses,
        observation_window_starts=observation_window_starts,
    )
    parents, parent_excluded = published_parent_candidates(
        report_path=report_path,
        db_path=db_path,
        ledger_rows=ledger_rows,
        channel_id=channel_id,
        as_of=as_of,
        tested_parents=tested_parents,
        tested_families=tested_families,
    )

    selected_scheduled = scheduled[0] if scheduled else None
    selected_parent = parents[0] if parents else None
    ready = selected_scheduled is not None and (
        cycle["lane"] == "scheduled_conversion" or selected_parent is not None
    )
    recommendation: dict[str, Any] = {
        "status": "READY" if ready else "HOLD",
        "lane": cycle["lane"],
        "target_slot": cycle["target_slot"],
        "manual_approval_required": True,
        "auto_apply": False,
        "dry_run_argv": [],
    }
    if ready and selected_scheduled is not None:
        experiment_hash = (
            str(selected_parent["content_hash"])
            if cycle["lane"] == "successful_post_variant"
            and selected_parent is not None
            else str(selected_scheduled["content_hash"])
        )
        experiment_id = formal_experiment_id(
            ordinal=ordinal,
            lane=str(cycle["lane"]),
            slot=str(cycle["target_slot"]),
            content_hash=experiment_hash,
        )
        recommendation.update(
            {
                "experiment_id": experiment_id,
                "expected_scheduled_at": selected_scheduled["scheduled_at"],
                "scheduled_target": selected_scheduled,
                "facebook_effect": (
                    "Future mutable mirror will be removed from the Facebook queue."
                ),
            }
        )
        if cycle["lane"] == "scheduled_conversion":
            recommendation.update(
                {
                    "content_hash": selected_scheduled["content_hash"],
                    "changed_variables": ["distribution_mode"],
                    "dry_run_argv_ready": True,
                    "dry_run_argv": [
                        "uv",
                        "run",
                        "python",
                        "reel_scheduler.py",
                        "trial-convert-scheduled",
                        "--channel",
                        channel_id,
                        "--content-hash",
                        str(selected_scheduled["content_hash"]),
                        "--experiment-id",
                        experiment_id,
                        "--expected-scheduled-at",
                        str(selected_scheduled["scheduled_at"]),
                    ],
                }
            )
        else:
            assert selected_parent is not None
            recommendation.update(
                {
                    "parent": selected_parent,
                    "displaced_content_hash": selected_scheduled["content_hash"],
                    "changed_variables": ["overlay_hook"],
                    "required_manual_checks": [
                        "Author and approve one new Japanese opening hook.",
                        "Rerender only the opening treatment while preserving the source clip.",
                        "Review the final MP4 before running the scheduler dry run.",
                    ],
                    "dry_run_argv_ready": False,
                    "dry_run_argv": [
                        "uv",
                        "run",
                        "python",
                        "reel_scheduler.py",
                        "trial-from-published",
                        "--channel",
                        channel_id,
                        "--parent-content-hash",
                        str(selected_parent["content_hash"]),
                        "--replace-content-hash",
                        str(selected_scheduled["content_hash"]),
                        "--media-path",
                        "<rerendered-variant.mp4>",
                        "--experiment-id",
                        experiment_id,
                        "--hook",
                        "<new-rendered-hook>",
                        "--asset-family-id",
                        str(selected_parent["asset_family_id"]),
                        "--changed-variable",
                        "overlay_hook",
                        "--caption-mode",
                        "preserve-parent",
                        "--expected-scheduled-at",
                        str(selected_scheduled["scheduled_at"]),
                    ],
                }
            )
    else:
        hold_reasons: list[str] = []
        if not selected_scheduled:
            hold_reasons.append("NO_ELIGIBLE_SCHEDULED_ROW_IN_CYCLE_SLOT")
        if cycle["lane"] == "successful_post_variant" and not selected_parent:
            hold_reasons.append("NO_ELIGIBLE_STRICT_72_96_PARENT")
        recommendation["hold_reasons"] = hold_reasons

    selected_week = (
        week_key(parse_aware_datetime(recommendation["expected_scheduled_at"], field="scheduled_at"))
        if ready
        else week_key(as_of)
    )
    selected_launch = (
        parse_aware_datetime(
            recommendation["expected_scheduled_at"],
            field="scheduled_at",
        )
        if ready
        else None
    )
    existing_windows_at_launch = (
        concurrent_observation_windows_at(
            selected_launch,
            observation_window_starts,
        )
        if selected_launch is not None
        else None
    )
    exclusions = scheduled_excluded + parent_excluded
    return {
        "schema_version": SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "read_only": True,
        "channel_id": channel_id,
        "as_of": as_of.isoformat(),
        "cycle": {
            **cycle,
            "sequence": [
                {"position": index, "lane": lane, "slot": slot}
                for index, (lane, slot) in enumerate(CYCLE)
            ],
        },
        "capacity": {
            "week": selected_week,
            "weekly_cap": WEEKLY_CAP,
            "used": int(weekly_counts.get(selected_week, 0)),
            "remaining": max(0, WEEKLY_CAP - int(weekly_counts.get(selected_week, 0))),
            "minimum_spacing_hours": MINIMUM_SPACING_HOURS,
            "lead_hours": lead_hours,
            "observation_window_hours": OBSERVATION_WINDOW_HOURS,
            "maximum_concurrent_observation_windows": (
                MAX_CONCURRENT_OBSERVATION_WINDOWS
            ),
            "nonterminal_experiments": len(observation_window_starts),
            "existing_observation_windows_at_launch": existing_windows_at_launch,
            "projected_observation_windows_at_launch": (
                existing_windows_at_launch + 1
                if existing_windows_at_launch is not None
                else None
            ),
        },
        "recommendation": recommendation,
        "shortlists": {
            "published_parents": parents,
            "scheduled_candidates": scheduled,
        },
        "exclusions": {
            "counts_by_reason": dict(sorted(exclusions.items())),
        },
        "provenance": {
            "source_db": str(db_path.expanduser().resolve()),
            "source_report": str(report_path.expanduser().resolve()),
            "facebook_db": (
                str(facebook_db.expanduser().resolve()) if facebook_db else ""
            ),
            "input_fingerprint": input_fingerprint(
                as_of=as_of,
                cycle=cycle,
                ledger_rows=ledger_rows,
                experiments=experiments,
            ),
        },
    }


def markdown_cell(value: Any) -> str:
    return str(value if value not in (None, "") else "—").replace("|", "\\|").replace("\n", " ")


def render_markdown(selection: Mapping[str, Any]) -> str:
    cycle = selection["cycle"]
    capacity = selection["capacity"]
    recommendation = selection["recommendation"]
    lines = [
        "# AI Brief JP Trial candidate",
        "",
        f"- As of: `{selection['as_of']}`",
        f"- Policy: `{selection['policy_version']}`",
        f"- Cycle: `{cycle['ordinal']}` / `{cycle['lane']}` / `{cycle['target_slot']}:00`",
        (
            f"- Capacity: {capacity['used']}/{capacity['weekly_cap']} used in "
            f"`{capacity['week']}`; minimum spacing {capacity['minimum_spacing_hours']}h"
        ),
        "- Selector mode: read-only; manual approval is required.",
        "",
        "## Recommendation",
        "",
        f"- Status: `{recommendation['status']}`",
    ]
    if recommendation["status"] == "READY":
        lines.extend(
            [
                f"- Experiment: `{recommendation['experiment_id']}`",
                f"- Expected slot: `{recommendation['expected_scheduled_at']}`",
                f"- Facebook effect: {recommendation['facebook_effect']}",
            ]
        )
        parent = recommendation.get("parent")
        if isinstance(parent, Mapping):
            lines.extend(
                [
                    f"- Published parent: {markdown_cell(parent.get('title'))}",
                    f"- Parent hash: `{parent.get('content_hash')}`",
                    f"- Parent class: `{parent.get('classification')}` at "
                    f"{float(parent.get('snapshot_age_hours') or 0):.2f}h",
                    "- Render review is required before the dry-run template is executable.",
                ]
            )
        else:
            target = recommendation["scheduled_target"]
            lines.extend(
                [
                    f"- Scheduled Reel: {markdown_cell(target.get('title'))}",
                    f"- Content hash: `{target.get('content_hash')}`",
                ]
            )
        lines.extend(
            [
                "",
                "Dry-run argv:",
                "",
                "```sh",
                shlex.join([str(value) for value in recommendation["dry_run_argv"]]),
                "```",
            ]
        )
    else:
        lines.append(
            "- Hold reasons: "
            + ", ".join(f"`{value}`" for value in recommendation.get("hold_reasons", []))
        )

    parents = selection["shortlists"]["published_parents"]
    lines.extend(
        [
            "",
            "## Published-parent shortlist",
            "",
            "| Rank | Classification | Snapshot age | Reach | Saves + shares / 1k | Title |",
            "|---:|---|---:|---:|---:|---|",
        ]
    )
    for item in parents:
        metrics = item["metrics"]
        lines.append(
            f"| {item['rank']} | {item['classification']} | "
            f"{item['snapshot_age_hours']:.2f}h | "
            f"{markdown_cell(metrics.get('instagram_reach'))} | "
            f"{float(metrics.get('save_share_per_1000_reached') or 0):.1f} | "
            f"{markdown_cell(item.get('title'))} |"
        )
    if not parents:
        lines.append("| — | — | — | — | — | No eligible parent for this cycle |")

    scheduled = selection["shortlists"]["scheduled_candidates"]
    lines.extend(
        [
            "",
            "## Scheduled shortlist",
            "",
            "| Rank | Selected | Slot | Selection key | Title |",
            "|---:|---|---|---|---|",
        ]
    )
    for item in scheduled:
        lines.append(
            f"| {item['rank']} | {'yes' if item['selected'] else 'no'} | "
            f"`{item['scheduled_at']}` | `{item['selection_key'][:12]}` | "
            f"{markdown_cell(item.get('title'))} |"
        )
    if not scheduled:
        lines.append("| — | — | — | — | No eligible scheduled Reel |")

    lines.extend(["", "## Exclusions", ""])
    exclusions = selection["exclusions"]["counts_by_reason"]
    if exclusions:
        lines.extend(
            f"- `{reason}`: {count}"
            for reason, count in exclusions.items()
        )
    else:
        lines.append("- None")
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", default=CHANNEL)
    parser.add_argument("--db", type=Path, default=ROOT / "state" / "reels.db")
    parser.add_argument(
        "--facebook-db",
        type=Path,
        default=ROOT / "state" / "facebook.db",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=ROOT / "out" / "reel_report.insights.json",
    )
    parser.add_argument("--as-of", help="Aware ISO 8601 selection time")
    parser.add_argument(
        "--json-out",
        type=Path,
        default=ROOT / "out" / "trial_candidates" / "latest.json",
    )
    parser.add_argument(
        "--markdown-out",
        type=Path,
        default=ROOT / "out" / "trial_candidates" / "latest.md",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    as_of = (
        parse_aware_datetime(args.as_of, field="as_of")
        if args.as_of
        else default_as_of()
    )
    selection = build_selection(
        db_path=args.db,
        report_path=args.report,
        facebook_db=args.facebook_db,
        channel_id=args.channel,
        as_of=as_of,
    )
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(
        json.dumps(selection, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    args.markdown_out.write_text(render_markdown(selection), encoding="utf-8")
    print(
        "[aibrief-trial-selector] "
        f"status={selection['recommendation']['status']} "
        f"lane={selection['cycle']['lane']} slot={selection['cycle']['target_slot']} "
        f"json={args.json_out.resolve()} markdown={args.markdown_out.resolve()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
