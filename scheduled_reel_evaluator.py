"""Schedule-aware adapter for the measured Reel candidate evaluator.

The candidate evaluator normally consumes whole ``candidates.json`` files.
That is unsafe for an active schedule because a source file may also contain
unscheduled sibling clips. This module selects the exact ledger-backed clips
first, normalizes only those candidates, and then adds a conservative
schedule-triage layer. It never changes the Reel ledger or publishing state.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import quote

import reel_candidate_evaluator as evaluator


SCHEMA_VERSION = 1

ACTION_KEEP_TRIAL = "KEEP EXISTING TRIAL"
ACTION_KEEP_REGULAR = "KEEP REGULAR — SUPPORTED, NOT PROVEN"
ACTION_TRIAL_CANDIDATE = "TRIAL CANDIDATE"
ACTION_REVISE = "REVISE CREATIVE"
ACTION_RESCORE = "RESCORE / MANUAL REVIEW"
ACTION_DIVERSITY = "DIVERSITY REVIEW"
ACTION_SOURCE_REVIEW = "SOURCE SUPPORT REVIEW"
ACTION_MANUAL = "MANUAL REVIEW"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> Sequence[Any]:
    return (
        value
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes))
        else ()
    )


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def readonly_connection(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Reel ledger not found: {resolved}")
    uri = f"file:{quote(str(resolved), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
        is not None
    )


def load_scheduled_rows(
    db_path: Path,
    *,
    channel_id: str,
    statuses: Sequence[str],
) -> list[dict[str, Any]]:
    clean_statuses = tuple(sorted({_text(value) for value in statuses if _text(value)}))
    if not clean_statuses:
        raise ValueError("At least one scheduled status is required")
    placeholders = ",".join("?" for _ in clean_statuses)
    with readonly_connection(db_path) as connection:
        trial_join = _table_exists(connection, "trial_experiments")
        if trial_join:
            query = f"""
                SELECT r.content_hash, r.channel_id, r.lang, r.clip_dir,
                       r.media_path, r.source_video, r.title, r.caption,
                       r.status, r.scheduled_at, r.published_at, r.media_id,
                       r.permalink, r.manifest_path, r.created_at, r.updated_at,
                       r.trial_reel, r.trial_graduation_strategy,
                       t.experiment_id, t.case_type AS trial_case_type,
                       t.parent_content_hash, t.parent_media_id,
                       t.asset_family_id, t.baseline_hook, t.variant_hook,
                       t.changed_variables_json, t.state AS trial_state
                FROM reels AS r
                LEFT JOIN trial_experiments AS t
                  ON t.content_hash=r.content_hash
                 AND t.channel_id=r.channel_id
                WHERE r.channel_id=?
                  AND r.status IN ({placeholders})
                ORDER BY r.scheduled_at, r.content_hash
            """
        else:
            query = f"""
                SELECT r.content_hash, r.channel_id, r.lang, r.clip_dir,
                       r.media_path, r.source_video, r.title, r.caption,
                       r.status, r.scheduled_at, r.published_at, r.media_id,
                       r.permalink, r.manifest_path, r.created_at, r.updated_at,
                       r.trial_reel, r.trial_graduation_strategy,
                       NULL AS experiment_id, NULL AS trial_case_type,
                       NULL AS parent_content_hash, NULL AS parent_media_id,
                       NULL AS asset_family_id, NULL AS baseline_hook,
                       NULL AS variant_hook, NULL AS changed_variables_json,
                       NULL AS trial_state
                FROM reels AS r
                WHERE r.channel_id=?
                  AND r.status IN ({placeholders})
                ORDER BY r.scheduled_at, r.content_hash
            """
        rows = connection.execute(
            query,
            (channel_id, *clean_statuses),
        ).fetchall()
    return [{key: row[key] for key in row.keys()} for row in rows]


def _changed_variables(value: Any) -> list[str]:
    if not _text(value):
        return []
    try:
        payload = json.loads(_text(value))
    except json.JSONDecodeError:
        return []
    return [
        _text(item)
        for item in _sequence(payload)
        if _text(item)
    ]


def normalize_scheduled_sources(
    rows: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
    *,
    db_path: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[_text(row.get("source_video"))].append(row)

    sources: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    coverage: Counter[str] = Counter()
    for video_id, source_rows in sorted(
        grouped.items(),
        key=lambda item: (
            min(_text(row.get("scheduled_at")) for row in item[1]),
            item[0],
        ),
    ):
        first_clip_dir = Path(_text(source_rows[0].get("clip_dir"))).expanduser()
        source_root = first_clip_dir.parent.parent
        candidates_path = source_root / "candidates.json"
        metadata_path = source_root / "metadata.json"
        payload = _read_json_object(candidates_path)
        metadata = _read_json_object(metadata_path)
        clips_by_slug = {
            _text(clip.get("slug")): clip
            for raw in _sequence(payload.get("clips"))
            if (clip := _mapping(raw)) and _text(clip.get("slug"))
        }
        source_url = _text(metadata.get("webpage_url"))
        normalized_candidates: list[dict[str, Any]] = []
        for position, row in enumerate(
            sorted(
                source_rows,
                key=lambda value: (
                    _text(value.get("scheduled_at")),
                    _text(value.get("content_hash")),
                ),
            ),
            start=1,
        ):
            clip_dir = Path(_text(row.get("clip_dir"))).expanduser()
            slug = clip_dir.name
            notes_path = clip_dir / "notes.json"
            manifest_path = Path(_text(row.get("manifest_path"))).expanduser()
            media_path = Path(_text(row.get("media_path"))).expanduser()
            manifest = _read_json_object(manifest_path)
            clip = clips_by_slug.get(slug)
            candidate_origin = "scheduled_candidates_exact_match"
            if clip:
                coverage["candidate_exact_match"] += 1
            else:
                clip = _read_json_object(notes_path)
                candidate_origin = "scheduled_notes_fallback"
                if clip:
                    coverage["notes_fallback"] += 1
                else:
                    clip = {}
                    issues.append(
                        {
                            "content_hash": _text(row.get("content_hash")),
                            "issue": "MISSING_CANDIDATE_AND_NOTES",
                            "clip_dir": str(clip_dir),
                        }
                    )
            if notes_path.is_file():
                coverage["notes"] += 1
            if candidates_path.is_file():
                coverage["candidates_file"] += 1
            if metadata_path.is_file():
                coverage["metadata"] += 1
            if manifest_path.is_file():
                coverage["manifest"] += 1
            if media_path.is_file():
                coverage["media"] += 1
            normalized = evaluator.normalize_clip_candidate(
                clip,
                position=position,
                video_id=video_id or source_root.name,
                source_url=(
                    source_url
                    or _text(manifest.get("source_url"))
                ),
                metadata={
                    "title": (
                        _text(metadata.get("title"))
                        or _text(manifest.get("source_title"))
                    ),
                    "uploader": (
                        _text(metadata.get("uploader"))
                        or _text(manifest.get("source_uploader"))
                    ),
                },
                config=config,
                origin=candidate_origin,
            )
            content_hash = _text(row.get("content_hash"))
            normalized["candidate_id"] = (
                f"scheduled:{_text(row.get('channel_id'))}:{content_hash}"
            )
            normalized["localized_scheduled_hook"] = (
                _text(row.get("title"))
                or _text(row.get("caption")).splitlines()[0]
            )
            normalized["schedule"] = {
                "content_hash": content_hash,
                "channel_id": _text(row.get("channel_id")),
                "status": _text(row.get("status")),
                "scheduled_at": _text(row.get("scheduled_at")),
                "current_lane": (
                    "trial" if bool(row.get("trial_reel")) else "regular"
                ),
                "trial_reel": bool(row.get("trial_reel")),
                "trial_graduation_strategy": _text(
                    row.get("trial_graduation_strategy")
                )
                or None,
                "title": _text(row.get("title")),
                "caption_hook": _text(row.get("caption")).splitlines()[0],
                "clip_slug": slug,
                "clip_dir": str(clip_dir),
                "notes_path": str(notes_path),
                "media_path": str(media_path),
                "manifest_path": str(manifest_path),
                "candidate_file": str(candidates_path),
                "source_match": candidate_origin,
                "experiment": {
                    "experiment_id": _text(row.get("experiment_id")) or None,
                    "case_type": _text(row.get("trial_case_type")) or None,
                    "parent_content_hash": (
                        _text(row.get("parent_content_hash")) or None
                    ),
                    "parent_media_id": _text(row.get("parent_media_id")) or None,
                    "asset_family_id": _text(row.get("asset_family_id")) or None,
                    "baseline_hook": _text(row.get("baseline_hook")) or None,
                    "variant_hook": _text(row.get("variant_hook")) or None,
                    "changed_variables": _changed_variables(
                        row.get("changed_variables_json")
                    ),
                    "state": _text(row.get("trial_state")) or None,
                },
            }
            normalized_candidates.append(normalized)

        sources.append(
            {
                "video_id": video_id or source_root.name,
                "title": _text(metadata.get("title")),
                "uploader": _text(metadata.get("uploader")),
                "url": source_url,
                "candidate_file": str(candidates_path.resolve()),
                "candidate_file_sha256": (
                    evaluator.sha256_file(candidates_path)
                    if candidates_path.is_file()
                    else None
                ),
                "metadata_file": (
                    str(metadata_path.resolve())
                    if metadata_path.is_file()
                    else None
                ),
                "selection_mode": _text(payload.get("selection_mode")),
                "caption_source": _text(payload.get("caption_source")),
                "selection_profile": _text(payload.get("selection_profile")),
                "selection_profile_version": payload.get(
                    "selection_profile_version"
                ),
                "candidate_reconciliation_version": payload.get(
                    "candidate_reconciliation_version"
                ),
                "prompt_versions": dict(_mapping(payload.get("prompt_versions"))),
                "prompt_lineage_sha256": _text(
                    payload.get("prompt_lineage_sha256")
                ),
                "candidate_count": len(normalized_candidates),
                "status": "AVAILABLE",
                "input_scope": "scheduled_pipeline_exact_clips",
                "candidates": normalized_candidates,
            }
        )

    total = len(rows)
    coverage_payload = {
        key: {
            "count": int(coverage.get(key, 0)),
            "total": total,
            "percentage": (
                round(coverage.get(key, 0) / total * 100, 1)
                if total
                else 0.0
            ),
        }
        for key in (
            "candidate_exact_match",
            "notes",
            "candidates_file",
            "metadata",
            "manifest",
            "media",
        )
    }
    return sources, {
        "db_path": str(db_path.expanduser().resolve()),
        "scheduled_row_count": total,
        "source_count": len(sources),
        "coverage": coverage_payload,
        "issues": issues,
    }


def prepare_scheduled_sources_for_llm(
    sources: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Use the actual scheduled hook while preserving source-hook provenance."""

    prepared: list[dict[str, Any]] = []
    for source in sources:
        candidates: list[dict[str, Any]] = []
        for raw_candidate in _sequence(source.get("candidates")):
            candidate = dict(_mapping(raw_candidate))
            source_selection_hook = _text(candidate.get("hook"))
            scheduled_hook = _text(
                candidate.get("localized_scheduled_hook")
            )
            candidate["source_selection_hook"] = (
                source_selection_hook or None
            )
            candidate["hook"] = scheduled_hook or source_selection_hook
            candidates.append(candidate)
        prepared.append(
            {
                **dict(source),
                "candidates": candidates,
                "candidate_count": len(candidates),
                "input_scope": "scheduled_pipeline_exact_clips",
            }
        )
    return prepared


