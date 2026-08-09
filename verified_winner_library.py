#!/usr/bin/env python3
"""Build a grounded hook-and-script library from Moneyball leaderboards.

The library is a snapshot of measured fixed-window leaderboard membership.
It does not claim that a hook caused performance or that a listed Reel will
generalize to future candidates.
"""

from __future__ import annotations

import hashlib
import html
import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qs, urlparse

import moneyball_analytics as moneyball


SCHEMA_VERSION = 1
PLATFORM = "instagram"
SIGNAL_FAMILIES = {
    "intent_action": {
        "label": "Intent / action",
        "metrics": [
            "total_interactions_per_reach",
            "saves_per_1000_reach",
        ],
        "boundary": (
            "Meta total_interactions includes saves, so these two rankings are "
            "correlated and count as one evidence family."
        ),
    },
    "attention_replay": {
        "label": "Attention / replay",
        "metrics": [
            "watch_depth",
            "three_second_skip_rate",
            "views_per_reached_account",
        ],
        "boundary": (
            "Watch depth, three-second skip, and views/reached are related "
            "attention signals; watch depth also remains duration-sensitive."
        ),
    },
}
METRIC_TO_FAMILY = {
    metric: family
    for family, definition in SIGNAL_FAMILIES.items()
    for metric in definition["metrics"]
}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _text(value: Any) -> str:
    return str(value or "").strip()


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _first_caption_line(caption: Any) -> str:
    for line in str(caption or "").splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def _read_json(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _youtube_video_id(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    if re.fullmatch(r"[\w-]{6,}", text) and "://" not in text:
        return text
    parsed = urlparse(text)
    host = parsed.netloc.casefold()
    if host.endswith("youtu.be"):
        return parsed.path.strip("/").split("/", 1)[0]
    if "youtube.com" in host:
        query = parse_qs(parsed.query)
        values = query.get("v")
        if values:
            return values[0]
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live"}:
            return parts[1]
    return ""


def _ass_seconds(value: str) -> float | None:
    match = re.fullmatch(r"(\d+):(\d{2}):(\d{2}(?:\.\d+)?)", value.strip())
    if not match:
        return None
    return (
        float(match.group(1)) * 3600
        + float(match.group(2)) * 60
        + float(match.group(3))
    )


def _plain_ass_text(value: str) -> str:
    value = re.sub(r"\{[^}]*\}", "", value)
    value = value.replace(r"\N", " ").replace(r"\n", " ").replace(r"\h", " ")
    return re.sub(r"\s+", " ", value).strip()


def read_ass_segments(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError:
        return []
    segments: list[dict[str, Any]] = []
    for raw_line in lines:
        if not raw_line.startswith("Dialogue:"):
            continue
        payload = raw_line.removeprefix("Dialogue:").lstrip()
        parts = payload.split(",", 9)
        if len(parts) != 10:
            continue
        text = _plain_ass_text(parts[9])
        if not text:
            continue
        segment = {
            "start_seconds": _ass_seconds(parts[1]),
            "end_seconds": _ass_seconds(parts[2]),
            "text": text,
        }
        if not segments or segments[-1] != segment:
            segments.append(segment)
    return segments


def _clip_index(value: Path) -> str:
    match = re.match(r"^(\d{3})(?:-|$)", value.name)
    return match.group(1) if match else ""


def resolve_clip_dir(post: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve a generation directory without silently fuzzy-matching content."""
    artifact = _mapping(post.get("generation_artifact"))
    stored_text = _text(artifact.get("clip_dir"))
    notes_text = _text(artifact.get("notes_path"))
    manifest_path = Path(_text(artifact.get("manifest_path"))) if _text(
        artifact.get("manifest_path")
    ) else None
    manifest = _read_json(manifest_path)

    candidates: list[tuple[Path, str]] = []
    if stored_text:
        candidates.append((Path(stored_text), "stored_clip_dir"))
    if notes_text:
        candidates.append((Path(notes_text).parent, "stored_notes_parent"))
    manifest_clip = _text(manifest.get("clip_dir"))
    if manifest_clip:
        candidates.append((Path(manifest_clip), "manifest_clip_dir"))
    for slide in _list(manifest.get("slides")):
        slide = _mapping(slide)
        slide_path = _text(slide.get("path"))
        if slide_path:
            candidates.append((Path(slide_path).parent, "manifest_media_parent"))

    seen: set[Path] = set()
    for path, method in candidates:
        path = path.expanduser()
        if path in seen:
            continue
        seen.add(path)
        if path.is_dir() and (
            (path / "notes.json").is_file()
            or (path / "subtitles.ja.ass").is_file()
        ):
            return {
                "status": "RESOLVED",
                "path": str(path.resolve()),
                "resolution_method": method,
                "confidence": "high",
                "original_stale_path": None,
                "ambiguity_note": None,
            }

    stale_paths = [path for path, _ in candidates if _clip_index(path)]
    for stale_path in stale_paths:
        parent = stale_path.expanduser().parent
        index = _clip_index(stale_path)
        if not parent.is_dir() or not index:
            continue
        replacements = sorted(
            path
            for path in parent.glob(f"{index}-*")
            if path.is_dir()
            and (
                (path / "notes.json").is_file()
                or (path / "subtitles.ja.ass").is_file()
            )
        )
        if len(replacements) == 1:
            replacement = replacements[0].resolve()
            return {
                "status": "RESOLVED",
                "path": str(replacement),
                "resolution_method": "source_video_clip_index_unique",
                "confidence": "medium",
                "original_stale_path": str(stale_path),
                "ambiguity_note": (
                    "The stored clip slug is stale. A unique directory with the "
                    f"same source-video clip index ({index}) supplies the script, "
                    "but the match is not treated as cryptographic proof."
                ),
            }

    return {
        "status": "UNAVAILABLE",
        "path": None,
        "resolution_method": None,
        "confidence": "unavailable",
        "original_stale_path": stored_text or None,
        "ambiguity_note": "No exact or unique clip-index artifact was available.",
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _published_media_candidates(
    post: Mapping[str, Any], resolved_clip_dir: Path | None
) -> list[Path]:
    artifact = _mapping(post.get("generation_artifact"))
    manifest_path = Path(_text(artifact.get("manifest_path"))) if _text(
        artifact.get("manifest_path")
    ) else None
    manifest = _read_json(manifest_path)
    candidates: list[Path] = []
    for slide in _list(manifest.get("slides")):
        path_text = _text(_mapping(slide).get("path"))
        if path_text:
            candidates.append(Path(path_text))
    if resolved_clip_dir is not None:
        candidates.append(resolved_clip_dir / "reel.ja.aibrief_jp.mp4")
    seen: set[Path] = set()
    return [
        path
        for path in candidates
        if not (path in seen or seen.add(path))
    ]


def verify_published_asset(
    post: Mapping[str, Any], resolved_clip_dir: Path | None
) -> dict[str, Any]:
    identity = _mapping(post.get("identity"))
    expected = _text(identity.get("content_hash"))
    mismatches: list[dict[str, str]] = []
    for path in _published_media_candidates(post, resolved_clip_dir):
        if not path.is_file():
            continue
        actual = _sha256_file(path)
        if expected and actual == expected:
            return {
                "status": "VERIFIED",
                "path": str(path.resolve()),
                "expected_sha256": expected,
                "actual_sha256": actual,
            }
        mismatches.append(
            {
                "path": str(path.resolve()),
                "actual_sha256": actual,
            }
        )
    if mismatches:
        return {
            "status": "MISMATCH",
            "path": mismatches[0]["path"],
            "expected_sha256": expected or None,
            "actual_sha256": mismatches[0]["actual_sha256"],
        }
    return {
        "status": "UNAVAILABLE",
        "path": None,
        "expected_sha256": expected or None,
        "actual_sha256": None,
    }


def _localized_hook_options(one_liners: Mapping[str, Any]) -> list[str]:
    output: list[str] = []
    legacy = _text(one_liners.get("ja"))
    if legacy:
        output.append(legacy)
    language = _mapping(_mapping(one_liners.get("languages")).get("ja"))
    primary = _text(language.get("text"))
    if primary:
        output.append(primary)
    for value in _list(language.get("variants")):
        text = _text(value)
        if text:
            output.append(text)
    return list(dict.fromkeys(output))


def _metric_value(
    metric_key: str, observation: Mapping[str, Any]
) -> float | None:
    raw = _mapping(observation.get("raw_metrics"))
    derived = _mapping(observation.get("derived_metrics"))
    if metric_key == "total_interactions_per_reach":
        return _finite_number(derived.get("engagement_rate_by_reach"))
    if metric_key == "watch_depth":
        return _finite_number(derived.get("watch_depth"))
    if metric_key == "three_second_skip_rate":
        return _finite_number(raw.get("reels_skip_rate"))
    if metric_key == "saves_per_1000_reach":
        return _finite_number(derived.get("saves_per_1000_reach"))
    if metric_key == "views_per_reached_account":
        return _finite_number(derived.get("views_per_reached_account"))
    return None


def _supporting_metrics(
    metric_key: str, observation: Mapping[str, Any]
) -> dict[str, Any]:
    raw = _mapping(observation.get("raw_metrics"))
    derived = _mapping(observation.get("derived_metrics"))
    if metric_key == "total_interactions_per_reach":
        return {
            "interactions": _finite_number(raw.get("interactions")),
            "reach": _finite_number(raw.get("reach")),
            "denominator_type": "reach",
        }
    if metric_key == "watch_depth":
        return {
            "average_watch_time_seconds": _finite_number(
                derived.get("average_watch_time_seconds")
            ),
            "duration_seconds": _finite_number(raw.get("duration_seconds")),
        }
    if metric_key == "three_second_skip_rate":
        return {
            "reels_skip_rate": _finite_number(raw.get("reels_skip_rate")),
        }
    if metric_key == "saves_per_1000_reach":
        return {
            "saves": _finite_number(raw.get("saves")),
            "reach": _finite_number(raw.get("reach")),
            "denominator_type": "reach",
        }
    if metric_key == "views_per_reached_account":
        return {
            "views": _finite_number(raw.get("views")),
            "reach": _finite_number(raw.get("reach")),
            "denominator_type": "reach",
        }
    return {}


def _metric_definitions(
    metric_rankings: Mapping[str, Any],
) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for specification in moneyball.PERFORMANCE_RANKING_METRICS:
        key = str(specification["key"])
        bucket = _mapping(metric_rankings.get(key))
        output[key] = {
            "label": bucket.get("label") or specification["label"],
            "short_label": bucket.get("short_label")
            or specification["short_label"],
            "direction": bucket.get("direction") or specification["direction"],
            "format": bucket.get("format") or specification["format"],
            "source": bucket.get("source") or specification["source"],
            "coverage": dict(_mapping(bucket.get("coverage"))),
            "signal_family": METRIC_TO_FAMILY[key],
        }
    return output


def _winner_tier(
    aggregate: Mapping[str, Any] | None, families: Sequence[str]
) -> str:
    family_set = set(families)
    if aggregate:
        return "BALANCED_REFERENCE"
    if len(family_set) >= 2:
        return "CROSS_FAMILY_REFERENCE"
    if family_set == {"intent_action"}:
        return "INTENT_ACTION_SPECIALIST"
    if family_set == {"attention_replay"}:
        return "ATTENTION_REPLAY_SPECIALIST"
    return "MEASURED_REFERENCE"


def _coverage(count: int, total: int) -> dict[str, Any]:
    return {
        "count": count,
        "total": total,
        "percentage": (count / total * 100.0) if total else None,
    }


def _candidate_protocol() -> dict[str, Any]:
    return {
        "principle": (
            "Use the library to find measured analogues, not to predict a winner "
            "from hook resemblance alone."
        ),
        "lanes": [
            {
                "id": "ATTENTION_REPLAY_HYPOTHESIS",
                "description": (
                    "Candidate is designed to reduce early skipping, hold watch "
                    "depth, or invite replay."
                ),
            },
            {
                "id": "INTENT_ACTION_HYPOTHESIS",
                "description": (
                    "Candidate is designed to earn saves or other measured "
                    "interactions per reached account."
                ),
            },
            {
                "id": "BALANCED_HYPOTHESIS",
                "description": (
                    "Candidate intentionally combines attention and useful-action "
                    "traits seen across both evidence families."
                ),
            },
            {
                "id": "NOVEL_EXPERIMENT",
                "description": (
                    "Candidate deliberately tests a structure without a close "
                    "winner analogue."
                ),
            },
        ],
        "required_checks": [
            "Name the intended evidence lane before comparing candidates.",
            "Identify the exact credible actor, specific fact, or concrete claim in the hook.",
            "State the reversal, anomaly, conflict, or consequence that creates curiosity.",
            "Verify that the script actually delivers a mechanism, proof point, or payoff.",
            "Compare duration with the selected analogues; do not ignore duration confounding.",
            "Show the three nearest measured analogues with Reel links and actual 24-hour metrics.",
            "Use at least two distinct source videos or speakers before calling a pattern repeatable.",
            "Check source, speaker, and topic saturation before allocating another slot.",
            "Confirm claim support and full-script availability; never fill gaps by inference.",
        ],
        "decisions": [
            "ADVANCE AS REPLICATION TEST",
            "ADVANCE AS NOVEL TEST",
            "REVISE",
            "HOLD FOR DIVERSITY",
            "INSUFFICIENT EVIDENCE",
        ],
        "required_output": [
            "intended_lane",
            "nearest_three_analogues",
            "aligned_elements",
            "meaningful_differences",
            "source_and_topic_saturation",
            "claim_support_status",
            "decision",
            "reason",
        ],
        "prohibited_claims": [
            "Do not say a matching hook will cause performance.",
            "Do not treat aggregate membership as a sixth independent vote.",
            "Do not call a candidate a follower-growth winner; media-level follows are unavailable.",
            "Do not compare later outcomes at different maturity windows.",
        ],
    }


def build_winner_library(
    report: Mapping[str, Any],
    *,
    source_report_path: str | Path | None = None,
) -> dict[str, Any]:
    report_metadata = _mapping(report.get("report_metadata"))
    account = _text(report_metadata.get("account"))
    rankings = _mapping(
        _mapping(report.get("performance_rankings")).get(PLATFORM)
    )
    metric_rankings = _mapping(rankings.get("metric_rankings"))
    aggregate_rows = _list(rankings.get("aggregate_top_10"))
    maturity_window = _text(rankings.get("maturity_window")) or "24h"
    definitions = _metric_definitions(metric_rankings)

    selected: dict[str, dict[str, Any]] = {}
    placement_count = 0
    for specification in moneyball.PERFORMANCE_RANKING_METRICS:
        metric_key = str(specification["key"])
        bucket = _mapping(metric_rankings.get(metric_key))
        cohort_size = int(_finite_number(_mapping(bucket.get("coverage")).get("count")) or 0)
        for raw_row in _list(bucket.get("top_10")):
            row = _mapping(raw_row)
            media_id = _text(row.get("media_id"))
            if not media_id:
                continue
            placement_count += 1
            entry = selected.setdefault(
                media_id,
                {"memberships": [], "aggregate": None},
            )
            entry["memberships"].append(
                {
                    "leaderboard": metric_key,
                    "label": bucket.get("label") or specification["label"],
                    "rank": int(_finite_number(row.get("rank")) or 0),
                    "cohort_size": cohort_size,
                    "direction": bucket.get("direction")
                    or specification["direction"],
                    "value": _finite_number(row.get("value")),
                    "directional_percentile": _finite_number(
                        row.get("directional_percentile")
                    ),
                    "actual_age_hours": _finite_number(
                        row.get("actual_age_hours")
                    ),
                    "supporting_metrics": dict(
                        _mapping(row.get("supporting_metrics"))
                    ),
                }
            )

    for raw_row in aggregate_rows:
        row = _mapping(raw_row)
        media_id = _text(row.get("media_id"))
        if not media_id:
            continue
        placement_count += 1
        entry = selected.setdefault(
            media_id,
            {"memberships": [], "aggregate": None},
        )
        entry["aggregate"] = {
            "rank": int(_finite_number(row.get("rank")) or 0),
            "cohort_size": int(
                _finite_number(_mapping(rankings.get("methodology")).get(
                    "eligible_post_count"
                ))
                or 0
            ),
            "average_directional_percentile": _finite_number(
                row.get("average_directional_percentile")
            ),
            "actual_age_hours": _finite_number(row.get("actual_age_hours")),
            "components": dict(_mapping(row.get("components"))),
            "strong_points": [dict(_mapping(value)) for value in _list(
                row.get("strong_points")
            )],
        }

    posts_by_media_id = {
        _text(_mapping(post).get("identity", {}).get("media_id")): _mapping(post)
        for post in _list(report.get("posts"))
        if _text(_mapping(_mapping(post).get("identity")).get("media_id"))
    }
    winners: list[dict[str, Any]] = []
    for media_id, selection in selected.items():
        post = posts_by_media_id.get(media_id, {})
        identity = _mapping(post.get("identity"))
        metadata = _mapping(post.get("content_metadata"))
        artifact = _mapping(post.get("generation_artifact"))
        observation = _mapping(
            _mapping(post.get("maturity_windows")).get(maturity_window)
        )
        raw = _mapping(observation.get("raw_metrics"))

        resolution = resolve_clip_dir(post)
        resolved_path = (
            Path(_text(resolution.get("path")))
            if _text(resolution.get("path"))
            else None
        )
        notes_path = (
            resolved_path / "notes.json" if resolved_path is not None else None
        )
        notes = _read_json(notes_path)
        subtitle_path = (
            resolved_path / "subtitles.ja.ass"
            if resolved_path is not None
            else None
        )
        japanese_segments = read_ass_segments(subtitle_path)
        english_subtitle_path = (
            resolved_path / "subtitles.en.ass"
            if resolved_path is not None
            else None
        )
        english_segments = read_ass_segments(english_subtitle_path)
        notes_transcript = _text(notes.get("transcript"))
        source_transcript = notes_transcript or " ".join(
            segment["text"] for segment in english_segments
        )
        source_transcript_path = (
            notes_path
            if notes_transcript
            else english_subtitle_path
            if english_segments
            else None
        )
        one_liners_path = (
            resolved_path / "one_liners.json"
            if resolved_path is not None
            else None
        )
        one_liners = _read_json(one_liners_path)

        caption = _text(identity.get("caption"))
        published_hook = _first_caption_line(caption)
        hook_source = "published_caption_first_line"
        if not published_hook:
            published_hook = _text(metadata.get("hook_text"))
            hook_source = "generation_pipeline_hook_text"
        families = sorted(
            {
                METRIC_TO_FAMILY.get(_text(row.get("leaderboard")))
                for row in selection["memberships"]
                if METRIC_TO_FAMILY.get(_text(row.get("leaderboard")))
            }
        )
        aggregate = _mapping(selection.get("aggregate")) or None

        all_metrics: dict[str, Any] = {}
        for specification in moneyball.PERFORMANCE_RANKING_METRICS:
            metric_key = str(specification["key"])
            all_metrics[metric_key] = {
                "value": _metric_value(metric_key, observation),
                "direction": specification["direction"],
                "supporting_metrics": _supporting_metrics(
                    metric_key, observation
                ),
            }

        asset_verification = verify_published_asset(post, resolved_path)
        japanese_text = " ".join(
            segment["text"] for segment in japanese_segments
        )
        script_basis = japanese_text or source_transcript or _text(
            identity.get("content_hash")
        )
        script_asset_id = (
            hashlib.sha256(script_basis.encode("utf-8")).hexdigest()[:16]
            if script_basis
            else None
        )
        transcript_confidence = (
            "high"
            if japanese_segments
            and _text(resolution.get("confidence")) == "high"
            else "medium"
            if japanese_segments
            else "unavailable"
        )

        flags: list[str] = []
        reach = _finite_number(raw.get("reach"))
        if reach is not None and reach < 100:
            flags.append("LOW_BASE_REACH")
        membership_keys = {
            _text(row.get("leaderboard")) for row in selection["memberships"]
        }
        interactions = _finite_number(raw.get("interactions"))
        if (
            "total_interactions_per_reach" in membership_keys
            and interactions is not None
            and interactions < 5
        ):
            flags.append("LOW_INTERACTION_COUNT")
        saves = _finite_number(raw.get("saves"))
        if (
            "saves_per_1000_reach" in membership_keys
            and saves is not None
            and saves < 5
        ):
            flags.append("LOW_SAVE_COUNT")
        if transcript_confidence == "medium":
            flags.append("TRANSCRIPT_MEDIUM_CONFIDENCE")
        if not japanese_segments:
            flags.append("JAPANESE_SCRIPT_UNAVAILABLE")
        if asset_verification["status"] == "MISMATCH":
            flags.append("PUBLISHED_ASSET_HASH_MISMATCH")
        if asset_verification["status"] == "UNAVAILABLE":
            flags.append("PUBLISHED_ASSET_UNAVAILABLE")

        source_url = _text(metadata.get("source"))
        winners.append(
            {
                "identity": {
                    "media_id": media_id,
                    "permalink": identity.get("permalink"),
                    "published_at": identity.get("published_at"),
                    "content_hash": identity.get("content_hash"),
                    "caption": caption or None,
                },
                "source": {
                    "url": source_url or None,
                    "video_id": _youtube_video_id(source_url) or None,
                    "title": _text(artifact.get("source_title")) or None,
                    "uploader": _text(artifact.get("source_uploader")) or None,
                    "chapter": _text(notes.get("source_chapter")) or None,
                },
                "content": {
                    "published_hook": {
                        "value": published_hook or None,
                        "source": hook_source,
                        "confidence": "high" if published_hook else "unavailable",
                    },
                    "opening_japanese_script": [
                        segment["text"]
                        for segment in japanese_segments
                        if segment.get("start_seconds") is None
                        or float(segment["start_seconds"]) < 3.1
                    ]
                    or (
                        [japanese_segments[0]["text"]]
                        if japanese_segments
                        else []
                    ),
                    "japanese_script": {
                        "status": (
                            "AVAILABLE" if japanese_segments else "UNAVAILABLE"
                        ),
                        "text": japanese_text or None,
                        "segments": japanese_segments,
                        "source_path": (
                            str(subtitle_path.resolve())
                            if subtitle_path is not None
                            and subtitle_path.is_file()
                            else None
                        ),
                        "confidence": transcript_confidence,
                    },
                    "source_transcript": {
                        "status": (
                            "AVAILABLE" if source_transcript else "UNAVAILABLE"
                        ),
                        "text": source_transcript or None,
                        "source_path": (
                            str(source_transcript_path.resolve())
                            if source_transcript_path is not None
                            and source_transcript_path.is_file()
                            else None
                        ),
                    },
                    "source_selection_hook": _text(notes.get("one_liner"))
                    or None,
                    "source_hook_variants": [
                        _text(value)
                        for value in _list(notes.get("hook_variants"))
                        if _text(value)
                    ],
                    "current_localized_hook_options": _localized_hook_options(
                        one_liners
                    ),
                    "duration_seconds": (
                        _finite_number(raw.get("duration_seconds"))
                        or _finite_number(notes.get("duration"))
                    ),
                    "script_asset_id": script_asset_id,
                },
                "winner_evidence": {
                    "definition": (
                        f"Current member of at least one {maturity_window} "
                        "Moneyball Top 10."
                    ),
                    "tier": _winner_tier(aggregate, families),
                    "maturity_window": maturity_window,
                    "actual_age_hours": _finite_number(
                        observation.get("actual_age_hours")
                    ),
                    "metric_top_10_count": len(selection["memberships"]),
                    "ranking_placement_count": len(selection["memberships"])
                    + (1 if aggregate else 0),
                    "ranking_memberships": sorted(
                        selection["memberships"],
                        key=lambda value: (
                            int(value.get("rank") or 999),
                            _text(value.get("leaderboard")),
                        ),
                    ),
                    "aggregate": aggregate,
                    "signal_families": families,
                    "independent_family_count": len(families),
                    "all_metrics_at_window": all_metrics,
                },
                "asset_provenance": {
                    "clip_resolution": resolution,
                    "published_asset": asset_verification,
                },
                "evidence_flags": flags,
            }
        )

    source_counts = Counter(
        _text(winner["source"].get("video_id") or winner["source"].get("url"))
        for winner in winners
        if _text(winner["source"].get("video_id") or winner["source"].get("url"))
    )
    uploader_counts = Counter(
        _text(winner["source"].get("uploader"))
        for winner in winners
        if _text(winner["source"].get("uploader"))
    )
    script_counts = Counter(
        _text(winner["content"].get("script_asset_id"))
        for winner in winners
        if _text(winner["content"].get("script_asset_id"))
    )
    for winner in winners:
        source_key = _text(
            winner["source"].get("video_id") or winner["source"].get("url")
        )
        script_key = _text(winner["content"].get("script_asset_id"))
        if source_key and source_counts[source_key] > 1:
            winner["evidence_flags"].append("SOURCE_REPEATED_IN_POOL")
        if script_key and script_counts[script_key] > 1:
            winner["evidence_flags"].append("SCRIPT_REUSED_IN_POOL")

    winners.sort(
        key=lambda winner: (
            0
            if _mapping(winner["winner_evidence"].get("aggregate"))
            else 1,
            int(
                _finite_number(
                    _mapping(winner["winner_evidence"].get("aggregate")).get(
                        "rank"
                    )
                )
                or 999
            ),
            -int(winner["winner_evidence"]["independent_family_count"]),
            -int(winner["winner_evidence"]["metric_top_10_count"]),
            min(
                [
                    int(row.get("rank") or 999)
                    for row in winner["winner_evidence"][
                        "ranking_memberships"
                    ]
                ]
                or [999]
            ),
            _text(winner["identity"].get("media_id")),
        )
    )

    total = len(winners)
    hook_count = sum(
        bool(_text(winner["content"]["published_hook"].get("value")))
        for winner in winners
    )
    japanese_count = sum(
        winner["content"]["japanese_script"]["status"] == "AVAILABLE"
        for winner in winners
    )
    source_transcript_count = sum(
        winner["content"]["source_transcript"]["status"] == "AVAILABLE"
        for winner in winners
    )
    verified_asset_count = sum(
        winner["asset_provenance"]["published_asset"]["status"] == "VERIFIED"
        for winner in winners
    )
    methodology = _mapping(rankings.get("methodology"))
    return {
        "library_metadata": {
            "schema_version": SCHEMA_VERSION,
            "account": account,
            "platform": PLATFORM,
            "generated_at": report_metadata.get("generated_at"),
            "generated_at_jst": report_metadata.get("generated_at_jst"),
            "source_report": (
                str(Path(source_report_path).expanduser().resolve())
                if source_report_path
                else None
            ),
            "source_report_generated_at": report_metadata.get("generated_at"),
            "maturity_window": maturity_window,
            "fixed_window_cohort_size": int(
                _finite_number(rankings.get("cohort_size")) or 0
            ),
            "complete_five_metric_cohort_size": int(
                _finite_number(methodology.get("eligible_post_count")) or 0
            ),
            "leaderboard_count": len(metric_rankings) + 1,
            "ranking_placement_count": placement_count,
            "unique_winner_posts": total,
            "unique_script_assets": len(script_counts),
            "winner_definition": (
                "Union of every current Instagram metric Top 10 and the "
                f"balanced aggregate Top 10 at the fixed {maturity_window} "
                "maturity window."
            ),
            "evidence_boundary": (
                "Verified means measured leaderboard membership in this report "
                "snapshot. It does not mean causal proof, repeatability, follower "
                "conversion, or guaranteed future performance."
            ),
        },
        "metric_definitions": definitions,
        "signal_families": SIGNAL_FAMILIES,
        "data_coverage": {
            "published_hooks": _coverage(hook_count, total),
            "japanese_scripts": _coverage(japanese_count, total),
            "source_transcripts": _coverage(source_transcript_count, total),
            "hash_verified_published_assets": _coverage(
                verified_asset_count, total
            ),
        },
        "source_concentration": {
            "unique_source_videos": len(source_counts),
            "unique_uploaders": len(uploader_counts),
            "top_source_videos": [
                {"source_video_id_or_url": key, "winner_posts": count}
                for key, count in source_counts.most_common(10)
            ],
            "top_uploaders": [
                {"uploader": key, "winner_posts": count}
                for key, count in uploader_counts.most_common(10)
            ],
        },
        "candidate_judging_protocol": _candidate_protocol(),
        "winners": winners,
    }


def _md(value: Any) -> str:
    return (
        _text(value)
        .replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("\n", " ")
    )


def _format_metric(metric_key: str, value: Any) -> str:
    number = _finite_number(value)
    if number is None:
        return "Unavailable"
    if metric_key in {"total_interactions_per_reach", "watch_depth"}:
        return f"{number * 100:.1f}%"
    if metric_key == "three_second_skip_rate":
        return f"{number:.1f}%"
    if metric_key == "saves_per_1000_reach":
        return f"{number:.1f}/1k"
    if metric_key == "views_per_reached_account":
        return f"{number:.2f}×"
    return f"{number:.3f}"


def _format_age(value: Any) -> str:
    number = _finite_number(value)
    return f"{number:.2f}h" if number is not None else "Unavailable"


def _raw_support_text(value: Mapping[str, Any]) -> str:
    pieces = []
    for key, raw in value.items():
        if key == "denominator_type" or raw is None:
            continue
        number = _finite_number(raw)
        rendered = (
            f"{number:g}" if number is not None else _text(raw)
        )
        pieces.append(f"{key}={rendered}")
    denominator = _text(value.get("denominator_type"))
    if denominator:
        pieces.append(f"denominator={denominator}")
    return ", ".join(pieces) or "Unavailable"


def render_winner_library_markdown(library: Mapping[str, Any]) -> str:
    metadata = _mapping(library.get("library_metadata"))
    coverage = _mapping(library.get("data_coverage"))
    winners = [_mapping(value) for value in _list(library.get("winners"))]
    lines = [
        "# AI Brief JP — measured winner hook & script library",
        "",
        (
            f"Generated: **{_md(metadata.get('generated_at_jst') or metadata.get('generated_at'))}**  "
        ),
        (
            f"Selection: the union of all fixed **{_md(metadata.get('maturity_window'))}** "
            f"Instagram metric Top 10s plus the aggregate Top 10."
        ),
        "",
        "## Evidence boundary",
        "",
        f"- {_md(metadata.get('evidence_boundary'))}",
        (
            f"- **{metadata.get('unique_winner_posts', 0)}** unique posts across "
            f"**{metadata.get('ranking_placement_count', 0)}** leaderboard placements; "
            f"fixed-window cohort **n={metadata.get('fixed_window_cohort_size', 0)}**."
        ),
        (
            f"- Japanese script coverage: "
            f"**{_mapping(coverage.get('japanese_scripts')).get('count', 0)}/"
            f"{_mapping(coverage.get('japanese_scripts')).get('total', 0)}**; "
            f"published-hook coverage: "
            f"**{_mapping(coverage.get('published_hooks')).get('count', 0)}/"
            f"{_mapping(coverage.get('published_hooks')).get('total', 0)}**."
        ),
        (
            "- `total_interactions` includes saves, so interaction rate and save "
            "rate are one intent/action family—not independent votes."
        ),
        (
            "- Watch depth, three-second skip, and views/reached are one related "
            "attention/replay family. Aggregate membership is a summary, not a "
            "sixth independent result."
        ),
        "",
        "## Winner index",
        "",
        "| Reference | Published hook | Top-10 memberships | 24h snapshot | Flags |",
        "|---|---|---|---|---|",
    ]
    for winner in winners:
        identity = _mapping(winner.get("identity"))
        content = _mapping(winner.get("content"))
        evidence = _mapping(winner.get("winner_evidence"))
        hook = _mapping(content.get("published_hook"))
        permalink = _text(identity.get("permalink"))
        linked_hook = (
            f"[{_md(hook.get('value') or identity.get('media_id'))}]({permalink})"
            if permalink
            else _md(hook.get("value") or identity.get("media_id"))
        )
        placements = [
            f"{_md(row.get('label'))} #{row.get('rank')}"
            for row in _list(evidence.get("ranking_memberships"))
        ]
        aggregate = _mapping(evidence.get("aggregate"))
        if aggregate:
            placements.append(f"Aggregate #{aggregate.get('rank')}")
        all_metrics = _mapping(evidence.get("all_metrics_at_window"))
        view_support = _mapping(
            _mapping(all_metrics.get("views_per_reached_account")).get(
                "supporting_metrics"
            )
        )
        reach = _finite_number(view_support.get("reach"))
        snapshot = "; ".join(
            [
                f"Reach {reach:g}" if reach is not None else "Reach Unavailable",
                f"Watch {_format_metric('watch_depth', _mapping(all_metrics.get('watch_depth')).get('value'))}",
                f"Skip {_format_metric('three_second_skip_rate', _mapping(all_metrics.get('three_second_skip_rate')).get('value'))}",
            ]
        )
        flags = ", ".join(_list(winner.get("evidence_flags"))) or "—"
        lines.append(
            f"| {_md(evidence.get('tier'))} | {linked_hook} | "
            f"{'<br>'.join(placements) or '—'} | {_md(snapshot)} | {_md(flags)} |"
        )

    protocol = _mapping(library.get("candidate_judging_protocol"))
    lines.extend(
        [
            "",
            "## How to judge new candidates with this library",
            "",
            f"{_md(protocol.get('principle'))}",
            "",
            "For every candidate:",
            "",
        ]
    )
    for index, check in enumerate(_list(protocol.get("required_checks")), start=1):
        lines.append(f"{index}. {_md(check)}")
    lines.extend(["", "Allowed decisions:", ""])
    for decision in _list(protocol.get("decisions")):
        lines.append(f"- **{_md(decision)}**")
    lines.extend(
        [
            "",
            "Required comparison output:",
            "",
            "```text",
            "Intended lane:",
            "Nearest three analogues (link + 24h metrics + distinct source):",
            "Aligned elements:",
            "Meaningful differences:",
            "Source/topic saturation:",
            "Claim and script support:",
            "Decision:",
            "Reason:",
            "```",
            "",
            "## Full hook and script pool",
            "",
        ]
    )

    definitions = _mapping(library.get("metric_definitions"))
    for winner in winners:
        identity = _mapping(winner.get("identity"))
        source = _mapping(winner.get("source"))
        content = _mapping(winner.get("content"))
        evidence = _mapping(winner.get("winner_evidence"))
        provenance = _mapping(winner.get("asset_provenance"))
        hook = _mapping(content.get("published_hook"))
        permalink = _text(identity.get("permalink"))
        title = _text(hook.get("value")) or _text(identity.get("media_id"))
        placements = [
            f"{_md(row.get('label'))} #{row.get('rank')}"
            for row in _list(evidence.get("ranking_memberships"))
        ]
        aggregate = _mapping(evidence.get("aggregate"))
        if aggregate:
            placements.append(f"Aggregate #{aggregate.get('rank')}")
        lines.extend(
            [
                "<details>",
                (
                    f"<summary><strong>{html.escape(_text(evidence.get('tier')))}</strong> "
                    f"· {html.escape(title)} · {html.escape(', '.join(placements))}</summary>"
                ),
                "",
                f"- Reel: [{_md(identity.get('media_id'))}]({permalink})"
                if permalink
                else f"- Reel: {_md(identity.get('media_id'))}",
                f"- Published: {_md(identity.get('published_at'))}",
                (
                    f"- Source: [{_md(source.get('title') or source.get('video_id') or 'Source')}]"
                    f"({_text(source.get('url'))})"
                    if _text(source.get("url"))
                    else f"- Source: {_md(source.get('title') or 'Unavailable')}"
                ),
                f"- Uploader: {_md(source.get('uploader') or 'Unavailable')}",
                f"- Script asset: `{_md(content.get('script_asset_id') or 'Unavailable')}`",
                (
                    f"- Asset verification: "
                    f"**{_md(_mapping(provenance.get('published_asset')).get('status'))}**; "
                    f"script resolution "
                    f"**{_md(_mapping(provenance.get('clip_resolution')).get('confidence'))}**"
                ),
                "",
                "### Published hook",
                "",
                f"> {title}",
                "",
                "### Ranking evidence",
                "",
                "| Leaderboard | Rank | Value | Percentile | Raw support | Actual age |",
                "|---|---:|---:|---:|---|---:|",
            ]
        )
        for row in _list(evidence.get("ranking_memberships")):
            row = _mapping(row)
            key = _text(row.get("leaderboard"))
            percentile = _finite_number(row.get("directional_percentile"))
            lines.append(
                f"| {_md(row.get('label'))} | #{row.get('rank')} / "
                f"{row.get('cohort_size')} | "
                f"{_format_metric(key, row.get('value'))} | "
                f"{f'P{percentile:.1f}' if percentile is not None else 'Unavailable'} | "
                f"{_md(_raw_support_text(_mapping(row.get('supporting_metrics'))))} | "
                f"{_format_age(row.get('actual_age_hours'))} |"
            )
        if aggregate:
            percentile = _finite_number(
                aggregate.get("average_directional_percentile")
            )
            lines.append(
                f"| Balanced aggregate | #{aggregate.get('rank')} / "
                f"{aggregate.get('cohort_size')} | "
                f"{f'P{percentile:.1f} mean' if percentile is not None else 'Unavailable'} | "
                "— | all five metrics required | "
                f"{_format_age(aggregate.get('actual_age_hours'))} |"
            )

        all_metrics = _mapping(evidence.get("all_metrics_at_window"))
        lines.extend(
            [
                "",
                "All five measured values at the same snapshot:",
                "",
            ]
        )
        for specification in moneyball.PERFORMANCE_RANKING_METRICS:
            key = str(specification["key"])
            value = _mapping(all_metrics.get(key))
            definition = _mapping(definitions.get(key))
            lines.append(
                f"- **{_md(definition.get('label') or specification['label'])}:** "
                f"{_format_metric(key, value.get('value'))} "
                f"({_md(_raw_support_text(_mapping(value.get('supporting_metrics'))))})"
            )

        opening = [_text(value) for value in _list(
            content.get("opening_japanese_script")
        ) if _text(value)]
        japanese = _mapping(content.get("japanese_script"))
        source_transcript = _mapping(content.get("source_transcript"))
        lines.extend(["", "### Opening 3 seconds", ""])
        if opening:
            lines.extend(f"> {line}  " for line in opening)
        else:
            lines.append("Unavailable; not inferred.")
        lines.extend(["", "### Full Japanese Reel script", ""])
        segments = [
            _text(_mapping(value).get("text"))
            for value in _list(japanese.get("segments"))
            if _text(_mapping(value).get("text"))
        ]
        if segments:
            lines.extend(f"> {segment}  " for segment in segments)
            lines.append(
                f"\nSource: `{_md(japanese.get('source_path'))}` "
                f"({_md(japanese.get('confidence'))} confidence)"
            )
        else:
            lines.append("Unavailable; not inferred.")

        lines.extend(["", "### Original-language source transcript", ""])
        if _text(source_transcript.get("text")):
            lines.append(f"> {_text(source_transcript.get('text'))}")
            lines.append(
                f"\nSource: `{_md(source_transcript.get('source_path'))}`"
            )
        else:
            lines.append("Unavailable; not inferred.")

        localized_options = [
            _text(value)
            for value in _list(content.get("current_localized_hook_options"))
            if _text(value)
        ]
        source_options = [
            _text(value)
            for value in _list(content.get("source_hook_variants"))
            if _text(value)
        ]
        if localized_options or source_options:
            lines.extend(
                [
                    "",
                    "### Generation-artifact hook options",
                    "",
                    (
                        "These are reference variants from current artifacts; "
                        "they do not override the published hook above."
                    ),
                    "",
                ]
            )
            for option in localized_options:
                lines.append(f"- JA: {_md(option)}")
            for option in source_options:
                lines.append(f"- Source: {_md(option)}")

        flags = _list(winner.get("evidence_flags"))
        lines.extend(["", "### Evidence notes", ""])
        if flags:
            for flag in flags:
                lines.append(f"- `{_md(flag)}`")
        else:
            lines.append("- No automatic low-base, low-count, or provenance flag.")
        ambiguity = _text(
            _mapping(provenance.get("clip_resolution")).get("ambiguity_note")
        )
        if ambiguity:
            lines.append(f"- {_md(ambiguity)}")
        lines.extend(["", "</details>", ""])

    concentration = _mapping(library.get("source_concentration"))
    lines.extend(
        [
            "## Coverage and concentration",
            "",
            (
                f"- Unique source videos: "
                f"**{concentration.get('unique_source_videos', 0)}**"
            ),
            (
                f"- Unique uploaders: "
                f"**{concentration.get('unique_uploaders', 0)}**"
            ),
            (
                f"- Unique script assets: "
                f"**{metadata.get('unique_script_assets', 0)}**"
            ),
            "",
            "Repeated sources are useful analogues but are not independent proof.",
            "",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def render_winner_library_json(library: Mapping[str, Any]) -> str:
    return (
        json.dumps(
            library,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    )
