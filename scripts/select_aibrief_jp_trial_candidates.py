#!/usr/bin/env python3
"""Build a deterministic, read-only AI Brief JP daily Trial Reel batch.

The selector never renders, schedules, changes a manifest, or publishes.  It
only reads the Instagram and optional Facebook ledgers and writes a JSON and
Markdown review packet. Every JST date has two independent lanes: one existing
scheduled Reel converted in place and one published-parent hook rerender added
at 19:00 without displacing a regular Reel. A packet may safely advance only
the ready lane when the other is blocked.
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
from datetime import date, datetime, time, timedelta
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
POLICY_VERSION = "aibrief-trial-daily-v2"
SCHEMA_VERSION = 2
DAILY_TRIAL_LANES = 2
PUBLISHED_VARIANT_HOUR = 19
PARENT_TARGET_LEAD_HOURS = 12
SCHEDULED_TARGET_LEAD_HOURS = 6
PARENT_MAX_AGE_DAYS = 45
SLOT_TOLERANCE_MINUTES = 15
REGULAR_SLOTS = ("09", "13", "18", "21")
SHORTLIST_SIZE = len(REGULAR_SLOTS)
PARENT_SHORTLIST_SIZE = 12
OBSERVATION_WINDOW_HOURS = 72
# A 72-hour window spans parts of four JST dates.  The 19:00 lane is fixed,
# while the in-place lane can move between 09/13/18/21.  At the boundary, the
# window can therefore contain two launches from one partial date, four from
# the two full dates, and one from the other partial date: 2 + 4 + 1 = 7.
MAX_CONCURRENT_OBSERVATION_WINDOWS = 7
PRIORITY_PARENT_RESERVE_FAMILIES = frozenset(
    {
        "gYfCm3zYajg",
        "0A3sGymV6kY",
        "7C_IHWkHKmU",
    }
)
NONTERMINAL_STATES = {"scheduled", "publishing", "active"}
FACEBOOK_MUTABLE_STATUSES = {
    "new",
    "skipped",
    "scheduled",
    "publish_previewed",
}

LANE_SUCCESSFUL_POST_VARIANT = "successful_post_variant"
LANE_SCHEDULED_CONVERSION = "scheduled_conversion"
DAILY_LANES = (
    LANE_SCHEDULED_CONVERSION,
    LANE_SUCCESSFUL_POST_VARIANT,
)
FORMAL_ID = re.compile(r"^TRIAL-V1-(\d{4})-[AB](09|13|18|19|21)-[0-9a-f]{8}$")
WINNER_PRIORITY = {
    "AUDIENCE_FIT_WINNER": 0,
    "COMPLETE_WINNER": 1,
    "DISTRIBUTION_WINNER": 2,
}
EVIDENCE_TIER_PRIORITY = {"A": 0, "B": 1, "C": 2, "D": 3}
CORE_SNAPSHOT_METRICS = (
    "views",
    "reach",
    "likes",
    "comments",
    "saved",
    "shares",
    "total_interactions",
)
TIER_B_MIN_SAVE_SHARE = 3
TIER_B_MIN_NATIVE_VIEWS = 250
TIER_B_MIN_NATIVE_REACH = 185
TIER_B_MAX_SKIP_RATE = 50.0
TIER_B_MIN_AVG_WATCH_MS = 9000.0
TIER_D_MIN_SAVE_SHARE = 2
TIER_D_MAX_SKIP_RATE = 45.0
TIER_D_MIN_AVG_WATCH_MS = 9000.0


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


def projected_daily_batch_windows(
    *,
    scheduled_launch: datetime,
    published_launch: datetime,
    observation_window_starts: Sequence[datetime],
) -> dict[str, int]:
    """Project open windows after each launch in one proposed daily batch."""
    return projected_proposal_windows(
        {
            LANE_SCHEDULED_CONVERSION: scheduled_launch,
            LANE_SUCCESSFUL_POST_VARIANT: published_launch,
        },
        observation_window_starts=observation_window_starts,
    )


def projected_proposal_windows(
    proposals: Mapping[str, datetime],
    *,
    observation_window_starts: Sequence[datetime],
) -> dict[str, int]:
    """Project open windows for only the missing lanes being recommended."""
    launches = sorted(
        proposals.items(),
        key=lambda item: (item[1], item[0]),
    )
    proposed_starts: list[datetime] = []
    projected: dict[str, int] = {}
    for lane, launch in launches:
        projected[lane] = (
            concurrent_observation_windows_at(
                launch,
                [*observation_window_starts, *proposed_starts],
            )
            + 1
        )
        proposed_starts.append(launch)
    return projected


def next_cycle_ordinal(experiments: Sequence[Mapping[str, Any]]) -> int:
    """Return the next durable experiment ordinal.

    Two ordinals are consumed by every daily batch.  Legacy PILOT experiments
    do not match the formal id, but still reserve ordinal zero.
    """
    formal_ordinals: list[int] = []
    for experiment in experiments:
        match = FORMAL_ID.fullmatch(str(experiment.get("experiment_id") or ""))
        if match:
            formal_ordinals.append(int(match.group(1)))
    if formal_ordinals:
        return max(formal_ordinals) + 1
    return 1 if experiments else 0


def canonical_slot(value: datetime) -> str | None:
    local = value.astimezone(JST)
    minute = local.hour * 60 + local.minute
    candidates = {
        slot: abs(minute - int(slot) * 60)
        for slot in REGULAR_SLOTS
    }
    slot, distance = min(candidates.items(), key=lambda item: (item[1], item[0]))
    return slot if distance <= SLOT_TOLERANCE_MINUTES else None


def jst_date_key(value: datetime) -> str:
    return value.astimezone(JST).date().isoformat()


def published_variant_launch(target_date: date) -> datetime:
    return datetime.combine(
        target_date,
        time(hour=PUBLISHED_VARIANT_HOUR),
        tzinfo=JST,
    )


def daily_lane_dates(
    experiments: Sequence[Mapping[str, Any]],
) -> dict[str, set[str]]:
    """Return JST dates already occupied by each Trial lane."""
    result = {lane: set() for lane in DAILY_LANES}
    for experiment in experiments:
        lane = str(experiment.get("case_type") or "").strip()
        when = experiment_time(experiment)
        if lane in result and when is not None:
            result[lane].add(jst_date_key(when))
    return result


def daily_lane_experiments(
    experiments: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    result: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for experiment in experiments:
        lane = str(experiment.get("case_type") or "").strip()
        when = experiment_time(experiment)
        if lane not in DAILY_LANES or when is None:
            continue
        date_key = jst_date_key(when)
        per_date = result.setdefault(
            date_key,
            {known_lane: [] for known_lane in DAILY_LANES},
        )
        per_date[lane].append(
            {
                "experiment_id": str(experiment.get("experiment_id") or ""),
                "state": str(experiment.get("state") or ""),
                "scheduled_at": when.isoformat(),
                "content_hash": str(experiment.get("content_hash") or ""),
            }
        )
    return result


def asset_family_id(row: Mapping[str, Any]) -> str:
    source = str(row.get("source_video") or "").strip()
    clip_name = Path(str(row.get("clip_dir") or "")).name
    # Treat every clip from one source video as a single experimental family.
    # This prevents two excerpts from the same interview from masquerading as
    # independent parent/content tests.
    return source or clip_name or str(row.get("content_hash") or "")


def normalized_experiment_family(value: Any) -> str:
    family = str(value or "").strip()
    if not family:
        return ""
    # Legacy experiments stored ``source_video/clip_name``.  New daily
    # experiments store the source-level family directly.
    return family.rsplit("/", 1)[0] if "/" in family else family


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


def stable_selection_key(selection_seed: str | int, content_hash: str) -> str:
    material = (
        f"{POLICY_VERSION}|{selection_seed}|{LANE_SCHEDULED_CONVERSION}|"
        f"{content_hash}"
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def formal_experiment_id(
    *,
    ordinal: int,
    lane: str,
    slot: str,
    content_hash: str,
) -> str:
    lane_code = "A" if lane == LANE_SUCCESSFUL_POST_VARIANT else "B"
    suffix = content_hash[:8].lower()
    if re.fullmatch(r"[0-9a-f]{8}", suffix) is None:
        suffix = hashlib.sha256(content_hash.encode("utf-8")).hexdigest()[:8]
    return f"TRIAL-V1-{ordinal:04d}-{lane_code}{slot}-{suffix}"


def trial_history(
    experiments: Sequence[Mapping[str, Any]],
) -> tuple[set[str], set[str], set[str]]:
    tested_parents: set[str] = set()
    tested_reels: set[str] = set()
    tested_families: set[str] = set()
    for experiment in experiments:
        parent = str(experiment.get("parent_content_hash") or "").strip()
        if parent:
            tested_parents.add(parent)
        content_hash = str(experiment.get("content_hash") or "").strip()
        if content_hash:
            tested_reels.add(content_hash)
        family = str(experiment.get("asset_family_id") or "").strip()
        if family:
            tested_families.add(normalized_experiment_family(family))
    return tested_parents, tested_reels, tested_families


def trial_family_observation_starts(
    experiments: Sequence[Mapping[str, Any]],
) -> dict[str, list[datetime]]:
    """Return every prior Trial launch grouped by normalized source family."""
    starts: dict[str, list[datetime]] = {}
    for experiment in experiments:
        family = normalized_experiment_family(
            experiment.get("asset_family_id")
        )
        launch = experiment_time(experiment)
        if not family or launch is None:
            continue
        starts.setdefault(family, []).append(launch)
    for values in starts.values():
        values.sort()
    return starts


def family_observation_window_open(
    *,
    family: str,
    candidate_launch: datetime,
    family_observation_starts: Mapping[str, Sequence[datetime]],
) -> bool:
    """Return whether a same-family Trial window overlaps the candidate.

    Both observation intervals are half-open. Exact 72-hour boundary reuse is
    therefore allowed in either direction, while an already scheduled future
    Trial cannot be back-filled with a correlated launch whose window would
    overlap it.
    """
    normalized = normalized_experiment_family(family)
    window = timedelta(hours=OBSERVATION_WINDOW_HOURS)
    return any(
        start < candidate_launch + window
        and candidate_launch < start + window
        for start in family_observation_starts.get(normalized, ())
    )


def parent_candidates_at_launch(
    candidates: Sequence[Mapping[str, Any]],
    *,
    launch: datetime,
    family_observation_starts: Mapping[str, Sequence[datetime]],
) -> tuple[list[dict[str, Any]], int]:
    """Filter parent candidates by the cross-lane 72h family cooldown."""
    available: list[dict[str, Any]] = []
    blocked = 0
    for candidate in candidates:
        if family_observation_window_open(
            family=str(candidate.get("asset_family_id") or ""),
            candidate_launch=launch,
            family_observation_starts=family_observation_starts,
        ):
            blocked += 1
            continue
        available.append(dict(candidate))
    return available, blocked


def occupied_launch_times(
    reels: Sequence[Mapping[str, Any]],
) -> set[datetime]:
    """Return live/future queue timestamps that an additive Trial must not take."""
    occupied: set[datetime] = set()
    for row in reels:
        if str(row.get("status") or "") not in {
            "scheduled",
            "publish_previewed",
            "publishing",
        }:
            continue
        try:
            occupied.add(
                parse_aware_datetime(row.get("scheduled_at"), field="scheduled_at")
            )
        except ValueError:
            continue
    return occupied


def scheduled_exclusions(
    row: Mapping[str, Any],
    *,
    as_of: datetime,
    tested_reels: set[str],
    family_observation_starts: Mapping[str, Sequence[datetime]],
    facebook_statuses: Mapping[str, str],
    lane_dates: Mapping[str, set[str]],
    queue_launch_times: set[datetime],
    published_lane_missing: bool = True,
    observation_window_starts: Sequence[datetime] = (),
) -> tuple[list[str], datetime | None]:
    """Validate one in-place lane candidate and any requested 19:00 pairing."""
    reasons: list[str] = []
    if str(row.get("status") or "") != "scheduled":
        reasons.append("NOT_SCHEDULED")
    if bool(row.get("trial_reel") or 0):
        reasons.append("ALREADY_TRIAL")
    content_hash = str(row.get("content_hash") or "")
    if content_hash in tested_reels:
        reasons.append("EXPERIMENT_ALREADY_LINKED")
    family = asset_family_id(row)
    scheduled_at: datetime | None = None
    try:
        scheduled_at = parse_aware_datetime(row.get("scheduled_at"), field="scheduled_at")
    except ValueError:
        reasons.append("INVALID_SCHEDULED_AT")
    if scheduled_at is not None:
        if family_observation_window_open(
            family=family,
            candidate_launch=scheduled_at,
            family_observation_starts=family_observation_starts,
        ):
            reasons.append("ASSET_FAMILY_OBSERVATION_COOLDOWN")
        local_date = scheduled_at.astimezone(JST).date()
        local_date_key = local_date.isoformat()
        additive_launch = published_variant_launch(local_date)
        if scheduled_at < as_of + timedelta(hours=SCHEDULED_TARGET_LEAD_HOURS):
            reasons.append("INSUFFICIENT_SCHEDULED_LEAD")
        if canonical_slot(scheduled_at) is None:
            reasons.append("NOT_REGULAR_SLOT")
        if local_date_key in lane_dates.get(LANE_SCHEDULED_CONVERSION, set()):
            reasons.append("SCHEDULED_CONVERSION_ALREADY_ON_DATE")
        proposals = {LANE_SCHEDULED_CONVERSION: scheduled_at}
        if published_lane_missing:
            if additive_launch < as_of + timedelta(hours=PARENT_TARGET_LEAD_HOURS):
                reasons.append("INSUFFICIENT_RERENDER_LEAD")
            if additive_launch in queue_launch_times:
                reasons.append("PUBLISHED_1900_SLOT_OCCUPIED")
            proposals[LANE_SUCCESSFUL_POST_VARIANT] = additive_launch
        projected = projected_proposal_windows(
            proposals,
            observation_window_starts=observation_window_starts,
        )
        if max(projected.values()) > MAX_CONCURRENT_OBSERVATION_WINDOWS:
            reasons.append("OBSERVATION_WINDOW_CAP_REACHED")
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
    tested_reels: set[str],
    family_observation_starts: Mapping[str, Sequence[datetime]],
    facebook_statuses: Mapping[str, str],
    lane_dates: Mapping[str, set[str]],
    queue_launch_times: set[datetime],
    reserved_parent_families: set[str] | None = None,
    target_date: date | None = None,
    published_lane_missing: bool = True,
    observation_window_starts: Sequence[datetime] = (),
) -> tuple[list[dict[str, Any]], Counter[str]]:
    eligible: list[tuple[datetime, Mapping[str, Any]]] = []
    excluded: Counter[str] = Counter()
    for row in reels:
        if str(row.get("status") or "") != "scheduled":
            continue
        if target_date is not None:
            try:
                row_launch = parse_aware_datetime(
                    row.get("scheduled_at"),
                    field="scheduled_at",
                )
            except ValueError:
                excluded["INVALID_SCHEDULED_AT"] += 1
                continue
            if row_launch.astimezone(JST).date() != target_date:
                continue
        reasons, scheduled_at = scheduled_exclusions(
            row,
            as_of=as_of,
            tested_reels=tested_reels,
            family_observation_starts=family_observation_starts,
            facebook_statuses=facebook_statuses,
            lane_dates=lane_dates,
            queue_launch_times=queue_launch_times,
            published_lane_missing=published_lane_missing,
            observation_window_starts=observation_window_starts,
        )
        if reasons:
            excluded.update(reasons)
            continue
        assert scheduled_at is not None
        eligible.append((scheduled_at, row))
    if not eligible:
        return [], excluded
    selected_date = target_date or min(
        scheduled_at.astimezone(JST).date()
        for scheduled_at, _ in eligible
    )
    daily_candidates = sorted(
        (
            (scheduled_at, row)
            for scheduled_at, row in eligible
            if scheduled_at.astimezone(JST).date() == selected_date
        ),
        key=lambda item: (item[0], str(item[1].get("content_hash") or "")),
    )

    # Preserve at least one conversion option on the next feasible queue date.
    # This is deliberately only a one-step lookahead: it prevents a greedy
    # choice from consuming the sole family available tomorrow without
    # replacing the existing deterministic daily lottery with a global
    # optimizer. Reservation is a preference; if every candidate would close
    # the next date, retain the original pool and keep making progress.
    future_date_values: set[date] = set()
    for row in reels:
        if str(row.get("status") or "") != "scheduled":
            continue
        try:
            future_launch = parse_aware_datetime(
                row.get("scheduled_at"),
                field="scheduled_at",
            )
        except ValueError:
            continue
        future_date = future_launch.astimezone(JST).date()
        if (
            future_date > selected_date
            and future_date.isoformat()
            not in lane_dates.get(LANE_SCHEDULED_CONVERSION, set())
        ):
            future_date_values.add(future_date)
    future_dates = sorted(future_date_values)
    next_conversion_date: date | None = None
    next_conversion_rows: list[tuple[datetime, Mapping[str, Any]]] = []
    for future_date in future_dates:
        for row in reels:
            if str(row.get("status") or "") != "scheduled":
                continue
            try:
                future_launch = parse_aware_datetime(
                    row.get("scheduled_at"),
                    field="scheduled_at",
                )
            except ValueError:
                continue
            if future_launch.astimezone(JST).date() != future_date:
                continue
            future_reasons, validated_launch = scheduled_exclusions(
                row,
                as_of=as_of,
                tested_reels=tested_reels,
                family_observation_starts=family_observation_starts,
                facebook_statuses=facebook_statuses,
                lane_dates=lane_dates,
                queue_launch_times=queue_launch_times,
                published_lane_missing=False,
                observation_window_starts=observation_window_starts,
            )
            if not future_reasons and validated_launch is not None:
                next_conversion_rows.append((validated_launch, row))
        if next_conversion_rows:
            next_conversion_date = future_date
            break

    preserved_options: dict[str, int] = {}
    if next_conversion_date is not None:
        for scheduled_at, candidate in daily_candidates:
            candidate_hash = str(candidate.get("content_hash") or "")
            candidate_family = asset_family_id(candidate)
            hypothetical_family_starts = {
                family: list(starts)
                for family, starts in family_observation_starts.items()
            }
            hypothetical_family_starts.setdefault(
                normalized_experiment_family(candidate_family),
                [],
            ).append(scheduled_at)
            hypothetical_lane_dates = {
                lane: set(dates)
                for lane, dates in lane_dates.items()
            }
            hypothetical_lane_dates.setdefault(
                LANE_SCHEDULED_CONVERSION,
                set(),
            ).add(selected_date.isoformat())
            option_count = 0
            for _, future_row in next_conversion_rows:
                future_reasons, _ = scheduled_exclusions(
                    future_row,
                    as_of=as_of,
                    tested_reels={*tested_reels, candidate_hash},
                    family_observation_starts=hypothetical_family_starts,
                    facebook_statuses=facebook_statuses,
                    lane_dates=hypothetical_lane_dates,
                    queue_launch_times=queue_launch_times,
                    published_lane_missing=False,
                    observation_window_starts=[
                        *observation_window_starts,
                        scheduled_at,
                    ],
                )
                if not future_reasons:
                    option_count += 1
            preserved_options[candidate_hash] = option_count
        horizon_safe = [
            item
            for item in daily_candidates
            if preserved_options.get(
                str(item[1].get("content_hash") or ""),
                0,
            )
            > 0
        ]
        if horizon_safe:
            excluded["NEXT_DATE_CONVERSION_HORIZON_RESERVED"] += (
                len(daily_candidates) - len(horizon_safe)
            )
            daily_candidates = horizon_safe

    reserved = reserved_parent_families or set()
    unreserved = [
        item
        for item in daily_candidates
        if asset_family_id(item[1]) not in reserved
    ]
    if unreserved:
        excluded["FUTURE_PARENT_FAMILY_RESERVED"] += (
            len(daily_candidates) - len(unreserved)
        )
        daily_pool = unreserved[:SHORTLIST_SIZE]
    else:
        # Reservation is a preference, not a hard queue stop. If every safe
        # candidate belongs to a future parent family, retain deterministic
        # progress and make the override explicit in the shortlist.
        daily_pool = daily_candidates[:SHORTLIST_SIZE]
    shortlist: list[dict[str, Any]] = []
    selection_seed = f"{selected_date.isoformat()}|{ordinal}"
    for scheduled_at, row in daily_pool:
        content_hash = str(row.get("content_hash") or "")
        key = stable_selection_key(selection_seed, content_hash)
        shortlist.append(
            {
                "content_hash": content_hash,
                "title": str(row.get("title") or ""),
                "scheduled_at": scheduled_at.isoformat(),
                "target_date": selected_date.isoformat(),
                "canonical_slot": canonical_slot(scheduled_at),
                "selection_key": key,
                "asset_family_id": asset_family_id(row),
                "future_parent_family_reserved": (
                    asset_family_id(row) in reserved
                ),
                "next_conversion_date": (
                    next_conversion_date.isoformat()
                    if next_conversion_date is not None
                    else None
                ),
                "next_conversion_options_preserved": preserved_options.get(
                    content_hash
                ),
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


def build_scheduled_conversion_selection(
    *,
    db_path: Path,
    facebook_db: Path | None,
    channel_id: str,
    as_of: datetime,
    reserved_parent_families: set[str] | None = None,
) -> dict[str, Any]:
    """Select only the next queue-native conversion lane.

    This is the reusable scheduling half of the daily v2 policy. It deliberately
    does not inspect published-parent evidence or reserve a 19:00 slot; that
    additive rerender remains a separate workflow. The selected regular slot is
    still deterministic for a given ledger state, which gives the queue a
    random-looking daily time without making repeated reshuffles unstable.
    """
    ledger_rows, experiments = load_ledger_state(db_path, channel_id=channel_id)
    facebook_statuses = load_facebook_statuses(
        facebook_db,
        channel_id=channel_id,
    )
    ordinal = next_cycle_ordinal(experiments)
    _, tested_reels, _ = trial_history(experiments)
    family_observation_starts = trial_family_observation_starts(experiments)
    lane_dates = daily_lane_dates(experiments)
    lane_experiments = daily_lane_experiments(experiments)
    observation_window_starts = nonterminal_observation_window_starts(experiments)
    queue_launch_times = occupied_launch_times(ledger_rows)
    today = as_of.astimezone(JST).date()
    candidate_dates: set[date] = set()
    trial_rows_by_date: dict[str, list[dict[str, Any]]] = {}
    for row in ledger_rows:
        if bool(row.get("trial_reel") or 0):
            try:
                trial_launch = parse_aware_datetime(
                    row.get("scheduled_at") or row.get("published_at"),
                    field="trial_launch",
                )
            except ValueError:
                trial_launch = None
            if trial_launch is not None:
                trial_rows_by_date.setdefault(
                    trial_launch.astimezone(JST).date().isoformat(),
                    [],
                ).append(row)
        if str(row.get("status") or "") != "scheduled":
            continue
        try:
            launch = parse_aware_datetime(
                row.get("scheduled_at"),
                field="scheduled_at",
            )
        except ValueError:
            continue
        local_date = launch.astimezone(JST).date()
        if local_date >= today:
            candidate_dates.add(local_date)

    excluded: Counter[str] = Counter()
    dates_considered: list[dict[str, Any]] = []
    reserved = (
        set(PRIORITY_PARENT_RESERVE_FAMILIES)
        if reserved_parent_families is None
        else set(reserved_parent_families)
    )
    for possible_date in sorted(candidate_dates):
        date_key = possible_date.isoformat()
        existing_lanes = lane_experiments.get(
            date_key,
            {lane: [] for lane in DAILY_LANES},
        )
        existing_conversions = existing_lanes.get(
            LANE_SCHEDULED_CONVERSION,
            [],
        )
        if len(existing_conversions) > 1:
            excluded["DUPLICATE_SCHEDULED_CONVERSION_ON_DATE"] += 1
            dates_considered.append(
                {
                    "date": date_key,
                    "status": "HOLD",
                    "hold_reasons": [
                        "DUPLICATE_SCHEDULED_CONVERSION_ON_DATE",
                    ],
                }
            )
            continue
        if existing_conversions:
            dates_considered.append(
                {
                    "date": date_key,
                    "status": "ALREADY_FILLED",
                    "hold_reasons": [],
                }
            )
            continue
        trial_rows = trial_rows_by_date.get(date_key, [])
        registered_hashes = {
            str(item.get("content_hash") or "")
            for lane in DAILY_LANES
            for item in existing_lanes.get(lane, [])
        }
        unregistered_trials = [
            row
            for row in trial_rows
            if str(row.get("content_hash") or "") not in registered_hashes
        ]
        if unregistered_trials:
            excluded["UNREGISTERED_TRIAL_ON_DATE"] += len(unregistered_trials)
            dates_considered.append(
                {
                    "date": date_key,
                    "status": "HOLD",
                    "hold_reasons": ["UNREGISTERED_TRIAL_ON_DATE"],
                }
            )
            continue
        if len(trial_rows) >= DAILY_TRIAL_LANES:
            excluded["DAILY_TRIAL_CAP_REACHED"] += 1
            dates_considered.append(
                {
                    "date": date_key,
                    "status": "HOLD",
                    "hold_reasons": ["DAILY_TRIAL_CAP_REACHED"],
                }
            )
            continue
        shortlist, attempt_excluded = scheduled_shortlist(
            ledger_rows,
            as_of=as_of,
            ordinal=ordinal,
            tested_reels=tested_reels,
            family_observation_starts=family_observation_starts,
            facebook_statuses=facebook_statuses,
            lane_dates=lane_dates,
            queue_launch_times=queue_launch_times,
            reserved_parent_families=reserved,
            target_date=possible_date,
            published_lane_missing=False,
            observation_window_starts=observation_window_starts,
        )
        excluded.update(attempt_excluded)
        if not shortlist:
            dates_considered.append(
                {
                    "date": date_key,
                    "status": "HOLD",
                    "hold_reasons": sorted(attempt_excluded),
                }
            )
            continue

        selected = shortlist[0]
        experiment_id = formal_experiment_id(
            ordinal=ordinal,
            lane=LANE_SCHEDULED_CONVERSION,
            slot=str(selected["canonical_slot"]),
            content_hash=str(selected["content_hash"]),
        )
        dates_considered.append(
            {
                "date": date_key,
                "status": "READY",
                "hold_reasons": [],
            }
        )
        return {
            "policy_version": POLICY_VERSION,
            "as_of": as_of.isoformat(),
            "lane": LANE_SCHEDULED_CONVERSION,
            "status": "READY",
            "target_date": date_key,
            "experiment_id": experiment_id,
            "content_hash": selected["content_hash"],
            "expected_scheduled_at": selected["scheduled_at"],
            "selected": selected,
            "shortlist": shortlist,
            "excluded": dict(sorted(excluded.items())),
            "dates_considered": dates_considered,
            "dry_run_argv": [
                "uv",
                "run",
                "python",
                "reel_scheduler.py",
                "trial-convert-scheduled",
                "--channel",
                channel_id,
                "--content-hash",
                str(selected["content_hash"]),
                "--experiment-id",
                experiment_id,
                "--expected-scheduled-at",
                str(selected["scheduled_at"]),
            ],
        }

    return {
        "policy_version": POLICY_VERSION,
        "as_of": as_of.isoformat(),
        "lane": LANE_SCHEDULED_CONVERSION,
        "status": "HOLD",
        "target_date": None,
        "experiment_id": None,
        "content_hash": None,
        "expected_scheduled_at": None,
        "selected": None,
        "shortlist": [],
        "excluded": dict(sorted(excluded.items())),
        "dates_considered": dates_considered,
        "dry_run_argv": [],
    }


def snapshot_in_window(
    snapshots: Sequence[Mapping[str, Any]],
    *,
    published_at: datetime,
    minimum_hours: float,
    maximum_hours: float,
) -> tuple[dict[str, Any] | None, float | None]:
    candidates: list[tuple[float, datetime, int, dict[str, Any]]] = []
    for snapshot in snapshots:
        captured_at = reach_analysis.parse_datetime(snapshot.get("captured_at"))
        if captured_at is None:
            continue
        age_hours = (captured_at - published_at).total_seconds() / 3600
        if minimum_hours <= age_hours <= maximum_hours:
            candidates.append(
                (
                    age_hours,
                    captured_at,
                    int(snapshot.get("id") or -1),
                    dict(snapshot),
                )
            )
    if not candidates:
        return None, None
    selected = min(candidates, key=lambda item: (item[0], item[1], item[2]))
    return selected[3], selected[0]


def core_snapshot_metrics(
    snapshot: Mapping[str, Any] | None,
) -> dict[str, int | float] | None:
    if snapshot is None:
        return None
    raw = reach_analysis.raw_metric_map(snapshot.get("raw_api_payload"))
    if any(
        not isinstance(raw.get(name), (int, float))
        or isinstance(raw.get(name), bool)
        or float(raw[name]) < 0
        for name in CORE_SNAPSHOT_METRICS
    ):
        return None
    return {
        **{name: raw[name] for name in CORE_SNAPSHOT_METRICS},
        **{
            name: raw[name]
            for name in (
                "total_views",
                "ig_reels_avg_watch_time",
                "reels_skip_rate",
            )
            if isinstance(raw.get(name), (int, float))
            and not isinstance(raw.get(name), bool)
            and float(raw[name]) >= 0
        },
    }


def tier_b_positive_evidence(metrics: Mapping[str, Any]) -> list[str]:
    saved = int(metrics.get("saved") or 0)
    shares = int(metrics.get("shares") or 0)
    views = float(metrics.get("views") or 0)
    reach = float(metrics.get("reach") or 0)
    skip_rate = metrics.get("reels_skip_rate")
    average_watch = metrics.get("ig_reels_avg_watch_time")
    reasons: list[str] = []
    if saved + shares >= TIER_B_MIN_SAVE_SHARE:
        reasons.append(f"saves+shares>={TIER_B_MIN_SAVE_SHARE}")
    native_signal = (
        views >= TIER_B_MIN_NATIVE_VIEWS
        or reach >= TIER_B_MIN_NATIVE_REACH
    )
    retention_signal = (
        isinstance(skip_rate, (int, float))
        and float(skip_rate) <= TIER_B_MAX_SKIP_RATE
    ) or (
        isinstance(average_watch, (int, float))
        and float(average_watch) >= TIER_B_MIN_AVG_WATCH_MS
    )
    if native_signal and retention_signal:
        reasons.append("native_distribution+retention")
    return reasons


def tier_d_diagnostic_evidence(metrics: Mapping[str, Any]) -> list[str]:
    saved = int(metrics.get("saved") or 0)
    shares = int(metrics.get("shares") or 0)
    skip_rate = metrics.get("reels_skip_rate")
    average_watch = metrics.get("ig_reels_avg_watch_time")
    reasons: list[str] = []
    if saved + shares >= TIER_D_MIN_SAVE_SHARE:
        reasons.append(f"saves+shares>={TIER_D_MIN_SAVE_SHARE}")
    if (
        isinstance(skip_rate, (int, float))
        and float(skip_rate) <= TIER_D_MAX_SKIP_RATE
    ):
        reasons.append(f"skip_rate<={TIER_D_MAX_SKIP_RATE:g}%")
    if (
        isinstance(average_watch, (int, float))
        and float(average_watch) >= TIER_D_MIN_AVG_WATCH_MS
    ):
        reasons.append(f"avg_watch_ms>={TIER_D_MIN_AVG_WATCH_MS:g}")
    return reasons


def parent_evidence(
    *,
    result: Mapping[str, Any],
    reel: Mapping[str, Any],
    published_at: datetime,
) -> dict[str, Any] | None:
    """Assign the strongest eligible evidence tier to one published parent."""
    classification = str(result.get("classification") or "")
    selected_age = result.get("snapshot_age_hours")
    selected_metrics = dict(result.get("metrics") or {})
    if (
        classification in WINNER_PRIORITY
        and isinstance(selected_age, (int, float))
        and 72 <= float(selected_age) <= 96
    ):
        return {
            "tier": "A",
            "label": "STRICT_72_96_WINNER",
            "reason": classification,
            "classification": classification,
            "snapshot_age_hours": float(selected_age),
            "snapshot_captured_at": str(result.get("snapshot_captured_at") or ""),
            "metrics": selected_metrics,
        }

    snapshots = [
        item
        for item in reel.get("snapshots", [])
        if isinstance(item, Mapping)
    ]
    strict_snapshot, strict_age = snapshot_in_window(
        snapshots,
        published_at=published_at,
        minimum_hours=72,
        maximum_hours=96,
    )
    if strict_snapshot is not None:
        strict_metrics = core_snapshot_metrics(strict_snapshot)
        diagnostic_reasons = (
            tier_d_diagnostic_evidence(strict_metrics)
            if strict_metrics is not None
            else []
        )
        if (
            classification == "NO_WINNER"
            and strict_metrics is not None
            and strict_age is not None
            and diagnostic_reasons
        ):
            return {
                "tier": "D",
                "label": "CORE_VALID_72H_DIAGNOSTIC",
                "reason": ", ".join(diagnostic_reasons),
                "classification": "DIAGNOSTIC_ONLY",
                "snapshot_age_hours": float(strict_age),
                "snapshot_captured_at": str(
                    strict_snapshot.get("captured_at") or ""
                ),
                "metrics": {
                    "base_views": strict_metrics.get("views"),
                    "combined_views": strict_metrics.get(
                        "total_views",
                        strict_metrics.get("views"),
                    ),
                    "reach": strict_metrics.get("reach"),
                    "total_interactions": strict_metrics.get(
                        "total_interactions"
                    ),
                    "saved": strict_metrics.get("saved"),
                    "shares": strict_metrics.get("shares"),
                    "diagnostics": {
                        "ig_reels_avg_watch_time": strict_metrics.get(
                            "ig_reels_avg_watch_time"
                        ),
                        "reels_skip_rate": strict_metrics.get(
                            "reels_skip_rate"
                        ),
                    },
                },
            }
        return None

    if (
        classification in WINNER_PRIORITY
        and isinstance(selected_age, (int, float))
        and 96 < float(selected_age) <= 144
    ):
        return {
            "tier": "C",
            "label": "NEAR_WINDOW_96_144_WINNER",
            "reason": classification,
            "classification": classification,
            "snapshot_age_hours": float(selected_age),
            "snapshot_captured_at": str(result.get("snapshot_captured_at") or ""),
            "metrics": selected_metrics,
        }

    checkpoint, checkpoint_age = snapshot_in_window(
        snapshots,
        published_at=published_at,
        minimum_hours=24,
        maximum_hours=28,
    )
    checkpoint_metrics = core_snapshot_metrics(checkpoint)
    if checkpoint_metrics is None or checkpoint_age is None:
        return None
    captured_at = str(checkpoint.get("captured_at") or "") if checkpoint else ""
    tier_b_reasons = tier_b_positive_evidence(checkpoint_metrics)
    if tier_b_reasons:
        candidate_metrics = {
            "base_views": checkpoint_metrics.get("views"),
            "combined_views": checkpoint_metrics.get(
                "total_views",
                checkpoint_metrics.get("views"),
            ),
            "reach": checkpoint_metrics.get("reach"),
            "total_interactions": checkpoint_metrics.get("total_interactions"),
            "saved": checkpoint_metrics.get("saved"),
            "shares": checkpoint_metrics.get("shares"),
            "diagnostics": {
                "ig_reels_avg_watch_time": checkpoint_metrics.get(
                    "ig_reels_avg_watch_time"
                ),
                "reels_skip_rate": checkpoint_metrics.get("reels_skip_rate"),
            },
        }
        candidate = reach_analysis.winner_candidate(candidate_metrics)
        return {
            "tier": "B",
            "label": "CORE_VALID_24H_POSITIVE_AUDIENCE_FIT",
            "reason": ", ".join(tier_b_reasons),
            "classification": str(candidate["winner"]),
            "snapshot_age_hours": float(checkpoint_age),
            "snapshot_captured_at": captured_at,
            "metrics": candidate_metrics,
        }

    tier_d_reasons = tier_d_diagnostic_evidence(checkpoint_metrics)
    if not tier_d_reasons:
        return None
    return {
        "tier": "D",
        "label": "CORE_VALID_24H_DIAGNOSTIC",
        "reason": ", ".join(tier_d_reasons),
        "classification": "DIAGNOSTIC_ONLY",
        "snapshot_age_hours": float(checkpoint_age),
        "snapshot_captured_at": captured_at,
        "metrics": {
            "base_views": checkpoint_metrics.get("views"),
            "combined_views": checkpoint_metrics.get(
                "total_views",
                checkpoint_metrics.get("views"),
            ),
            "reach": checkpoint_metrics.get("reach"),
            "total_interactions": checkpoint_metrics.get("total_interactions"),
            "saved": checkpoint_metrics.get("saved"),
            "shares": checkpoint_metrics.get("shares"),
            "diagnostics": {
                "ig_reels_avg_watch_time": checkpoint_metrics.get(
                    "ig_reels_avg_watch_time"
                ),
                "reels_skip_rate": checkpoint_metrics.get("reels_skip_rate"),
            },
        },
    }


def published_parent_candidates(
    *,
    report_path: Path,
    db_path: Path,
    ledger_rows: Sequence[Mapping[str, Any]],
    channel_id: str,
    as_of: datetime,
    tested_parents: set[str],
    tested_reels: set[str],
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
    source_reels = {
        str(item.get("content_hash") or ""): item
        for item in regular
    }
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
        if content_hash in tested_reels:
            reasons.append("REEL_ALREADY_TESTED")
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
        evidence: dict[str, Any] | None = None
        source_reel = source_reels.get(content_hash)
        if published_at is not None and source_reel is not None:
            evidence = parent_evidence(
                result=result,
                reel=source_reel,
                published_at=published_at,
            )
        if evidence is None:
            reasons.append("NO_ELIGIBLE_EVIDENCE_TIER")
        if row is not None:
            reasons.extend(file_readiness_reasons(row, published_parent=True))
        if reasons:
            excluded.update(sorted(set(reasons)))
            continue
        assert row is not None
        assert evidence is not None
        metrics = dict(evidence["metrics"])
        reach = float(metrics.get("reach") or 0)
        saves = int(metrics.get("saved") or 0)
        shares = int(metrics.get("shares") or 0)
        interactions = int(metrics.get("total_interactions") or 0)
        save_share_rate = 1000 * (saves + shares) / reach if reach > 0 else 0.0
        assert published_at is not None
        evidence_tier = str(evidence["tier"])
        classification = str(evidence["classification"])
        if evidence_tier in {"A", "C"}:
            tier_sort: tuple[Any, ...] = (
                WINNER_PRIORITY.get(classification, len(WINNER_PRIORITY)),
                -save_share_rate,
                -interactions,
                -reach,
            )
        elif evidence_tier == "B":
            tier_sort = (
                -reach,
                -(saves + shares),
                -interactions,
            )
        else:
            diagnostics = metrics.get("diagnostics")
            diagnostics = diagnostics if isinstance(diagnostics, Mapping) else {}
            skip_rate = diagnostics.get("reels_skip_rate")
            average_watch = diagnostics.get("ig_reels_avg_watch_time")
            tier_sort = (
                -(saves + shares),
                float(skip_rate) if isinstance(skip_rate, (int, float)) else 101.0,
                -float(average_watch)
                if isinstance(average_watch, (int, float))
                else 0.0,
            )
        candidates.append(
            {
                "content_hash": content_hash,
                "media_id": str(result.get("media_id") or ""),
                "title": str(result.get("title") or ""),
                "published_at": published_at.isoformat(),
                "evidence_tier": evidence_tier,
                "evidence_label": str(evidence["label"]),
                "evidence_reason": str(evidence["reason"]),
                "snapshot_captured_at": str(evidence["snapshot_captured_at"]),
                "snapshot_age_hours": float(evidence["snapshot_age_hours"]),
                "classification": classification,
                "action": (
                    str(result.get("action") or "")
                    if evidence_tier in {"A", "C"}
                    else "MANUAL_RERENDER_REVIEW"
                ),
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
                    EVIDENCE_TIER_PRIORITY[evidence_tier],
                    *tier_sort,
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


def future_parent_reserve_families(
    *,
    report_path: Path,
    db_path: Path,
    ledger_rows: Sequence[Mapping[str, Any]],
    as_of: datetime,
    tested_parents: set[str],
    tested_reels: set[str],
    eligible_parent_candidates: Sequence[Mapping[str, Any]],
) -> set[str]:
    """Protect viable current/future parent supply when alternatives exist.

    Besides today's eligible parent shortlist, reserve rerender-ready parents
    that are still immature or waiting for usable analytics. A mature Reel
    with a valid negative checkpoint is not reserved indefinitely.
    """
    report = reach_analysis.load_report(report_path)
    loaded = reach_analysis.load_reels(report, db_path)
    regular = [
        item
        for item in loaded
        if not reach_analysis.truthy_flag(item.get("trial_reel"))
    ]
    coverage = (
        sum(bool(item.get("snapshots")) for item in regular) / len(regular)
        if regular
        else 0.0
    )
    classified = {
        str(item.get("content_hash") or ""): reach_analysis.classify_reel(
            item,
            coverage=coverage,
        )
        for item in regular
    }
    source_reels = {
        str(item.get("content_hash") or ""): item
        for item in regular
    }
    reserved = {
        str(item.get("asset_family_id") or "")
        for item in eligible_parent_candidates
        if str(item.get("asset_family_id") or "")
    }
    reserved.update(PRIORITY_PARENT_RESERVE_FAMILIES)

    for row in ledger_rows:
        if str(row.get("status") or "") != "published":
            continue
        content_hash = str(row.get("content_hash") or "")
        if (
            not content_hash
            or content_hash in tested_parents
            or content_hash in tested_reels
        ):
            continue
        if file_readiness_reasons(row, published_parent=True):
            continue
        result = classified.get(content_hash)
        source_reel = source_reels.get(content_hash)
        if result is None or source_reel is None:
            continue
        try:
            published_at = parse_aware_datetime(
                result.get("published_at"),
                field="published_at",
            )
        except ValueError:
            continue
        age = as_of - published_at
        if age < timedelta(0) or age > timedelta(days=PARENT_MAX_AGE_DAYS):
            continue
        immature = age < timedelta(hours=72)
        analytics_hold = bool(result.get("data_errors"))
        if not immature and not analytics_hold:
            strict_snapshot, _ = snapshot_in_window(
                [
                    item
                    for item in source_reel.get("snapshots", [])
                    if isinstance(item, Mapping)
                ],
                published_at=published_at,
                minimum_hours=72,
                maximum_hours=96,
            )
            checkpoint, _ = snapshot_in_window(
                [
                    item
                    for item in source_reel.get("snapshots", [])
                    if isinstance(item, Mapping)
                ],
                published_at=published_at,
                minimum_hours=24,
                maximum_hours=28,
            )
            analytics_hold = (
                strict_snapshot is None
                and core_snapshot_metrics(checkpoint) is None
            )
        if immature or analytics_hold:
            family = asset_family_id(row)
            if family:
                reserved.add(family)
    return reserved


def input_fingerprint(
    *,
    as_of: datetime,
    batch: Mapping[str, Any],
    ledger_rows: Sequence[Mapping[str, Any]],
    experiments: Sequence[Mapping[str, Any]],
) -> str:
    payload = {
        "policy_version": POLICY_VERSION,
        "as_of": as_of.isoformat(),
        "batch": dict(batch),
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
    tested_parents, tested_reels, _ = trial_history(experiments)
    family_observation_starts = trial_family_observation_starts(experiments)
    lane_dates = daily_lane_dates(experiments)
    lane_experiments = daily_lane_experiments(experiments)
    observation_window_starts = nonterminal_observation_window_starts(experiments)
    queue_launch_times = occupied_launch_times(ledger_rows)
    parent_pool, parent_excluded = published_parent_candidates(
        report_path=report_path,
        db_path=db_path,
        ledger_rows=ledger_rows,
        channel_id=channel_id,
        as_of=as_of,
        tested_parents=tested_parents,
        tested_reels=tested_reels,
    )
    reserved_parent_families = future_parent_reserve_families(
        report_path=report_path,
        db_path=db_path,
        ledger_rows=ledger_rows,
        as_of=as_of,
        tested_parents=tested_parents,
        tested_reels=tested_reels,
        eligible_parent_candidates=parent_pool,
    )
    today = as_of.astimezone(JST).date()
    candidate_dates: set[date] = set()
    for row in ledger_rows:
        if str(row.get("status") or "") != "scheduled":
            continue
        try:
            launch = parse_aware_datetime(row.get("scheduled_at"), field="scheduled_at")
        except ValueError:
            continue
        local_date = launch.astimezone(JST).date()
        if local_date >= today:
            candidate_dates.add(local_date)
    for dates in lane_dates.values():
        candidate_dates.update(
            date.fromisoformat(value)
            for value in dates
            if date.fromisoformat(value) >= today
        )

    scheduled_excluded: Counter[str] = Counter()
    dates_considered: list[dict[str, Any]] = []
    date_attempts: list[dict[str, Any]] = []
    target_date: date | None = None
    missing_lanes: tuple[str, ...] = ()
    recommended_lanes: tuple[str, ...] = ()
    existing_for_target = {lane: [] for lane in DAILY_LANES}
    lane_hold_reasons = {lane: [] for lane in DAILY_LANES}
    scheduled: list[dict[str, Any]] = []
    parents = [dict(item) for item in parent_pool]
    selected_scheduled: dict[str, Any] | None = None
    selected_parent: dict[str, Any] | None = None
    proposals: dict[str, datetime] = {}
    projected_windows: dict[str, int] | None = None

    for possible_date in sorted(candidate_dates):
        date_key = possible_date.isoformat()
        existing = lane_experiments.get(
            date_key,
            {lane: [] for lane in DAILY_LANES},
        )
        missing = tuple(
            lane
            for lane in DAILY_LANES
            if not existing.get(lane)
        )
        if not missing:
            continue
        attempt_scheduled: list[dict[str, Any]] = []
        attempt_parent_pool = [dict(item) for item in parent_pool]
        attempt_selected_scheduled: dict[str, Any] | None = None
        attempt_selected_parent: dict[str, Any] | None = None
        attempt_proposals: dict[str, datetime] = {}
        attempt_lane_reasons = {lane: [] for lane in DAILY_LANES}

        published_missing = LANE_SUCCESSFUL_POST_VARIANT in missing
        if LANE_SCHEDULED_CONVERSION in missing:
            attempt_scheduled, attempt_excluded = scheduled_shortlist(
                ledger_rows,
                as_of=as_of,
                ordinal=ordinal,
                tested_reels=tested_reels,
                family_observation_starts=family_observation_starts,
                facebook_statuses=facebook_statuses,
                lane_dates=lane_dates,
                queue_launch_times=queue_launch_times,
                reserved_parent_families=reserved_parent_families,
                target_date=possible_date,
                # Evaluate the conversion lane on its own. A blocked parent
                # lane must not suppress a later, otherwise-safe conversion.
                published_lane_missing=False,
                observation_window_starts=observation_window_starts,
            )
            scheduled_excluded.update(attempt_excluded)
            if not attempt_scheduled:
                attempt_lane_reasons[LANE_SCHEDULED_CONVERSION].append(
                    "NO_ELIGIBLE_SCHEDULED_CONVERSION"
                )
            else:
                attempt_selected_scheduled = attempt_scheduled[0]
                scheduled_at = parse_aware_datetime(
                    attempt_selected_scheduled["scheduled_at"],
                    field="scheduled_at",
                )
                attempt_proposals[LANE_SCHEDULED_CONVERSION] = scheduled_at

        if published_missing:
            additive_at = published_variant_launch(possible_date)
            attempt_parent_pool, family_cooldown_count = (
                parent_candidates_at_launch(
                    attempt_parent_pool,
                    launch=additive_at,
                    family_observation_starts=family_observation_starts,
                )
            )
            if family_cooldown_count:
                parent_excluded[
                    "ASSET_FAMILY_OBSERVATION_COOLDOWN"
                ] += family_cooldown_count
            if additive_at < as_of + timedelta(hours=PARENT_TARGET_LEAD_HOURS):
                attempt_lane_reasons[LANE_SUCCESSFUL_POST_VARIANT].append(
                    "INSUFFICIENT_RERENDER_LEAD"
                )
            if additive_at in queue_launch_times:
                attempt_lane_reasons[LANE_SUCCESSFUL_POST_VARIANT].append(
                    "PUBLISHED_1900_SLOT_OCCUPIED"
                )
            if not attempt_parent_pool:
                attempt_lane_reasons[LANE_SUCCESSFUL_POST_VARIANT].append(
                    "NO_ELIGIBLE_PUBLISHED_PARENT"
                )
            if not attempt_lane_reasons[LANE_SUCCESSFUL_POST_VARIANT]:
                attempt_selected_parent = attempt_parent_pool[0]
                parent_projection = projected_proposal_windows(
                    {LANE_SUCCESSFUL_POST_VARIANT: additive_at},
                    observation_window_starts=observation_window_starts,
                )
                if max(parent_projection.values()) > MAX_CONCURRENT_OBSERVATION_WINDOWS:
                    attempt_lane_reasons[LANE_SUCCESSFUL_POST_VARIANT].append(
                        "OBSERVATION_WINDOW_CAP_REACHED"
                    )
                    attempt_selected_parent = None
                else:
                    attempt_proposals[LANE_SUCCESSFUL_POST_VARIANT] = additive_at

        # When both lanes are independently ready, choose the first stable
        # family-distinct pair. If capacity or family separation permits only
        # one lane, keep the parent lane: parent supply is the scarce input and
        # the automation can re-run to fill the conversion lane independently.
        if attempt_selected_scheduled is not None and attempt_selected_parent is not None:
            selected_pair: tuple[dict[str, Any], dict[str, Any]] | None = None
            for scheduled_candidate in attempt_scheduled:
                scheduled_family = str(
                    scheduled_candidate.get("asset_family_id") or ""
                )
                for parent_candidate in attempt_parent_pool:
                    if (
                        str(parent_candidate.get("asset_family_id") or "")
                        != scheduled_family
                    ):
                        selected_pair = (scheduled_candidate, parent_candidate)
                        break
                if selected_pair is not None:
                    break
            if selected_pair is None:
                attempt_lane_reasons[LANE_SCHEDULED_CONVERSION].append(
                    "NO_DISTINCT_PARENT_FAMILY_FOR_BATCH"
                )
                attempt_selected_scheduled = None
                attempt_proposals.pop(LANE_SCHEDULED_CONVERSION, None)
            else:
                attempt_selected_scheduled, attempt_selected_parent = selected_pair
                attempt_proposals[LANE_SCHEDULED_CONVERSION] = parse_aware_datetime(
                    attempt_selected_scheduled["scheduled_at"],
                    field="scheduled_at",
                )
                combined_projection = projected_proposal_windows(
                    attempt_proposals,
                    observation_window_starts=observation_window_starts,
                )
                if max(combined_projection.values()) > MAX_CONCURRENT_OBSERVATION_WINDOWS:
                    attempt_lane_reasons[LANE_SCHEDULED_CONVERSION].append(
                        "OBSERVATION_WINDOW_CAP_REACHED"
                    )
                    attempt_selected_scheduled = None
                    attempt_proposals.pop(LANE_SCHEDULED_CONVERSION, None)

        attempt_ready_lanes = tuple(
            lane
            for lane, selected in (
                (LANE_SCHEDULED_CONVERSION, attempt_selected_scheduled),
                (LANE_SUCCESSFUL_POST_VARIANT, attempt_selected_parent),
            )
            if selected is not None
        )
        attempt_projection = (
            projected_proposal_windows(
                attempt_proposals,
                observation_window_starts=observation_window_starts,
            )
            if attempt_proposals
            else None
        )
        public_lane_status: dict[str, dict[str, Any]] = {}
        for lane in DAILY_LANES:
            if existing.get(lane):
                public_lane_status[lane] = {
                    "status": "ALREADY_FILLED",
                    "hold_reasons": [],
                }
            elif lane in attempt_ready_lanes:
                public_lane_status[lane] = {
                    "status": "READY",
                    "hold_reasons": [],
                }
            else:
                public_lane_status[lane] = {
                    "status": "HOLD",
                    "hold_reasons": sorted(set(attempt_lane_reasons[lane])),
                }

        dates_considered.append(
            {
                "date": date_key,
                "existing_lanes": [
                    lane for lane in DAILY_LANES if existing.get(lane)
                ],
                "missing_lanes": list(missing),
                "ready_lanes": list(attempt_ready_lanes),
                "hold_reasons": sorted(
                    {
                        reason
                        for lane in missing
                        for reason in attempt_lane_reasons[lane]
                    }
                ),
                "lanes": public_lane_status,
            }
        )
        date_attempts.append(
            {
                "date": possible_date,
                "existing": existing,
                "missing": missing,
                "ready_lanes": attempt_ready_lanes,
                "lane_reasons": attempt_lane_reasons,
                "scheduled": attempt_scheduled,
                "parents": attempt_parent_pool,
                "selected_scheduled": attempt_selected_scheduled,
                "selected_parent": attempt_selected_parent,
                "proposals": attempt_proposals,
                "projection": attempt_projection,
            }
        )

    # Published parents are the scarce lane, so take the earliest feasible
    # parent gap first. If none exists, conversions continue independently
    # through the queue horizon instead of stopping behind that parent gap.
    selected_attempt = next(
        (
            attempt
            for attempt in date_attempts
            if LANE_SUCCESSFUL_POST_VARIANT in attempt["ready_lanes"]
        ),
        None,
    )
    if selected_attempt is None:
        selected_attempt = next(
            (
                attempt
                for attempt in date_attempts
                if LANE_SCHEDULED_CONVERSION in attempt["ready_lanes"]
            ),
            None,
        )

    if selected_attempt is not None:
        target_date = selected_attempt["date"]
        missing_lanes = selected_attempt["missing"]
        recommended_lanes = selected_attempt["ready_lanes"]
        existing_for_target = {
            lane: list(selected_attempt["existing"].get(lane) or [])
            for lane in DAILY_LANES
        }
        lane_hold_reasons = {
            lane: sorted(set(selected_attempt["lane_reasons"][lane]))
            for lane in DAILY_LANES
        }
        scheduled = selected_attempt["scheduled"]
        parents = selected_attempt["parents"]
        selected_scheduled = selected_attempt["selected_scheduled"]
        selected_parent = selected_attempt["selected_parent"]
        proposals = selected_attempt["proposals"]
        projected_windows = selected_attempt["projection"]

    ready = target_date is not None
    for rank, item in enumerate(parents, start=1):
        item["rank"] = rank
        item["selected"] = (
            selected_parent is not None
            and item["content_hash"] == selected_parent["content_hash"]
        )

    lane_ordinals: dict[str, int] = {}
    next_ordinal = ordinal
    for lane in DAILY_LANES:
        if lane in recommended_lanes:
            lane_ordinals[lane] = next_ordinal
            next_ordinal += 1

    recommendation: dict[str, Any] = {
        "status": "READY" if ready else "HOLD",
        "target_date": target_date.isoformat() if target_date else None,
        "manual_approval_required": (
            ready and LANE_SUCCESSFUL_POST_VARIANT in recommended_lanes
        ),
        "auto_apply": False,
        "existing_lane_count": (
            DAILY_TRIAL_LANES - len(missing_lanes) if ready else None
        ),
        "recommended_lane_count": len(recommended_lanes) if ready else 0,
        "projected_daily_trial_count": (
            DAILY_TRIAL_LANES - len(missing_lanes) + len(recommended_lanes)
            if ready
            else None
        ),
        "lanes": {},
    }

    for lane in DAILY_LANES:
        if ready and lane not in missing_lanes:
            recommendation["lanes"][lane] = {
                "lane": lane,
                "status": "ALREADY_FILLED",
                "manual_approval_required": False,
                "auto_apply": False,
                "existing_trials": existing_for_target[lane],
                "dry_run_argv": [],
            }

    if ready and LANE_SCHEDULED_CONVERSION in recommended_lanes:
        assert selected_scheduled is not None
        scheduled_experiment_id = formal_experiment_id(
            ordinal=lane_ordinals[LANE_SCHEDULED_CONVERSION],
            lane=LANE_SCHEDULED_CONVERSION,
            slot=str(selected_scheduled["canonical_slot"]),
            content_hash=str(selected_scheduled["content_hash"]),
        )
        recommendation["lanes"][LANE_SCHEDULED_CONVERSION] = {
            "lane": LANE_SCHEDULED_CONVERSION,
            "status": "READY",
            "mode": "convert_in_place",
            "manual_approval_required": False,
            "auto_apply": False,
            "experiment_id": scheduled_experiment_id,
            "content_hash": selected_scheduled["content_hash"],
            "expected_scheduled_at": selected_scheduled["scheduled_at"],
            "scheduled_target": selected_scheduled,
            "changed_variables": ["distribution_mode"],
            "facebook_effect": (
                "Future mutable mirror will be removed from the Facebook queue."
            ),
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
                scheduled_experiment_id,
                "--expected-scheduled-at",
                str(selected_scheduled["scheduled_at"]),
            ],
        }

    if ready and LANE_SUCCESSFUL_POST_VARIANT in recommended_lanes:
        assert selected_parent is not None
        additive_launch = proposals[LANE_SUCCESSFUL_POST_VARIANT]
        parent_experiment_id = formal_experiment_id(
            ordinal=lane_ordinals[LANE_SUCCESSFUL_POST_VARIANT],
            lane=LANE_SUCCESSFUL_POST_VARIANT,
            slot=f"{PUBLISHED_VARIANT_HOUR:02d}",
            content_hash=str(selected_parent["content_hash"]),
        )
        recommendation["lanes"][LANE_SUCCESSFUL_POST_VARIANT] = {
            "lane": LANE_SUCCESSFUL_POST_VARIANT,
            "status": "READY",
            "mode": "add_at_1900",
            "target_time": "19:00:00",
            "manual_approval_required": True,
            "auto_apply": False,
            "experiment_id": parent_experiment_id,
            "parent": selected_parent,
            "scheduled_at": additive_launch.isoformat(),
            "expected_scheduled_at": additive_launch.isoformat(),
            "changed_variables": ["overlay_hook"],
            "facebook_effect": (
                "Additive Trial remains Instagram-only; no Facebook mirror is created."
            ),
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
                "trial-add-from-published",
                "--channel",
                channel_id,
                "--parent-content-hash",
                str(selected_parent["content_hash"]),
                "--media-path",
                "<rerendered-variant.mp4>",
                "--experiment-id",
                parent_experiment_id,
                "--hook",
                "<new-rendered-hook>",
                "--scheduled-at",
                additive_launch.isoformat(),
                "--expected-scheduled-at",
                additive_launch.isoformat(),
                "--asset-family-id",
                str(selected_parent["asset_family_id"]),
                "--changed-variable",
                "overlay_hook",
                "--caption-mode",
                "preserve-parent",
            ],
        }

    if ready:
        for lane in missing_lanes:
            if lane in recommended_lanes:
                continue
            recommendation["lanes"][lane] = {
                "lane": lane,
                "status": "HOLD",
                "blocked_date": target_date.isoformat(),
                "manual_approval_required": (
                    lane == LANE_SUCCESSFUL_POST_VARIANT
                ),
                "auto_apply": False,
                "dry_run_argv": [],
                "hold_reasons": lane_hold_reasons[lane],
            }
    else:
        earliest_blocked_entry = next(
            (
                item
                for item in dates_considered
                if any(
                    details["status"] == "HOLD"
                    for details in item["lanes"].values()
                )
            ),
            None,
        )
        earliest_blocked = (
            {
                "date": earliest_blocked_entry["date"],
                "lanes": [
                    {
                        "lane": lane,
                        "hold_reasons": earliest_blocked_entry["lanes"][lane][
                            "hold_reasons"
                        ],
                    }
                    for lane in DAILY_LANES
                    if earliest_blocked_entry["lanes"][lane]["status"] == "HOLD"
                ],
            }
            if earliest_blocked_entry is not None
            else {
                "date": None,
                "lanes": [
                    {
                        "lane": lane,
                        "hold_reasons": [
                            "NO_MISSING_LANE_IN_QUEUE_HORIZON"
                        ],
                    }
                    for lane in DAILY_LANES
                ],
            }
        )
        recommendation["earliest_blocked"] = earliest_blocked
        recommendation["hold_reasons"] = sorted(
            {
                reason
                for lane in earliest_blocked["lanes"]
                for reason in lane["hold_reasons"]
            }
        )
        if not recommendation["hold_reasons"]:
            recommendation["hold_reasons"] = [
                "NO_FEASIBLE_DAILY_TRIAL_GAP_IN_QUEUE_HORIZON"
            ]
        for lane in DAILY_LANES:
            lane_block = next(
                (
                    {
                        "date": item["date"],
                        "hold_reasons": item["lanes"][lane]["hold_reasons"],
                    }
                    for item in dates_considered
                    if item["lanes"][lane]["status"] == "HOLD"
                ),
                None,
            )
            recommendation["lanes"][lane] = {
                "lane": lane,
                "status": "HOLD",
                "blocked_date": (
                    lane_block["date"] if lane_block is not None else None
                ),
                "manual_approval_required": lane == LANE_SUCCESSFUL_POST_VARIANT,
                "auto_apply": False,
                "dry_run_argv": [],
                "hold_reasons": (
                    lane_block["hold_reasons"]
                    if lane_block is not None
                    else ["NO_MISSING_LANE_IN_QUEUE_HORIZON"]
                ),
            }

    existing_windows = (
        {
            lane: concurrent_observation_windows_at(
                launch,
                observation_window_starts,
            )
            for lane, launch in proposals.items()
        }
        if ready
        else None
    )
    exclusions = scheduled_excluded + parent_excluded
    batch = {
        "target_date": target_date.isoformat() if target_date else None,
        "timezone": str(JST),
        "ordinal_start": ordinal,
        "lanes_per_day": DAILY_TRIAL_LANES,
        "existing_lanes": [
            lane for lane in DAILY_LANES if lane not in missing_lanes
        ] if ready else [],
        "missing_lanes": list(missing_lanes),
        "recommended_lanes": list(recommended_lanes),
        "blocked_lanes": [
            lane
            for lane in missing_lanes
            if lane not in recommended_lanes
        ],
        "lane_plan": [
            {
                "lane": LANE_SCHEDULED_CONVERSION,
                "slot": "existing_regular_slot",
                "displaces_regular_row": False,
            },
            {
                "lane": LANE_SUCCESSFUL_POST_VARIANT,
                "slot": f"{PUBLISHED_VARIANT_HOUR:02d}:00",
                "displaces_regular_row": False,
            },
        ],
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "read_only": True,
        "channel_id": channel_id,
        "as_of": as_of.isoformat(),
        "daily_batch": batch,
        "capacity": {
            "daily_trial_lanes": DAILY_TRIAL_LANES,
            "scheduled_conversion_lead_hours": SCHEDULED_TARGET_LEAD_HOURS,
            "published_rerender_lead_hours": PARENT_TARGET_LEAD_HOURS,
            "observation_window_hours": OBSERVATION_WINDOW_HOURS,
            "maximum_concurrent_observation_windows": (
                MAX_CONCURRENT_OBSERVATION_WINDOWS
            ),
            "maximum_concurrent_observation_windows_derivation": (
                "72h spans two full JST dates plus two partial dates; with one "
                "fixed 19:00 launch and one variable regular-slot launch per date, "
                "the safe boundary maximum is 2 + 4 + 1 = 7."
            ),
            "nonterminal_experiments": len(observation_window_starts),
            "existing_observation_windows_at_launch": existing_windows,
            "projected_observation_windows_at_launch": projected_windows,
            "existing_lane_dates": {
                lane: sorted(dates)
                for lane, dates in lane_dates.items()
            },
            "reserved_parent_families": sorted(reserved_parent_families),
        },
        "recommendation": recommendation,
        "shortlists": {
            "published_parents": parents,
            "scheduled_candidates": scheduled,
        },
        "dates_considered": dates_considered,
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
                batch=batch,
                ledger_rows=ledger_rows,
                experiments=experiments,
            ),
        },
    }


def markdown_cell(value: Any) -> str:
    return str(value if value not in (None, "") else "—").replace("|", "\\|").replace("\n", " ")


def render_markdown(selection: Mapping[str, Any]) -> str:
    batch = selection["daily_batch"]
    capacity = selection["capacity"]
    recommendation = selection["recommendation"]
    lines = [
        "# AI Brief JP daily Trial batch",
        "",
        f"- As of: `{selection['as_of']}`",
        f"- Policy: `{selection['policy_version']}`",
        f"- Target JST date: `{batch['target_date'] or 'not selected'}`",
        f"- Daily lanes: `{batch['lanes_per_day']}`",
        (
            f"- Observation capacity: "
            f"`{capacity['maximum_concurrent_observation_windows']}` concurrent "
            f"{capacity['observation_window_hours']}h windows"
        ),
        "- Selector mode: read-only; emitted commands never include `--apply`.",
        "",
        "## Recommendation",
        "",
        f"- Status: `{recommendation['status']}`",
        (
            f"- Ready lanes in this packet: "
            f"`{recommendation['recommended_lane_count']}`"
        ),
    ]
    if recommendation["status"] != "READY":
        lines.append(
            "- Hold reasons: "
            + ", ".join(
                f"`{value}`"
                for value in recommendation.get("hold_reasons", [])
            )
        )
        earliest_blocked = recommendation.get("earliest_blocked")
        if isinstance(earliest_blocked, Mapping):
            lines.append(
                "- Earliest blocked date: "
                f"`{earliest_blocked.get('date') or 'outside queue horizon'}`"
            )

    for lane_name in DAILY_LANES:
        lane = recommendation["lanes"][lane_name]
        lines.extend(
            [
                "",
                f"### {lane_name}",
                "",
                f"- Status: `{lane['status']}`",
            ]
        )
        if lane["status"] == "ALREADY_FILLED":
            for existing in lane.get("existing_trials", []):
                lines.append(
                    f"- Existing experiment: `{existing.get('experiment_id')}` at "
                    f"`{existing.get('scheduled_at')}`"
                )
            continue
        if lane["status"] != "READY":
            if lane.get("blocked_date"):
                lines.append(f"- Blocked date: `{lane['blocked_date']}`")
            lines.append(
                "- Hold reasons: "
                + ", ".join(
                    f"`{value}`"
                    for value in lane.get("hold_reasons", [])
                )
            )
            continue
        lines.extend(
            [
                f"- Experiment: `{lane['experiment_id']}`",
                f"- Expected slot: `{lane['expected_scheduled_at']}`",
                f"- Facebook effect: {lane['facebook_effect']}",
            ]
        )
        parent = lane.get("parent")
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
            target = lane["scheduled_target"]
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
                shlex.join([str(value) for value in lane["dry_run_argv"]]),
                "```",
            ]
        )

    parents = selection["shortlists"]["published_parents"]
    lines.extend(
        [
            "",
            "## Published-parent shortlist",
            "",
            "| Rank | Tier | Evidence | Classification | Snapshot age | Reach | Saves + shares / 1k | Title |",
            "|---:|---|---|---|---:|---:|---:|---|",
        ]
    )
    for item in parents:
        metrics = item["metrics"]
        lines.append(
            f"| {item['rank']} | {item['evidence_tier']} | "
            f"{item['evidence_label']} | {item['classification']} | "
            f"{item['snapshot_age_hours']:.2f}h | "
            f"{markdown_cell(metrics.get('instagram_reach'))} | "
            f"{float(metrics.get('save_share_per_1000_reached') or 0):.1f} | "
            f"{markdown_cell(item.get('title'))} |"
        )
    if not parents:
        lines.append("| — | — | — | — | — | — | — | No eligible parent for this batch |")

    scheduled = selection["shortlists"]["scheduled_candidates"]
    lines.extend(
        [
            "",
            "## Scheduled shortlist",
            "",
            "| Rank | Selected | Parent reserve | Slot | Selection key | Title |",
            "|---:|---|---|---|---|---|",
        ]
    )
    for item in scheduled:
        lines.append(
            f"| {item['rank']} | {'yes' if item['selected'] else 'no'} | "
            f"{'yes' if item.get('future_parent_family_reserved') else 'no'} | "
            f"`{item['scheduled_at']}` | `{item['selection_key'][:12]}` | "
            f"{markdown_cell(item.get('title'))} |"
        )
    if not scheduled:
        lines.append("| — | — | — | — | — | No eligible scheduled Reel |")

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
        f"target_date={selection['daily_batch']['target_date']} "
        f"lanes={selection['daily_batch']['lanes_per_day']} "
        f"json={args.json_out.resolve()} markdown={args.markdown_out.resolve()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