def _schedule_action(evaluation: Mapping[str, Any]) -> tuple[str, str]:
    candidate = _mapping(evaluation.get("candidate"))
    schedule = _mapping(candidate.get("schedule"))
    decision = _mapping(evaluation.get("decision"))
    recommendation = _text(decision.get("recommendation"))
    evidence_status = _text(
        _mapping(evaluation.get("evidence_status")).get("status")
    )
    reason = _text(decision.get("reason"))
    if bool(schedule.get("trial_reel")):
        return (
            ACTION_KEEP_TRIAL,
            (
                "This is already a registered Trial Reel. Keep its one-variable "
                "experiment context; the source-candidate screen does not "
                "evaluate the localized overlay-hook variant."
            ),
        )
    if recommendation == evaluator.DECISION_REPLICATION:
        return (
            ACTION_KEEP_REGULAR,
            "Strongest available analogue support; still not causal proof.",
        )
    if recommendation == evaluator.DECISION_NOVEL:
        if evidence_status in {
            "DEVELOPING_ANALOGUE_SET",
            "THIN_ANALOGUE_SET",
        }:
            return (
                ACTION_KEEP_REGULAR,
                (
                    f"{evidence_status.replace('_', ' ').title()} supports "
                    "retaining the regular slot, but the format is not proven."
                ),
            )
        return (
            ACTION_TRIAL_CANDIDATE,
            (
                "The source and readiness gates pass, but no relevant measured "
                "24-hour analogue exists. Trial distribution can test audience "
                "fit without calling the concept a winner."
            ),
        )
    if recommendation == evaluator.DECISION_REVISE:
        if "unsupported exact hook anchor" in reason:
            return (
                ACTION_SOURCE_REVIEW,
                (
                    "The exact-anchor screen needs semantic/editorial review; "
                    "do not remove the Reel from this automated signal alone."
                ),
            )
        return (
            ACTION_REVISE,
            (
                "A known hook, value, or opening score is below the configured "
                "readiness threshold. Revise the creative before considering "
                "Trial distribution."
            ),
        )
    if recommendation == evaluator.DECISION_INSUFFICIENT:
        if "selection score unavailable" in reason:
            return (
                ACTION_RESCORE,
                (
                    "Historical readiness metadata is missing. Missing data is "
                    "not a performance failure and is not removal evidence."
                ),
            )
        return ACTION_MANUAL, reason
    if recommendation == evaluator.DECISION_HOLD:
        return (
            ACTION_DIVERSITY,
            (
                "The coarse concept-overlap screen found a nearby scheduled "
                "candidate. Review both manually; this is not duplicate proof."
            ),
        )
    return ACTION_MANUAL, reason or "No schedule rule matched."


def apply_schedule_triage(
    report: dict[str, Any],
    *,
    input_audit: Mapping[str, Any],
    official_trial_selection: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    current_lanes: Counter[str] = Counter()
    for source in _sequence(report.get("sources")):
        source_map = _mapping(source)
        for evaluation_value in _sequence(source_map.get("evaluations")):
            evaluation = (
                evaluation_value
                if isinstance(evaluation_value, dict)
                else {}
            )
            candidate = _mapping(evaluation.get("candidate"))
            schedule = _mapping(candidate.get("schedule"))
            action, action_reason = _schedule_action(evaluation)
            evaluation["schedule_triage"] = {
                "action": action,
                "reason": action_reason,
                "automatic_schedule_mutation": False,
                "safe_automatic_removal": False,
            }
            analogues = [
                _mapping(value)
                for value in _sequence(
                    evaluation.get("nearest_measured_analogues")
                )
                if _mapping(value)
            ]
            nearest = analogues[0] if analogues else {}
            decision = _mapping(evaluation.get("decision"))
            evidence = _mapping(evaluation.get("evidence_status"))
            selection_scores = _mapping(candidate.get("selection_scores"))
            row = {
                "content_hash": _text(schedule.get("content_hash")),
                "scheduled_at": _text(schedule.get("scheduled_at")),
                "current_lane": _text(schedule.get("current_lane")),
                "action": action,
                "localized_hook": _text(
                    candidate.get("localized_scheduled_hook")
                ),
                "source_hook": _text(candidate.get("hook")),
                "source_timestamp_url": _text(
                    candidate.get("source_timestamp_url")
                ),
                "source_video_id": _text(
                    _mapping(candidate.get("source")).get("video_id")
                ),
                "source_uploader": _text(source_map.get("uploader")),
                "base_decision": _text(decision.get("recommendation")),
                "base_decision_reason": _text(decision.get("reason")),
                "evidence_status": _text(evidence.get("status")),
                "action_reason": action_reason,
                "overall_score": _number(selection_scores.get("overall")),
                "hook_score": _number(selection_scores.get("hook")),
                "value_score": _number(selection_scores.get("value")),
                "opening_score": _number(selection_scores.get("opening")),
                "nearest_winner_hook": _text(
                    nearest.get("published_hook")
                ),
                "nearest_winner_permalink": _text(nearest.get("permalink")),
                "nearest_winner_role": _text(
                    nearest.get("analogue_role")
                ),
                "manifest_path": _text(schedule.get("manifest_path")),
                "media_path": _text(schedule.get("media_path")),
                "experiment": dict(_mapping(schedule.get("experiment"))),
            }
            rows.append(row)
            counts[action] += 1
            current_lanes[_text(schedule.get("current_lane"))] += 1
    rows.sort(key=lambda row: (row["scheduled_at"], row["content_hash"]))

    generated_at_text = _text(
        _mapping(report.get("report_metadata")).get("generated_at")
    )
    try:
        generated_at = datetime.fromisoformat(
            generated_at_text.replace("Z", "+00:00")
        )
    except ValueError:
        generated_at = None
    regular_future_days: list[float] = []
    if generated_at is not None and generated_at.tzinfo is not None:
        for row in rows:
            if row.get("current_lane") != "regular":
                continue
            try:
                scheduled_at = datetime.fromisoformat(
                    _text(row.get("scheduled_at")).replace("Z", "+00:00")
                )
            except ValueError:
                continue
            if scheduled_at.tzinfo is None:
                continue
            regular_future_days.append(
                (scheduled_at - generated_at).total_seconds() / 86_400
            )
    horizon = {
        "regular_reels": sum(
            row.get("current_lane") == "regular" for row in rows
        ),
        "more_than_14_days_away": sum(
            value > 14 for value in regular_future_days
        ),
        "more_than_30_days_away": sum(
            value > 30 for value in regular_future_days
        ),
        "warning": (
            "Long-horizon labels are provisional because the measured winner "
            "library and news freshness can change before publication."
        ),
    }

    selector_payload = dict(official_trial_selection or {})
    selected_hash = _text(selector_payload.get("content_hash"))
    matching = next(
        (row for row in rows if row["content_hash"] == selected_hash),
        None,
    )
    if selector_payload:
        selector_payload["candidate_evaluator"] = {
            "matched": matching is not None,
            "action": matching.get("action") if matching else None,
            "agreement": (
                bool(matching)
                and matching.get("action") == ACTION_TRIAL_CANDIDATE
            ),
            "warning": (
                "The official selector enforces capacity, cooldowns, dates, and "
                "ledger guards. Conversion is still a separate approved action."
            ),
        }

    report["report_metadata"]["input_scope"] = "scheduled_pipeline"
    report["report_metadata"]["scheduled_row_count"] = len(rows)
    report["scheduled_pipeline"] = {
        "schema_version": SCHEMA_VERSION,
        "status": "READ_ONLY_REVIEW",
        "schedule_mutated": False,
        "safe_automatic_removal_count": 0,
        "current_lane_counts": dict(sorted(current_lanes.items())),
        "action_counts": dict(sorted(counts.items())),
        "schedule_horizon": horizon,
        "input_audit": dict(input_audit),
        "official_trial_selection": selector_payload or None,
        "rows": rows,
        "interpretation": {
            ACTION_KEEP_TRIAL: (
                "Existing one-variable Trial experiment; preserve its experiment "
                "metadata and evaluate after publishing."
            ),
            ACTION_KEEP_REGULAR: (
                "Passing creative with some measured analogue support. Supported "
                "does not mean proven."
            ),
            ACTION_TRIAL_CANDIDATE: (
                "Passing creative with no relevant measured analogue; eligible "
                "for the separate capacity-aware Trial selector."
            ),
            ACTION_REVISE: (
                "Known source-candidate readiness miss. Revise before Trial; do "
                "not assume Trial distribution fixes weak creative."
            ),
            ACTION_RESCORE: (
                "Legacy metadata gap. Rescore or review; do not infer failure."
            ),
            ACTION_DIVERSITY: (
                "Coarse concept overlap only. Manual portfolio review required."
            ),
            ACTION_SOURCE_REVIEW: (
                "Exact-anchor screen needs semantic review. Not removal proof."
            ),
        },
    }
    report["summary"]["schedule_action_counts"] = dict(sorted(counts.items()))
    report["summary"]["safe_automatic_removals"] = 0
    return report


def _cell(value: Any) -> str:
    return _text(value).replace("|", "\\|").replace("\n", "<br>")


def _score(value: Any) -> str:
    number = _number(value)
    return "—" if number is None else f"{number:g}"


def render_scheduled_markdown(report: Mapping[str, Any]) -> str:
    metadata = _mapping(report.get("report_metadata"))
    scheduled = _mapping(report.get("scheduled_pipeline"))
    rows = [
        _mapping(value)
        for value in _sequence(scheduled.get("rows"))
        if _mapping(value)
    ]
    counts = _mapping(scheduled.get("action_counts"))
    audit = _mapping(scheduled.get("input_audit"))
    coverage = _mapping(audit.get("coverage"))
    lines = [
        "# AI Brief JP — scheduled Reel candidate recheck",
        "",
        f"Generated from the current 24-hour winner library: **{_cell(metadata.get('generated_at'))}**",
        "",
        (
            f"Scope: **{len(rows)} scheduled Reels** across "
            f"**{int(_number(audit.get('source_count')) or 0)} source videos**. "
            "This report is read-only; it did not remove, convert, reschedule, "
            "or publish anything."
        ),
        "",
        "## Decision boundary",
        "",
        (
            "**Safe automatic removals: 0.** Candidate similarity and source "
            "scores are useful for triage, but they do not predict performance. "
            "Missing legacy scores remain unavailable rather than being treated "
            "as failures."
        ),
    ]
    horizon = _mapping(scheduled.get("schedule_horizon"))
    if horizon:
        lines.extend(
            [
                "",
                (
                    f"Of **{int(_number(horizon.get('regular_reels')) or 0)}** "
                    "regular Reels, "
                    f"**{int(_number(horizon.get('more_than_14_days_away')) or 0)}** "
                    "are more than 14 days away and "
                    f"**{int(_number(horizon.get('more_than_30_days_away')) or 0)}** "
                    "are more than 30 days away. Tail decisions are provisional "
                    "and should be rerun close to publication."
                ),
            ]
        )
    lines.extend(
        [
            "",
            "## Queue summary",
            "",
            "| Recommendation | Reels |",
            "|---|---:|",
        ]
    )
    action_order = (
        ACTION_KEEP_TRIAL,
        ACTION_KEEP_REGULAR,
        ACTION_TRIAL_CANDIDATE,
        ACTION_REVISE,
        ACTION_RESCORE,
        ACTION_SOURCE_REVIEW,
        ACTION_DIVERSITY,
        ACTION_MANUAL,
    )
    for action in action_order:
        count = int(_number(counts.get(action)) or 0)
        if count:
            lines.append(f"| {_cell(action)} | {count} |")

    lines.extend(
        [
            "",
            "## Input coverage",
            "",
            "| Field | Coverage |",
            "|---|---:|",
        ]
    )
    for key, label in (
        ("candidate_exact_match", "Exact scheduled clip → candidates.json match"),
        ("notes", "Per-clip notes"),
        ("metadata", "Source metadata"),
        ("manifest", "Schedule manifest"),
        ("media", "Rendered media"),
    ):
        item = _mapping(coverage.get(key))
        lines.append(
            f"| {label} | {int(_number(item.get('count')) or 0)}/"
            f"{int(_number(item.get('total')) or 0)} "
            f"({_score(item.get('percentage'))}%) |"
        )

    selector = _mapping(scheduled.get("official_trial_selection"))
    if selector:
        selector_eval = _mapping(selector.get("candidate_evaluator"))
        selected = _mapping(selector.get("selected"))
        lines.extend(
            [
                "",
                "## Next capacity-aware Trial conversion",
                "",
                (
                    f"- Selector status: **{_cell(selector.get('status'))}**; "
                    f"target date: **{_cell(selector.get('target_date'))}**"
                ),
                (
                    f"- Selected: **{_cell(selected.get('title'))}** "
                    f"(`{_cell(selector.get('content_hash'))[:12]}…`)"
                ),
                (
                    f"- Candidate-evaluator action: "
                    f"**{_cell(selector_eval.get('action'))}**; "
                    f"agreement: **{'yes' if selector_eval.get('agreement') else 'no'}**"
                ),
                (
                    "- Important: converting the Instagram Reel to Trial removes "
                    "its mutable Facebook mirror from the regular cross-platform "
                    "lane."
                ),
            ]
        )

    for action in action_order:
        selected_rows = [row for row in rows if row.get("action") == action]
        if not selected_rows:
            continue
        lines.extend(
            [
                "",
                f"## {action} ({len(selected_rows)})",
                "",
                "| Scheduled | Current | Japanese scheduled hook | Source hook | Scores H/V/O | Evidence | Nearest measured winner |",
                "|---|---|---|---|---:|---|---|",
            ]
        )
        for row in selected_rows:
            source_hook = _cell(row.get("source_hook"))
            source_url = _text(row.get("source_timestamp_url"))
            if source_url:
                source_hook = f"[{source_hook}]({source_url})"
            winner_hook = _cell(row.get("nearest_winner_hook")) or "—"
            winner_url = _text(row.get("nearest_winner_permalink"))
            if winner_url:
                winner_hook = f"[{winner_hook}]({winner_url})"
            lines.append(
                "| "
                + " | ".join(
                    [
                        _cell(row.get("scheduled_at")),
                        _cell(row.get("current_lane")),
                        _cell(row.get("localized_hook")),
                        source_hook,
                        "/".join(
                            [
                                _score(row.get("hook_score")),
                                _score(row.get("value_score")),
                                _score(row.get("opening_score")),
                            ]
                        ),
                        _cell(row.get("evidence_status")),
                        winner_hook,
                    ]
                )
                + " |"
            )
        lines.extend(
            [
                "",
                f"Interpretation: {_cell(_mapping(scheduled.get('interpretation')).get(action))}",
            ]
        )

    lines.extend(
        [
            "",
            "## How to use this report",
            "",
            "1. Keep registered Trials intact and review their parent/variant results after publishing.",
            "2. Send only `TRIAL CANDIDATE` rows through the existing capacity-aware daily Trial selector.",
            "3. Rewrite `REVISE CREATIVE` rows before deciding whether to retain their slots.",
            "4. Rescore `RESCORE / MANUAL REVIEW` rows; do not remove them because historical metadata is missing.",
            "5. Rerun this audit about three days before publication because the winner library and news freshness change.",
            "",
        ]
    )
    return "\n".join(lines)


def render_scheduled_csv(report: Mapping[str, Any]) -> str:
    scheduled = _mapping(report.get("scheduled_pipeline"))
    rows = [
        _mapping(value)
        for value in _sequence(scheduled.get("rows"))
        if _mapping(value)
    ]
    fields = [
        "content_hash",
        "scheduled_at",
        "current_lane",
        "action",
        "localized_hook",
        "source_hook",
        "source_timestamp_url",
        "source_video_id",
        "source_uploader",
        "base_decision",
        "evidence_status",
        "overall_score",
        "hook_score",
        "value_score",
        "opening_score",
        "nearest_winner_hook",
        "nearest_winner_permalink",
        "action_reason",
        "base_decision_reason",
        "manifest_path",
        "media_path",
    ]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(dict(row))
    return buffer.getvalue()
