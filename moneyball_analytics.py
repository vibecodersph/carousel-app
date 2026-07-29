#!/usr/bin/env python3
"""Transparent, age-matched Moneyball analytics for Instagram Reels.

This module is deliberately additive.  It reads the existing Reel ledger,
generation artifacts, and durable manual annotations; it does not publish,
change content-ranking weights, or mutate the source insight history.

All math in this file is intentionally inspectable.  There is no combined
"engagement score", missing values remain ``None``, and rates retain their
denominator type so reach- and view-based observations cannot be mixed.
"""

from __future__ import annotations

import csv
import html
import io
import json
import math
import os
import sqlite3
import tempfile
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
DEFAULT_DB_PATH = ROOT / "state" / "reels.db"
DEFAULT_FACEBOOK_DB_PATH = ROOT / "state" / "facebook.db"
DEFAULT_CONFIG_PATH = ROOT / "config" / "moneyball_analytics.json"
DEFAULT_ANNOTATIONS_PATH = ROOT / "data" / "reel_annotations.json"
JST = ZoneInfo("Asia/Tokyo")

CONTENT_GOALS = {"discovery", "utility", "authority", "retention"}
WINDOW_ORDER = ("2h", "24h", "72h", "7d")
DECISION_WINDOW_ORDER = ("7d", "72h", "24h", "2h")
COHORT_DIMENSIONS = (
    "duration_bucket",
    "series",
    "content_goal",
    "posting_window",
    "format",
    "hook_style",
)

INSIGHT_COLUMN_TO_CANONICAL = {
    "views": "views",
    "total_views": "total_views",
    "reach": "reach",
    "likes": "likes",
    "comments": "comments",
    "saved": "saves",
    "shares": "shares",
    "reposts": "reposts",
    "total_interactions": "interactions",
    "ig_reels_video_view_total_time": "total_watch_time_seconds",
    "ig_reels_avg_watch_time": "average_watch_time_seconds",
    "reels_skip_rate": "reels_skip_rate",
    "clips_replays_count": "replays",
    "facebook_views": "facebook_views",
    "crossposted_views": "crossposted_views",
}

SOURCE_TO_CANONICAL = {
    "views": "views",
    "total_views": "total_views",
    "plays": "plays",
    "reach": "reach",
    "non_follower_reach": "non_follower_reach",
    "follower_reach": "follower_reach",
    "likes": "likes",
    "comments": "comments",
    "saved": "saves",
    "saves": "saves",
    "shares": "shares",
    "reposts": "reposts",
    "sends": "sends",
    "total_interactions": "interactions",
    "reels_skip_rate": "reels_skip_rate",
    "clips_replays_count": "replays",
    "facebook_views": "facebook_views",
    "crossposted_views": "crossposted_views",
    "profile_visits": "profile_visits",
    "dm_keyword_hits": "dm_keyword_hits",
    "returning_viewers": "returning_viewers",
}

# The account-level tables were added after the first Moneyball report.  The
# alternate names keep older development ledgers readable while the first
# names are the canonical v5 schema.  Account rows are never joined to a Reel.
ACCOUNT_FOLLOWER_SNAPSHOT_TABLES = (
    "account_follower_snapshots",
    "account_insight_snapshots",
)
ACCOUNT_FOLLOWER_FLOW_TABLES = (
    "account_follower_flows",
    "account_follow_flows",
)

ACCOUNT_ATTRIBUTION_WARNING = (
    "Account follower movement is account-wide and cannot be assigned to a "
    "specific Reel, series, format, hook, or experiment. Timing overlap is not "
    "causal evidence."
)

CANONICAL_RAW_METRICS = (
    "views",
    "total_views",
    "facebook_views",
    "crossposted_views",
    "plays",
    "initial_plays",
    "replays",
    "reach",
    "non_follower_reach",
    "follower_reach",
    "total_watch_time_seconds",
    "average_watch_time_seconds",
    "reels_skip_rate",
    "duration_seconds",
    "likes",
    "reactions",
    "comments",
    "saves",
    "shares",
    "reposts",
    "sends",
    "interactions",
    "follows",
    "profile_visits",
    "dm_keyword_hits",
    "returning_viewers",
    "three_second_retention_rate",
    "three_second_dropoff_rate",
    "retention_graph",
)

CONTENT_METADATA_FIELDS = (
    "series",
    "content_goal",
    "topic",
    "source",
    "hook_style",
    "hook_text",
    "format",
    "visual_style",
    "caption_style",
    "cta",
    "experiment_id",
    "experiment_variant",
    "changed_variable",
    "hypothesis",
    "production_minutes",
    "manual_effort_minutes",
    "direct_cost_jpy",
    "metadata_source",
    "metadata_confidence",
)

SOURCE_METRIC_DEFINITIONS = {
    "views": "Instagram Graph `views`: times the Reel was played or displayed.",
    "total_views": "Meta all-surface `total_views`; overlaps other view scopes and is not added.",
    "reach": "Instagram Graph estimated unique accounts reached.",
    "likes": "Instagram Graph Reel likes.",
    "total_likes": "Meta all-surface likes; retained as provenance, not added to Instagram likes.",
    "comments": "Instagram Graph Reel comments.",
    "total_comments": "Meta all-surface comments; retained as provenance, not added.",
    "saved": "Instagram Graph Reel saves; canonical field is `saves`.",
    "shares": "Instagram Graph Reel shares. Equivalence to private sends is not established.",
    "reposts": (
        "Instagram Graph Reel reposts. This is separate from `shares` and is not "
        "included in Meta's documented `total_interactions` definition."
    ),
    "total_interactions": (
        "Instagram Graph net aggregate: likes, saves, comments and shares minus "
        "unlikes, unsaves and deleted comments. It remains an ambiguous aggregate."
    ),
    "ig_reels_video_view_total_time": "Instagram total Reel watch time in milliseconds.",
    "ig_reels_avg_watch_time": "Instagram average Reel watch time in milliseconds.",
    "reels_skip_rate": (
        "Estimated percentage of initial views that skipped during the first three "
        "seconds; the returned value is already a percentage."
    ),
    "clips_replays_count": (
        "Deprecated legacy replay-count field retained only for compatibility; it is "
        "not requested on current Graph API versions."
    ),
    "facebook_views": "Facebook view scope; not added to Instagram views.",
    "crossposted_views": "Explicit Instagram-plus-Facebook scope; overlaps other view fields.",
    "follows": (
        "Reserved post-attributed field. Account follows/unfollows are stored "
        "separately and are never copied into a Reel."
    ),
}


def numeric(value: Any) -> int | float | None:
    """Return a finite number, rejecting booleans and non-numeric values."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return int(number) if number.is_integer() else number


def metric_available(value: Any) -> bool:
    """Recognize finite scalar metrics and non-empty structured Graph metrics."""
    if numeric(value) is not None:
        return True
    return isinstance(value, (Mapping, list)) and bool(value)


def safe_divide(numerator: Any, denominator: Any) -> float | None:
    """Divide finite numbers only; a missing, zero, or negative divisor is unavailable."""
    top = numeric(numerator)
    bottom = numeric(denominator)
    if top is None or bottom is None or float(bottom) <= 0:
        return None
    result = float(top) / float(bottom)
    return result if math.isfinite(result) else None


# Short alias used internally and convenient for callers.
safe_div = safe_divide


def safe_sum(values: Iterable[Any], *, require_all: bool = True) -> int | float | None:
    cleaned = [numeric(value) for value in values]
    if require_all and any(value is None for value in cleaned):
        return None
    present = [value for value in cleaned if value is not None]
    if not present:
        return None
    total = sum(float(value) for value in present)
    return int(total) if total.is_integer() else total


def finite_values(values: Iterable[Any]) -> list[float]:
    return [float(value) for value in values if numeric(value) is not None]


def percentile(values: Sequence[Any], q: float) -> float | None:
    """R-7/linear-interpolation percentile used by Python and the reach analyzer."""
    cleaned = sorted(finite_values(values))
    if not cleaned or not 0 <= q <= 1:
        return None
    if len(cleaned) == 1:
        return cleaned[0]
    position = (len(cleaned) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return cleaned[lower]
    weight = position - lower
    return cleaned[lower] + (cleaned[upper] - cleaned[lower]) * weight


def percentile_rank(values: Sequence[Any], value: Any) -> float | None:
    """Midrank percentile, so ties receive half the tied mass."""
    target = numeric(value)
    cleaned = finite_values(values)
    if target is None or not cleaned:
        return None
    less = sum(item < float(target) for item in cleaned)
    equal = sum(item == float(target) for item in cleaned)
    return (less + 0.5 * equal) / len(cleaned) * 100.0


def distribution(values: Sequence[Any], *, quartiles_min_n: int = 4) -> dict[str, Any]:
    cleaned = finite_values(values)
    return {
        "n": len(cleaned),
        "median": percentile(cleaned, 0.5),
        "p25": percentile(cleaned, 0.25) if len(cleaned) >= quartiles_min_n else None,
        "p75": percentile(cleaned, 0.75) if len(cleaned) >= quartiles_min_n else None,
        "min": min(cleaned) if cleaned else None,
        "max": max(cleaned) if cleaned else None,
    }


def parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def isoformat_seconds(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def read_optional_json(path: Path | None) -> Any | None:
    if path is None or not path.is_file():
        return None
    try:
        return read_json(path)
    except (OSError, json.JSONDecodeError):
        return None


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    data = read_json(path)
    if not isinstance(data, dict):
        raise ValueError(f"Moneyball config must be a JSON object: {path}")
    windows = data.get("maturity_windows")
    if not isinstance(windows, dict) or not all(name in windows for name in WINDOW_ORDER):
        raise ValueError("Moneyball config must define 2h, 24h, 72h, and 7d windows")
    return data


def load_annotations(path: Path = DEFAULT_ANNOTATIONS_PATH) -> list[dict[str, Any]]:
    data = read_json(path)
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError(f"Unsupported annotations schema: {path}")
    rows = data.get("annotations")
    if not isinstance(rows, list):
        raise ValueError("annotations must be a JSON array")
    seen_media: set[tuple[str, str]] = set()
    seen_hashes: set[tuple[str, str]] = set()
    output: list[dict[str, Any]] = []
    for index, raw in enumerate(rows):
        if not isinstance(raw, dict):
            raise ValueError(f"annotation {index} must be an object")
        row = dict(raw)
        account = str(row.get("account") or "").strip()
        media_id = str(row.get("media_id") or "").strip()
        content_hash = str(row.get("content_hash") or "").strip()
        if not media_id and not content_hash:
            raise ValueError(f"annotation {index} requires media_id or content_hash")
        goal = row.get("content_goal")
        if goal is not None and goal not in CONTENT_GOALS:
            raise ValueError(
                f"annotation {index} has invalid content_goal {goal!r}; "
                f"allowed: {sorted(CONTENT_GOALS)}"
            )
        if (
            str(row.get("metadata_source") or "").strip().lower() == "inferred"
            and row.get("metadata_confidence") in (None, "")
        ):
            raise ValueError(
                f"annotation {index} with metadata_source='inferred' requires "
                "metadata_confidence"
            )
        if media_id:
            key = (account, media_id)
            if key in seen_media:
                raise ValueError(f"duplicate annotation media_id: {media_id}")
            seen_media.add(key)
        if content_hash:
            key = (account, content_hash)
            if key in seen_hashes:
                raise ValueError(f"duplicate annotation content_hash: {content_hash}")
            seen_hashes.add(key)
        output.append(row)
    return output


def annotation_for_post(
    annotations: Sequence[Mapping[str, Any]],
    *,
    account: str,
    media_id: str,
    content_hash: str,
) -> dict[str, Any]:
    by_hash: dict[str, Any] | None = None
    for raw in annotations:
        row_account = str(raw.get("account") or "")
        if row_account and row_account != account:
            continue
        row_media = str(raw.get("media_id") or "")
        row_hash = str(raw.get("content_hash") or "")
        if row_media and row_media == media_id:
            return dict(raw)
        if row_hash and row_hash == content_hash:
            by_hash = dict(raw)
    return by_hash or {}


def raw_metric_items(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    if not isinstance(raw, dict):
        return []
    data = raw.get("data")
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def raw_metric_values(raw: Any) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for item in raw_metric_items(raw):
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        rows = item.get("values") if isinstance(item.get("values"), list) else []
        latest = rows[-1] if rows and isinstance(rows[-1], dict) else {}
        value = latest.get("value") if isinstance(latest, dict) else None
        if value is None and isinstance(item.get("total_value"), dict):
            value = item["total_value"].get("value")
        if value is not None:
            values[name] = value
    return values


def raw_metric_metadata(raw: Any) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for item in raw_metric_items(raw):
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        output[name] = {
            "period": item.get("period"),
            "title": item.get("title"),
            "description": item.get("description"),
        }
    return output


def nested_metric_total(value: Any) -> int | float | None:
    """Sum a nested Graph metric object without treating booleans as numbers."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        number = float(value)
        return int(number) if number.is_integer() else number
    if not isinstance(value, Mapping):
        return None
    values = [
        number
        for nested in value.values()
        if (number := nested_metric_total(nested)) is not None
    ]
    if not values:
        return None
    total = sum(float(number) for number in values)
    return int(total) if total.is_integer() else total


def facebook_social_action_value(value: Any, *names: str) -> int | float | None:
    """Read one named action from Meta's Facebook social-action object."""
    if not isinstance(value, Mapping):
        return None
    aliases = {name.casefold() for name in names}
    matches = [
        number
        for key, nested in value.items()
        if str(key).casefold() in aliases
        and (number := nested_metric_total(nested)) is not None
    ]
    if not matches:
        return None
    total = sum(float(number) for number in matches)
    return int(total) if total.is_integer() else total


def retention_rate_at_second(value: Any, second: int) -> float | None:
    """Return an exact retention-graph point, never an interpolated estimate."""
    if not isinstance(value, Mapping):
        return None
    for raw_key, raw_value in value.items():
        key = numeric(raw_key)
        if key is None and isinstance(raw_key, str):
            try:
                parsed_key = float(raw_key.strip())
            except ValueError:
                parsed_key = math.nan
            if math.isfinite(parsed_key):
                key = parsed_key
        point = numeric(raw_value)
        if key is None or point is None or float(key) != float(second):
            continue
        number = float(point)
        if 0.0 <= number <= 1.0:
            return number
        if 1.0 < number <= 100.0:
            return number / 100.0
    return None


def canonical_snapshot_metrics(
    row: Mapping[str, Any],
    *,
    duration_seconds: Any = None,
    platform: str = "instagram",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Normalize one stored observation while retaining source provenance."""
    raw = row.get("raw")
    source_values = raw_metric_values(raw)
    metrics: dict[str, Any] = {name: None for name in CANONICAL_RAW_METRICS}
    provenance: dict[str, Any] = {}

    for source_name, canonical_name in SOURCE_TO_CANONICAL.items():
        value = numeric(source_values.get(source_name))
        if value is not None:
            metrics[canonical_name] = value
            provenance[canonical_name] = {
                "source_field": source_name,
                "source": "meta_graph_api_raw",
            }

    for column_name, canonical_name in INSIGHT_COLUMN_TO_CANONICAL.items():
        if metrics[canonical_name] is not None:
            continue
        value = numeric(row.get(column_name))
        if value is not None:
            metrics[canonical_name] = value
            provenance[canonical_name] = {
                "source_field": column_name,
                "source": "insights_column",
            }

    if platform == "facebook":
        facebook_scalars = (
            ("fb_reels_total_plays", "plays"),
            ("blue_reels_play_count", "initial_plays"),
            ("fb_reels_replay_count", "replays"),
        )
        for source_name, canonical_name in facebook_scalars:
            value = numeric(source_values.get(source_name))
            if value is None:
                continue
            metrics[canonical_name] = value
            provenance[canonical_name] = {
                "source_field": source_name,
                "source": "facebook_video_insights",
                "period": "lifetime",
            }
        if metrics["plays"] is not None:
            metrics["views"] = metrics["plays"]
            provenance["views"] = {
                "source_field": "fb_reels_total_plays",
                "source": "facebook_video_insights",
                "semantics": "initial plays plus replays; not unique reach",
            }

        unique_viewer_source = next(
            (
                name
                for name in (
                    "post_total_media_view_unique",
                    "post_impressions_unique",
                )
                if numeric(source_values.get(name)) is not None
            ),
            None,
        )
        if unique_viewer_source is not None:
            metrics["reach"] = numeric(source_values.get(unique_viewer_source))
            provenance["reach"] = {
                "source_field": unique_viewer_source,
                "source": "facebook_video_insights",
                "semantics": "unique media viewers",
                "denominator_type": "unique_media_viewers",
            }

        reactions = source_values.get("post_video_likes_by_reaction_type")
        if isinstance(reactions, Mapping):
            exact_like = next(
                (
                    nested_metric_total(value)
                    for key, value in reactions.items()
                    if str(key).casefold() == "like"
                ),
                None,
            )
            reaction_total = nested_metric_total(reactions)
            if exact_like is not None:
                metrics["likes"] = exact_like
                provenance["likes"] = {
                    "source_field": "post_video_likes_by_reaction_type.LIKE",
                    "source": "facebook_video_insights",
                }
            if reaction_total is not None:
                metrics["reactions"] = reaction_total
                provenance["reactions"] = {
                    "source_field": "post_video_likes_by_reaction_type",
                    "source": "facebook_video_insights",
                }

        social = source_values.get("post_video_social_actions")
        for canonical_name, aliases in (
            ("comments", ("comment", "comments")),
            ("shares", ("share", "shares")),
        ):
            value = facebook_social_action_value(social, *aliases)
            if value is None:
                continue
            metrics[canonical_name] = value
            provenance[canonical_name] = {
                "source_field": f"post_video_social_actions.{canonical_name}",
                "source": "facebook_video_insights",
            }
        if all(
            numeric(metrics.get(name)) is not None
            for name in ("reactions", "comments", "shares")
        ):
            metrics["interactions"] = sum(
                float(metrics[name])
                for name in ("reactions", "comments", "shares")
            )
            provenance["interactions"] = {
                "source_field": (
                    "post_video_likes_by_reaction_type + "
                    "post_video_social_actions.comments + "
                    "post_video_social_actions.shares"
                ),
                "source": "transparent_sum",
                "warning": "Facebook saves are not included because they are unavailable.",
            }

        attributed_follows = numeric(source_values.get("post_video_followers"))
        if attributed_follows is not None:
            metrics["follows"] = attributed_follows
            provenance["follows"] = {
                "source_field": "post_video_followers",
                "source": "facebook_video_insights",
                "semantics": "follows attributed by Meta to this Facebook Reel",
            }

        retention_graph = source_values.get("post_video_retention_graph")
        if isinstance(retention_graph, Mapping):
            metrics["retention_graph"] = dict(retention_graph)
            provenance["retention_graph"] = {
                "source_field": "post_video_retention_graph",
                "source": "facebook_video_insights",
            }
            retention_3s = retention_rate_at_second(retention_graph, 3)
            if retention_3s is not None:
                metrics["three_second_retention_rate"] = retention_3s
                metrics["three_second_dropoff_rate"] = 1.0 - retention_3s
                provenance["three_second_retention_rate"] = {
                    "source_field": "post_video_retention_graph[3]",
                    "source": "derived_exact_graph_point",
                    "warning": (
                        "Retention drop-off is not Meta's Instagram reels_skip_rate."
                    ),
                }
                provenance["three_second_dropoff_rate"] = {
                    **provenance["three_second_retention_rate"],
                    "formula": "1 - exact three-second retention",
                }
    else:
        # The currently supported Instagram follower source is account-wide.
        # A legacy/custom `follows` value is not silently treated as post
        # attribution; it remains in the audit only.
        excluded_follow = numeric(source_values.get("follows"))
        if excluded_follow is None:
            excluded_follow = numeric(row.get("follows"))
        if excluded_follow is not None:
            provenance["excluded_post_follows"] = {
                "source_field": "follows",
                "value": excluded_follow,
                "reason": (
                    "Post attribution is not verified; account follower movement "
                    "must remain in account_growth."
                ),
            }

    if platform == "facebook":
        total_watch_ms = numeric(source_values.get("post_video_view_time"))
        if total_watch_ms is not None:
            metrics["total_watch_time_seconds"] = float(total_watch_ms) / 1000.0
            provenance["total_watch_time_seconds"] = {
                "source_field": "post_video_view_time",
                "source_unit": "milliseconds",
                "canonical_unit": "seconds",
            }
        average_watch_ms = numeric(
            source_values.get("post_video_avg_time_watched")
        )
        if average_watch_ms is not None:
            metrics["average_watch_time_seconds"] = (
                float(average_watch_ms) / 1000.0
            )
            provenance["average_watch_time_seconds"] = {
                "source_field": "post_video_avg_time_watched",
                "source_unit": "milliseconds",
                "canonical_unit": "seconds",
            }

    total_watch_ms = numeric(source_values.get("ig_reels_video_view_total_time"))
    if total_watch_ms is None:
        total_watch_ms = numeric(row.get("ig_reels_video_view_total_time"))
    if total_watch_ms is not None:
        metrics["total_watch_time_seconds"] = float(total_watch_ms) / 1000.0
        provenance["total_watch_time_seconds"] = {
            "source_field": "ig_reels_video_view_total_time",
            "source_unit": "milliseconds",
            "canonical_unit": "seconds",
        }

    average_watch_ms = numeric(source_values.get("ig_reels_avg_watch_time"))
    if average_watch_ms is None:
        average_watch_ms = numeric(row.get("ig_reels_avg_watch_time"))
    if average_watch_ms is not None:
        metrics["average_watch_time_seconds"] = float(average_watch_ms) / 1000.0
        provenance["average_watch_time_seconds"] = {
            "source_field": "ig_reels_avg_watch_time",
            "source_unit": "milliseconds",
            "canonical_unit": "seconds",
        }

    duration = numeric(duration_seconds)
    if duration is not None and float(duration) > 0:
        metrics["duration_seconds"] = duration
        provenance["duration_seconds"] = {
            "source_field": "notes.json.duration",
            "source": "generation_pipeline",
        }

    return metrics, {
        "source_platform": platform,
        "canonical_fields": provenance,
        "source_values": {
            key: raw_value
            for key, raw_value in source_values.items()
            if metric_available(raw_value)
        },
        "source_metadata": raw_metric_metadata(raw),
    }


def duration_bucket(duration_seconds: Any, config: Mapping[str, Any]) -> str | None:
    duration = numeric(duration_seconds)
    if duration is None or float(duration) < 0:
        return None
    buckets = config.get("duration_buckets")
    if not isinstance(buckets, list):
        return None
    for raw in buckets:
        if not isinstance(raw, dict):
            continue
        minimum = numeric(raw.get("min_seconds"))
        maximum = numeric(raw.get("max_seconds"))
        min_ok = (
            True
            if minimum is None
            else float(duration) >= float(minimum)
            if raw.get("min_inclusive", True)
            else float(duration) > float(minimum)
        )
        max_ok = (
            True
            if maximum is None
            else float(duration) <= float(maximum)
            if raw.get("max_inclusive", True)
            else float(duration) < float(maximum)
        )
        if min_ok and max_ok:
            return str(raw.get("label") or raw.get("id") or "") or None
    return None


def posting_window(scheduled_at: Any, published_at: Any) -> str | None:
    value = parse_datetime(scheduled_at) or parse_datetime(published_at)
    if value is None:
        return None
    local = value.astimezone(JST)
    return f"{local.hour:02d}:00 JST"


def select_window_snapshot(
    snapshots: Sequence[Mapping[str, Any]],
    published_at: Any,
    window: Mapping[str, Any],
    *,
    media_id: str | None = None,
    as_of: datetime | None = None,
) -> dict[str, Any] | None:
    """Choose the nearest real observation at/after a configured target."""
    published = parse_datetime(published_at)
    target = numeric(window.get("target_hours"))
    tolerance = numeric(window.get("max_hours_after_target"))
    if published is None or target is None or tolerance is None or float(tolerance) < 0:
        return None
    candidates: list[tuple[float, datetime, int, dict[str, Any]]] = []
    for raw in snapshots:
        if media_id and str(raw.get("media_id") or "") != media_id:
            continue
        captured = parse_datetime(raw.get("captured_at"))
        if captured is None or (as_of is not None and captured > as_of):
            continue
        age = (captured - published).total_seconds() / 3600.0
        if age < float(target) or age > float(target) + float(tolerance):
            continue
        snapshot = dict(raw)
        raw_metrics = snapshot.get("raw_metrics")
        if isinstance(raw_metrics, Mapping):
            if numeric(raw_metrics.get("reach")) is None and numeric(raw_metrics.get("views")) is None:
                continue
        candidates.append((age, captured, int(raw.get("id") or -1), snapshot))
    if not candidates:
        return None
    age, captured, insight_id, selected = min(
        candidates, key=lambda item: (item[0], item[1], item[2])
    )
    selected["actual_age_hours"] = age
    selected["target_age_hours"] = float(target)
    selected["insight_id"] = insight_id
    return selected


def latest_snapshot(
    snapshots: Sequence[Mapping[str, Any]],
    *,
    media_id: str | None = None,
    as_of: datetime | None = None,
) -> dict[str, Any] | None:
    candidates: list[tuple[datetime, int, dict[str, Any]]] = []
    for raw in snapshots:
        if media_id and str(raw.get("media_id") or "") != media_id:
            continue
        captured = parse_datetime(raw.get("captured_at"))
        if captured is None or (as_of is not None and captured > as_of):
            continue
        candidates.append((captured, int(raw.get("id") or -1), dict(raw)))
    if not candidates:
        return None
    captured, insight_id, selected = max(candidates, key=lambda item: (item[0], item[1]))
    selected["insight_id"] = insight_id
    return selected


def collapse_exact_snapshot_duplicates(
    snapshots: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Collapse legacy exact duplicate rows but retain later identical fetches."""
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    duplicates = 0
    for raw in sorted(
        snapshots,
        key=lambda row: (
            str(row.get("captured_at") or ""),
            int(row.get("id") or -1),
        ),
    ):
        identity = {
            "content_hash": raw.get("content_hash"),
            "channel_id": raw.get("channel_id"),
            "media_id": raw.get("media_id"),
            "captured_at": raw.get("captured_at"),
            "raw_metrics": raw.get("raw_metrics"),
            "raw": raw.get("raw"),
        }
        key = json.dumps(identity, ensure_ascii=False, sort_keys=True, default=str)
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        output.append(dict(raw))
    return output, duplicates


def summarize_snapshot_audit(
    snapshots: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    canonical_counts: Counter[str] = Counter()
    source_fields: dict[str, dict[str, Any]] = {}
    raw_column_conflicts: Counter[str] = Counter()
    for row in snapshots:
        captured = str(row.get("captured_at") or "")
        raw_metrics = row.get("raw_metrics")
        raw_metrics = raw_metrics if isinstance(raw_metrics, Mapping) else {}
        for name in CANONICAL_RAW_METRICS:
            if metric_available(raw_metrics.get(name)):
                canonical_counts[name] += 1
        provenance = row.get("metric_provenance")
        provenance = provenance if isinstance(provenance, Mapping) else {}
        source_values = provenance.get("source_values")
        source_values = source_values if isinstance(source_values, Mapping) else {}
        source_metadata = provenance.get("source_metadata")
        source_metadata = source_metadata if isinstance(source_metadata, Mapping) else {}
        for name, value in source_values.items():
            entry = source_fields.setdefault(
                str(name),
                {
                    "count": 0,
                    "first_captured_at": captured or None,
                    "last_captured_at": captured or None,
                    "periods": set(),
                    "titles": set(),
                    "descriptions": set(),
                },
            )
            entry["count"] += 1
            if captured:
                first = entry.get("first_captured_at")
                last = entry.get("last_captured_at")
                entry["first_captured_at"] = min(first, captured) if first else captured
                entry["last_captured_at"] = max(last, captured) if last else captured
            metadata = source_metadata.get(name)
            metadata = metadata if isinstance(metadata, Mapping) else {}
            for source_key, target_key in (
                ("period", "periods"),
                ("title", "titles"),
                ("description", "descriptions"),
            ):
                text = str(metadata.get(source_key) or "").strip()
                if text:
                    entry[target_key].add(text)

        for source_name, column_name in (
            ("views", "views"),
            ("reach", "reach"),
            ("likes", "likes"),
            ("comments", "comments"),
            ("saved", "saved"),
            ("shares", "shares"),
            ("total_interactions", "total_interactions"),
            ("follows", "follows"),
        ):
            raw_value = numeric(source_values.get(source_name))
            column_value = numeric(row.get(column_name))
            if (
                raw_value is not None
                and column_value is not None
                and float(raw_value) != float(column_value)
            ):
                raw_column_conflicts[source_name] += 1
    return {
        "stored_rows": len(snapshots),
        "canonical_metric_counts": dict(sorted(canonical_counts.items())),
        "source_fields": {
            name: {
                **entry,
                "periods": sorted(entry["periods"]),
                "titles": sorted(entry["titles"]),
                "descriptions": sorted(entry["descriptions"]),
            }
            for name, entry in sorted(source_fields.items())
        },
        "raw_column_conflicts": dict(sorted(raw_column_conflicts.items())),
    }


def generation_metadata(
    reel: Mapping[str, Any],
    trial: Mapping[str, Any],
    config: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Load only durable, directly supported fields from generation artifacts."""
    clip_text = str(reel.get("clip_dir") or "").strip()
    clip_dir = Path(clip_text) if clip_text else None
    manifest_text = str(reel.get("manifest_path") or "").strip()
    manifest_path = Path(manifest_text) if manifest_text else None
    manifest_data = read_optional_json(manifest_path)
    manifest = manifest_data if isinstance(manifest_data, dict) else {}
    notes_data = read_optional_json(clip_dir / "notes.json") if clip_dir else None
    notes = notes_data if isinstance(notes_data, dict) else {}
    source_root = clip_dir.parents[1] if clip_dir and len(clip_dir.parents) >= 2 else None
    source_data = read_optional_json(source_root / "metadata.json") if source_root else None
    source_metadata = source_data if isinstance(source_data, dict) else {}

    duration = numeric(notes.get("duration"))
    source_url = str(
        manifest.get("source_url")
        or source_metadata.get("webpage_url")
        or source_metadata.get("original_url")
        or ""
    ).strip()
    topic = str(manifest.get("topic") or "").strip() or None
    trial_hook = str(trial.get("variant_hook") or "").strip()
    ledger_title = str(reel.get("title") or "").strip()
    hook_text = trial_hook or ledger_title or topic

    changed_variables: list[str] = []
    raw_changed = trial.get("changed_variables_json")
    if isinstance(raw_changed, str) and raw_changed.strip():
        try:
            parsed_changed = json.loads(raw_changed)
        except json.JSONDecodeError:
            parsed_changed = []
        if isinstance(parsed_changed, list):
            changed_variables = [
                str(value).strip() for value in parsed_changed if str(value).strip()
            ]

    values: dict[str, Any] = {
        "series": None,
        "content_goal": None,
        "topic": topic,
        "source": source_url or str(reel.get("source_video") or "").strip() or None,
        "hook_style": None,
        "hook_text": hook_text,
        "format": "video_reel",
        "visual_style": None,
        "caption_style": None,
        "cta": None,
        "duration_bucket": duration_bucket(duration, config),
        "posting_window": posting_window(
            reel.get("scheduled_at"), reel.get("published_at")
        ),
        "trial_reel": bool(reel.get("trial_reel")),
        "experiment_id": str(trial.get("experiment_id") or "").strip() or None,
        "experiment_variant": "variant" if trial.get("experiment_id") else None,
        "changed_variable": changed_variables[0] if len(changed_variables) == 1 else None,
        "changed_variables": changed_variables,
        "hypothesis": None,
        "production_minutes": None,
        "manual_effort_minutes": None,
        "direct_cost_jpy": None,
        "metadata_source": "generation_pipeline",
        "metadata_confidence": "high",
    }
    provenance: dict[str, Any] = {}
    for field in ("topic", "source", "hook_text", "format", "duration_bucket"):
        if values.get(field) is not None:
            provenance[field] = {
                "source": "generation_pipeline",
                "confidence": "high",
            }
    for field in ("posting_window", "trial_reel"):
        provenance[field] = {"source": "ledger", "confidence": "high"}
    for field in ("experiment_id", "experiment_variant", "changed_variable"):
        if values.get(field) is not None:
            provenance[field] = {
                "source": "trial_experiments",
                "confidence": "high",
            }
    artifact = {
        "clip_dir": str(clip_dir or ""),
        "manifest_path": str(manifest_path or ""),
        "notes_path": str((clip_dir / "notes.json") if clip_dir else ""),
        "duration_seconds": duration,
        "source_title": str(
            manifest.get("source_title") or source_metadata.get("title") or ""
        ).strip(),
        "source_uploader": str(
            manifest.get("source_uploader")
            or source_metadata.get("uploader")
            or source_metadata.get("channel")
            or ""
        ).strip(),
    }
    return values, provenance, artifact


def merge_annotation_metadata(
    generated: Mapping[str, Any],
    generated_provenance: Mapping[str, Any],
    annotation: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Manual field presence wins, including an explicit manual null."""
    values = dict(generated)
    provenance = dict(generated_provenance)
    manual_fields: list[str] = []
    for field in CONTENT_METADATA_FIELDS:
        if field not in annotation:
            continue
        values[field] = annotation.get(field)
        manual_fields.append(field)
        provenance[field] = {
            "source": str(annotation.get("metadata_source") or "manual"),
            "confidence": annotation.get("metadata_confidence") or "high",
        }
    if manual_fields:
        values["metadata_source"] = str(annotation.get("metadata_source") or "manual")
        values["metadata_confidence"] = annotation.get("metadata_confidence") or "high"
    values["manual_fields"] = sorted(manual_fields)
    values["metadata_provenance"] = provenance
    return values, provenance


def compute_post_metrics(
    raw_metrics: Mapping[str, Any],
    content_metadata: Mapping[str, Any],
    *,
    plays_semantics_verified: bool = False,
) -> dict[str, Any]:
    """Compute transparent post-level metrics with explicit denominator labels."""
    output: dict[str, Any] = {}
    warnings: list[str] = []

    total_watch_seconds = numeric(raw_metrics.get("total_watch_time_seconds"))
    average_watch_seconds = numeric(raw_metrics.get("average_watch_time_seconds"))
    if average_watch_seconds is None and plays_semantics_verified:
        average_watch_seconds = safe_divide(
            total_watch_seconds, raw_metrics.get("plays")
        )
        if average_watch_seconds is not None:
            output["average_watch_time_source"] = "total_watch_time_seconds / verified_plays"
    elif average_watch_seconds is not None:
        output["average_watch_time_source"] = "direct_meta_metric"
    else:
        output["average_watch_time_source"] = "unavailable"

    duration_seconds = numeric(raw_metrics.get("duration_seconds"))
    output["average_watch_time_seconds"] = average_watch_seconds
    output["watch_depth"] = safe_divide(average_watch_seconds, duration_seconds)
    output["total_watch_hours"] = safe_divide(total_watch_seconds, 3600)

    reach = numeric(raw_metrics.get("reach"))
    views = numeric(raw_metrics.get("views"))
    interactions = numeric(raw_metrics.get("interactions"))
    output["interactions_per_1000_reach"] = (
        safe_divide(float(interactions) * 1000, reach)
        if interactions is not None and reach is not None
        else None
    )
    output["interactions_per_1000_views"] = (
        safe_divide(float(interactions) * 1000, views)
        if interactions is not None and reach is None and views is not None
        else None
    )
    output["engagement_rate_by_reach"] = (
        safe_divide(interactions, reach)
        if interactions is not None and reach is not None
        else None
    )
    output["views_per_reached_account"] = (
        safe_divide(views, reach)
        if views is not None and reach is not None
        else None
    )
    distribution_denominator: int | float | None
    distribution_denominator_type: str | None
    if reach is not None and float(reach) > 0:
        distribution_denominator = reach
        distribution_denominator_type = "reach"
    elif views is not None and float(views) > 0:
        distribution_denominator = views
        distribution_denominator_type = "views"
        warnings.append(
            "Reach unavailable; view-based rates are labeled separately and excluded "
            "from reach-based rankings."
        )
    else:
        distribution_denominator = None
        distribution_denominator_type = None

    for action in (
        "shares",
        "reposts",
        "sends",
        "saves",
        "comments",
        "likes",
        "reactions",
    ):
        count = numeric(raw_metrics.get(action))
        reach_name = f"{action}_per_1000_reach"
        views_name = f"{action}_per_1000_views"
        output[reach_name] = (
            safe_divide(float(count) * 1000, reach)
            if count is not None and reach is not None
            else None
        )
        output[views_name] = (
            safe_divide(float(count) * 1000, views)
            if count is not None and reach is None and views is not None
            else None
        )

    shares = numeric(raw_metrics.get("shares"))
    sends = numeric(raw_metrics.get("sends"))
    saves = numeric(raw_metrics.get("saves"))
    if shares is not None:
        intent_distribution = shares
        intent_source = "shares_plus_saves"
        if sends is not None:
            warnings.append(
                "Both shares and sends are present; sends were not added because overlap "
                "is unverified."
            )
    elif sends is not None:
        intent_distribution = sends
        intent_source = "sends_plus_saves"
    else:
        intent_distribution = None
        intent_source = None
    intent_actions = safe_sum((intent_distribution, saves), require_all=True)
    output["intent_actions"] = intent_actions
    output["intent_action_source"] = intent_source
    output["intent_actions_per_1000_reach"] = (
        safe_divide(float(intent_actions) * 1000, reach)
        if intent_actions is not None and reach is not None
        else None
    )
    output["intent_actions_per_1000_views"] = (
        safe_divide(float(intent_actions) * 1000, views)
        if intent_actions is not None and reach is None and views is not None
        else None
    )
    output["satisfaction_rate"] = {
        "value": (
            output["intent_actions_per_1000_reach"]
            if distribution_denominator_type == "reach"
            else output["intent_actions_per_1000_views"]
            if distribution_denominator_type == "views"
            else None
        ),
        "denominator_type": distribution_denominator_type,
        "denominator": distribution_denominator,
        "actions": intent_source,
    }

    profile_visits = numeric(raw_metrics.get("profile_visits"))
    follows = numeric(raw_metrics.get("follows"))
    non_follower_reach = numeric(raw_metrics.get("non_follower_reach"))
    output["profile_visits_per_1000_reach"] = (
        safe_divide(float(profile_visits) * 1000, reach)
        if profile_visits is not None and reach is not None
        else None
    )
    preferred_follow_rate = (
        safe_divide(float(follows) * 1000, non_follower_reach)
        if follows is not None and non_follower_reach is not None
        else None
    )
    fallback_follow_rate = (
        safe_divide(float(follows) * 1000, reach)
        if follows is not None and reach is not None
        else None
    )
    output["follows_per_1000_non_follower_reach"] = preferred_follow_rate
    output["follows_per_1000_reach"] = fallback_follow_rate
    if preferred_follow_rate is not None:
        output["follow_conversion"] = {
            "value": preferred_follow_rate,
            "denominator_type": "non_follower_reach",
            "denominator": non_follower_reach,
        }
    elif fallback_follow_rate is not None:
        output["follow_conversion"] = {
            "value": fallback_follow_rate,
            "denominator_type": "reach",
            "denominator": reach,
        }
    else:
        output["follow_conversion"] = {
            "value": None,
            "denominator_type": None,
            "denominator": None,
        }
    output["profile_visit_to_follow_rate"] = (
        safe_divide(follows, profile_visits)
        if follows is not None and profile_visits is not None
        else None
    )

    production_minutes = numeric(content_metadata.get("production_minutes"))
    production_hours = safe_divide(production_minutes, 60)
    output["production_hours"] = production_hours
    output["follows_per_production_hour"] = safe_divide(follows, production_hours)
    output["shares_per_production_hour"] = safe_divide(shares, production_hours)
    output["saves_per_production_hour"] = safe_divide(saves, production_hours)
    output["dm_keyword_hits_per_production_hour"] = safe_divide(
        raw_metrics.get("dm_keyword_hits"), production_hours
    )
    output["watch_hours_per_production_hour"] = safe_divide(
        output["total_watch_hours"], production_hours
    )
    output["reach_per_production_hour"] = safe_divide(reach, production_hours)
    output["views_per_production_hour"] = safe_divide(views, production_hours)
    output["three_second_retention_rate"] = numeric(
        raw_metrics.get("three_second_retention_rate")
    )
    output["three_second_dropoff_rate"] = numeric(
        raw_metrics.get("three_second_dropoff_rate")
    )

    direct_cost = numeric(content_metadata.get("direct_cost_jpy"))
    output["follows_per_1000_jpy"] = (
        safe_divide(float(follows) * 1000, direct_cost)
        if follows is not None and direct_cost is not None
        else None
    )
    output["warnings"] = warnings
    return output


def metric_value(observation: Mapping[str, Any], metric: str) -> Any:
    raw = observation.get("raw_metrics")
    raw = raw if isinstance(raw, Mapping) else {}
    derived = observation.get("derived_metrics")
    derived = derived if isinstance(derived, Mapping) else {}
    if metric == "follow_conversion":
        conversion = derived.get("follow_conversion")
        return conversion.get("value") if isinstance(conversion, Mapping) else None
    if metric in raw:
        return raw.get(metric)
    return derived.get(metric)


def follow_denominator_type(observation: Mapping[str, Any]) -> str | None:
    derived = observation.get("derived_metrics")
    derived = derived if isinstance(derived, Mapping) else {}
    conversion = derived.get("follow_conversion")
    return (
        str(conversion.get("denominator_type"))
        if isinstance(conversion, Mapping) and conversion.get("denominator_type")
        else None
    )


def snapshot_observation(
    snapshot: Mapping[str, Any],
    *,
    metadata: Mapping[str, Any],
    window_name: str,
    plays_semantics_verified: bool = False,
    platform: str = "instagram",
) -> dict[str, Any]:
    raw_metrics = snapshot.get("raw_metrics")
    raw_metrics = dict(raw_metrics) if isinstance(raw_metrics, Mapping) else {}
    derived = compute_post_metrics(
        raw_metrics,
        metadata,
        plays_semantics_verified=plays_semantics_verified,
    )
    if platform == "facebook":
        if numeric(raw_metrics.get("reach")) is not None:
            derived["distribution_denominator_type"] = "unique_media_viewers"
            for metric in (
                "interactions",
                "likes",
                "reactions",
                "comments",
                "shares",
                "saves",
            ):
                derived[f"{metric}_per_1000_unique_media_viewers"] = derived.get(
                    f"{metric}_per_1000_reach"
                )
            conversion = derived.get("follow_conversion")
            if isinstance(conversion, dict) and conversion.get("value") is not None:
                conversion["denominator_type"] = "unique_media_viewers"
                derived["follows_per_1000_unique_media_viewers"] = conversion.get(
                    "value"
                )
            satisfaction = derived.get("satisfaction_rate")
            if isinstance(satisfaction, dict):
                satisfaction["denominator_type"] = "unique_media_viewers"
        elif numeric(raw_metrics.get("views")) is not None:
            derived["distribution_denominator_type"] = "views"
        else:
            derived["distribution_denominator_type"] = None
    return {
        "maturity_window": window_name,
        "target_age_hours": snapshot.get("target_age_hours"),
        "actual_age_hours": snapshot.get("actual_age_hours"),
        "captured_at": snapshot.get("captured_at"),
        "insight_id": snapshot.get("insight_id", snapshot.get("id")),
        "raw_metrics": raw_metrics,
        "metric_provenance": snapshot.get("metric_provenance", {}),
        "derived_metrics": derived,
    }


def growth_curve_metrics(windows: Mapping[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    warnings: list[str] = []
    pairs = (("2h", "24h"), ("24h", "72h"), ("72h", "7d"))
    for earlier, later in pairs:
        earlier_observation = windows.get(earlier)
        later_observation = windows.get(later)
        earlier_observation = (
            earlier_observation if isinstance(earlier_observation, Mapping) else {}
        )
        later_observation = (
            later_observation if isinstance(later_observation, Mapping) else {}
        )
        for metric, prefix in (("reach", "reach"), ("follows", "follow")):
            start = metric_value(earlier_observation, metric)
            end = metric_value(later_observation, metric)
            delta = (
                float(end) - float(start)
                if numeric(start) is not None and numeric(end) is not None
                else None
            )
            key = f"{prefix}_delta_{earlier}_to_{later}"
            output[key] = delta
            if delta is not None and delta < 0:
                warnings.append(f"{key} is negative; the API lifetime total was revised.")
    ratios = (
        ("reach_72h_vs_24h_ratio", "reach", "24h", "72h"),
        ("reach_7d_vs_24h_ratio", "reach", "24h", "7d"),
        ("follow_7d_vs_24h_ratio", "follows", "24h", "7d"),
    )
    for key, metric, earlier, later in ratios:
        start_observation = windows.get(earlier)
        end_observation = windows.get(later)
        start_observation = (
            start_observation if isinstance(start_observation, Mapping) else {}
        )
        end_observation = end_observation if isinstance(end_observation, Mapping) else {}
        output[key] = safe_divide(
            metric_value(end_observation, metric),
            metric_value(start_observation, metric),
        )
    output["warnings"] = warnings
    return output


def canonicalize_post(
    reel: Mapping[str, Any],
    snapshots: Sequence[Mapping[str, Any]],
    annotation: Mapping[str, Any],
    config: Mapping[str, Any],
    *,
    trial: Mapping[str, Any] | None = None,
    as_of: datetime | None = None,
    platform: str = "instagram",
) -> dict[str, Any]:
    trial_data = dict(trial or {})
    generated, generated_provenance, artifact = generation_metadata(
        reel, trial_data, config
    )
    metadata, _ = merge_annotation_metadata(
        generated, generated_provenance, annotation
    )
    duration = numeric(artifact.get("duration_seconds"))

    normalized_snapshots: list[dict[str, Any]] = []
    for raw in snapshots:
        row = dict(raw)
        raw_metrics, metric_provenance = canonical_snapshot_metrics(
            row,
            duration_seconds=duration,
            platform=platform,
        )
        row["raw_metrics"] = raw_metrics
        row["metric_provenance"] = metric_provenance
        normalized_snapshots.append(row)
    snapshot_audit = summarize_snapshot_audit(normalized_snapshots)
    normalized_snapshots, duplicate_count = collapse_exact_snapshot_duplicates(
        normalized_snapshots
    )

    published = parse_datetime(reel.get("published_at"))
    media_id = str(reel.get("media_id") or "")
    windows: dict[str, Any] = {}
    plays_semantics_verified = bool(
        config.get("metric_semantics", {}).get("plays_semantics_verified", False)
    )
    for name in WINDOW_ORDER:
        window_config = config["maturity_windows"][name]
        selected = select_window_snapshot(
            normalized_snapshots,
            published,
            window_config,
            media_id=media_id,
            as_of=as_of,
        )
        windows[name] = (
            snapshot_observation(
                selected,
                metadata=metadata,
                window_name=name,
                plays_semantics_verified=plays_semantics_verified,
                platform=platform,
            )
            if selected is not None
            else None
        )
    latest = latest_snapshot(
        normalized_snapshots, media_id=media_id, as_of=as_of
    )
    if latest is not None and published is not None:
        captured = parse_datetime(latest.get("captured_at"))
        latest["actual_age_hours"] = (
            (captured - published).total_seconds() / 3600 if captured else None
        )
        latest["target_age_hours"] = None
    windows["latest"] = (
        snapshot_observation(
            latest,
            metadata=metadata,
            window_name="latest",
            plays_semantics_verified=plays_semantics_verified,
            platform=platform,
        )
        if latest is not None
        else None
    )

    fetched_at = windows["latest"].get("captured_at") if windows["latest"] else None
    post_age_hours = (
        windows["latest"].get("actual_age_hours") if windows["latest"] else None
    )
    permalink = str(reel.get("permalink") or "")
    if platform == "facebook" and permalink.startswith("/"):
        permalink = f"https://www.facebook.com{permalink}"
    elif platform == "facebook" and not permalink and media_id:
        permalink = f"https://www.facebook.com/reel/{media_id}/"
    identity = {
        "media_id": media_id,
        "content_hash": str(reel.get("content_hash") or ""),
        "account": str(reel.get("channel_id") or ""),
        "platform": platform,
        "permalink": permalink,
        "caption": str(reel.get("caption") or ""),
        "published_at": str(reel.get("published_at") or ""),
        "fetched_at": fetched_at,
        "post_age_hours": post_age_hours,
    }
    return {
        "identity": identity,
        "content_metadata": metadata,
        "generation_artifact": artifact,
        "maturity_windows": windows,
        "growth_curve_metrics": growth_curve_metrics(windows),
        "trial_experiment": trial_data,
        "snapshot_count": len(normalized_snapshots),
        "collapsed_exact_duplicate_snapshots": duplicate_count,
        "snapshot_audit": snapshot_audit,
        "classifications": [],
        "funnel_diagnostics": [],
    }


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"file:{db_path.expanduser().resolve()}?mode=ro", uri=True
    )
    connection.row_factory = sqlite3.Row
    return connection


def _existing_table(
    connection: sqlite3.Connection,
    candidates: Sequence[str],
) -> str | None:
    existing = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    return next((name for name in candidates if name in existing), None)


def _first_value(row: Mapping[str, Any], names: Sequence[str]) -> Any:
    for name in names:
        if name in row and row.get(name) is not None:
            return row.get(name)
    return None


def _raw_object(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return value


def _nested_numeric(value: Any, names: set[str]) -> int | float | None:
    """Find a named finite value in a stored raw object without guessing units."""
    value = _raw_object(value)
    if isinstance(value, Mapping):
        for name in names:
            found = numeric(value.get(name))
            if found is not None:
                return found
        metric_name = str(value.get("name") or "").strip()
        if metric_name in names:
            for candidate in (
                value.get("value"),
                (value.get("total_value") or {}).get("value")
                if isinstance(value.get("total_value"), Mapping)
                else None,
            ):
                found = numeric(candidate)
                if found is not None:
                    return found
            rows = value.get("values")
            if isinstance(rows, list):
                for candidate in reversed(rows):
                    if isinstance(candidate, Mapping):
                        found = numeric(candidate.get("value"))
                        if found is not None:
                            return found
        for nested in value.values():
            found = _nested_numeric(nested, names)
            if found is not None:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _nested_numeric(nested, names)
            if found is not None:
                return found
    return None


def _account_metric(
    row: Mapping[str, Any],
    column_names: Sequence[str],
    raw_names: Sequence[str] = (),
) -> int | float | None:
    value = numeric(_first_value(row, column_names))
    if value is not None:
        return value
    names = {str(name) for name in (*column_names, *raw_names)}
    return _nested_numeric(row.get("raw"), names) if names else None


def _account_source(
    row: Mapping[str, Any],
    *,
    table: str,
    field: str,
) -> dict[str, Any]:
    return {
        "table": table,
        "field": field,
        "graph_api_version": row.get("graph_api_version"),
        "graph_api_root": row.get("graph_api_root"),
        "login_type": row.get("login_type"),
        "provenance": row.get("provenance"),
    }


def _query_account_rows(
    connection: sqlite3.Connection,
    table: str | None,
    *,
    channel: str,
) -> list[dict[str, Any]]:
    if table is None:
        return []
    columns = _table_columns(connection, table)
    order_columns = [
        name
        for name in ("fetched_at", "observed_since", "observed_until", "id")
        if name in columns
    ]
    order_sql = f" ORDER BY {', '.join(order_columns)}" if order_columns else ""
    if "channel_id" in columns:
        rows = connection.execute(
            f"SELECT * FROM {table} WHERE channel_id=?{order_sql}",
            (channel,),
        ).fetchall()
    elif "account" in columns:
        rows = connection.execute(
            f"SELECT * FROM {table} WHERE account=?{order_sql}",
            (channel,),
        ).fetchall()
    else:
        rows = connection.execute(f"SELECT * FROM {table}{order_sql}").fetchall()
    return [dict(row) for row in rows]


def _normalize_account_stock_snapshots(
    rows: Sequence[Mapping[str, Any]],
    *,
    table: str | None,
    as_of: datetime,
) -> list[dict[str, Any]]:
    if table is None:
        return []
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for raw in rows:
        row = dict(raw)
        fetched = parse_datetime(
            _first_value(row, ("fetched_at", "captured_at", "observed_at"))
        )
        if fetched is None or fetched > as_of:
            continue
        followers = _account_metric(
            row,
            ("followers_count", "follower_count"),
            ("followers",),
        )
        media_count = _account_metric(row, ("media_count",))
        key = (
            str(row.get("ig_user_id") or ""),
            isoformat_seconds(fetched),
            followers,
            media_count,
        )
        if key in seen:
            continue
        seen.add(key)
        normalized.append(
            {
                "fetched_at": isoformat_seconds(fetched),
                "followers_count": followers,
                "media_count": media_count,
                "account": row.get("account"),
                "ig_user_id": row.get("ig_user_id"),
                "source": _account_source(
                    row,
                    table=table,
                    field="followers_count",
                ),
            }
        )
    normalized.sort(
        key=lambda row: (
            str(row.get("fetched_at") or ""),
            str(row.get("ig_user_id") or ""),
        )
    )
    return normalized


def _normalize_account_flow_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    table: str | None,
    config: Mapping[str, Any],
    as_of: datetime,
) -> tuple[list[dict[str, Any]], int]:
    if table is None:
        return [], 0
    account_config = config.get("account_growth")
    account_config = account_config if isinstance(account_config, Mapping) else {}
    expected_hours = float(
        numeric(account_config.get("expected_daily_interval_hours")) or 24.0
    )
    tolerance_hours = float(
        numeric(account_config.get("daily_interval_tolerance_hours")) or 2.0
    )
    preliminary_hours = float(
        numeric(account_config.get("preliminary_hours_after_interval_end")) or 48.0
    )

    # A later fetch of the exact same source interval supersedes the older
    # fetch in the report view. Both remain append-only in the ledger.
    by_interval: dict[tuple[str, str], tuple[datetime, int, dict[str, Any]]] = {}
    invalid_rows = 0
    for raw in rows:
        row = dict(raw)
        observed_since = parse_datetime(
            _first_value(
                row,
                ("observed_since", "interval_start", "period_start", "start_time"),
            )
        )
        observed_until = parse_datetime(
            _first_value(
                row,
                ("observed_until", "interval_end", "period_end", "end_time"),
            )
        )
        fetched = parse_datetime(
            _first_value(row, ("fetched_at", "captured_at", "observed_at"))
        )
        if (
            observed_since is None
            or observed_until is None
            or fetched is None
            or observed_until <= observed_since
            or fetched > as_of
            or observed_until > as_of
        ):
            invalid_rows += 1
            continue
        interval_key = (
            isoformat_seconds(observed_since),
            isoformat_seconds(observed_until),
        )
        candidate = (fetched, int(row.get("id") or -1), row)
        current = by_interval.get(interval_key)
        if current is None or candidate[:2] > current[:2]:
            by_interval[interval_key] = candidate

    normalized: list[dict[str, Any]] = []
    for (since_text, until_text), (fetched, _, row) in sorted(by_interval.items()):
        observed_since = parse_datetime(since_text)
        observed_until = parse_datetime(until_text)
        if observed_since is None or observed_until is None:
            continue
        interval_hours = (
            observed_until - observed_since
        ).total_seconds() / 3600.0
        follows = _account_metric(
            row,
            ("follows", "gross_follows", "follow_count"),
        )
        unfollows = _account_metric(
            row,
            ("unfollows", "gross_unfollows", "unfollow_count"),
        )
        unknown = _account_metric(row, ("unknown", "unknown_follow_flow"))
        account_reach = _account_metric(
            row,
            ("reach", "account_reach"),
            ("daily_reach",),
        )
        reel_reach = _account_metric(row, ("reel_reach",))
        reel_non_follower_reach = _account_metric(
            row,
            ("reel_non_follower_reach",),
        )
        reel_follower_reach = _account_metric(
            row,
            ("reel_follower_reach",),
        )
        reel_views = _account_metric(row, ("reel_views",))
        reel_likes = _account_metric(row, ("reel_likes",))
        reel_comments = _account_metric(row, ("reel_comments",))
        reel_saves = _account_metric(row, ("reel_saves",))
        reel_shares = _account_metric(row, ("reel_shares",))
        reel_total_interactions = _account_metric(
            row,
            ("reel_total_interactions",),
        )
        reel_content_breakdown_fetched = _account_metric(
            row,
            ("reel_content_breakdown_fetched",),
        )
        reel_audience_breakdown_fetched = _account_metric(
            row,
            ("reel_audience_breakdown_fetched",),
        )
        net = (
            float(follows) - float(unfollows)
            if follows is not None and unfollows is not None
            else None
        )
        age_after_interval = (as_of - observed_until).total_seconds() / 3600.0
        is_daily = abs(interval_hours - expected_hours) <= tolerance_hours
        normalized.append(
            {
                "observed_since": since_text,
                "observed_until": until_text,
                "fetched_at": isoformat_seconds(fetched),
                "interval_hours": interval_hours,
                "is_daily_interval": is_daily,
                "follows": follows,
                "unfollows": unfollows,
                "unknown": unknown,
                "net_growth": net,
                "account_reach": account_reach,
                "reel_reach": reel_reach,
                "reel_non_follower_reach": reel_non_follower_reach,
                "reel_follower_reach": reel_follower_reach,
                "reel_views": reel_views,
                "reel_likes": reel_likes,
                "reel_comments": reel_comments,
                "reel_saves": reel_saves,
                "reel_shares": reel_shares,
                "reel_total_interactions": reel_total_interactions,
                "reel_content_breakdown_fetched": (
                    bool(reel_content_breakdown_fetched)
                    if reel_content_breakdown_fetched is not None
                    else None
                ),
                "reel_audience_breakdown_fetched": (
                    bool(reel_audience_breakdown_fetched)
                    if reel_audience_breakdown_fetched is not None
                    else None
                ),
                "gross_follows_per_1000_account_reach": (
                    safe_divide(float(follows) * 1000, account_reach)
                    if follows is not None
                    else None
                ),
                "gross_follows_per_1000_reel_reach": (
                    safe_divide(float(follows) * 1000, reel_reach)
                    if follows is not None
                    else None
                ),
                "gross_follows_per_1000_reel_non_follower_reach": (
                    safe_divide(float(follows) * 1000, reel_non_follower_reach)
                    if follows is not None
                    else None
                ),
                "reel_interactions_per_1000_reel_reach": (
                    safe_divide(float(reel_total_interactions) * 1000, reel_reach)
                    if reel_total_interactions is not None
                    else None
                ),
                "reel_views_per_reached_account": safe_divide(
                    reel_views,
                    reel_reach,
                ),
                "net_follows_per_1000_account_reach": (
                    safe_divide(float(net) * 1000, account_reach)
                    if net is not None
                    else None
                ),
                "preliminary": age_after_interval < preliminary_hours,
                "hours_since_interval_end": age_after_interval,
                "included_in_aggregate": False,
                "exclusion_reason": (
                    None
                    if is_daily
                    else (
                        f"interval is {interval_hours:.2f}h; expected "
                        f"{expected_hours:.2f}±{tolerance_hours:.2f}h"
                    )
                ),
                "source": _account_source(
                    row,
                    table=table,
                    field=(
                        "follows, unfollows, unknown, reach, reel_reach, "
                        "reel_non_follower_reach, reel_follower_reach, "
                        "reel_views, reel_likes, reel_comments, reel_saves, "
                        "reel_shares, reel_total_interactions, "
                        "reel_content_breakdown_fetched, "
                        "reel_audience_breakdown_fetched"
                    ),
                ),
            }
        )

    previous_end: datetime | None = None
    for row in normalized:
        if not row["is_daily_interval"]:
            continue
        start = parse_datetime(row["observed_since"])
        end = parse_datetime(row["observed_until"])
        if start is None or end is None:
            continue
        if previous_end is not None and start < previous_end:
            row["exclusion_reason"] = (
                "overlaps a prior included daily interval; excluded to prevent double-counting"
            )
            continue
        row["included_in_aggregate"] = True
        previous_end = end
    return normalized, invalid_rows


def _complete_sum(
    rows: Sequence[Mapping[str, Any]],
    field: str,
) -> tuple[int | float | None, int | float | None, dict[str, Any]]:
    values = [numeric(row.get(field)) for row in rows]
    known = [float(value) for value in values if value is not None]
    known_total: int | float | None = None
    if known:
        raw_total = sum(known)
        known_total = int(raw_total) if raw_total.is_integer() else raw_total
    complete_total = known_total if rows and len(known) == len(rows) else None
    return complete_total, known_total, coverage_entry(len(known), len(rows))


def summarize_account_growth(
    *,
    snapshots: Sequence[Mapping[str, Any]],
    flow_rows: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
    as_of: datetime,
    snapshot_table: str | None,
    flow_table: str | None,
    invalid_flow_rows: int = 0,
) -> dict[str, Any]:
    account_config = config.get("account_growth")
    account_config = account_config if isinstance(account_config, Mapping) else {}
    stale_after_hours = float(
        numeric(account_config.get("stale_after_hours")) or 48.0
    )
    preliminary_hours = float(
        numeric(account_config.get("preliminary_hours_after_interval_end")) or 48.0
    )
    minimum_trend_rows = int(
        numeric(account_config.get("minimum_finalized_daily_intervals_for_trend"))
        or 7
    )
    lookback = int(
        numeric(account_config.get("trend_lookback_intervals")) or 14
    )

    stock = [dict(row) for row in snapshots]
    latest_stock = stock[-1] if stock else None
    first_stock = stock[0] if stock else None
    stock_lag = None
    if latest_stock:
        fetched = parse_datetime(latest_stock.get("fetched_at"))
        if fetched is not None:
            stock_lag = max(0.0, (as_of - fetched).total_seconds() / 3600.0)
    first_count = numeric(first_stock.get("followers_count")) if first_stock else None
    latest_count = numeric(latest_stock.get("followers_count")) if latest_stock else None
    stock_change = (
        float(latest_count) - float(first_count)
        if latest_count is not None and first_count is not None and len(stock) >= 2
        else None
    )

    daily = [dict(row) for row in flow_rows if row.get("is_daily_interval")]
    included = [row for row in daily if row.get("included_in_aggregate")]
    follows, known_follows, follows_coverage = _complete_sum(included, "follows")
    unfollows, known_unfollows, unfollows_coverage = _complete_sum(
        included, "unfollows"
    )
    reach, known_reach, reach_coverage = _complete_sum(included, "account_reach")
    reel_reach, known_reel_reach, reel_reach_coverage = _complete_sum(
        included,
        "reel_reach",
    )
    (
        reel_non_follower_reach,
        known_reel_non_follower_reach,
        reel_non_follower_reach_coverage,
    ) = _complete_sum(included, "reel_non_follower_reach")
    (
        reel_follower_reach,
        known_reel_follower_reach,
        reel_follower_reach_coverage,
    ) = _complete_sum(included, "reel_follower_reach")
    reel_content_fetch_coverage = coverage_entry(
        sum(row.get("reel_content_breakdown_fetched") is True for row in included),
        len(included),
    )
    reel_audience_fetch_coverage = coverage_entry(
        sum(row.get("reel_audience_breakdown_fetched") is True for row in included),
        len(included),
    )
    net_growth = (
        float(follows) - float(unfollows)
        if follows is not None and unfollows is not None
        else None
    )
    known_net_growth = (
        float(known_follows) - float(known_unfollows)
        if known_follows is not None and known_unfollows is not None
        else None
    )
    gross_rate = (
        safe_divide(float(follows) * 1000, reach)
        if follows is not None
        else None
    )
    net_rate = (
        safe_divide(float(net_growth) * 1000, reach)
        if net_growth is not None
        else None
    )
    reel_reach_rate = (
        safe_divide(float(follows) * 1000, reel_reach)
        if follows is not None
        else None
    )
    reel_non_follower_reach_rate = (
        safe_divide(float(follows) * 1000, reel_non_follower_reach)
        if follows is not None
        else None
    )

    expected_days = 0
    if included:
        first_start = parse_datetime(included[0].get("observed_since"))
        last_end = parse_datetime(included[-1].get("observed_until"))
        if first_start is not None and last_end is not None:
            expected_days = max(
                1,
                math.ceil((last_end - first_start).total_seconds() / 86400.0),
            )
    interval_coverage = coverage_entry(len(included), expected_days)
    overlap_count = sum(
        row.get("is_daily_interval") and not row.get("included_in_aggregate")
        for row in flow_rows
    )
    non_daily_count = sum(not row.get("is_daily_interval") for row in flow_rows)

    latest_interval_end = (
        max(
            (
                parsed
                for row in daily
                if (parsed := parse_datetime(row.get("observed_until"))) is not None
            ),
            default=None,
        )
        if daily
        else None
    )
    latest_flow_fetch = max(
        (
            parsed
            for row in flow_rows
            if (parsed := parse_datetime(row.get("fetched_at"))) is not None
        ),
        default=None,
    )
    flow_data_lag = (
        max(0.0, (as_of - latest_interval_end).total_seconds() / 3600.0)
        if latest_interval_end is not None
        else None
    )
    flow_fetch_lag = (
        max(0.0, (as_of - latest_flow_fetch).total_seconds() / 3600.0)
        if latest_flow_fetch is not None
        else None
    )
    preliminary_count = sum(bool(row.get("preliminary")) for row in included)

    finalized = [
        row
        for row in included
        if not row.get("preliminary") and numeric(row.get("net_growth")) is not None
    ][-lookback:]
    trend_net = [row["net_growth"] for row in finalized]
    trend = {
        "status": (
            "AVAILABLE"
            if len(finalized) >= minimum_trend_rows
            else "INSUFFICIENT DATA"
        ),
        "evidence_status": "descriptive_account_level_not_causal",
        "lookback_intervals": len(finalized),
        "minimum_intervals": minimum_trend_rows,
        "median_daily_gross_follows": percentile(
            [row.get("follows") for row in finalized],
            0.5,
        ),
        "median_daily_unfollows": percentile(
            [row.get("unfollows") for row in finalized],
            0.5,
        ),
        "median_daily_net_growth": percentile(trend_net, 0.5),
        "positive_net_days": sum(float(value) > 0 for value in trend_net),
        "negative_net_days": sum(float(value) < 0 for value in trend_net),
        "zero_net_days": sum(float(value) == 0 for value in trend_net),
        "attribution_scope": "account",
    }

    has_any = bool(stock or flow_rows)
    complete_flows = bool(
        included
        and follows is not None
        and unfollows is not None
        and len(included) == expected_days
        and not overlap_count
        and not invalid_flow_rows
    )
    stale = bool(
        (stock_lag is not None and stock_lag > stale_after_hours)
        or (flow_fetch_lag is not None and flow_fetch_lag > stale_after_hours)
    )
    if not has_any:
        status = "UNAVAILABLE"
    elif preliminary_count:
        status = "PRELIMINARY"
    elif stale:
        status = "STALE"
    elif not complete_flows:
        status = "PARTIAL"
    else:
        status = "AVAILABLE"

    warnings = [ACCOUNT_ATTRIBUTION_WARNING]
    warnings.append(
        "Daily Reel reach covers every Reel viewed in the interval, including older "
        "Reels; the linked newly published Reels are timing context only."
    )
    warnings.append(
        "Summed daily reach is a reach-day denominator and may count the same account "
        "on more than one day."
    )
    if snapshot_table is None:
        warnings.append("Account follower-stock table is unavailable in this ledger.")
    if flow_table is None:
        warnings.append("Account follow-flow table is unavailable in this ledger.")
    if invalid_flow_rows:
        warnings.append(
            f"{invalid_flow_rows} flow rows were excluded because their timestamps "
            "were invalid, future-dated, or incomplete."
        )
    if overlap_count:
        warnings.append(
            f"{overlap_count} overlapping daily flow rows were excluded from aggregates."
        )
    if non_daily_count:
        warnings.append(
            f"{non_daily_count} non-daily flow rows are shown for provenance but "
            "excluded from daily aggregates."
        )

    return {
        "status": status,
        "attribution_scope": "account_only",
        "follower_stock": {
            "latest": latest_count,
            "latest_fetched_at": (
                latest_stock.get("fetched_at") if latest_stock else None
            ),
            "first": first_count,
            "first_fetched_at": first_stock.get("fetched_at") if first_stock else None,
            "snapshot_change": stock_change,
            "snapshot_count": len(stock),
            "source_field": "followers_count",
            "source_table": snapshot_table,
        },
        "gross_follows": follows,
        "known_gross_follows": known_follows,
        "unfollows": unfollows,
        "known_unfollows": known_unfollows,
        "net_growth": net_growth,
        "known_net_growth": known_net_growth,
        "account_reach": reach,
        "known_account_reach": known_reach,
        "reel_reach": reel_reach,
        "known_reel_reach": known_reel_reach,
        "reel_non_follower_reach": reel_non_follower_reach,
        "known_reel_non_follower_reach": known_reel_non_follower_reach,
        "reel_follower_reach": reel_follower_reach,
        "known_reel_follower_reach": known_reel_follower_reach,
        "gross_follows_per_1000_account_reach": gross_rate,
        "net_follows_per_1000_account_reach": net_rate,
        "gross_follows_per_1000_reel_reach": reel_reach_rate,
        "gross_follows_per_1000_reel_non_follower_reach": (
            reel_non_follower_reach_rate
        ),
        "coverage": {
            "stock_snapshots": {
                "count": len(stock),
                "table_available": snapshot_table is not None,
            },
            "flow_rows": {
                "count": len(flow_rows),
                "table_available": flow_table is not None,
                "invalid_rows": invalid_flow_rows,
            },
            "daily_intervals": interval_coverage,
            "follows": follows_coverage,
            "unfollows": unfollows_coverage,
            "account_reach": reach_coverage,
            "reel_reach": reel_reach_coverage,
            "reel_non_follower_reach": reel_non_follower_reach_coverage,
            "reel_follower_reach": reel_follower_reach_coverage,
            "reel_content_breakdown_fetch": reel_content_fetch_coverage,
            "reel_audience_breakdown_fetch": reel_audience_fetch_coverage,
            "overlapping_daily_intervals": overlap_count,
            "non_daily_intervals": non_daily_count,
        },
        "lag": {
            "follower_stock_fetch_lag_hours": stock_lag,
            "flow_data_lag_hours": flow_data_lag,
            "flow_fetch_lag_hours": flow_fetch_lag,
            "stale_after_hours": stale_after_hours,
            "stale": stale,
        },
        "preliminary": {
            "status": preliminary_count > 0,
            "daily_interval_count": preliminary_count,
            "finalization_lag_hours": preliminary_hours,
            "reason": (
                f"{preliminary_count} daily interval(s) ended less than "
                f"{preliminary_hours:g} hours before the report cutoff and may revise."
                if preliminary_count
                else "No included daily interval is inside the configured revision window."
            ),
        },
        "daily": daily,
        "daily_intervals": daily,
        "other_intervals": [
            dict(row) for row in flow_rows if not row.get("is_daily_interval")
        ],
        "stock_snapshots": stock,
        "trend": trend,
        "source_labels": {
            "follower_stock": (
                "Instagram Graph account followers_count point-in-time stock"
            ),
            "gross_follows": (
                "Instagram Graph account follows flow over explicit daily intervals"
            ),
            "unfollows": (
                "Instagram Graph account unfollows flow over explicit daily intervals"
            ),
            "account_reach": (
                "Instagram Graph account reach over the same explicit daily intervals"
            ),
            "reel_reach": (
                "Instagram Graph account-day reach filtered to media_product_type=REEL"
            ),
            "reel_non_follower_reach": (
                "Instagram Graph account-day reach filtered to REEL and NON_FOLLOWER"
            ),
            "post_follows": (
                "Unavailable; account flow is never copied to a Reel"
            ),
        },
        "denominator_labels": {
            "gross_follows_per_1000_account_reach": (
                "gross account follows / account-wide reach × 1,000 over the same "
                "included daily intervals; denominator is never Reel reach"
            ),
            "net_follows_per_1000_account_reach": (
                "net account follows / account-wide reach × 1,000 over the same "
                "included daily intervals; denominator is never Reel reach"
            ),
            "gross_follows_per_1000_reel_reach": (
                "gross account follows / account-day Reel reach × 1,000 over the "
                "same interval; observational, not post attribution"
            ),
            "gross_follows_per_1000_reel_non_follower_reach": (
                "gross account follows / account-day non-follower Reel reach × 1,000 "
                "over the same interval; observational, not post attribution"
            ),
        },
        "warnings": warnings,
    }


def add_account_publication_context(
    account_growth: dict[str, Any],
    posts: Sequence[Mapping[str, Any]],
) -> None:
    """Add time-overlap context only; never divide or allocate account flows."""
    daily = account_growth.get("daily_intervals")
    if not isinstance(daily, list):
        return
    published: list[tuple[datetime, str]] = []
    for post in posts:
        identity = post.get("identity")
        identity = identity if isinstance(identity, Mapping) else {}
        published_at = parse_datetime(identity.get("published_at"))
        if published_at is None:
            continue
        published.append((published_at, str(identity.get("media_id") or "")))
    published.sort(key=lambda row: (row[0], row[1]))

    for row in daily:
        if not isinstance(row, dict):
            continue
        observed_since = parse_datetime(row.get("observed_since"))
        observed_until = parse_datetime(row.get("observed_until"))
        media_ids = (
            [
                media_id
                for published_at, media_id in published
                if observed_since <= published_at < observed_until
            ]
            if observed_since is not None and observed_until is not None
            else []
        )
        row["publication_context"] = {
            "published_post_count": len(media_ids),
            "media_ids": media_ids,
            "evidence_status": "time_overlap_context_not_attribution",
            "warning": ACCOUNT_ATTRIBUTION_WARNING,
        }
    # Keep the short alias and explicit name identical after enrichment.
    account_growth["daily"] = daily


def load_account_growth(
    *,
    db_path: Path,
    channel: str,
    config: Mapping[str, Any],
    as_of: datetime,
) -> dict[str, Any]:
    """Load account-wide follower stock/flows without joining them to posts."""
    with closing(_open_readonly(db_path)) as connection:
        snapshot_table = _existing_table(
            connection,
            ACCOUNT_FOLLOWER_SNAPSHOT_TABLES,
        )
        flow_table = _existing_table(
            connection,
            ACCOUNT_FOLLOWER_FLOW_TABLES,
        )
        snapshot_rows = _query_account_rows(
            connection,
            snapshot_table,
            channel=channel,
        )
        flow_rows = _query_account_rows(
            connection,
            flow_table,
            channel=channel,
        )
    snapshots = _normalize_account_stock_snapshots(
        snapshot_rows,
        table=snapshot_table,
        as_of=as_of,
    )
    normalized_flows, invalid_rows = _normalize_account_flow_rows(
        flow_rows,
        table=flow_table,
        config=config,
        as_of=as_of,
    )
    return summarize_account_growth(
        snapshots=snapshots,
        flow_rows=normalized_flows,
        config=config,
        as_of=as_of,
        snapshot_table=snapshot_table,
        flow_table=flow_table,
        invalid_flow_rows=invalid_rows,
    )


def load_canonical_posts(
    *,
    db_path: Path,
    channel: str,
    config: Mapping[str, Any],
    annotations: Sequence[Mapping[str, Any]],
    as_of: datetime | None = None,
    platform: str = "instagram",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read the ledger as the canonical source; generation files only enrich it."""
    with closing(_open_readonly(db_path)) as connection:
        insight_columns = _table_columns(connection, "insights")
        reel_columns = _table_columns(connection, "reels")
        trial_columns = _table_columns(connection, "trial_experiments")
        required_reels = {
            "content_hash",
            "channel_id",
            "clip_dir",
            "media_path",
            "source_video",
            "title",
            "caption",
            "scheduled_at",
            "published_at",
            "media_id",
            "permalink",
            "manifest_path",
        }
        selected_reel_columns = sorted(required_reels & reel_columns)
        if "trial_reel" in reel_columns:
            selected_reel_columns.append("trial_reel")
        reel_rows = connection.execute(
            f"SELECT {', '.join(selected_reel_columns)} FROM reels "
            "WHERE channel_id=? AND status='published' "
            "ORDER BY published_at, content_hash",
            (channel,),
        ).fetchall()

        selected_insight_columns = [
            name
            for name in (
                "id",
                "content_hash",
                "channel_id",
                "media_id",
                "captured_at",
                "views",
                "total_views",
                "reach",
                "likes",
                "total_likes",
                "comments",
                "total_comments",
                "saved",
                "shares",
                "total_interactions",
                "ig_reels_video_view_total_time",
                "ig_reels_avg_watch_time",
                "reels_skip_rate",
                "clips_replays_count",
                "facebook_views",
                "crossposted_views",
                "follows",
                "raw",
            )
            if name in insight_columns
        ]
        insight_rows = connection.execute(
            f"SELECT {', '.join(selected_insight_columns)} FROM insights "
            "WHERE channel_id=? ORDER BY captured_at, id",
            (channel,),
        ).fetchall()

        trial_rows: list[sqlite3.Row] = []
        if {"content_hash", "channel_id"}.issubset(trial_columns):
            trial_rows = connection.execute(
                "SELECT * FROM trial_experiments WHERE channel_id=? "
                "ORDER BY created_at, experiment_id",
                (channel,),
            ).fetchall()

    snapshots_by_identity: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in insight_rows:
        data = dict(row)
        key = (
            str(data.get("content_hash") or ""),
            str(data.get("channel_id") or ""),
            str(data.get("media_id") or ""),
        )
        snapshots_by_identity[key].append(data)
    trials_by_reel = {
        (str(row["content_hash"] or ""), str(row["channel_id"] or "")): dict(row)
        for row in trial_rows
    }

    posts: list[dict[str, Any]] = []
    for row in reel_rows:
        reel = dict(row)
        media_id = str(reel.get("media_id") or "")
        content_hash = str(reel.get("content_hash") or "")
        key = (content_hash, channel, media_id)
        annotation = annotation_for_post(
            annotations,
            account=channel,
            media_id=media_id,
            content_hash=content_hash,
        )
        posts.append(
            canonicalize_post(
                reel,
                snapshots_by_identity.get(key, []),
                annotation,
                config,
                trial=trials_by_reel.get((content_hash, channel), {}),
                as_of=as_of,
                platform=platform,
            )
        )
    inventory = {
        "published_reels": len(reel_rows),
        "stored_snapshot_rows": len(insight_rows),
        "trial_experiments": len(trial_rows),
        "insight_columns": sorted(insight_columns),
    }
    return posts, inventory


def observations_for_window(
    posts: Sequence[Mapping[str, Any]], window_name: str
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for post in posts:
        windows = post.get("maturity_windows")
        windows = windows if isinstance(windows, Mapping) else {}
        raw_observation = windows.get(window_name)
        if not isinstance(raw_observation, Mapping):
            continue
        observation = dict(raw_observation)
        observation["identity"] = post.get("identity", {})
        observation["content_metadata"] = post.get("content_metadata", {})
        output.append(observation)
    return output


COHORT_METRICS = (
    "reach",
    "watch_depth",
    "shares_per_1000_reach",
    "saves_per_1000_reach",
    "intent_actions_per_1000_reach",
    "follows_per_production_hour",
    "production_hours",
    "follows",
    "intent_actions",
    "profile_visits_per_1000_reach",
    "profile_visit_to_follow_rate",
    "returning_viewers",
)


def _metric_distribution(
    observations: Sequence[Mapping[str, Any]],
    metric: str,
    *,
    quartiles_min_n: int,
    denominator_type: str | None = None,
) -> dict[str, Any]:
    selected = observations
    if metric == "follow_conversion" and denominator_type is not None:
        selected = [
            observation
            for observation in observations
            if follow_denominator_type(observation) == denominator_type
        ]
    return distribution(
        [metric_value(observation, metric) for observation in selected],
        quartiles_min_n=quartiles_min_n,
    )


def summarize_cohort(
    observations: Sequence[Mapping[str, Any]],
    *,
    maturity_window: str,
    dimension: str,
    value: str,
    quartiles_min_n: int,
    account_baseline: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    reaches = [metric_value(observation, "reach") for observation in observations]
    known_reaches = finite_values(reaches)
    summary: dict[str, Any] = {
        "maturity_window": maturity_window,
        "dimension": dimension,
        "value": value,
        "post_count": len(observations),
        "total_reach": sum(known_reaches) if len(known_reaches) == len(observations) else None,
        "known_total_reach": sum(known_reaches) if known_reaches else None,
        "reach_coverage": {
            "count": len(known_reaches),
            "total": len(observations),
        },
        "metrics": {},
    }
    for metric in COHORT_METRICS:
        summary["metrics"][metric] = _metric_distribution(
            observations,
            metric,
            quartiles_min_n=quartiles_min_n,
        )
    summary["metrics"]["follow_conversion"] = {
        denominator: _metric_distribution(
            observations,
            "follow_conversion",
            quartiles_min_n=quartiles_min_n,
            denominator_type=denominator,
        )
        for denominator in ("non_follower_reach", "reach")
    }

    above_account: dict[str, Any] = {}
    above_cohort: dict[str, Any] = {}
    for metric in COHORT_METRICS:
        values = [
            float(value)
            for observation in observations
            if (value := numeric(metric_value(observation, metric))) is not None
        ]
        cohort_median = summary["metrics"][metric]["median"]
        account_median = None
        if isinstance(account_baseline, Mapping):
            account_metric = account_baseline.get("metrics", {}).get(metric, {})
            if isinstance(account_metric, Mapping):
                account_median = account_metric.get("median")
        elif dimension == "account":
            account_median = cohort_median
        above_cohort[metric] = (
            sum(value > float(cohort_median) for value in values) / len(values) * 100
            if values and numeric(cohort_median) is not None
            else None
        )
        above_account[metric] = (
            sum(value > float(account_median) for value in values) / len(values) * 100
            if values and numeric(account_median) is not None
            else None
        )
    follow_above_cohort: dict[str, Any] = {}
    follow_above_account: dict[str, Any] = {}
    for denominator in ("non_follower_reach", "reach"):
        values = [
            float(value)
            for observation in observations
            if follow_denominator_type(observation) == denominator
            and (value := numeric(metric_value(observation, "follow_conversion")))
            is not None
        ]
        cohort_median = summary["metrics"]["follow_conversion"][denominator][
            "median"
        ]
        account_median = None
        if isinstance(account_baseline, Mapping):
            account_follow = (
                account_baseline.get("metrics", {})
                .get("follow_conversion", {})
                .get(denominator, {})
            )
            if isinstance(account_follow, Mapping):
                account_median = account_follow.get("median")
        elif dimension == "account":
            account_median = cohort_median
        follow_above_cohort[denominator] = (
            sum(value > float(cohort_median) for value in values) / len(values) * 100
            if values and numeric(cohort_median) is not None
            else None
        )
        follow_above_account[denominator] = (
            sum(value > float(account_median) for value in values) / len(values) * 100
            if values and numeric(account_median) is not None
            else None
        )
    above_cohort["follow_conversion"] = follow_above_cohort
    above_account["follow_conversion"] = follow_above_account
    summary["percentage_above_account_median"] = above_account
    summary["percentage_above_comparable_cohort_median"] = above_cohort
    return summary


def build_cohorts(
    observations: Sequence[Mapping[str, Any]],
    dimensions: Sequence[str],
    config: Mapping[str, Any],
) -> dict[str, Any]:
    if not observations:
        return {"account": None, "by_dimension": {}}
    maturity_window = str(observations[0].get("maturity_window") or "")
    quartiles_min_n = int(
        config.get("statistics", {}).get("minimum_posts_for_percentiles", 4)
    )
    account = summarize_cohort(
        observations,
        maturity_window=maturity_window,
        dimension="account",
        value="all",
        quartiles_min_n=quartiles_min_n,
    )
    by_dimension: dict[str, list[dict[str, Any]]] = {}
    for dimension in dimensions:
        groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for observation in observations:
            metadata = observation.get("content_metadata")
            metadata = metadata if isinstance(metadata, Mapping) else {}
            value = metadata.get(dimension)
            if value is None or str(value).strip() == "":
                continue
            groups[str(value)].append(observation)
        by_dimension[dimension] = [
            summarize_cohort(
                rows,
                maturity_window=maturity_window,
                dimension=dimension,
                value=value,
                quartiles_min_n=quartiles_min_n,
                account_baseline=account,
            )
            for value, rows in sorted(groups.items())
        ]
    return {"account": account, "by_dimension": by_dimension}


def choose_comparison_baseline(
    observation: Mapping[str, Any],
    all_observations: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Use one deterministic within-maturity hierarchy; never cross windows."""
    minimum = int(
        config.get("funnel_diagnostics", {}).get("minimum_comparable_posts", 3)
    )
    metadata = observation.get("content_metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    maturity = str(observation.get("maturity_window") or "")

    candidates = [
        (
            "duration_bucket+format",
            [
                row
                for row in all_observations
                if row.get("maturity_window") == maturity
                and row.get("content_metadata", {}).get("duration_bucket")
                == metadata.get("duration_bucket")
                and row.get("content_metadata", {}).get("format")
                == metadata.get("format")
                and metadata.get("duration_bucket") is not None
                and metadata.get("format") is not None
            ],
        ),
        (
            "duration_bucket",
            [
                row
                for row in all_observations
                if row.get("maturity_window") == maturity
                and row.get("content_metadata", {}).get("duration_bucket")
                == metadata.get("duration_bucket")
                and metadata.get("duration_bucket") is not None
            ],
        ),
        (
            "account",
            [row for row in all_observations if row.get("maturity_window") == maturity],
        ),
    ]
    quartiles_min_n = int(
        config.get("statistics", {}).get("minimum_posts_for_percentiles", 4)
    )
    for label, rows in candidates:
        if len(rows) < minimum:
            continue
        summary = summarize_cohort(
            rows,
            maturity_window=maturity,
            dimension=label,
            value=(
                "all"
                if label == "account"
                else " + ".join(
                    str(metadata.get(field))
                    for field in label.split("+")
                )
            ),
            quartiles_min_n=quartiles_min_n,
        )
        summary["_observations"] = list(rows)
        return summary
    return None


def baseline_distribution(
    baseline: Mapping[str, Any] | None,
    metric: str,
    *,
    denominator_type: str | None = None,
) -> Mapping[str, Any]:
    if not isinstance(baseline, Mapping):
        return {}
    metrics = baseline.get("metrics")
    metrics = metrics if isinstance(metrics, Mapping) else {}
    value = metrics.get(metric)
    if metric == "follow_conversion":
        value = value.get(denominator_type) if isinstance(value, Mapping) else None
    return value if isinstance(value, Mapping) else {}


def baseline_quantile(
    baseline: Mapping[str, Any] | None,
    metric: str,
    q: float,
    *,
    denominator_type: str | None = None,
) -> float | None:
    if not isinstance(baseline, Mapping):
        return None
    observations = baseline.get("_observations")
    if isinstance(observations, list):
        selected = observations
        if metric == "follow_conversion" and denominator_type is not None:
            selected = [
                row
                for row in observations
                if follow_denominator_type(row) == denominator_type
            ]
        return percentile(
            [metric_value(row, metric) for row in selected],
            q,
        )
    known = baseline_distribution(
        baseline, metric, denominator_type=denominator_type
    )
    if q == 0.25:
        return numeric(known.get("p25"))
    if q == 0.5:
        return numeric(known.get("median"))
    if q == 0.75:
        return numeric(known.get("p75"))
    return None


def _format_number(value: Any, decimals: int = 2) -> str:
    number = numeric(value)
    if number is None:
        return "Unavailable"
    if float(number).is_integer():
        return f"{int(number):,}"
    return f"{float(number):,.{decimals}f}"


def _format_percent(value: Any, decimals: int = 1) -> str:
    number = numeric(value)
    return (
        "Unavailable"
        if number is None
        else f"{float(number) * 100:.{decimals}f}%"
    )


def _classification(
    *,
    label: str,
    observation: Mapping[str, Any],
    baseline: Mapping[str, Any],
    reason: str,
    supporting_metrics: Mapping[str, Any],
    comparison_percentiles: Mapping[str, Any],
    evidence_status: str = "observational",
) -> dict[str, Any]:
    identity = observation.get("identity")
    identity = identity if isinstance(identity, Mapping) else {}
    return {
        "label": label,
        "media_id": identity.get("media_id"),
        "content_hash": identity.get("content_hash"),
        "post": identity.get("permalink") or identity.get("media_id"),
        "maturity_window": observation.get("maturity_window"),
        "actual_age_hours": observation.get("actual_age_hours"),
        "comparison_cohort": {
            "dimension": baseline.get("dimension"),
            "value": baseline.get("value"),
            "n": baseline.get("post_count"),
        },
        "supporting_metrics": dict(supporting_metrics),
        "comparison_percentiles": dict(comparison_percentiles),
        "reason": reason,
        "evidence_status": evidence_status,
    }


def classify_post(
    observation: Mapping[str, Any],
    baseline: Mapping[str, Any] | None,
    config: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Apply each configured post label independently and transparently."""
    if not isinstance(baseline, Mapping):
        return []
    classifications: list[dict[str, Any]] = []
    class_config = config.get("classifications")
    class_config = class_config if isinstance(class_config, Mapping) else {}
    baseline_n = int(baseline.get("post_count") or 0)

    reach = numeric(metric_value(observation, "reach"))
    follows = numeric(metric_value(observation, "follows"))
    intent_actions = numeric(metric_value(observation, "intent_actions"))
    intent_rate = numeric(
        metric_value(observation, "intent_actions_per_1000_reach")
    )
    follow_rate = numeric(metric_value(observation, "follow_conversion"))
    follow_type = follow_denominator_type(observation)
    fph = numeric(metric_value(observation, "follows_per_production_hour"))
    production_hours = numeric(metric_value(observation, "production_hours"))

    reach_dist = baseline_distribution(baseline, "reach")
    intent_dist = baseline_distribution(
        baseline, "intent_actions_per_1000_reach"
    )
    follow_dist = baseline_distribution(
        baseline, "follow_conversion", denominator_type=follow_type
    )
    fph_dist = baseline_distribution(baseline, "follows_per_production_hour")
    production_dist = baseline_distribution(baseline, "production_hours")
    follows_dist = baseline_distribution(baseline, "follows")
    intent_total_dist = baseline_distribution(baseline, "intent_actions")

    hidden_config = class_config.get("hidden_gem", {})
    hidden_minimum = int(hidden_config.get("minimum_comparable_posts", 4))
    hidden_reach_q = float(
        hidden_config.get("reach_percentile_max_exclusive", 0.5)
    )
    hidden_follow_q = float(
        hidden_config.get("follow_conversion_percentile_min", 0.75)
    )
    hidden_efficiency_q = float(
        hidden_config.get(
            "follows_per_production_hour_percentile_min", 0.75
        )
    )
    reach_median = baseline_quantile(
        baseline, "reach", hidden_reach_q
    )
    follow_p75 = baseline_quantile(
        baseline,
        "follow_conversion",
        hidden_follow_q,
        denominator_type=follow_type,
    )
    fph_p75 = baseline_quantile(
        baseline, "follows_per_production_hour", hidden_efficiency_q
    )
    follow_denominator = (
        observation.get("derived_metrics", {})
        .get("follow_conversion", {})
        .get("denominator")
    )
    adequate_follow = (
        follow_rate is not None
        and follow_p75 is not None
        and numeric(follow_denominator) is not None
        and float(follow_denominator)
        >= float(
            config.get("statistics", {}).get("minimum_positive_denominator", 1)
        )
    )
    hidden_follow = adequate_follow and float(follow_rate) >= float(follow_p75)
    hidden_efficiency = (
        fph is not None and fph_p75 is not None and float(fph) >= float(fph_p75)
    )
    if (
        baseline_n >= hidden_minimum
        and reach is not None
        and reach_median is not None
        and float(reach) < float(reach_median)
        and (hidden_follow or hidden_efficiency)
    ):
        reach_gap = (float(reach_median) - float(reach)) / float(reach_median) * 100
        trigger_parts: list[str] = []
        if hidden_follow:
            trigger_parts.append(
                f"follow conversion {_format_number(follow_rate)} per 1,000 "
                f"{follow_type} was at/above cohort p75 {_format_number(follow_p75)}"
            )
        if hidden_efficiency:
            trigger_parts.append(
                f"follows/production-hour {_format_number(fph)} was at/above "
                f"cohort p75 {_format_number(fph_p75)}"
            )
        reason = (
            f"Hidden Gem: reach {_format_number(reach)} was {reach_gap:.1f}% below "
            f"the {observation.get('maturity_window')} cohort median "
            f"{_format_number(reach_median)}; " + " and ".join(trigger_parts)
            + f" (n={baseline_n})."
        )
        classifications.append(
            _classification(
                label="Hidden Gem",
                observation=observation,
                baseline=baseline,
                reason=reason,
                supporting_metrics={
                    "reach": reach,
                    "cohort_median_reach": reach_median,
                    "follow_conversion": follow_rate,
                    "follow_conversion_denominator_type": follow_type,
                    "follows_per_production_hour": fph,
                },
                comparison_percentiles={
                    "reach": percentile_rank(
                        [
                            metric_value(row, "reach")
                            for row in baseline.get("_observations", [])
                        ],
                        reach,
                    ),
                    "follow_conversion": percentile_rank(
                        [
                            metric_value(row, "follow_conversion")
                            for row in baseline.get("_observations", [])
                            if follow_denominator_type(row) == follow_type
                        ],
                        follow_rate,
                    ),
                    "follows_per_production_hour": percentile_rank(
                        [
                            metric_value(row, "follows_per_production_hour")
                            for row in baseline.get("_observations", [])
                        ],
                        fph,
                    ),
                },
            )
        )

    vanity_config = class_config.get("vanity_winner", {})
    vanity_minimum = int(vanity_config.get("minimum_comparable_posts", 4))
    vanity_reach_q = float(vanity_config.get("reach_percentile_min", 0.75))
    vanity_follow_q = float(
        vanity_config.get("follow_conversion_percentile_max_exclusive", 0.5)
    )
    vanity_intent_q = float(
        vanity_config.get(
            "intent_actions_per_1000_reach_percentile_max_exclusive", 0.5
        )
    )
    reach_p75 = baseline_quantile(baseline, "reach", vanity_reach_q)
    follow_median = baseline_quantile(
        baseline,
        "follow_conversion",
        vanity_follow_q,
        denominator_type=follow_type,
    )
    intent_median = baseline_quantile(
        baseline, "intent_actions_per_1000_reach", vanity_intent_q
    )
    if (
        baseline_n >= vanity_minimum
        and reach is not None
        and reach_p75 is not None
        and float(reach) >= float(reach_p75)
        and follow_rate is not None
        and follow_median is not None
        and float(follow_rate) < float(follow_median)
        and intent_rate is not None
        and intent_median is not None
        and float(intent_rate) < float(intent_median)
    ):
        follow_gap = (
            (float(follow_median) - float(follow_rate)) / float(follow_median) * 100
            if float(follow_median) > 0
            else None
        )
        reason = (
            f"Vanity Winner: reach {_format_number(reach)} was at/above cohort p75 "
            f"{_format_number(reach_p75)}, but follow conversion "
            f"{_format_number(follow_rate)} per 1,000 {follow_type} was below median "
            f"{_format_number(follow_median)}"
            + (f" by {follow_gap:.1f}%" if follow_gap is not None else "")
            + f", and intent actions {_format_number(intent_rate)} per 1,000 reach "
            f"were below median {_format_number(intent_median)} (n={baseline_n})."
        )
        classifications.append(
            _classification(
                label="Vanity Winner",
                observation=observation,
                baseline=baseline,
                reason=reason,
                supporting_metrics={
                    "reach": reach,
                    "cohort_p75_reach": reach_p75,
                    "follow_conversion": follow_rate,
                    "follow_conversion_denominator_type": follow_type,
                    "cohort_median_follow_conversion": follow_median,
                    "intent_actions_per_1000_reach": intent_rate,
                    "cohort_median_intent_actions_per_1000_reach": intent_median,
                },
                comparison_percentiles={},
            )
        )

    star_config = class_config.get("expensive_star", {})
    star_minimum = int(star_config.get("minimum_comparable_posts", 4))
    star_outcome_q = float(
        star_config.get("total_outcome_percentile_min", 0.75)
    )
    star_efficiency_q = float(
        star_config.get(
            "follows_per_production_hour_percentile_max_exclusive", 0.5
        )
    )
    outcome_checks = {
        "follows": (
            follows,
            baseline_quantile(baseline, "follows", star_outcome_q),
        ),
        "reach": (reach, baseline_quantile(baseline, "reach", star_outcome_q)),
        "intent_actions": (
            intent_actions,
            baseline_quantile(
                baseline, "intent_actions", star_outcome_q
            ),
        ),
    }
    top_outcomes = [
        name
        for name, (value, threshold) in outcome_checks.items()
        if value is not None
        and threshold is not None
        and float(value) >= float(threshold)
    ]
    fph_median = baseline_quantile(
        baseline, "follows_per_production_hour", star_efficiency_q
    )
    production_median = numeric(production_dist.get("median"))
    production_multiplier = float(
        star_config.get("production_time_to_account_median_min", 1.25)
    )
    if (
        baseline_n >= star_minimum
        and top_outcomes
        and fph is not None
        and fph_median is not None
        and float(fph) < float(fph_median)
        and production_hours is not None
        and production_median is not None
        and float(production_hours) >= float(production_median) * production_multiplier
    ):
        reason = (
            f"Expensive Star: {', '.join(top_outcomes)} reached the cohort top quartile, "
            f"but follows/production-hour {_format_number(fph)} was below median "
            f"{_format_number(fph_median)} and production time "
            f"{_format_number(production_hours)}h was {production_multiplier:.2f}× or "
            f"more of median {_format_number(production_median)}h (n={baseline_n})."
        )
        classifications.append(
            _classification(
                label="Expensive Star",
                observation=observation,
                baseline=baseline,
                reason=reason,
                supporting_metrics={
                    "top_quartile_outcomes": top_outcomes,
                    "follows_per_production_hour": fph,
                    "cohort_median_follows_per_production_hour": fph_median,
                    "production_hours": production_hours,
                    "cohort_median_production_hours": production_median,
                },
                comparison_percentiles={},
            )
        )

    under_config = class_config.get("underperformer", {})
    under_minimum = int(under_config.get("minimum_comparable_posts", 5))
    under_follow_q = float(
        under_config.get("follow_conversion_percentile_max_exclusive", 0.5)
    )
    under_intent_q = float(
        under_config.get(
            "intent_actions_per_1000_reach_percentile_max_exclusive", 0.5
        )
    )
    under_efficiency_q = float(
        under_config.get(
            "follows_per_production_hour_percentile_max_exclusive", 0.5
        )
    )
    under_follow_threshold = baseline_quantile(
        baseline,
        "follow_conversion",
        under_follow_q,
        denominator_type=follow_type,
    )
    under_intent_threshold = baseline_quantile(
        baseline, "intent_actions_per_1000_reach", under_intent_q
    )
    under_efficiency_threshold = baseline_quantile(
        baseline, "follows_per_production_hour", under_efficiency_q
    )
    primary_below = (
        follow_rate is not None
        and under_follow_threshold is not None
        and float(follow_rate) < float(under_follow_threshold)
        and intent_rate is not None
        and under_intent_threshold is not None
        and float(intent_rate) < float(under_intent_threshold)
        and fph is not None
        and under_efficiency_threshold is not None
        and float(fph) < float(under_efficiency_threshold)
    )
    secondary_top = False
    secondary_q = float(
        under_config.get("secondary_metric_top_percentile", 0.75)
    )
    for metric in ("reach", "watch_depth", "shares_per_1000_reach", "saves_per_1000_reach"):
        value = numeric(metric_value(observation, metric))
        threshold = baseline_quantile(baseline, metric, secondary_q)
        secondary_top = secondary_top or (
            value is not None
            and threshold is not None
            and float(value) >= float(threshold)
        )
    if baseline_n >= under_minimum and primary_below and not secondary_top:
        reason = (
            "Underperformer: follow conversion, intent actions per 1,000 reach, "
            "and follows/production-hour were each below their comparable medians, "
            f"with no reach, watch-depth, share-rate, or save-rate metric in the top "
            f"quartile (n={baseline_n})."
        )
        classifications.append(
            _classification(
                label="Underperformer",
                observation=observation,
                baseline=baseline,
                reason=reason,
                supporting_metrics={
                    "follow_conversion": follow_rate,
                    "intent_actions_per_1000_reach": intent_rate,
                    "follows_per_production_hour": fph,
                },
                comparison_percentiles={},
            )
        )
    return classifications


def _compare_to_median(value: Any, baseline_value: Any) -> str:
    number = numeric(value)
    baseline = numeric(baseline_value)
    if number is None or baseline is None:
        return "UNKNOWN"
    if float(number) > float(baseline):
        return "ABOVE"
    if float(number) < float(baseline):
        return "BELOW"
    return "EQUAL"


def stage_statuses(
    observation: Mapping[str, Any],
    baseline: Mapping[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    follow_type = follow_denominator_type(observation)
    mappings = {
        "exposure": ("reach", "reach", None),
        "attention": ("watch_depth", "watch_depth", None),
        "satisfaction": (
            "intent_actions_per_1000_reach",
            "intent_actions_per_1000_reach",
            None,
        ),
        "curiosity": (
            "profile_visits_per_1000_reach",
            "profile_visits_per_1000_reach",
            None,
        ),
        "conversion": ("follow_conversion", "follow_conversion", follow_type),
        "production_efficiency": (
            "follows_per_production_hour",
            "follows_per_production_hour",
            None,
        ),
        "retention": ("returning_viewers", "returning_viewers", None),
    }
    output: dict[str, dict[str, Any]] = {}
    for stage, (metric, baseline_metric, denominator) in mappings.items():
        value = metric_value(observation, metric)
        baseline_value = baseline_distribution(
            baseline, baseline_metric, denominator_type=denominator
        ).get("median")
        comparison = _compare_to_median(value, baseline_value)
        status = (
            "SUCCEEDED"
            if comparison == "ABOVE"
            else "WEAK"
            if comparison == "BELOW"
            else "AT_BASELINE"
            if comparison == "EQUAL"
            else "UNKNOWN"
        )
        output[stage] = {
            "status": status,
            "metric": metric,
            "value": value,
            "cohort_median": baseline_value,
            "denominator_type": denominator,
        }
    return output


def diagnose_funnel(
    observation: Mapping[str, Any],
    baseline: Mapping[str, Any] | None,
    config: Mapping[str, Any],
) -> list[dict[str, Any]]:
    if not isinstance(baseline, Mapping):
        return []
    minimum = int(
        config.get("funnel_diagnostics", {}).get("minimum_comparable_posts", 3)
    )
    if int(baseline.get("post_count") or 0) < minimum:
        return []
    stages = stage_statuses(observation, baseline)
    diagnoses: list[dict[str, Any]] = []
    identity = observation.get("identity")
    identity = identity if isinstance(identity, Mapping) else {}

    def add(code: str, hypothesis: str, supporting: Mapping[str, Any]) -> None:
        diagnoses.append(
            {
                "diagnostic": code,
                "media_id": identity.get("media_id"),
                "content_hash": identity.get("content_hash"),
                "maturity_window": observation.get("maturity_window"),
                "actual_age_hours": observation.get("actual_age_hours"),
                "comparison_cohort": {
                    "dimension": baseline.get("dimension"),
                    "value": baseline.get("value"),
                    "n": baseline.get("post_count"),
                },
                "supporting_metrics": dict(supporting),
                "hypothesis": hypothesis,
                "evidence_status": "diagnostic_hypothesis_not_causal",
            }
        )

    if stages["exposure"]["status"] == "SUCCEEDED" and stages["attention"]["status"] == "WEAK":
        add(
            "PACKAGING_WON_CONTENT_LOST",
            "Packaging earned above-median exposure, while attention fell below the "
            "comparable median; inspect the opening-to-payoff transition.",
            {"exposure": stages["exposure"], "attention": stages["attention"]},
        )

    share_status = _compare_to_median(
        metric_value(observation, "shares_per_1000_reach"),
        baseline_distribution(baseline, "shares_per_1000_reach").get("median"),
    )
    save_status = _compare_to_median(
        metric_value(observation, "saves_per_1000_reach"),
        baseline_distribution(baseline, "saves_per_1000_reach").get("median"),
    )
    if (
        stages["exposure"]["status"] == "WEAK"
        and stages["attention"]["status"] == "SUCCEEDED"
        and (share_status == "ABOVE" or save_status == "ABOVE")
    ):
        add(
            "STRONG_CONTENT_WEAK_INITIAL_DISTRIBUTION",
            "The reached audience showed above-median attention and save/share intent "
            "despite below-median exposure. Retest packaging or distribution conditions; "
            "the data does not prove the hook caused low reach.",
            {
                "exposure": stages["exposure"],
                "attention": stages["attention"],
                "shares_comparison": share_status,
                "saves_comparison": save_status,
            },
        )

    if (
        stages["satisfaction"]["status"] == "SUCCEEDED"
        and stages["curiosity"]["status"] == "WEAK"
        and stages["conversion"]["status"] == "WEAK"
    ):
        add(
            "USEFUL_BUT_ACCOUNT_IDENTITY_WEAK",
            "Intent was strong, but post-attributed profile curiosity and follow conversion "
            "were weak; inspect whether the account promise is visible and specific.",
            {
                "satisfaction": stages["satisfaction"],
                "curiosity": stages["curiosity"],
                "conversion": stages["conversion"],
            },
        )

    profile_rate = metric_value(observation, "profile_visit_to_follow_rate")
    profile_rate_median = baseline_distribution(
        baseline, "profile_visit_to_follow_rate"
    ).get("median")
    if (
        stages["curiosity"]["status"] == "SUCCEEDED"
        and _compare_to_median(profile_rate, profile_rate_median) == "BELOW"
    ):
        add(
            "PROFILE_PROMISE_MISMATCH",
            "Profile visits were above the comparable median, but profile-visit-to-follow "
            "conversion was below it; the profile promise may not match post expectations.",
            {
                "curiosity": stages["curiosity"],
                "profile_visit_to_follow_rate": profile_rate,
                "cohort_median_profile_visit_to_follow_rate": profile_rate_median,
            },
        )

    if (
        stages["conversion"]["status"] == "SUCCEEDED"
        and metric_value(observation, "returning_viewers") is None
    ):
        add(
            "STRONG_ACQUISITION_UNKNOWN_RETENTION",
            "Follow conversion was above the comparable median, but returning-viewer "
            "data is unavailable; retention cannot be concluded.",
            {
                "conversion": stages["conversion"],
                "returning_viewers": None,
            },
        )

    follows_status = _compare_to_median(
        metric_value(observation, "follows"),
        baseline_distribution(baseline, "follows").get("median"),
    )
    intent_total_status = _compare_to_median(
        metric_value(observation, "intent_actions"),
        baseline_distribution(baseline, "intent_actions").get("median"),
    )
    if (
        (follows_status == "ABOVE" or intent_total_status == "ABOVE")
        and stages["production_efficiency"]["status"] == "WEAK"
    ):
        add(
            "STRONG_RESULT_TOO_EXPENSIVE",
            "Total follows or intent actions were above the comparable median, but "
            "follows per production hour were below it.",
            {
                "follows_comparison": follows_status,
                "intent_actions_comparison": intent_total_status,
                "production_efficiency": stages["production_efficiency"],
            },
        )
    return diagnoses


def _published_sort_key(observation: Mapping[str, Any]) -> tuple[datetime, str]:
    identity = observation.get("identity")
    identity = identity if isinstance(identity, Mapping) else {}
    published = parse_datetime(identity.get("published_at"))
    return (
        published or datetime.min.replace(tzinfo=timezone.utc),
        str(identity.get("media_id") or identity.get("content_hash") or ""),
    )


def _same_denominator_baseline_median(
    observation: Mapping[str, Any],
    baseline: Mapping[str, Any],
) -> Any:
    return baseline_distribution(
        baseline,
        "follow_conversion",
        denominator_type=follow_denominator_type(observation),
    ).get("median")


def analyze_series(
    posts: Sequence[Mapping[str, Any]],
    account_baselines: Mapping[str, Any],
    config: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Treat manually tagged recurring series as a roster at one fixed maturity."""
    series_config = config.get("series_recommendations")
    series_config = series_config if isinstance(series_config, Mapping) else {}
    primary_window = str(series_config.get("primary_maturity_window") or "72h")
    observations = observations_for_window(posts, primary_window)
    baseline = account_baselines.get(primary_window)
    if not isinstance(baseline, Mapping):
        return [], []
    baseline = dict(baseline)
    baseline["_observations"] = observations

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    classifications_by_hash = {
        str(post.get("identity", {}).get("content_hash") or ""): post.get(
            "classifications", []
        )
        for post in posts
    }
    diagnostics_by_hash = {
        str(post.get("identity", {}).get("content_hash") or ""): post.get(
            "funnel_diagnostics", []
        )
        for post in posts
    }
    for observation in observations:
        metadata = observation.get("content_metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        series = str(metadata.get("series") or "").strip()
        if series:
            groups[series].append(observation)

    summaries: list[dict[str, Any]] = []
    workhorses: list[dict[str, Any]] = []
    minimum = int(series_config.get("minimum_comparable_posts", 5))
    recent_count = int(series_config.get("recent_post_count", 5))
    scale_minimum = int(
        series_config.get("scale", {}).get(
            "minimum_recent_above_both_medians", 3
        )
    )
    pause_minimum = int(
        series_config.get("pause", {}).get(
            "minimum_recent_below_both_medians", 4
        )
    )
    top_secondary_minimum = int(
        series_config.get("pause", {}).get(
            "minimum_recent_top_secondary_for_consistency", 3
        )
    )
    revise_minimum = int(
        series_config.get("revise", {}).get(
            "minimum_recent_with_same_split_funnel", 3
        )
    )

    for series, rows in sorted(groups.items()):
        ordered = sorted(rows, key=_published_sort_key, reverse=True)
        recent = ordered[:recent_count]
        production_values = [
            numeric(metric_value(row, "production_hours")) for row in rows
        ]
        follow_values = [numeric(metric_value(row, "follows")) for row in rows]
        follow_types = {
            follow_denominator_type(row)
            for row in rows
            if numeric(metric_value(row, "follow_conversion")) is not None
        }
        follow_conversion_summary = {
            denominator: distribution(
                [
                    metric_value(row, "follow_conversion")
                    for row in rows
                    if follow_denominator_type(row) == denominator
                ],
                quartiles_min_n=int(
                    config.get("statistics", {}).get(
                        "minimum_posts_for_percentiles", 4
                    )
                ),
            )
            for denominator in ("non_follower_reach", "reach")
        }

        comparisons: list[dict[str, Any]] = []
        for row in recent:
            follow_value = numeric(metric_value(row, "follow_conversion"))
            follow_median = numeric(_same_denominator_baseline_median(row, baseline))
            efficiency = numeric(
                metric_value(row, "follows_per_production_hour")
            )
            efficiency_median = numeric(
                baseline_distribution(
                    baseline, "follows_per_production_hour"
                ).get("median")
            )
            comparisons.append(
                {
                    "media_id": row.get("identity", {}).get("media_id"),
                    "follow_conversion": follow_value,
                    "follow_conversion_denominator_type": follow_denominator_type(row),
                    "follow_conversion_baseline_median": follow_median,
                    "follows_per_production_hour": efficiency,
                    "follows_per_production_hour_baseline_median": efficiency_median,
                    "above_both": (
                        follow_value is not None
                        and follow_median is not None
                        and float(follow_value) > float(follow_median)
                        and efficiency is not None
                        and efficiency_median is not None
                        and float(efficiency) > float(efficiency_median)
                    ),
                    "below_both": (
                        follow_value is not None
                        and follow_median is not None
                        and float(follow_value) < float(follow_median)
                        and efficiency is not None
                        and efficiency_median is not None
                        and float(efficiency) < float(efficiency_median)
                    ),
                    "complete": all(
                        value is not None
                        for value in (
                            follow_value,
                            follow_median,
                            efficiency,
                            efficiency_median,
                        )
                    ),
                }
            )
        above_both = sum(bool(row["above_both"]) for row in comparisons)
        below_both = sum(bool(row["below_both"]) for row in comparisons)
        complete_recent = sum(bool(row["complete"]) for row in comparisons)

        secondary_counts: dict[str, int] = {}
        for metric in (
            "watch_depth",
            "shares_per_1000_reach",
            "saves_per_1000_reach",
        ):
            p75 = numeric(baseline_distribution(baseline, metric).get("p75"))
            secondary_counts[metric] = sum(
                p75 is not None
                and numeric(metric_value(row, metric)) is not None
                and float(metric_value(row, metric)) >= float(p75)
                for row in recent
            )
        consistent_secondary = [
            metric
            for metric, count in secondary_counts.items()
            if count >= top_secondary_minimum
        ]

        diagnostic_counts: Counter[str] = Counter()
        weak_stage_counts: Counter[str] = Counter()
        for row in recent:
            content_hash = str(row.get("identity", {}).get("content_hash") or "")
            diagnostic_counts.update(
                str(item.get("diagnostic"))
                for item in diagnostics_by_hash.get(content_hash, [])
                if isinstance(item, Mapping) and item.get("diagnostic")
            )
            for stage, result in stage_statuses(row, baseline).items():
                if result.get("status") == "WEAK":
                    weak_stage_counts[stage] += 1

        if len(rows) < minimum:
            recommendation = "INSUFFICIENT DATA"
            reason = (
                f"Series has {len(rows)} comparable {primary_window} posts; "
                f"at least {minimum} are required."
            )
        elif len(recent) < recent_count or complete_recent < recent_count:
            recommendation = "INSUFFICIENT DATA"
            reason = (
                f"Only {complete_recent}/{recent_count} recent posts have both "
                "same-denominator follow conversion and measured production efficiency."
            )
        elif above_both >= scale_minimum:
            recommendation = "SCALE"
            reason = (
                f"{above_both}/{recent_count} recent {primary_window} posts beat their "
                "same-denominator follow-conversion median and the account "
                "follows/production-hour median."
            )
        elif below_both >= pause_minimum and not consistent_secondary:
            recommendation = "PAUSE"
            reason = (
                f"{below_both}/{recent_count} recent posts were below both primary "
                "medians, with no secondary metric in the top quartile on "
                f"{top_secondary_minimum}/{recent_count} posts."
            )
        elif diagnostic_counts and diagnostic_counts.most_common(1)[0][1] >= revise_minimum:
            diagnostic, count = diagnostic_counts.most_common(1)[0]
            recommendation = "REVISE"
            reason = (
                f"{count}/{recent_count} recent posts share the {diagnostic} split-funnel "
                "diagnostic; preserve the strong stage and revise the weak stage."
            )
        else:
            recommendation = "HOLD"
            reason = (
                f"Complete data exists for {recent_count} recent posts, but SCALE, "
                "REVISE, and PAUSE thresholds were not met."
            )

        goals: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            goal = str(row.get("content_metadata", {}).get("content_goal") or "").strip()
            if goal:
                goals[goal].append(row)
        strongest_goal: str | None = None
        if len(follow_types) == 1 and follow_types:
            goal_medians = {
                goal: percentile(
                    [metric_value(row, "follow_conversion") for row in goal_rows],
                    0.5,
                )
                for goal, goal_rows in goals.items()
            }
            comparable_goals = {
                goal: value
                for goal, value in goal_medians.items()
                if numeric(value) is not None
            }
            if comparable_goals:
                strongest_goal = max(
                    comparable_goals,
                    key=lambda goal: (float(comparable_goals[goal]), goal),
                )

        hidden_count = 0
        vanity_count = 0
        for row in rows:
            content_hash = str(row.get("identity", {}).get("content_hash") or "")
            labels = {
                str(item.get("label"))
                for item in classifications_by_hash.get(content_hash, [])
                if isinstance(item, Mapping)
            }
            hidden_count += "Hidden Gem" in labels
            vanity_count += "Vanity Winner" in labels

        known_production = [value for value in production_values if value is not None]
        known_follows = [value for value in follow_values if value is not None]
        summary = {
            "series": series,
            "maturity_window": primary_window,
            "post_count": len(rows),
            "total_production_hours": (
                sum(float(value) for value in known_production)
                if len(known_production) == len(rows)
                else None
            ),
            "known_production_hours": (
                sum(float(value) for value in known_production)
                if known_production
                else None
            ),
            "production_time_coverage": {
                "count": len(known_production),
                "total": len(rows),
            },
            "total_reach": (
                sum(float(metric_value(row, "reach")) for row in rows)
                if all(numeric(metric_value(row, "reach")) is not None for row in rows)
                else None
            ),
            "total_follows": (
                sum(float(value) for value in known_follows)
                if len(known_follows) == len(rows)
                else None
            ),
            "known_follows": (
                sum(float(value) for value in known_follows) if known_follows else None
            ),
            "follows_coverage": {
                "count": len(known_follows),
                "total": len(rows),
            },
            "median_follow_conversion": follow_conversion_summary,
            "median_follows_per_production_hour": percentile(
                [metric_value(row, "follows_per_production_hour") for row in rows],
                0.5,
            ),
            "median_watch_depth": percentile(
                [metric_value(row, "watch_depth") for row in rows], 0.5
            ),
            "median_shares_per_1000_reach": percentile(
                [metric_value(row, "shares_per_1000_reach") for row in rows],
                0.5,
            ),
            "median_saves_per_1000_reach": percentile(
                [metric_value(row, "saves_per_1000_reach") for row in rows],
                0.5,
            ),
            "consistency_last_five": {
                "above_both_primary_medians": above_both,
                "below_both_primary_medians": below_both,
                "complete_comparisons": complete_recent,
                "posts_considered": len(recent),
                "details": comparisons,
            },
            "hidden_gems": hidden_count,
            "vanity_winners": vanity_count,
            "strongest_content_goal": strongest_goal,
            "weakest_funnel_stage": (
                weak_stage_counts.most_common(1)[0][0]
                if weak_stage_counts
                else None
            ),
            "recommendation": recommendation,
            "recommendation_reason": reason,
            "evidence_status": (
                "complete" if recommendation != "INSUFFICIENT DATA" else "insufficient"
            ),
        }
        summaries.append(summary)
        if recommendation == "SCALE":
            workhorses.append(
                {
                    "series": series,
                    "maturity_window": primary_window,
                    "sample_size": len(rows),
                    "recent_above_both": above_both,
                    "reason": reason,
                    "evidence_status": "workhorse",
                }
            )
    return summaries, workhorses


def _experiment_common_window(
    posts: Sequence[Mapping[str, Any]],
    priority: Sequence[str],
) -> str | None:
    for window in priority:
        if all(
            isinstance(post.get("maturity_windows", {}).get(window), Mapping)
            for post in posts
        ):
            return window
    return None


def analyze_experiments(
    posts: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Compare declared arms; surface confounding rather than hiding it."""
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for post in posts:
        metadata = post.get("content_metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        experiment_id = str(metadata.get("experiment_id") or "").strip()
        if experiment_id:
            groups[experiment_id].append(post)

    # The Trial ledger declares its parent relationship even though the parent
    # row itself does not carry experiment_id.  Include it, but Trial mode is
    # explicitly surfaced as an uncontrolled difference.
    posts_by_hash = {
        str(post.get("identity", {}).get("content_hash") or ""): post
        for post in posts
    }
    for experiment_id, rows in list(groups.items()):
        for post in list(rows):
            trial = post.get("trial_experiment")
            trial = trial if isinstance(trial, Mapping) else {}
            parent_hash = str(trial.get("parent_content_hash") or "")
            parent = posts_by_hash.get(parent_hash)
            if parent is not None and parent not in rows:
                rows.append(parent)

    priority = config.get("statistics", {}).get(
        "decision_window_priority", ["7d", "72h", "24h", "2h"]
    )
    priority = [str(value) for value in priority] if isinstance(priority, list) else list(DECISION_WINDOW_ORDER)
    output: list[dict[str, Any]] = []
    for experiment_id, group in sorted(groups.items()):
        changed_variables: set[str] = set()
        for post in group:
            metadata = post.get("content_metadata", {})
            changed = metadata.get("changed_variable")
            if changed:
                changed_variables.add(str(changed))
            trial = post.get("trial_experiment", {})
            raw_changed = trial.get("changed_variables_json")
            if isinstance(raw_changed, str) and raw_changed.strip():
                try:
                    parsed = json.loads(raw_changed)
                except json.JSONDecodeError:
                    parsed = []
                if isinstance(parsed, list):
                    changed_variables.update(
                        str(value) for value in parsed if str(value).strip()
                    )
        maturity = _experiment_common_window(group, priority)
        variants: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        sorted_group = sorted(group, key=lambda post: str(post.get("identity", {}).get("media_id") or ""))
        for index, post in enumerate(sorted_group):
            metadata = post.get("content_metadata", {})
            variant = str(metadata.get("experiment_variant") or "").strip()
            if not variant:
                variant = "control" if index == 0 else f"variant_{index}"
            variants[variant].append(post)

        uncontrolled: list[str] = []
        control_fields = (
            "series",
            "content_goal",
            "topic",
            "source",
            "format",
            "duration_bucket",
            "posting_window",
            "trial_reel",
        )
        for field in control_fields:
            values = {
                json.dumps(post.get("content_metadata", {}).get(field), sort_keys=True)
                for post in group
            }
            if len(values) > 1 and field not in changed_variables:
                uncontrolled.append(field)
        publication_times = [
            post.get("identity", {}).get("published_at") for post in group
        ]
        known_differences = {
            "publication_times": publication_times,
            "media_ids": [
                post.get("identity", {}).get("media_id") for post in group
            ],
            "uncontrolled_fields": uncontrolled,
        }

        controlled = (
            len(group) >= 2
            and len(variants) >= 2
            and len(changed_variables) == 1
            and maturity is not None
            and not uncontrolled
        )
        warnings: list[str] = []
        if len(group) < 2 or len(variants) < 2:
            warnings.append("At least two declared arms are required.")
        if len(changed_variables) != 1:
            warnings.append(
                f"Exactly one changed variable is required; found {len(changed_variables)}."
            )
        if maturity is None:
            warnings.append("No equivalent fixed maturity window exists for all arms.")
        if uncontrolled:
            warnings.append(
                "Known uncontrolled differences: " + ", ".join(uncontrolled) + "."
            )

        metric_name: str | None = None
        control_value: float | None = None
        variant_value: float | None = None
        control_label = "control" if "control" in variants else sorted(variants)[0]
        variant_labels = [name for name in sorted(variants) if name != control_label]
        variant_label = variant_labels[0] if variant_labels else None
        denominator_type: str | None = None
        if maturity and variant_label:
            control_observations = [
                post["maturity_windows"][maturity] for post in variants[control_label]
            ]
            variant_observations = [
                post["maturity_windows"][maturity] for post in variants[variant_label]
            ]
            control_types = {
                follow_denominator_type(row)
                for row in control_observations
                if metric_value(row, "follow_conversion") is not None
            }
            variant_types = {
                follow_denominator_type(row)
                for row in variant_observations
                if metric_value(row, "follow_conversion") is not None
            }
            if len(control_types) == len(variant_types) == 1 and control_types == variant_types:
                metric_name = "follow_conversion"
                denominator_type = next(iter(control_types))
            elif any(
                metric_value(row, "intent_actions_per_1000_reach") is not None
                for row in (*control_observations, *variant_observations)
            ):
                metric_name = "intent_actions_per_1000_reach"
            else:
                metric_name = "reach"
            control_value = percentile(
                [metric_value(row, metric_name) for row in control_observations], 0.5
            )
            variant_value = percentile(
                [metric_value(row, metric_name) for row in variant_observations], 0.5
            )

        absolute_difference = (
            float(variant_value) - float(control_value)
            if variant_value is not None and control_value is not None
            else None
        )
        percentage_lift = (
            safe_divide(absolute_difference, control_value)
            if absolute_difference is not None
            else None
        )
        result = (
            "variant_higher"
            if absolute_difference is not None and absolute_difference > 0
            else "variant_lower"
            if absolute_difference is not None and absolute_difference < 0
            else "no_observed_difference"
            if absolute_difference == 0
            else "unavailable"
        )
        output.append(
            {
                "experiment_id": experiment_id,
                "comparison_type": (
                    "controlled_one_variable_comparison"
                    if controlled
                    else "uncontrolled_comparison"
                ),
                "changed_variable": (
                    next(iter(changed_variables))
                    if len(changed_variables) == 1
                    else None
                ),
                "declared_changed_variables": sorted(changed_variables),
                "hypothesis": next(
                    (
                        post.get("content_metadata", {}).get("hypothesis")
                        for post in group
                        if post.get("content_metadata", {}).get("hypothesis")
                    ),
                    None,
                ),
                "maturity_window": maturity,
                "metric": metric_name,
                "denominator_type": denominator_type,
                "control_variant": control_label,
                "variant": variant_label,
                "control_metric": control_value,
                "variant_metric": variant_value,
                "absolute_difference": absolute_difference,
                "percentage_lift": percentage_lift,
                "sample_size": len(group),
                "arm_sample_sizes": {
                    name: len(rows) for name, rows in sorted(variants.items())
                },
                "result": result,
                "known_differences": known_differences,
                "uncertainty_warning": (
                    "Observational comparison only; no statistical significance is claimed. "
                    + " ".join(warnings)
                ).strip(),
                "evidence_status": "eligible" if controlled else "uncontrolled",
            }
        )
    return output


def coverage_entry(count: int, total: int) -> dict[str, Any]:
    return {
        "count": count,
        "total": total,
        "percentage": (count / total * 100.0) if total else None,
    }


def build_data_coverage(
    posts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    latest = observations_for_window(posts, "latest")
    latest_raw: dict[str, Any] = {}
    for metric in CANONICAL_RAW_METRICS:
        count = sum(
            metric_available(metric_value(observation, metric))
            for observation in latest
        )
        latest_raw[metric] = coverage_entry(count, len(posts))

    derived_names = (
        "watch_depth",
        "total_watch_hours",
        "interactions_per_1000_reach",
        "interactions_per_1000_views",
        "interactions_per_1000_unique_media_viewers",
        "engagement_rate_by_reach",
        "views_per_reached_account",
        "likes_per_1000_reach",
        "likes_per_1000_views",
        "likes_per_1000_unique_media_viewers",
        "reactions_per_1000_reach",
        "reactions_per_1000_views",
        "reactions_per_1000_unique_media_viewers",
        "comments_per_1000_reach",
        "comments_per_1000_views",
        "comments_per_1000_unique_media_viewers",
        "shares_per_1000_reach",
        "shares_per_1000_views",
        "shares_per_1000_unique_media_viewers",
        "reposts_per_1000_reach",
        "reposts_per_1000_views",
        "saves_per_1000_reach",
        "saves_per_1000_views",
        "saves_per_1000_unique_media_viewers",
        "intent_actions_per_1000_reach",
        "intent_actions_per_1000_views",
        "profile_visits_per_1000_reach",
        "follow_conversion",
        "follows_per_1000_unique_media_viewers",
        "profile_visit_to_follow_rate",
        "follows_per_production_hour",
        "views_per_production_hour",
        "three_second_retention_rate",
        "three_second_dropoff_rate",
    )
    latest_derived = {
        metric: coverage_entry(
            sum(
                numeric(metric_value(observation, metric)) is not None
                for observation in latest
            ),
            len(posts),
        )
        for metric in derived_names
    }
    metadata_names = (
        "series",
        "content_goal",
        "topic",
        "source",
        "hook_style",
        "hook_text",
        "format",
        "visual_style",
        "caption_style",
        "cta",
        "duration_bucket",
        "posting_window",
        "production_minutes",
        "manual_effort_minutes",
        "direct_cost_jpy",
        "experiment_id",
        "changed_variable",
        "hypothesis",
    )
    metadata_coverage = {
        field: coverage_entry(
            sum(
                post.get("content_metadata", {}).get(field) is not None
                and str(post.get("content_metadata", {}).get(field)).strip() != ""
                for post in posts
            ),
            len(posts),
        )
        for field in metadata_names
    }
    inferred_metadata: list[dict[str, Any]] = []
    for post in posts:
        provenance = post.get("content_metadata", {}).get("metadata_provenance", {})
        fields = sorted(
            field
            for field, entry in provenance.items()
            if isinstance(entry, Mapping)
            and str(entry.get("source") or "").strip().lower() == "inferred"
        )
        if fields:
            inferred_metadata.append(
                {
                    "media_id": post.get("identity", {}).get("media_id"),
                    "content_hash": post.get("identity", {}).get("content_hash"),
                    "fields": fields,
                    "confidence": post.get("content_metadata", {}).get(
                        "metadata_confidence"
                    ),
                }
            )
    maturity = {
        window: coverage_entry(
            sum(
                isinstance(post.get("maturity_windows", {}).get(window), Mapping)
                for post in posts
            ),
            len(posts),
        )
        for window in (*WINDOW_ORDER, "latest")
    }

    snapshot_rows = sum(
        int(post.get("snapshot_audit", {}).get("stored_rows") or 0)
        for post in posts
    )
    deduplicated_rows = sum(int(post.get("snapshot_count") or 0) for post in posts)
    snapshot_metric_counts: Counter[str] = Counter()
    source_fields: dict[str, dict[str, Any]] = {}
    conflicts: Counter[str] = Counter()
    for post in posts:
        audit = post.get("snapshot_audit")
        audit = audit if isinstance(audit, Mapping) else {}
        snapshot_metric_counts.update(audit.get("canonical_metric_counts", {}))
        conflicts.update(audit.get("raw_column_conflicts", {}))
        for name, raw_entry in audit.get("source_fields", {}).items():
            if not isinstance(raw_entry, Mapping):
                continue
            entry = source_fields.setdefault(
                str(name),
                {
                    "count": 0,
                    "first_captured_at": None,
                    "last_captured_at": None,
                    "periods": set(),
                    "titles": set(),
                    "descriptions": set(),
                },
            )
            entry["count"] += int(raw_entry.get("count") or 0)
            first = str(raw_entry.get("first_captured_at") or "")
            last = str(raw_entry.get("last_captured_at") or "")
            if first:
                entry["first_captured_at"] = (
                    min(entry["first_captured_at"], first)
                    if entry["first_captured_at"]
                    else first
                )
            if last:
                entry["last_captured_at"] = (
                    max(entry["last_captured_at"], last)
                    if entry["last_captured_at"]
                    else last
                )
            for key in ("periods", "titles", "descriptions"):
                values = raw_entry.get(key)
                if isinstance(values, list):
                    entry[key].update(str(value) for value in values)
    all_snapshot_metrics = {
        metric: coverage_entry(int(snapshot_metric_counts.get(metric, 0)), snapshot_rows)
        for metric in CANONICAL_RAW_METRICS
    }
    return {
        "published_posts": len(posts),
        "latest_snapshot_posts": len(latest),
        "stored_snapshot_rows": snapshot_rows,
        "deduplicated_snapshot_rows": deduplicated_rows,
        "collapsed_exact_duplicate_rows": snapshot_rows - deduplicated_rows,
        "latest_post_metrics": latest_raw,
        "latest_derived_metrics": latest_derived,
        "content_metadata": metadata_coverage,
        "inferred_metadata": inferred_metadata,
        "snapshot_maturity": maturity,
        "all_snapshot_metrics": all_snapshot_metrics,
        "source_fields": {
            name: {
                **entry,
                "periods": sorted(entry["periods"]),
                "titles": sorted(entry["titles"]),
                "descriptions": sorted(entry["descriptions"]),
                "coverage": coverage_entry(int(entry["count"]), snapshot_rows),
            }
            for name, entry in sorted(source_fields.items())
        },
        "raw_column_conflicts": dict(sorted(conflicts.items())),
    }


def account_scoreboard(
    posts: Sequence[Mapping[str, Any]],
    *,
    window_name: str = "latest",
) -> dict[str, Any]:
    observations = observations_for_window(posts, window_name)
    reach_values = finite_values(
        [metric_value(observation, "reach") for observation in observations]
    )
    follow_values = finite_values(
        [metric_value(observation, "follows") for observation in observations]
    )
    production_values = finite_values(
        [metric_value(observation, "production_hours") for observation in observations]
    )
    metric_names = (
        "reach",
        "follows",
        "shares_per_1000_reach",
        "saves_per_1000_reach",
        "watch_depth",
        "production_hours",
        "follows_per_production_hour",
    )
    medians = {
        metric: percentile(
            [metric_value(observation, metric) for observation in observations],
            0.5,
        )
        for metric in metric_names
    }
    follow_conversion = {
        denominator: distribution(
            [
                metric_value(observation, "follow_conversion")
                for observation in observations
                if follow_denominator_type(observation) == denominator
            ]
        )
        for denominator in ("non_follower_reach", "reach")
    }
    return {
        "maturity_window": window_name,
        "post_count": len(observations),
        "total_reach": (
            sum(reach_values) if len(reach_values) == len(posts) else None
        ),
        "known_total_reach": sum(reach_values) if reach_values else None,
        "reach_coverage": coverage_entry(len(reach_values), len(posts)),
        "total_follows": (
            sum(follow_values) if len(follow_values) == len(posts) else None
        ),
        "known_total_follows": sum(follow_values) if follow_values else None,
        "follows_coverage": coverage_entry(len(follow_values), len(posts)),
        "total_production_hours": (
            sum(production_values)
            if len(production_values) == len(posts)
            else None
        ),
        "known_production_hours": (
            sum(production_values) if production_values else None
        ),
        "production_coverage": coverage_entry(len(production_values), len(posts)),
        "medians": medians,
        "follow_conversion_by_denominator": follow_conversion,
    }


def build_data_gaps(
    coverage: Mapping[str, Any],
    account_growth: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    latest = coverage.get("latest_post_metrics", {})
    metadata = coverage.get("content_metadata", {})
    maturity = coverage.get("snapshot_maturity", {})
    total = int(coverage.get("published_posts") or 0)
    gap_specs = (
        (
            "follows",
            latest.get("follows", {}),
            "Without post-attributed follows, follower conversion, Hidden Gem, Vanity "
            "Winner, Workhorse, and SCALE/PAUSE conclusions are unavailable.",
            "Verify API support or add a verified post-attributed source; never substitute "
            "account-level follower movement.",
        ),
        (
            "non_follower_reach",
            latest.get("non_follower_reach", {}),
            "The preferred new-audience follow-conversion denominator is unavailable.",
            "Capture post-level non-follower reach from a verified source when supported.",
        ),
        (
            "profile_visits",
            latest.get("profile_visits", {}),
            "Curiosity and profile-promise diagnostics cannot be measured.",
            "Capture post-attributed profile visits; account-wide profile activity is not a substitute.",
        ),
        (
            "returning_viewers",
            latest.get("returning_viewers", {}),
            "Acquisition can be observed when follows exist, but durable viewer retention cannot be concluded.",
            "Add a verified returning-viewer source if Instagram exposes it.",
        ),
        (
            "production_minutes",
            metadata.get("production_minutes", {}),
            "Production efficiency and the time-as-payroll part of Moneyball cannot be ranked.",
            "Record measured production_minutes in data/reel_annotations.json immediately.",
        ),
        (
            "series",
            metadata.get("series", {}),
            "Repeatability and roster recommendations cannot be attributed to recurring series.",
            "Apply stable manual series tags to every new Reel.",
        ),
        (
            "content_goal",
            metadata.get("content_goal", {}),
            "Discovery, utility, authority, and retention jobs cannot be compared separately.",
            "Tag each post with one allowed content_goal before publication.",
        ),
        (
            "dm_keyword_hits",
            latest.get("dm_keyword_hits", {}),
            "DM-intent efficiency is unavailable.",
            "Add an append-only verified post-attributed DM keyword event source if used.",
        ),
    )
    output: list[dict[str, Any]] = []
    for name, entry, limitation, action in gap_specs:
        count = int(entry.get("count") or 0) if isinstance(entry, Mapping) else 0
        if count < total:
            output.append(
                {
                    "field": name,
                    "coverage": coverage_entry(count, total),
                    "limitation": limitation,
                    "instrumentation_action": action,
                }
            )
    for window in WINDOW_ORDER:
        entry = maturity.get(window, {})
        count = int(entry.get("count") or 0) if isinstance(entry, Mapping) else 0
        if count < total:
            output.append(
                {
                    "field": f"{window}_snapshot",
                    "coverage": coverage_entry(count, total),
                    "limitation": (
                        f"Only {count}/{total} posts have a valid at-or-after {window} "
                        "observation within configured tolerance; missing historical "
                        "windows cannot be reconstructed."
                    ),
                    "instrumentation_action": (
                        f"Collect future {window} snapshots prospectively using the existing sync."
                    ),
                }
            )
    growth = account_growth if isinstance(account_growth, Mapping) else {}
    growth_coverage = growth.get("coverage")
    growth_coverage = (
        growth_coverage if isinstance(growth_coverage, Mapping) else {}
    )
    stock_coverage = growth_coverage.get("stock_snapshots")
    stock_coverage = (
        stock_coverage if isinstance(stock_coverage, Mapping) else {}
    )
    flow_coverage = growth_coverage.get("daily_intervals")
    flow_coverage = flow_coverage if isinstance(flow_coverage, Mapping) else {}
    account_reach_coverage = growth_coverage.get("account_reach")
    account_reach_coverage = (
        account_reach_coverage
        if isinstance(account_reach_coverage, Mapping)
        else {}
    )
    reel_breakdown_fetch_coverage = growth_coverage.get(
        "reel_audience_breakdown_fetch"
    )
    reel_breakdown_fetch_coverage = (
        reel_breakdown_fetch_coverage
        if isinstance(reel_breakdown_fetch_coverage, Mapping)
        else {}
    )
    if int(stock_coverage.get("count") or 0) == 0:
        output.append(
            {
                "field": "account_follower_stock",
                "coverage": coverage_entry(0, 1),
                "limitation": (
                    "Current follower stock and point-in-time stock change are unavailable."
                ),
                "instrumentation_action": (
                    "Sync Instagram account followers_count snapshots prospectively."
                ),
            }
        )
    if int(flow_coverage.get("count") or 0) == 0:
        output.append(
            {
                "field": "account_follow_flows",
                "coverage": coverage_entry(0, 1),
                "limitation": (
                    "Gross follows, unfollows, and account-wide net follower growth "
                    "cannot be reported."
                ),
                "instrumentation_action": (
                    "Sync explicit daily account follows/unfollows intervals; never "
                    "substitute or allocate them to posts."
                ),
            }
        )
    elif int(account_reach_coverage.get("count") or 0) < int(
        account_reach_coverage.get("total") or 0
    ):
        output.append(
            {
                "field": "daily_account_reach",
                "coverage": dict(account_reach_coverage),
                "limitation": (
                    "Account follower-flow rates per 1,000 account reach are unavailable "
                    "for intervals without the same-period account reach denominator."
                ),
                "instrumentation_action": (
                    "Sync account-wide daily reach for exactly the same intervals as "
                    "follows/unfollows; never substitute a post's lifetime Reel reach."
                ),
            }
        )
    if int(flow_coverage.get("count") or 0) and int(
        reel_breakdown_fetch_coverage.get("count") or 0
    ) < int(reel_breakdown_fetch_coverage.get("total") or 0):
        output.append(
            {
                "field": "daily_reel_reach_breakdown",
                "coverage": dict(reel_breakdown_fetch_coverage),
                "limitation": (
                    "Same-day observational follower efficiency against REEL-filtered "
                    "account reach is unavailable for uncovered intervals."
                ),
                "instrumentation_action": (
                    "Sync account reach with media_product_type and follow_type "
                    "breakdowns for the exact follower-flow interval."
                ),
            }
        )
    return output


def build_audit_catalog(
    coverage: Mapping[str, Any],
) -> list[dict[str, Any]]:
    source_fields = coverage.get("source_fields")
    source_fields = source_fields if isinstance(source_fields, Mapping) else {}
    all_snapshot = coverage.get("all_snapshot_metrics")
    all_snapshot = all_snapshot if isinstance(all_snapshot, Mapping) else {}
    rows: list[dict[str, Any]] = []
    mappings = (
        ("views", "views"),
        ("total_views", "total_views"),
        ("facebook_views", "facebook_views"),
        ("crossposted_views", "crossposted_views"),
        ("plays", "plays"),
        ("replays", "clips_replays_count"),
        ("reach", "reach"),
        ("non_follower_reach", "non_follower_reach"),
        ("follower_reach", "follower_reach"),
        ("total_watch_time_seconds", "ig_reels_video_view_total_time"),
        ("average_watch_time_seconds", "ig_reels_avg_watch_time"),
        ("reels_skip_rate", "reels_skip_rate"),
        ("likes", "likes"),
        ("comments", "comments"),
        ("saves", "saved"),
        ("shares", "shares"),
        ("reposts", "reposts"),
        ("sends", "sends"),
        ("interactions", "total_interactions"),
        ("follows", "follows"),
        ("profile_visits", "profile_visits"),
        ("dm_keyword_hits", "dm_keyword_hits"),
        ("returning_viewers", "returning_viewers"),
    )
    for canonical, source_name in mappings:
        source = source_fields.get(source_name)
        source = source if isinstance(source, Mapping) else {}
        canonical_coverage = all_snapshot.get(canonical)
        canonical_coverage = (
            canonical_coverage if isinstance(canonical_coverage, Mapping) else {}
        )
        known_optional_source_names = {
            "views",
            "total_views",
            "reach",
            "ig_reels_video_view_total_time",
            "ig_reels_avg_watch_time",
            "reels_skip_rate",
            "likes",
            "comments",
            "saved",
            "shares",
            "reposts",
            "total_interactions",
            "facebook_views",
            "crossposted_views",
            "follows",
        }
        rows.append(
            {
                "canonical_field": canonical,
                "source_api_field": (
                    source_name
                    if source or source_name in known_optional_source_names
                    else None
                ),
                "semantics": SOURCE_METRIC_DEFINITIONS.get(
                    source_name, "Unavailable in the current stored Meta response."
                ),
                "canonical_unit": (
                    "seconds"
                    if canonical
                    in {"total_watch_time_seconds", "average_watch_time_seconds"}
                    else "percent"
                    if canonical == "reels_skip_rate"
                    else "count"
                ),
                "source_unit": (
                    "milliseconds"
                    if source_name
                    in {
                        "ig_reels_video_view_total_time",
                        "ig_reels_avg_watch_time",
                    }
                    else None
                ),
                "coverage": dict(canonical_coverage),
                "periods": source.get("periods", []),
                "first_captured_at": source.get("first_captured_at"),
                "last_captured_at": source.get("last_captured_at"),
                "availability": (
                    "excluded_unverified_post_attribution"
                    if canonical == "follows" and source
                    else "available"
                    if source
                    else "unavailable"
                ),
            }
        )
    for source_name in ("total_likes", "total_comments"):
        source = source_fields.get(source_name)
        source = source if isinstance(source, Mapping) else {}
        source_coverage = source.get("coverage")
        source_coverage = (
            source_coverage
            if isinstance(source_coverage, Mapping)
            else coverage_entry(0, int(coverage.get("stored_snapshot_rows") or 0))
        )
        rows.append(
            {
                "canonical_field": f"source_only:{source_name}",
                "source_api_field": source_name,
                "semantics": SOURCE_METRIC_DEFINITIONS.get(
                    source_name,
                    "Optional source field retained for provenance; unavailable in the "
                    "current stored response set.",
                ),
                "canonical_unit": (
                    "percent" if source_name == "reels_skip_rate" else "count"
                ),
                "source_unit": None,
                "coverage": dict(source_coverage),
                "periods": source.get("periods", []),
                "first_captured_at": source.get("first_captured_at"),
                "last_captured_at": source.get("last_captured_at"),
                "availability": "available" if source else "schema_only_or_unavailable",
            }
        )
    return rows


def _strip_internal(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_internal(item)
            for key, item in value.items()
            if not str(key).startswith("_")
        }
    if isinstance(value, list):
        return [_strip_internal(item) for item in value]
    return value


def _analysis_window_for_post(
    post: Mapping[str, Any],
    priority: Sequence[str],
) -> str | None:
    windows = post.get("maturity_windows")
    windows = windows if isinstance(windows, Mapping) else {}
    return next(
        (window for window in priority if isinstance(windows.get(window), Mapping)),
        None,
    )


def account_growth_recommendation(
    account_growth: Mapping[str, Any],
    *,
    account: str,
) -> dict[str, Any] | None:
    """Return a descriptive account-monitoring action, never a content action."""
    trend = account_growth.get("trend")
    trend = trend if isinstance(trend, Mapping) else {}
    if trend.get("status") != "AVAILABLE":
        return None
    sample_size = int(trend.get("lookback_intervals") or 0)
    median_net = numeric(trend.get("median_daily_net_growth"))
    gross_rate = numeric(
        account_growth.get("gross_follows_per_1000_account_reach")
    )
    net_rate = numeric(
        account_growth.get("net_follows_per_1000_account_reach")
    )
    reason = (
        f"Across {sample_size} finalized daily account intervals, median net "
        f"growth was {_format_number(median_net)} followers/day"
    )
    if gross_rate is not None and net_rate is not None:
        reason += (
            f"; gross follows were {gross_rate:.2f}/1,000 account reach and "
            f"net follows were {net_rate:.2f}/1,000 account reach over covered intervals"
        )
    reason += (
        ". Continue account-level monitoring. This describes the account trend "
        "and does not identify a causal post, series, hook, or format."
    )
    return {
        "recommendation": "MONITOR",
        "entity_type": "account",
        "entity_id_or_name": account,
        "maturity_window": "daily_account_intervals",
        "supporting_metrics": {
            "median_daily_gross_follows": trend.get(
                "median_daily_gross_follows"
            ),
            "median_daily_unfollows": trend.get("median_daily_unfollows"),
            "median_daily_net_growth": trend.get("median_daily_net_growth"),
            "gross_follows_per_1000_account_reach": gross_rate,
            "net_follows_per_1000_account_reach": net_rate,
            "denominator_type": "account_reach",
        },
        "comparison_baseline": (
            "finalized daily account intervals; no post-level comparison"
        ),
        "sample_size": sample_size,
        "reason": reason,
        "confidence_or_evidence_status": (
            "descriptive_account_level_not_causal"
        ),
    }


FACEBOOK_COMPARABLE_METRICS = (
    "views",
    "plays",
    "initial_plays",
    "replays",
    "reach",
    "follow_conversion",
    "likes_per_1000_views",
    "likes_per_1000_reach",
    "likes_per_1000_unique_media_viewers",
    "reactions_per_1000_views",
    "reactions_per_1000_reach",
    "reactions_per_1000_unique_media_viewers",
    "comments_per_1000_views",
    "comments_per_1000_reach",
    "comments_per_1000_unique_media_viewers",
    "shares_per_1000_views",
    "shares_per_1000_reach",
    "shares_per_1000_unique_media_viewers",
    "interactions_per_1000_views",
    "interactions_per_1000_reach",
    "interactions_per_1000_unique_media_viewers",
    "watch_depth",
    "total_watch_hours",
    "three_second_retention_rate",
    "three_second_dropoff_rate",
    "follows_per_production_hour",
    "views_per_production_hour",
)


def facebook_window_summary(
    observations: Sequence[Mapping[str, Any]],
    *,
    quartiles_min_n: int,
) -> dict[str, Any]:
    metrics = {
        metric: distribution(
            [metric_value(observation, metric) for observation in observations],
            quartiles_min_n=quartiles_min_n,
        )
        for metric in FACEBOOK_COMPARABLE_METRICS
    }
    known_views = finite_values(
        [metric_value(observation, "views") for observation in observations]
    )
    return {
        "post_count": len(observations),
        "known_total_views": sum(known_views) if known_views else None,
        "views_coverage": coverage_entry(len(known_views), len(observations)),
        "metrics": metrics,
    }


def facebook_cohorts(
    observations: Sequence[Mapping[str, Any]],
    *,
    quartiles_min_n: int,
) -> dict[str, Any]:
    output: dict[str, Any] = {
        "account": facebook_window_summary(
            observations,
            quartiles_min_n=quartiles_min_n,
        )
    }
    for dimension in COHORT_DIMENSIONS:
        groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for observation in observations:
            metadata = observation.get("content_metadata")
            metadata = metadata if isinstance(metadata, Mapping) else {}
            value = metadata.get(dimension)
            if value is None or not str(value).strip():
                continue
            groups[str(value)].append(observation)
        output[dimension] = {
            value: facebook_window_summary(
                rows,
                quartiles_min_n=quartiles_min_n,
            )
            for value, rows in sorted(groups.items())
        }
    return output


def facebook_data_gaps(
    coverage: Mapping[str, Any],
) -> list[dict[str, Any]]:
    total = int(coverage.get("published_posts") or 0)
    latest = coverage.get("latest_post_metrics")
    latest = latest if isinstance(latest, Mapping) else {}
    derived = coverage.get("latest_derived_metrics")
    derived = derived if isinstance(derived, Mapping) else {}
    metadata = coverage.get("content_metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    maturity = coverage.get("snapshot_maturity")
    maturity = maturity if isinstance(maturity, Mapping) else {}
    specs = (
        (
            "reach",
            latest.get("reach"),
            "Unique-viewer-based engagement and distribution efficiency cannot be calculated.",
            "Grant `read_insights` and collect `post_total_media_view_unique`; keep it unavailable until Meta returns it.",
        ),
        (
            "shares",
            latest.get("shares"),
            "Share intent is incomplete; Page-post `shares.count` is retained only when returned.",
            "Continue requesting the associated Page-post share field; never turn an omitted field into zero.",
        ),
        (
            "saves",
            latest.get("saves"),
            "Facebook save intent cannot be compared with Instagram saves.",
            "Add only a verified Facebook save/bookmark source if one becomes available.",
        ),
        (
            "interactions",
            latest.get("interactions"),
            "Total engagement per reach or per view remains unavailable unless every component is known.",
            "Do not manufacture total interactions by treating missing shares as zero.",
        ),
        (
            "average_watch_time_seconds",
            latest.get("average_watch_time_seconds"),
            "Watch depth and retention quality cannot be measured.",
            "Grant `read_insights` and collect documented `post_video_avg_time_watched`.",
        ),
        (
            "reels_skip_rate",
            coverage.get("latest_derived_metrics", {}).get(
                "three_second_dropoff_rate"
            ),
            "Facebook does not expose Instagram's first-three-second skip metric; an exact 3-second retention drop-off is unavailable.",
            "Collect the retention graph with `read_insights`; derive only an exact 3-second graph point and label it as drop-off, not Meta skip rate.",
        ),
        (
            "follows",
            latest.get("follows"),
            "Facebook Reel-attributed follower conversion cannot be measured.",
            "Grant `read_insights` and collect documented `post_video_followers`; do not assign Page-level movement to a Reel.",
        ),
        (
            "production_minutes",
            metadata.get("production_minutes"),
            "Views per production hour and production efficiency cannot be ranked.",
            "Record measured production_minutes once per shared content asset.",
        ),
        (
            "series",
            metadata.get("series"),
            "Repeatability by recurring series cannot be evaluated.",
            "Use stable content-hash annotations so Instagram and Facebook share the same series tag.",
        ),
    )
    output: list[dict[str, Any]] = []
    for field, raw_entry, limitation, action in specs:
        entry = raw_entry if isinstance(raw_entry, Mapping) else {}
        count = int(entry.get("count") or 0)
        if count < total:
            output.append(
                {
                    "field": field,
                    "coverage": coverage_entry(count, total),
                    "limitation": limitation,
                    "instrumentation_action": action,
                }
            )
    for window in WINDOW_ORDER:
        entry = maturity.get(window)
        entry = entry if isinstance(entry, Mapping) else {}
        count = int(entry.get("count") or 0)
        if count < total:
            output.append(
                {
                    "field": f"{window}_snapshot",
                    "coverage": coverage_entry(count, total),
                    "limitation": (
                        f"Only {count}/{total} Facebook Reels have a real {window} "
                        "snapshot inside the configured tolerance."
                    ),
                    "instrumentation_action": (
                        f"Collect the {window} checkpoint prospectively from the "
                        "Facebook publication clock; never infer it from a later total."
                    ),
                }
            )
    return output


def build_facebook_analytics(
    *,
    db_path: Path | None,
    channel: str,
    config: Mapping[str, Any],
    annotations: Sequence[Mapping[str, Any]],
    as_of: datetime,
    instagram_posts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    resolved = db_path.expanduser().resolve() if db_path is not None else None
    if resolved is None or not resolved.is_file():
        return {
            "status": "UNAVAILABLE",
            "platform": "facebook",
            "db_path": str(resolved) if resolved is not None else None,
            "reason": "Independent Facebook ledger is unavailable.",
            "data_coverage": {
                "published_posts": 0,
                "latest_snapshot_posts": 0,
            },
            "maturity_windows": {},
            "posts": [],
            "data_gaps": [],
        }

    posts, inventory = load_canonical_posts(
        db_path=resolved,
        channel=channel,
        config=config,
        annotations=annotations,
        as_of=as_of,
        platform="facebook",
    )
    instagram_by_hash = {
        str(post.get("identity", {}).get("content_hash") or ""): post
        for post in instagram_posts
        if isinstance(post, Mapping)
    }
    paired_count = 0
    priority_raw = config.get("statistics", {}).get(
        "decision_window_priority",
        list(DECISION_WINDOW_ORDER),
    )
    priority = (
        [str(value) for value in priority_raw]
        if isinstance(priority_raw, list)
        else list(DECISION_WINDOW_ORDER)
    )
    for post in posts:
        identity = post.get("identity")
        identity = identity if isinstance(identity, dict) else {}
        paired = instagram_by_hash.get(str(identity.get("content_hash") or ""))
        if isinstance(paired, Mapping):
            paired_identity = paired.get("identity")
            paired_identity = (
                paired_identity if isinstance(paired_identity, Mapping) else {}
            )
            identity["paired_instagram"] = {
                "media_id": paired_identity.get("media_id"),
                "permalink": paired_identity.get("permalink"),
                "published_at": paired_identity.get("published_at"),
            }
            paired_count += 1
        else:
            identity["paired_instagram"] = None
        post["analysis_maturity_window"] = _analysis_window_for_post(
            post,
            priority,
        )
        post["classifications"] = []
        post["funnel_diagnostics"] = []

    coverage = build_data_coverage(posts)
    quartiles_min_n = int(
        config.get("statistics", {}).get("minimum_posts_for_percentiles", 4)
    )
    maturity_windows: dict[str, Any] = {}
    for window in (*WINDOW_ORDER, "latest"):
        observations = observations_for_window(posts, window)
        maturity_windows[window] = {
            "target": (
                config.get("maturity_windows", {}).get(window)
                if window in WINDOW_ORDER
                else {"label": "latest available"}
            ),
            **facebook_window_summary(
                observations,
                quartiles_min_n=quartiles_min_n,
            ),
            "cohorts": facebook_cohorts(
                observations,
                quartiles_min_n=quartiles_min_n,
            ),
        }
    latest_summary = maturity_windows["latest"]
    latest_metrics = latest_summary.get("metrics")
    latest_metrics = latest_metrics if isinstance(latest_metrics, Mapping) else {}
    gaps = facebook_data_gaps(coverage)
    source_fields = coverage.get("source_fields")
    source_fields = source_fields if isinstance(source_fields, Mapping) else {}
    rich_source_names = (
        "blue_reels_play_count",
        "fb_reels_replay_count",
        "fb_reels_total_plays",
        "post_total_media_view_unique",
        "post_video_avg_time_watched",
        "post_video_followers",
        "post_video_likes_by_reaction_type",
        "post_video_retention_graph",
        "post_video_social_actions",
        "post_video_view_time",
    )
    rich_rows = sum(
        int(
            (
                source_fields.get(name)
                if isinstance(source_fields.get(name), Mapping)
                else {}
            ).get("count")
            or 0
        )
        for name in rich_source_names
    )
    latest_raw = coverage.get("latest_post_metrics")
    latest_raw = latest_raw if isinstance(latest_raw, Mapping) else {}
    follow_count = int(
        (
            latest_raw.get("follows")
            if isinstance(latest_raw.get("follows"), Mapping)
            else {}
        ).get("count")
        or 0
    )
    reach_count = int(
        (
            latest_raw.get("reach")
            if isinstance(latest_raw.get("reach"), Mapping)
            else {}
        ).get("count")
        or 0
    )
    if follow_count and reach_count:
        primary_metric = (
            "Facebook Reel-attributed follows per 1,000 unique media viewers, "
            "kept separate from Instagram"
        )
    elif reach_count:
        primary_metric = (
            "intent and engagement actions per 1,000 unique media viewers; "
            "follower outcome unavailable"
        )
    else:
        primary_metric = (
            "Facebook Video views, with likes/comments per 1,000 views as "
            "explicit view-denominator diagnostics; follower outcome unavailable"
        )
    return {
        "status": "AVAILABLE" if posts else "NO_PUBLISHED_POSTS",
        "platform": "facebook",
        "db_path": str(resolved),
        "analytical_scope": (
            "Independent Facebook Page Reel uploads only. Facebook and Instagram "
            "publication clocks, media IDs, denominators, cohorts, and rankings remain separate."
        ),
        "api_semantics": {
            "graph_api_version": "v25.0",
            "rich_video_insights": {
                "endpoint": "/{facebook-video-id}/video_insights",
                "status": (
                    "COLLECTED"
                    if rich_rows
                    else "NO_DOCUMENTED_RICH_METRICS_STORED"
                ),
                "required_permissions": [
                    "read_insights",
                    "pages_read_engagement",
                    "pages_manage_engagement",
                ],
                "stored_source_observations": rich_rows,
            },
            "views": (
                "Graph API v25 direct Facebook Video `views` field; a playback count, "
                "not verified unique reach. When available, documented "
                "`fb_reels_total_plays` takes precedence."
            ),
            "reach": (
                "`post_total_media_view_unique` is normalized as unique media "
                "viewers and displayed with that denominator label."
            ),
            "likes": (
                "Graph API v25 Facebook Video `likes` edge summary total_count."
            ),
            "comments": (
                "Graph API v25 Facebook Video `comments` edge summary total_count."
            ),
            "shares": (
                "Associated Facebook Page-post `shares.count`; an omitted field remains "
                "unavailable and is never converted to zero. Documented "
                "`post_video_social_actions` takes precedence when collected."
            ),
            "unavailable": (
                "Facebook saves, direct Instagram-style first-three-second skip, "
                "per-Reel profile visits, unique non-follower reach, and returning "
                "viewers are not documented for Facebook Reels."
            ),
        },
        "inventory": inventory,
        "paired_instagram_posts": coverage_entry(paired_count, len(posts)),
        "data_coverage": coverage,
        "account_summary": {
            "current_primary_metric": primary_metric,
            "post_count": latest_summary.get("post_count"),
            "known_total_views": latest_summary.get("known_total_views"),
            "views_coverage": latest_summary.get("views_coverage"),
            "median_views": (
                latest_metrics.get("views", {}).get("median")
                if isinstance(latest_metrics.get("views"), Mapping)
                else None
            ),
            "median_unique_media_viewers": (
                latest_metrics.get("reach", {}).get("median")
                if isinstance(latest_metrics.get("reach"), Mapping)
                else None
            ),
            "median_follow_conversion": (
                latest_metrics.get("follow_conversion", {}).get("median")
                if isinstance(
                    latest_metrics.get("follow_conversion"),
                    Mapping,
                )
                else None
            ),
            "median_likes_per_1000_views": (
                latest_metrics.get("likes_per_1000_views", {}).get("median")
                if isinstance(
                    latest_metrics.get("likes_per_1000_views"),
                    Mapping,
                )
                else None
            ),
            "median_likes_per_1000_unique_media_viewers": (
                latest_metrics.get(
                    "likes_per_1000_unique_media_viewers",
                    {},
                ).get("median")
                if isinstance(
                    latest_metrics.get(
                        "likes_per_1000_unique_media_viewers"
                    ),
                    Mapping,
                )
                else None
            ),
            "median_comments_per_1000_views": (
                latest_metrics.get("comments_per_1000_views", {}).get("median")
                if isinstance(
                    latest_metrics.get("comments_per_1000_views"),
                    Mapping,
                )
                else None
            ),
            "median_comments_per_1000_unique_media_viewers": (
                latest_metrics.get(
                    "comments_per_1000_unique_media_viewers",
                    {},
                ).get("median")
                if isinstance(
                    latest_metrics.get(
                        "comments_per_1000_unique_media_viewers"
                    ),
                    Mapping,
                )
                else None
            ),
            "median_shares_per_1000_views": (
                latest_metrics.get("shares_per_1000_views", {}).get("median")
                if isinstance(
                    latest_metrics.get("shares_per_1000_views"),
                    Mapping,
                )
                else None
            ),
            "median_shares_per_1000_unique_media_viewers": (
                latest_metrics.get(
                    "shares_per_1000_unique_media_viewers",
                    {},
                ).get("median")
                if isinstance(
                    latest_metrics.get(
                        "shares_per_1000_unique_media_viewers"
                    ),
                    Mapping,
                )
                else None
            ),
            "median_watch_depth": (
                latest_metrics.get("watch_depth", {}).get("median")
                if isinstance(latest_metrics.get("watch_depth"), Mapping)
                else None
            ),
            "median_total_watch_hours": (
                latest_metrics.get("total_watch_hours", {}).get("median")
                if isinstance(latest_metrics.get("total_watch_hours"), Mapping)
                else None
            ),
            "repeatable_winner": None,
            "evidence_status": "descriptive_view_based_not_growth_attributed",
        },
        "maturity_windows": maturity_windows,
        "posts": posts,
        "classifications": {
            "status": "INSUFFICIENT DATA",
            "reason": (
                "No Facebook Hidden Gem, Vanity Winner, Workhorse, or SCALE/PAUSE "
                "label is assigned without reach/follow conversion, intent, production, "
                "and adequate repeatability coverage."
            ),
            "hidden_gems": [],
            "vanity_winners": [],
            "workhorses": [],
            "expensive_stars": [],
            "underperformers": [],
        },
        "recommendations": [
            {
                "recommendation": "COLLECT",
                "entity_type": "platform_instrumentation",
                "entity_id_or_name": gap["field"],
                "maturity_window": "prospective",
                "supporting_metrics": {"coverage": gap["coverage"]},
                "comparison_baseline": "independent Facebook Reel uploads",
                "sample_size": len(posts),
                "reason": gap["instrumentation_action"],
                "confidence_or_evidence_status": "measured_data_gap",
            }
            for gap in gaps[:5]
        ],
        "data_gaps": gaps,
    }


def build_moneyball_report(
    *,
    db_path: Path = DEFAULT_DB_PATH,
    channel: str = "aibrief_jp",
    config_path: Path = DEFAULT_CONFIG_PATH,
    annotations_path: Path = DEFAULT_ANNOTATIONS_PATH,
    generated_at: datetime | None = None,
    as_of: datetime | None = None,
    facebook_db_path: Path | None = None,
) -> dict[str, Any]:
    config = load_config(config_path)
    annotations = load_annotations(annotations_path)
    generated = generated_at or datetime.now(timezone.utc)
    effective_as_of = as_of or generated
    posts, inventory = load_canonical_posts(
        db_path=db_path,
        channel=channel,
        config=config,
        annotations=annotations,
        as_of=effective_as_of,
    )
    account_growth = load_account_growth(
        db_path=db_path,
        channel=channel,
        config=config,
        as_of=effective_as_of,
    )
    add_account_publication_context(account_growth, posts)
    facebook_analytics = build_facebook_analytics(
        db_path=facebook_db_path,
        channel=channel,
        config=config,
        annotations=annotations,
        as_of=effective_as_of,
        instagram_posts=posts,
    )

    maturity_windows: dict[str, Any] = {}
    account_baselines: dict[str, Any] = {}
    observations_by_window: dict[str, list[dict[str, Any]]] = {}
    for window in (*WINDOW_ORDER, "latest"):
        observations = observations_for_window(posts, window)
        observations_by_window[window] = observations
        cohorts = build_cohorts(observations, COHORT_DIMENSIONS, config)
        maturity_windows[window] = {
            "target": (
                config.get("maturity_windows", {}).get(window)
                if window in WINDOW_ORDER
                else {"label": "latest available"}
            ),
            "post_count": len(observations),
            "cohorts": cohorts,
        }
        if cohorts.get("account") is not None:
            account_baselines[window] = cohorts["account"]

    priority_raw = config.get("statistics", {}).get(
        "decision_window_priority", list(DECISION_WINDOW_ORDER)
    )
    priority = (
        [str(value) for value in priority_raw]
        if isinstance(priority_raw, list)
        else list(DECISION_WINDOW_ORDER)
    )
    post_by_hash = {
        str(post.get("identity", {}).get("content_hash") or ""): post
        for post in posts
    }
    all_classifications: list[dict[str, Any]] = []
    all_diagnostics: list[dict[str, Any]] = []
    stage_summary: dict[str, Counter[str]] = {
        stage: Counter()
        for stage in (
            "exposure",
            "attention",
            "satisfaction",
            "curiosity",
            "conversion",
            "production_efficiency",
            "retention",
        )
    }
    for post in posts:
        analysis_window = _analysis_window_for_post(post, priority)
        post["analysis_maturity_window"] = analysis_window
        if analysis_window is None:
            continue
        observation = next(
            (
                row
                for row in observations_by_window[analysis_window]
                if row.get("identity", {}).get("content_hash")
                == post.get("identity", {}).get("content_hash")
            ),
            None,
        )
        if observation is None:
            continue
        baseline = choose_comparison_baseline(
            observation, observations_by_window[analysis_window], config
        )
        if baseline is None:
            continue
        classifications = classify_post(observation, baseline, config)
        diagnostics = diagnose_funnel(observation, baseline, config)
        post["classifications"] = classifications
        post["funnel_diagnostics"] = diagnostics
        post["comparison_baseline"] = _strip_internal(baseline)
        post["funnel_stage_statuses"] = stage_statuses(observation, baseline)
        all_classifications.extend(classifications)
        all_diagnostics.extend(diagnostics)
        for stage, result in post["funnel_stage_statuses"].items():
            stage_summary[stage][str(result.get("status") or "UNKNOWN")] += 1

    series, workhorses = analyze_series(posts, account_baselines, config)
    experiments = analyze_experiments(posts, config)
    coverage = build_data_coverage(posts)
    coverage["account_growth"] = account_growth.get("coverage", {})
    gaps = build_data_gaps(coverage, account_growth)
    scoreboard = account_scoreboard(posts)

    classifications = {
        "hidden_gems": [
            item for item in all_classifications if item["label"] == "Hidden Gem"
        ],
        "vanity_winners": [
            item for item in all_classifications if item["label"] == "Vanity Winner"
        ],
        "workhorses": workhorses,
        "expensive_stars": [
            item for item in all_classifications if item["label"] == "Expensive Star"
        ],
        "underperformers": [
            item for item in all_classifications if item["label"] == "Underperformer"
        ],
    }
    for values in classifications.values():
        values.sort(
            key=lambda item: (
                str(item.get("maturity_window") or ""),
                str(item.get("media_id") or item.get("series") or ""),
            )
        )

    recommendations: list[dict[str, Any]] = []
    for row in series:
        recommendations.append(
            {
                "recommendation": row["recommendation"],
                "entity_type": "series",
                "entity_id_or_name": row["series"],
                "maturity_window": row["maturity_window"],
                "supporting_metrics": {
                    "post_count": row["post_count"],
                    "consistency_last_five": row["consistency_last_five"],
                    "median_follows_per_production_hour": row[
                        "median_follows_per_production_hour"
                    ],
                    "median_follow_conversion": row["median_follow_conversion"],
                },
                "comparison_baseline": "same-maturity account medians, denominator-separated",
                "sample_size": row["post_count"],
                "reason": row["recommendation_reason"],
                "confidence_or_evidence_status": row["evidence_status"],
            }
        )
    for gap in gaps[:4]:
        recommendations.append(
            {
                "recommendation": "COLLECT",
                "entity_type": "instrumentation",
                "entity_id_or_name": gap["field"],
                "maturity_window": "prospective",
                "supporting_metrics": {"coverage": gap["coverage"]},
                "comparison_baseline": "published Reels",
                "sample_size": coverage["published_posts"],
                "reason": gap["instrumentation_action"],
                "confidence_or_evidence_status": "measured_data_gap",
            }
        )
    growth_recommendation = account_growth_recommendation(
        account_growth,
        account=channel,
    )
    if growth_recommendation is not None:
        recommendations.append(growth_recommendation)

    follower_coverage = coverage["latest_post_metrics"]["follows"]["count"]
    production_coverage = coverage["content_metadata"]["production_minutes"]["count"]
    if workhorses and follower_coverage == len(posts) and production_coverage == len(posts):
        next_ten = {
            "status": "AVAILABLE",
            "allocation": [
                {
                    "bucket": "proven_workhorses",
                    "posts": 5,
                    "support": f"{len(workhorses)} series met the full Workhorse rule.",
                },
                {
                    "bucket": "promising_hidden_gems",
                    "posts": 2,
                    "support": f"{len(classifications['hidden_gems'])} Hidden Gems measured.",
                },
                {
                    "bucket": "limited_flagship",
                    "posts": 1,
                    "support": f"{len(classifications['expensive_stars'])} Expensive Stars measured.",
                },
                {
                    "bucket": "controlled_experiments",
                    "posts": 2,
                    "support": "Reserve learning capacity for one-variable tests.",
                },
            ],
            "reason": "Allocation is supported by complete follow and production coverage.",
        }
    else:
        next_ten = {
            "status": "INSUFFICIENT DATA",
            "allocation": [],
            "reason": (
                f"Do not prescribe a ten-post allocation yet: follows cover "
                f"{follower_coverage}/{len(posts)} posts, production_minutes cover "
                f"{production_coverage}/{len(posts)}, and {len(workhorses)} series meet "
                "the full repeatability rule. Record those fields and at least five "
                "comparable posts per series."
            ),
        }

    hidden_sorted = sorted(
        classifications["hidden_gems"],
        key=lambda item: (
            numeric(item.get("supporting_metrics", {}).get("follow_conversion"))
            is not None,
            float(
                numeric(item.get("supporting_metrics", {}).get("follow_conversion"))
                or -math.inf
            ),
        ),
        reverse=True,
    )
    vanity_sorted = sorted(
        classifications["vanity_winners"],
        key=lambda item: float(
            numeric(item.get("supporting_metrics", {}).get("reach")) or -math.inf
        ),
        reverse=True,
    )
    efficiency_candidates = []
    for post in posts:
        window = post.get("analysis_maturity_window")
        observation = post.get("maturity_windows", {}).get(window) if window else None
        if isinstance(observation, Mapping):
            value = numeric(metric_value(observation, "follows_per_production_hour"))
            if value is not None:
                efficiency_candidates.append((float(value), post))
    efficiency_candidates.sort(
        key=lambda pair: (
            pair[0],
            str(pair[1].get("identity", {}).get("media_id") or ""),
        ),
        reverse=True,
    )

    if numeric(account_growth.get("net_growth")) is not None:
        primary_metric = (
            "net account follower growth (account-wide daily flows; not post-attributed)"
        )
    elif numeric(account_growth.get("follower_stock", {}).get("latest")) is not None:
        primary_metric = (
            "account follower stock (point-in-time; post attribution unavailable)"
        )
    else:
        primary_metric = (
            "follows_per_1000_non_follower_reach / follows_per_1000_reach, "
            "denominator-separated"
            if follower_coverage
            else (
                "intent_actions_per_1000_reach "
                "(leading indicator; follower outcome unavailable)"
            )
        )
    strongest_series = next(
        (row for row in series if row.get("recommendation") == "SCALE"), None
    )
    report = {
        "report_metadata": {
            "schema_version": 1,
            "generated_at": isoformat_seconds(generated),
            "generated_at_jst": generated.astimezone(JST).replace(microsecond=0).isoformat(),
            "as_of": isoformat_seconds(effective_as_of),
            "account": channel,
            "db_path": str(db_path.expanduser().resolve()),
            "facebook_db_path": facebook_analytics.get("db_path"),
            "config_path": str(config_path.expanduser().resolve()),
            "annotations_path": str(annotations_path.expanduser().resolve()),
            "platforms": (
                ["instagram", "facebook"]
                if facebook_analytics.get("status")
                in {"AVAILABLE", "NO_PUBLISHED_POSTS"}
                else ["instagram"]
            ),
            "analytical_scope": (
                "Independent Instagram and Facebook Reel observations. Platform "
                "media IDs, publication clocks, maturity windows, denominators, "
                "cohorts, totals, and rankings are never combined."
            ),
        },
        "platform_analytics": {
            "instagram": {
                "status": "AVAILABLE" if posts else "NO_PUBLISHED_POSTS",
                "platform": "instagram",
                "db_path": str(db_path.expanduser().resolve()),
                "published_posts": len(posts),
                "latest_snapshot_posts": coverage["latest_snapshot_posts"],
            },
            "facebook": facebook_analytics,
        },
        "data_coverage": coverage,
        "data_audit": {
            "metric_catalog": build_audit_catalog(coverage),
            "source_metric_fields": coverage["source_fields"],
            "raw_column_conflicts": coverage["raw_column_conflicts"],
            "snapshot_semantics": (
                "Stored Graph values are lifetime totals observed at captured_at. "
                "Snapshots are historical observations; they are not per-window increments."
            ),
            "shares_and_sends": (
                "`shares` is the exact current source field. `sends` is unavailable and "
                "equivalence is not verified; the fields are not combined."
            ),
            "interactions": (
                "`total_interactions` is retained as the Graph net aggregate and is not "
                "used as an opaque combined score."
            ),
            "account_growth": {
                "coverage": account_growth.get("coverage", {}),
                "lag": account_growth.get("lag", {}),
                "preliminary": account_growth.get("preliminary", {}),
                "source_labels": account_growth.get("source_labels", {}),
                "denominator_labels": account_growth.get(
                    "denominator_labels", {}
                ),
                "attribution_warning": ACCOUNT_ATTRIBUTION_WARNING,
            },
            "inventory": inventory,
        },
        "account_summary": {
            **scoreboard,
            "current_primary_metric": primary_metric,
            "strongest_repeatable_series": strongest_series,
            "strongest_hidden_gem": hidden_sorted[0] if hidden_sorted else None,
            "largest_vanity_winner": vanity_sorted[0] if vanity_sorted else None,
            "most_production_efficient_post": (
                {
                    "media_id": efficiency_candidates[0][1]["identity"]["media_id"],
                    "follows_per_production_hour": efficiency_candidates[0][0],
                    "maturity_window": efficiency_candidates[0][1].get(
                        "analysis_maturity_window"
                    ),
                }
                if efficiency_candidates
                else None
            ),
            "most_important_data_gap": gaps[0] if gaps else None,
            "next_ten_post_allocation": next_ten,
        },
        "maturity_windows": maturity_windows,
        "account_growth": account_growth,
        "account_baselines": account_baselines,
        "posts": posts,
        "series": series,
        "experiments": experiments,
        "classifications": classifications,
        "funnel_diagnostics": {
            "diagnostics": all_diagnostics,
            "diagnostic_counts": dict(
                sorted(Counter(item["diagnostic"] for item in all_diagnostics).items())
            ),
            "stage_counts": {
                stage: dict(sorted(counts.items()))
                for stage, counts in stage_summary.items()
            },
        },
        "recommendations": recommendations,
        "data_gaps": gaps,
    }
    return _strip_internal(report)


def markdown_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def format_rate(value: Any, *, suffix: str = "") -> str:
    number = numeric(value)
    return "Unavailable" if number is None else f"{float(number):,.2f}{suffix}"


def format_coverage(entry: Any) -> str:
    if not isinstance(entry, Mapping):
        return "Unavailable"
    count = int(entry.get("count") or 0)
    total = int(entry.get("total") or 0)
    percentage = numeric(entry.get("percentage"))
    return (
        f"{count}/{total} ({float(percentage):.1f}%)"
        if percentage is not None
        else f"{count}/{total}"
    )


def post_label(identity: Mapping[str, Any]) -> str:
    media_id = str(identity.get("media_id") or "unknown")
    permalink = str(identity.get("permalink") or "")
    return f"[{media_id}]({permalink})" if permalink else media_id


def _follow_conversion_display(observation: Mapping[str, Any]) -> str:
    value = numeric(metric_value(observation, "follow_conversion"))
    denominator = follow_denominator_type(observation)
    return (
        "Unavailable"
        if value is None
        else f"{float(value):.2f}/1k {denominator}"
    )


def facebook_markdown_lines(facebook: Mapping[str, Any]) -> list[str]:
    """Render a denominator-explicit Facebook-native appendix."""
    status = str(facebook.get("status") or "UNAVAILABLE")
    coverage = facebook.get("data_coverage")
    coverage = coverage if isinstance(coverage, Mapping) else {}
    summary = facebook.get("account_summary")
    summary = summary if isinstance(summary, Mapping) else {}
    lines = [
        "",
        "## B3. Facebook-native Reel analytics",
        "",
        f"Status: **{status}**. Facebook uses its own video IDs, publication "
        "clock, snapshots, maturity cohorts, and denominators. Nothing in this "
        "section is added to or ranked against Instagram.",
    ]
    if status not in {"AVAILABLE", "NO_PUBLISHED_POSTS"}:
        lines.append(str(facebook.get("reason") or "Facebook ledger unavailable."))
        return lines

    maturity = coverage.get("snapshot_maturity")
    maturity = maturity if isinstance(maturity, Mapping) else {}
    api = facebook.get("api_semantics")
    api = api if isinstance(api, Mapping) else {}
    rich = api.get("rich_video_insights")
    rich = rich if isinstance(rich, Mapping) else {}
    lines.extend(
        [
            "",
            f"- Reels: **{coverage.get('published_posts', 0)}**; latest snapshots: "
            f"**{coverage.get('latest_snapshot_posts', 0)}/"
            f"{coverage.get('published_posts', 0)}**.",
            "- Fixed-window coverage: "
            + ", ".join(
                f"{window} **{format_coverage(maturity.get(window))}**"
                for window in WINDOW_ORDER
            )
            + ".",
            f"- Current primary metric: **{summary.get('current_primary_metric')}**.",
            f"- Rich `/video_insights` status: **{rich.get('status', 'UNAVAILABLE')}**; "
            f"stored rich source observations: **{rich.get('stored_source_observations', 0)}**. "
            "The direct Video/Page-post fallback remains active.",
            "",
            "| Latest-scoreboard metric | Median / value |",
            "|---|---:|",
            f"| Views | {_format_number(summary.get('median_views'))} |",
            f"| Unique media viewers | {_format_number(summary.get('median_unique_media_viewers'))} |",
            f"| Follows / 1,000 unique media viewers | {format_rate(summary.get('median_follow_conversion'))} |",
            f"| Likes / 1,000 views | {format_rate(summary.get('median_likes_per_1000_views'))} |",
            f"| Likes / 1,000 unique media viewers | {format_rate(summary.get('median_likes_per_1000_unique_media_viewers'))} |",
            f"| Comments / 1,000 views | {format_rate(summary.get('median_comments_per_1000_views'))} |",
            f"| Comments / 1,000 unique media viewers | {format_rate(summary.get('median_comments_per_1000_unique_media_viewers'))} |",
            f"| Shares / 1,000 views | {format_rate(summary.get('median_shares_per_1000_views'))} |",
            f"| Shares / 1,000 unique media viewers | {format_rate(summary.get('median_shares_per_1000_unique_media_viewers'))} |",
            f"| Watch depth | {_format_percent(summary.get('median_watch_depth'))} |",
            f"| Total watch hours | {format_rate(summary.get('median_total_watch_hours'))} |",
            "",
            "### Facebook per-Reel evidence",
            "",
            "| Reel | Paired Instagram | Published | Latest age | Views | Unique viewers | Engagement /1k | Likes /1k | Comments /1k | Shares /1k | Follows /1k unique viewers | Avg watch | Watch depth | 3s skip / drop-off |",
            "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    posts = facebook.get("posts")
    posts = posts if isinstance(posts, list) else []
    for post in sorted(
        (row for row in posts if isinstance(row, Mapping)),
        key=lambda row: (
            str(row.get("identity", {}).get("published_at") or ""),
            str(row.get("identity", {}).get("media_id") or ""),
        ),
        reverse=True,
    ):
        identity = post.get("identity")
        identity = identity if isinstance(identity, Mapping) else {}
        paired = identity.get("paired_instagram")
        paired = paired if isinstance(paired, Mapping) else {}
        observation = post.get("maturity_windows", {}).get("latest")
        observation = observation if isinstance(observation, Mapping) else {}
        raw = observation.get("raw_metrics")
        raw = raw if isinstance(raw, Mapping) else {}
        derived = observation.get("derived_metrics")
        derived = derived if isinstance(derived, Mapping) else {}
        reach_known = numeric(raw.get("reach")) is not None
        denominator = "unique viewers" if reach_known else "views"
        interactions_rate = derived.get(
            "interactions_per_1000_reach"
            if reach_known
            else "interactions_per_1000_views"
        )
        action_rates = {
            action: derived.get(
                f"{action}_per_1000_reach"
                if reach_known
                else f"{action}_per_1000_views"
            )
            for action in ("likes", "comments", "shares")
        }
        follow = derived.get("follow_conversion")
        follow = follow if isinstance(follow, Mapping) else {}
        direct_skip = numeric(raw.get("reels_skip_rate"))
        dropoff = numeric(derived.get("three_second_dropoff_rate"))
        skip_display = (
            f"{float(direct_skip):.1f}% direct"
            if direct_skip is not None
            else f"{float(dropoff) * 100:.1f}% derived drop-off"
            if dropoff is not None
            else "Unavailable"
        )
        paired_label = (
            post_label(paired)
            if paired.get("media_id")
            else "Unavailable"
        )
        lines.append(
            f"| {post_label(identity)} | {paired_label} | "
            f"{markdown_cell(identity.get('published_at') or 'Unavailable')} | "
            f"{format_rate(observation.get('actual_age_hours'), suffix='h')} | "
            f"{_format_number(raw.get('views'))} | {_format_number(raw.get('reach'))} | "
            f"{format_rate(interactions_rate)} {denominator} | "
            f"{format_rate(action_rates['likes'])} {denominator} | "
            f"{format_rate(action_rates['comments'])} {denominator} | "
            f"{format_rate(action_rates['shares'])} {denominator} | "
            f"{format_rate(follow.get('value'))} | "
            f"{format_rate(derived.get('average_watch_time_seconds'), suffix='s')} | "
            f"{_format_percent(derived.get('watch_depth'))} | {skip_display} |"
        )

    maturity_windows = facebook.get("maturity_windows")
    maturity_windows = (
        maturity_windows if isinstance(maturity_windows, Mapping) else {}
    )
    lines.extend(["", "### Facebook fixed-window performance", ""])
    for window in WINDOW_ORDER:
        rows: list[tuple[float, Mapping[str, Any], Mapping[str, Any]]] = []
        for post in posts:
            if not isinstance(post, Mapping):
                continue
            observation = post.get("maturity_windows", {}).get(window)
            if not isinstance(observation, Mapping):
                continue
            views = numeric(metric_value(observation, "views"))
            rows.append(
                (
                    float(views) if views is not None else -math.inf,
                    post,
                    observation,
                )
            )
        rows.sort(
            key=lambda item: (
                -item[0],
                str(item[1].get("identity", {}).get("media_id") or ""),
            )
        )
        lines.extend(
            [
                f"#### {window}",
                "",
                f"Valid observations: **{len(rows)}/{coverage.get('published_posts', 0)}**. "
                "Only Facebook observations from this maturity window are compared.",
            ]
        )
        if not rows:
            lines.extend(["", "No valid snapshot is available.", ""])
            continue
        lines.extend(
            [
                "",
                "| Reel | Actual age | Views | Unique viewers | Likes /1k denominator | Comments /1k denominator | Shares /1k denominator |",
                "|---|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for _, post, observation in rows:
            identity = post.get("identity", {})
            raw = observation.get("raw_metrics", {})
            derived = observation.get("derived_metrics", {})
            reach_known = numeric(raw.get("reach")) is not None
            suffix = "unique viewers" if reach_known else "views"
            lines.append(
                f"| {post_label(identity)} | "
                f"{format_rate(observation.get('actual_age_hours'), suffix='h')} | "
                f"{_format_number(raw.get('views'))} | "
                f"{_format_number(raw.get('reach'))} | "
                f"{format_rate(derived.get('likes_per_1000_reach' if reach_known else 'likes_per_1000_views'))} {suffix} | "
                f"{format_rate(derived.get('comments_per_1000_reach' if reach_known else 'comments_per_1000_views'))} {suffix} | "
                f"{format_rate(derived.get('shares_per_1000_reach' if reach_known else 'shares_per_1000_views'))} {suffix} |"
            )
        lines.append("")

    gaps = facebook.get("data_gaps")
    gaps = gaps if isinstance(gaps, list) else []
    lines.extend(["", "### Facebook data gaps", ""])
    if not gaps:
        lines.append("No measured Facebook data gap was recorded.")
    else:
        for gap in gaps:
            if not isinstance(gap, Mapping):
                continue
            lines.append(
                f"- **{gap.get('field')}** — {format_coverage(gap.get('coverage'))}. "
                f"{gap.get('limitation')} {gap.get('instrumentation_action')}"
            )
    return lines


def render_moneyball_markdown(report: Mapping[str, Any]) -> str:
    metadata = report.get("report_metadata", {})
    coverage = report.get("data_coverage", {})
    summary = report.get("account_summary", {})
    classifications = report.get("classifications", {})
    lines = [
        "# Reel Moneyball Analytics",
        "",
        "## A. Executive summary",
        "",
        f"- Report timestamp: **{metadata.get('generated_at_jst')} (JST)**",
        f"- Account: **{metadata.get('account')}**",
        f"- Reels analyzed: **{coverage.get('published_posts', 0)}**; latest insight "
        f"coverage is **{coverage.get('latest_snapshot_posts', 0)}/"
        f"{coverage.get('published_posts', 0)}**.",
    ]
    maturity = coverage.get("snapshot_maturity", {})
    lines.append(
        "- Fixed-window coverage: "
        + ", ".join(
            f"{window} **{format_coverage(maturity.get(window))}**"
            for window in WINDOW_ORDER
        )
        + "."
    )
    lines.append(
        f"- Current primary metric: **{summary.get('current_primary_metric')}**."
    )
    strongest_series = summary.get("strongest_repeatable_series")
    if isinstance(strongest_series, Mapping):
        lines.append(
            f"- Strongest repeatable series: **{strongest_series.get('series')}** — "
            f"{strongest_series.get('recommendation_reason')}"
        )
    else:
        lines.append(
            f"- Strongest repeatable series: **Unavailable**; "
            f"{len(classifications.get('workhorses', []))} series satisfy the full "
            "five-post follow-conversion and production-efficiency rule."
        )
    hidden = summary.get("strongest_hidden_gem")
    if isinstance(hidden, Mapping):
        lines.append(
            f"- Strongest Hidden Gem: **{hidden.get('media_id')}** — {hidden.get('reason')}"
        )
    else:
        lines.append(
            f"- Strongest Hidden Gem: **Unavailable**; "
            f"{len(classifications.get('hidden_gems', []))} posts meet the complete rule."
        )
    vanity = summary.get("largest_vanity_winner")
    if isinstance(vanity, Mapping):
        lines.append(
            f"- Largest Vanity Winner: **{vanity.get('media_id')}** — {vanity.get('reason')}"
        )
    else:
        lines.append(
            f"- Largest Vanity Winner: **Unavailable**; "
            f"{len(classifications.get('vanity_winners', []))} posts meet the complete rule."
        )
    efficient = summary.get("most_production_efficient_post")
    if isinstance(efficient, Mapping):
        lines.append(
            f"- Most production-efficient post: **{efficient.get('media_id')}**, "
            f"{format_rate(efficient.get('follows_per_production_hour'))} follows/hour "
            f"at {efficient.get('maturity_window')}."
        )
    else:
        production_entry = coverage.get("content_metadata", {}).get(
            "production_minutes", {}
        )
        lines.append(
            "- Most production-efficient post: **Unavailable**; production minutes "
            f"cover {format_coverage(production_entry)}."
        )
    gap = summary.get("most_important_data_gap")
    if isinstance(gap, Mapping):
        lines.append(
            f"- Most important data gap: **{gap.get('field')}**, coverage "
            f"**{format_coverage(gap.get('coverage'))}**. {gap.get('limitation')}"
        )
    next_ten = summary.get("next_ten_post_allocation", {})
    lines.append(
        f"- Next ten posts: **{next_ten.get('status')}**. {next_ten.get('reason')}"
    )

    scoreboard = summary
    medians = scoreboard.get("medians", {})
    displayed_reach_total = (
        scoreboard.get("total_reach")
        if scoreboard.get("total_reach") is not None
        else scoreboard.get("known_total_reach")
    )
    displayed_follow_total = (
        scoreboard.get("total_follows")
        if scoreboard.get("total_follows") is not None
        else scoreboard.get("known_total_follows")
    )
    displayed_production_total = (
        scoreboard.get("total_production_hours")
        if scoreboard.get("total_production_hours") is not None
        else scoreboard.get("known_production_hours")
    )
    lines.extend(
        [
            "",
            "## B. Moneyball scoreboard",
            "",
            "Latest observations are shown as medians across posts. Known totals are "
            "labeled when unsynced posts prevent a complete account total.",
            "",
            "| Metric | Median | Total / coverage |",
            "|---|---:|---:|",
            f"| Reach | {_format_number(medians.get('reach'))} | "
            f"{_format_number(displayed_reach_total)}"
            f"{' known' if displayed_reach_total is not None else ''}; "
            f"{format_coverage(scoreboard.get('reach_coverage'))} |",
            f"| Follows | {_format_number(medians.get('follows'))} | "
            f"{_format_number(displayed_follow_total)}"
            f"{' known' if displayed_follow_total is not None else ''}; "
            f"{format_coverage(scoreboard.get('follows_coverage'))} |",
            f"| Shares per 1,000 reach | {format_rate(medians.get('shares_per_1000_reach'))} | "
            f"reach only; {format_coverage(coverage.get('latest_derived_metrics', {}).get('shares_per_1000_reach'))} |",
            f"| Saves per 1,000 reach | {format_rate(medians.get('saves_per_1000_reach'))} | "
            f"reach only; {format_coverage(coverage.get('latest_derived_metrics', {}).get('saves_per_1000_reach'))} |",
            f"| Watch depth | {_format_percent(medians.get('watch_depth'))} | "
            f"{format_coverage(coverage.get('latest_derived_metrics', {}).get('watch_depth'))}; "
            "uncapped |",
            f"| Production hours | {format_rate(medians.get('production_hours'))} | "
            f"{_format_number(displayed_production_total)}"
            f"{' known' if displayed_production_total is not None else ''}; "
            f"{format_coverage(scoreboard.get('production_coverage'))} |",
            f"| Follows per production hour | {format_rate(medians.get('follows_per_production_hour'))} | requires both source fields |",
        ]
    )
    follow_groups = scoreboard.get("follow_conversion_by_denominator", {})
    for denominator in ("non_follower_reach", "reach"):
        group = follow_groups.get(denominator, {})
        lines.append(
            f"| Follow conversion / 1,000 {denominator} | "
            f"{format_rate(group.get('median'))} | n={group.get('n', 0)}; never mixed |"
        )

    growth = report.get("account_growth")
    growth = growth if isinstance(growth, Mapping) else {}
    stock = growth.get("follower_stock")
    stock = stock if isinstance(stock, Mapping) else {}
    growth_coverage = growth.get("coverage")
    growth_coverage = (
        growth_coverage if isinstance(growth_coverage, Mapping) else {}
    )
    lag = growth.get("lag")
    lag = lag if isinstance(lag, Mapping) else {}
    preliminary = growth.get("preliminary")
    preliminary = preliminary if isinstance(preliminary, Mapping) else {}
    follows_coverage = growth_coverage.get("follows")
    follows_coverage = (
        follows_coverage if isinstance(follows_coverage, Mapping) else {}
    )
    unfollows_coverage = growth_coverage.get("unfollows")
    unfollows_coverage = (
        unfollows_coverage if isinstance(unfollows_coverage, Mapping) else {}
    )
    gross_follows_value = growth.get("gross_follows")
    gross_follows_note = "complete included intervals only"
    if gross_follows_value is None and growth.get("known_gross_follows") is not None:
        gross_follows_value = growth.get("known_gross_follows")
        gross_follows_note = (
            f"partial known total; {format_coverage(follows_coverage)}; "
            "not a full-period total"
        )
    unfollows_value = growth.get("unfollows")
    unfollows_note = "complete included intervals only"
    if unfollows_value is None and growth.get("known_unfollows") is not None:
        unfollows_value = growth.get("known_unfollows")
        unfollows_note = (
            f"partial known total; {format_coverage(unfollows_coverage)}; "
            "not a full-period total"
        )
    net_growth_value = growth.get("net_growth")
    net_growth_note = "gross account follows − account unfollows"
    if net_growth_value is None and growth.get("known_net_growth") is not None:
        net_growth_value = growth.get("known_net_growth")
        net_growth_note = (
            f"partial known net; {format_coverage(follows_coverage)}; "
            "not a full-period total"
        )
    lines.extend(
        [
            "",
            "## B2. Account growth (account-wide)",
            "",
            f"Status: **{growth.get('status', 'UNAVAILABLE')}**. "
            f"{ACCOUNT_ATTRIBUTION_WARNING}",
            "",
            "| Metric | Value | Source / denominator |",
            "|---|---:|---|",
            f"| Current follower stock | {_format_number(stock.get('latest'))} | "
            "point-in-time `followers_count` |",
            f"| Follower stock change across stored snapshots | "
            f"{_format_number(stock.get('snapshot_change'))} | "
            f"{stock.get('snapshot_count', 0)} point-in-time snapshots; not a flow decomposition |",
            f"| Gross follows | {_format_number(gross_follows_value)} | "
            f"account daily flow; {gross_follows_note} |",
            f"| Unfollows | {_format_number(unfollows_value)} | "
            f"account daily flow; {unfollows_note} |",
            f"| Net growth | {_format_number(net_growth_value)} | "
            f"{net_growth_note} |",
            f"| Account reach | {_format_number(growth.get('account_reach'))} | "
            "account-wide reach over configured daily intervals; rates require "
            "matched flow coverage |",
            f"| Gross follows / 1,000 account reach | "
            f"{format_rate(growth.get('gross_follows_per_1000_account_reach'))} | "
            "account reach; never Reel reach |",
            f"| Net follows / 1,000 account reach | "
            f"{format_rate(growth.get('net_follows_per_1000_account_reach'))} | "
            "account reach; never Reel reach |",
            f"| Reel reach-days | {_format_number(growth.get('reel_reach'))} | "
            "daily REEL-filtered reach summed across covered days; not unique "
            "across the whole period |",
            f"| Non-follower Reel reach-days | "
            f"{_format_number(growth.get('reel_non_follower_reach'))} | "
            "daily REEL + NON_FOLLOWER breakdown; estimated reach buckets may "
            "not sum exactly |",
            f"| Gross follows / 1,000 Reel reach | "
            f"{format_rate(growth.get('gross_follows_per_1000_reel_reach'))} | "
            "same account-day interval; observational, not per-post attribution |",
            f"| Gross follows / 1,000 non-follower Reel reach | "
            f"{format_rate(growth.get('gross_follows_per_1000_reel_non_follower_reach'))} | "
            "same account-day interval; observational, not per-post attribution |",
            "",
            f"- Daily interval coverage: **{format_coverage(growth_coverage.get('daily_intervals'))}**; "
            f"follows **{format_coverage(growth_coverage.get('follows'))}**; "
            f"unfollows **{format_coverage(growth_coverage.get('unfollows'))}**; "
            f"account reach **{format_coverage(growth_coverage.get('account_reach'))}**; "
            f"Reel reach **{format_coverage(growth_coverage.get('reel_reach'))}**; "
            f"non-follower Reel reach "
            f"**{format_coverage(growth_coverage.get('reel_non_follower_reach'))}**.",
            f"- Lag: stock fetch **{format_rate(lag.get('follower_stock_fetch_lag_hours'), suffix='h')}**; "
            f"flow source **{format_rate(lag.get('flow_data_lag_hours'), suffix='h')}**; "
            f"flow fetch **{format_rate(lag.get('flow_fetch_lag_hours'), suffix='h')}**. "
            f"Stale: **{bool(lag.get('stale'))}**.",
            f"- Preliminary: **{bool(preliminary.get('status'))}**. "
            f"{preliminary.get('reason') or 'Unavailable.'}",
            "",
            "### Daily account flows",
            "",
        ]
    )
    daily_growth = growth.get("daily_intervals")
    daily_growth = daily_growth if isinstance(daily_growth, list) else []
    if not daily_growth:
        lines.append("No valid daily account flow intervals are stored.")
    else:
        lines.extend(
            [
                "| Interval (UTC) | Follows | Unfollows | Net | Account reach | Reel reach | Non-follower Reel reach | Gross/1k Reel reach | Gross/1k non-follower Reel reach | Status | Published context |",
                "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|",
            ]
        )
        for row in daily_growth:
            if not isinstance(row, Mapping):
                continue
            context = row.get("publication_context")
            context = context if isinstance(context, Mapping) else {}
            context_ids = context.get("media_ids")
            context_ids = context_ids if isinstance(context_ids, list) else []
            interval_status = (
                "excluded"
                if not row.get("included_in_aggregate")
                else "preliminary"
                if row.get("preliminary")
                else "finalized"
            )
            lines.append(
                f"| {markdown_cell(row.get('observed_since'))} → "
                f"{markdown_cell(row.get('observed_until'))} | "
                f"{_format_number(row.get('follows'))} | "
                f"{_format_number(row.get('unfollows'))} | "
                f"{_format_number(row.get('net_growth'))} | "
                f"{_format_number(row.get('account_reach'))} | "
                f"{_format_number(row.get('reel_reach'))} | "
                f"{_format_number(row.get('reel_non_follower_reach'))} | "
                f"{format_rate(row.get('gross_follows_per_1000_reel_reach'))} | "
                f"{format_rate(row.get('gross_follows_per_1000_reel_non_follower_reach'))} | "
                f"{interval_status} | "
                f"{context.get('published_post_count', 0)} post(s): "
                f"{markdown_cell(', '.join(str(value) for value in context_ids) or 'none')} "
                "(context only) |"
            )
    lines.extend(["", "### Follower stock snapshots", ""])
    stock_rows = growth.get("stock_snapshots")
    stock_rows = stock_rows if isinstance(stock_rows, list) else []
    if not stock_rows:
        lines.append("No account follower-stock snapshots are stored.")
    else:
        lines.extend(
            [
                "| Fetched at (UTC) | Followers | Media count |",
                "|---|---:|---:|",
            ]
        )
        for row in stock_rows:
            if not isinstance(row, Mapping):
                continue
            lines.append(
                f"| {markdown_cell(row.get('fetched_at'))} | "
                f"{_format_number(row.get('followers_count'))} | "
                f"{_format_number(row.get('media_count'))} |"
            )

    platforms = report.get("platform_analytics")
    platforms = platforms if isinstance(platforms, Mapping) else {}
    facebook = platforms.get("facebook")
    if isinstance(facebook, Mapping):
        lines.extend(facebook_markdown_lines(facebook))

    lines.extend(["", "## C. Undervalued content", ""])
    hidden_gems = classifications.get("hidden_gems", [])
    if not hidden_gems:
        lines.append(
            "No Hidden Gem is assignable under the full rule. This is not evidence that "
            "none exists; it reflects follow-conversion and/or production-time coverage."
        )
    else:
        lines.extend(
            [
                "| Post | Series | Window | Reach | Follow conversion | Follows/hour | Percentile | Reason |",
                "|---|---|---:|---:|---:|---:|---:|---|",
            ]
        )
        posts_by_hash = {
            str(post.get("identity", {}).get("content_hash") or ""): post
            for post in report.get("posts", [])
        }
        for item in hidden_gems:
            post = posts_by_hash.get(str(item.get("content_hash") or ""), {})
            support = item.get("supporting_metrics", {})
            percentile_value = item.get("comparison_percentiles", {}).get(
                "follow_conversion"
            )
            lines.append(
                f"| {markdown_cell(item.get('media_id'))} | "
                f"{markdown_cell(post.get('content_metadata', {}).get('series') or 'Unassigned')} | "
                f"{item.get('maturity_window')} | {_format_number(support.get('reach'))} | "
                f"{format_rate(support.get('follow_conversion'))} "
                f"{markdown_cell(support.get('follow_conversion_denominator_type') or '')} | "
                f"{format_rate(support.get('follows_per_production_hour'))} | "
                f"{format_rate(percentile_value, suffix='th')} | "
                f"{markdown_cell(item.get('reason'))} |"
            )

    lines.extend(["", "## D. Overvalued content", ""])
    vanity_winners = classifications.get("vanity_winners", [])
    if not vanity_winners:
        lines.append(
            "No Vanity Winner is assignable under the full rule. High reach alone is "
            "never enough; a label also requires measured weak follow conversion and intent."
        )
    else:
        lines.extend(
            [
                "| Post | Window | Reach | Follow conversion | Intent / 1k reach | Reason |",
                "|---|---:|---:|---:|---:|---|",
            ]
        )
        for item in vanity_winners:
            support = item.get("supporting_metrics", {})
            lines.append(
                f"| {markdown_cell(item.get('media_id'))} | {item.get('maturity_window')} | "
                f"{_format_number(support.get('reach'))} | "
                f"{format_rate(support.get('follow_conversion'))} "
                f"{markdown_cell(support.get('follow_conversion_denominator_type') or '')} | "
                f"{format_rate(support.get('intent_actions_per_1000_reach'))} | "
                f"{markdown_cell(item.get('reason'))} |"
            )

    lines.extend(["", "## E. Content roster", ""])
    series_rows = report.get("series", [])
    if not series_rows:
        series_coverage = coverage.get("content_metadata", {}).get("series", {})
        lines.append(
            "No recurring-series table can be evaluated: series metadata covers "
            f"**{format_coverage(series_coverage)}**."
        )
    else:
        lines.extend(
            [
                "| Series | n | Hours | Reach | Follows | Follow conversion | Follows/hour | Watch depth | Shares/1k | Saves/1k | Last-five consistency | Hidden | Vanity | Strongest goal | Weakest stage | Recommendation | Reason |",
                "|---|---:|---:|---:|---:|---|---:|---:|---:|---:|---|---:|---:|---|---|---|---|",
            ]
        )
        for row in series_rows:
            conversion = row.get("median_follow_conversion", {})
            conversion_parts = [
                f"{denominator}: {format_rate(values.get('median'))} (n={values.get('n', 0)})"
                for denominator, values in conversion.items()
                if isinstance(values, Mapping) and values.get("n")
            ]
            consistency = row.get("consistency_last_five", {})
            lines.append(
                f"| {markdown_cell(row.get('series'))} | {row.get('post_count')} | "
                f"{format_rate(row.get('total_production_hours'))} | "
                f"{_format_number(row.get('total_reach'))} | "
                f"{_format_number(row.get('total_follows'))} | "
                f"{markdown_cell('; '.join(conversion_parts) or 'Unavailable')} | "
                f"{format_rate(row.get('median_follows_per_production_hour'))} | "
                f"{_format_percent(row.get('median_watch_depth'))} | "
                f"{format_rate(row.get('median_shares_per_1000_reach'))} | "
                f"{format_rate(row.get('median_saves_per_1000_reach'))} | "
                f"{consistency.get('above_both_primary_medians', 0)}/"
                f"{consistency.get('posts_considered', 0)} above both | "
                f"{row.get('hidden_gems', 0)} | {row.get('vanity_winners', 0)} | "
                f"{markdown_cell(row.get('strongest_content_goal') or 'Unavailable')} | "
                f"{markdown_cell(row.get('weakest_funnel_stage') or 'Unavailable')} | "
                f"**{row.get('recommendation')}** | "
                f"{markdown_cell(row.get('recommendation_reason'))} |"
            )

    funnel = report.get("funnel_diagnostics", {})
    lines.extend(
        [
            "",
            "## F. Funnel diagnostics",
            "",
            "Stage status is relative to the same fixed-window comparison cohort. "
            "`UNKNOWN` means the metric is unavailable, not failure.",
            "",
            "| Stage | Succeeded | At baseline | Weak | Unknown |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for stage, counts in funnel.get("stage_counts", {}).items():
        lines.append(
            f"| {stage} | {counts.get('SUCCEEDED', 0)} | "
            f"{counts.get('AT_BASELINE', 0)} | {counts.get('WEAK', 0)} | "
            f"{counts.get('UNKNOWN', 0)} |"
        )
    lines.extend(["", "Diagnostic hypotheses:"])
    if not funnel.get("diagnostic_counts"):
        lines.append("")
        lines.append("- None assignable with current comparable metrics.")
    else:
        lines.append("")
        for name, count in funnel.get("diagnostic_counts", {}).items():
            lines.append(f"- {name}: **{count}** posts.")

    lines.extend(["", "## G. Fixed-window performance", ""])
    posts = report.get("posts", [])
    for window in WINDOW_ORDER:
        rows: list[tuple[float, Mapping[str, Any], Mapping[str, Any]]] = []
        for post in posts:
            observation = post.get("maturity_windows", {}).get(window)
            if not isinstance(observation, Mapping):
                continue
            reach = numeric(metric_value(observation, "reach"))
            rows.append((float(reach) if reach is not None else -math.inf, post, observation))
        rows.sort(
            key=lambda item: (
                -item[0],
                str(item[1].get("identity", {}).get("media_id") or ""),
            )
        )
        lines.extend(
            [
                f"### {window}",
                "",
                f"Valid observations: **{len(rows)}/{coverage.get('published_posts', 0)}**. "
                "This table is ranked only within this maturity window.",
                "",
            ]
        )
        if not rows:
            lines.append("No valid observations in the configured window.")
            lines.append("")
            continue
        lines.extend(
            [
                "| Post | Actual age | Reach | Follow conversion | Watch depth | Shares/1k reach | Saves/1k reach | Follows/hour |",
                "|---|---:|---:|---|---:|---:|---:|---:|",
            ]
        )
        for _, post, observation in rows:
            identity = post.get("identity", {})
            lines.append(
                f"| {post_label(identity)} | "
                f"{format_rate(observation.get('actual_age_hours'), suffix='h')} | "
                f"{_format_number(metric_value(observation, 'reach'))} | "
                f"{_follow_conversion_display(observation)} | "
                f"{_format_percent(metric_value(observation, 'watch_depth'))} | "
                f"{format_rate(metric_value(observation, 'shares_per_1000_reach'))} | "
                f"{format_rate(metric_value(observation, 'saves_per_1000_reach'))} | "
                f"{format_rate(metric_value(observation, 'follows_per_production_hour'))} |"
            )
        lines.append("")

    experiments = report.get("experiments", [])
    lines.extend(["## H. Experiments", ""])
    for comparison_type, heading in (
        ("controlled_one_variable_comparison", "Valid one-variable tests"),
        ("uncontrolled_comparison", "Uncontrolled comparisons"),
    ):
        rows = [
            row
            for row in experiments
            if row.get("comparison_type") == comparison_type
        ]
        lines.extend([f"### {heading}", ""])
        if not rows:
            lines.extend(["None.", ""])
            continue
        lines.extend(
            [
                "| Experiment | Changed variable | Window | Metric | Control | Variant | Difference | Lift | n | Result | Warning |",
                "|---|---|---:|---|---:|---:|---:|---:|---:|---|---|",
            ]
        )
        for row in rows:
            lines.append(
                f"| {markdown_cell(row.get('experiment_id'))} | "
                f"{markdown_cell(row.get('changed_variable') or row.get('declared_changed_variables'))} | "
                f"{row.get('maturity_window') or 'Unavailable'} | "
                f"{markdown_cell(row.get('metric') or 'Unavailable')} | "
                f"{format_rate(row.get('control_metric'))} | "
                f"{format_rate(row.get('variant_metric'))} | "
                f"{format_rate(row.get('absolute_difference'))} | "
                f"{_format_percent(row.get('percentage_lift'))} | "
                f"{row.get('sample_size')} | {row.get('result')} | "
                f"{markdown_cell(row.get('uncertainty_warning'))} |"
            )
        lines.append("")

    lines.extend(["## I. Next ten-post recommendations", ""])
    if next_ten.get("status") != "AVAILABLE":
        lines.append(f"**INSUFFICIENT DATA.** {next_ten.get('reason')}")
    else:
        lines.append("| Portfolio bucket | Posts | Evidence |")
        lines.append("|---|---:|---|")
        for row in next_ten.get("allocation", []):
            lines.append(
                f"| {markdown_cell(row.get('bucket'))} | {row.get('posts')} | "
                f"{markdown_cell(row.get('support'))} |"
            )

    lines.extend(
        [
            "",
            "## J. Data gaps and instrumentation",
            "",
            "| Field | Coverage | What it blocks | What to record |",
            "|---|---:|---|---|",
        ]
    )
    for gap in report.get("data_gaps", []):
        lines.append(
            f"| {markdown_cell(gap.get('field'))} | "
            f"{format_coverage(gap.get('coverage'))} | "
            f"{markdown_cell(gap.get('limitation'))} | "
            f"{markdown_cell(gap.get('instrumentation_action'))} |"
        )
    inferred = coverage.get("inferred_metadata", [])
    if inferred:
        lines.extend(["", "Inferred metadata used (visually flagged):", ""])
        for row in inferred:
            lines.append(
                f"- **INFERRED ({markdown_cell(row.get('confidence') or 'unknown confidence')})** "
                f"`{markdown_cell(row.get('media_id'))}` — "
                f"{markdown_cell(', '.join(row.get('fields', [])))}."
            )
    lines.extend(
        [
            "",
            "### Interpretation boundary",
            "",
            "Current data supports age-matched distribution, watch-depth, skip/watch-time, "
            "and save/share-intent diagnostics where coverage exists. Account follower "
            "stock and daily follows/unfollows describe account-wide growth where their "
            "coverage is complete; they do not attribute growth to any Reel or series. "
            "The report does not support claims about post-level follower acquisition, "
            "returning-viewer retention, production efficiency, or proven recurring "
            "winners until the named post-attributed fields meet the configured rules.",
            "",
        ]
    )
    return "\n".join(lines)


def render_data_audit_markdown(report: Mapping[str, Any]) -> str:
    metadata = report.get("report_metadata", {})
    coverage = report.get("data_coverage", {})
    audit = report.get("data_audit", {})
    lines = [
        "# Moneyball data audit",
        "",
        f"- Account: **{metadata.get('account')}**",
        f"- Generated: **{metadata.get('generated_at_jst')} JST**",
        f"- Published Reels: **{coverage.get('published_posts', 0)}**",
        f"- Stored attached observations: **{coverage.get('stored_snapshot_rows', 0)}**; "
        f"deduplicated read view: **{coverage.get('deduplicated_snapshot_rows', 0)}**; "
        f"collapsed exact legacy duplicates: **{coverage.get('collapsed_exact_duplicate_rows', 0)}**.",
        "",
        "## Metric inventory",
        "",
        "| Canonical field | Exact API/source field | Semantics | Unit conversion | Coverage | Period | Availability |",
        "|---|---|---|---|---:|---|---|",
    ]
    for row in audit.get("metric_catalog", []):
        conversion = (
            f"{row.get('source_unit')} → {row.get('canonical_unit')}"
            if row.get("source_unit")
            else row.get("canonical_unit")
        )
        lines.append(
            f"| {markdown_cell(row.get('canonical_field'))} | "
            f"{markdown_cell(row.get('source_api_field') or 'Unavailable')} | "
            f"{markdown_cell(row.get('semantics'))} | "
            f"{markdown_cell(conversion)} | {format_coverage(row.get('coverage'))} | "
            f"{markdown_cell(', '.join(row.get('periods', [])) or 'Unavailable')} | "
            f"{row.get('availability')} |"
        )

    account_audit = audit.get("account_growth")
    account_audit = account_audit if isinstance(account_audit, Mapping) else {}
    account_coverage = account_audit.get("coverage")
    account_coverage = (
        account_coverage if isinstance(account_coverage, Mapping) else {}
    )
    stock_coverage = account_coverage.get("stock_snapshots")
    stock_coverage = (
        stock_coverage if isinstance(stock_coverage, Mapping) else {}
    )
    lines.extend(
        [
            "",
            "## Account-growth source coverage",
            "",
            f"- Follower-stock table available: "
            f"**{bool(stock_coverage.get('table_available'))}**; stored snapshots: "
            f"**{int(stock_coverage.get('count') or 0)}**.",
            f"- Daily flow coverage: "
            f"**{format_coverage(account_coverage.get('daily_intervals'))}**.",
            f"- Gross follows coverage: "
            f"**{format_coverage(account_coverage.get('follows'))}**.",
            f"- Unfollows coverage: "
            f"**{format_coverage(account_coverage.get('unfollows'))}**.",
            f"- Same-interval account reach coverage: "
            f"**{format_coverage(account_coverage.get('account_reach'))}**.",
            f"- Same-interval REEL-filtered reach coverage: "
            f"**{format_coverage(account_coverage.get('reel_reach'))}**.",
            f"- Same-interval non-follower Reel reach coverage: "
            f"**{format_coverage(account_coverage.get('reel_non_follower_reach'))}**.",
            f"- REEL content-breakdown fetch coverage: "
            f"**{format_coverage(account_coverage.get('reel_content_breakdown_fetch'))}**.",
            f"- REEL audience-breakdown fetch coverage: "
            f"**{format_coverage(account_coverage.get('reel_audience_breakdown_fetch'))}**.",
            f"- Preliminary status: "
            f"**{bool(account_audit.get('preliminary', {}).get('status'))}**; "
            f"lag details are `{json.dumps(account_audit.get('lag', {}), ensure_ascii=False, sort_keys=True)}`.",
            f"- Attribution boundary: {account_audit.get('attribution_warning') or ACCOUNT_ATTRIBUTION_WARNING}",
            "",
            "Account rates use either account-wide or explicitly REEL-filtered reach "
            "over the same daily intervals, with the denominator named. REEL reach "
            "includes all Reels viewed that day, not only newly published Reels. "
            "A successful fetch can still have a null REEL value when Meta returns "
            "no REEL category for that interval; null is not rewritten as zero. "
            "Account follower movement is never copied into a post or series.",
            "",
        ]
    )

    lines.extend(
        [
            "",
            "## Snapshot semantics",
            "",
            str(audit.get("snapshot_semantics")),
            "",
            "A fixed window is available only when a stored observation is at or after "
            "the target and within its configured tolerance. Lifetime totals from a later "
            "fetch are never copied backward, interpolated, or reverse-engineered.",
            "",
            "| Window | Coverage |",
            "|---|---:|",
        ]
    )
    for window, entry in coverage.get("snapshot_maturity", {}).items():
        lines.append(f"| {window} | {format_coverage(entry)} |")

    lines.extend(
        [
            "",
            "## API row consistency and provenance",
            "",
            "Older and recent rows are allowed to have different metric sets. The table "
            "below shows when each exact raw field first and last appeared; missing older "
            "fields remain unavailable.",
            "",
            "| Raw field | Rows | First capture | Last capture | Periods |",
            "|---|---:|---|---|---|",
        ]
    )
    for name, row in audit.get("source_metric_fields", {}).items():
        lines.append(
            f"| {markdown_cell(name)} | {row.get('count', 0)} | "
            f"{markdown_cell(row.get('first_captured_at') or 'Unavailable')} | "
            f"{markdown_cell(row.get('last_captured_at') or 'Unavailable')} | "
            f"{markdown_cell(', '.join(row.get('periods', [])) or 'Unavailable')} |"
        )
    conflicts = audit.get("raw_column_conflicts", {})
    lines.extend(["", "Raw-payload versus normalized-column conflicts:"])
    lines.append("")
    if conflicts:
        for name, count in conflicts.items():
            lines.append(
                f"- `{name}`: **{count}** stored rows. The raw API value takes precedence."
            )
    else:
        lines.append("- None detected for fields that exist in both places.")

    lines.extend(
        [
            "",
            "## Ambiguous and unavailable fields",
            "",
            f"- Shares/sends: {audit.get('shares_and_sends')}",
            f"- Interactions: {audit.get('interactions')}",
            "- `views` is not relabeled as a verified `plays` denominator. Average-watch "
            "fallback therefore remains unavailable unless a verified plays source is added.",
            "- `total_views`, `facebook_views`, and `crossposted_views` may overlap with "
            "Instagram views and are never added together.",
            "- Post-attributed profile visits, follower/non-follower reach, returning "
            "viewers, DM keyword hits, and measured production cost/time are unavailable "
            "unless their coverage table says otherwise.",
            "",
            "## Desired metadata coverage",
            "",
            "| Field | Coverage |",
            "|---|---:|",
        ]
    )
    for field, entry in coverage.get("content_metadata", {}).items():
        lines.append(f"| {markdown_cell(field)} | {format_coverage(entry)} |")
    inferred = coverage.get("inferred_metadata", [])
    lines.extend(["", "### Inferred metadata markers", ""])
    if inferred:
        for row in inferred:
            lines.append(
                f"- **INFERRED ({markdown_cell(row.get('confidence') or 'unknown confidence')})** "
                f"media `{markdown_cell(row.get('media_id'))}`: "
                f"{markdown_cell(', '.join(row.get('fields', [])))}."
            )
    else:
        lines.append("- No inferred metadata is used in this report.")

    platforms = report.get("platform_analytics")
    platforms = platforms if isinstance(platforms, Mapping) else {}
    facebook = platforms.get("facebook")
    facebook = facebook if isinstance(facebook, Mapping) else {}
    if facebook:
        facebook_coverage = facebook.get("data_coverage")
        facebook_coverage = (
            facebook_coverage
            if isinstance(facebook_coverage, Mapping)
            else {}
        )
        lines.extend(
            [
                "",
                "## Facebook-native source coverage",
                "",
                f"- Status: **{facebook.get('status', 'UNAVAILABLE')}**.",
                f"- Published Facebook Reels: **{facebook_coverage.get('published_posts', 0)}**; "
                f"latest snapshots: **{facebook_coverage.get('latest_snapshot_posts', 0)}**.",
                "- Facebook media IDs, publish times, snapshots, and denominators "
                "come from the independent Facebook ledger and are never merged "
                "with Instagram.",
                "",
                "| Raw Facebook source field | Rows | Coverage | First capture | Last capture |",
                "|---|---:|---:|---|---|",
            ]
        )
        for name, raw_entry in facebook_coverage.get("source_fields", {}).items():
            if not isinstance(raw_entry, Mapping):
                continue
            lines.append(
                f"| {markdown_cell(name)} | {raw_entry.get('count', 0)} | "
                f"{format_coverage(raw_entry.get('coverage'))} | "
                f"{markdown_cell(raw_entry.get('first_captured_at') or 'Unavailable')} | "
                f"{markdown_cell(raw_entry.get('last_captured_at') or 'Unavailable')} |"
            )
        lines.extend(
            [
                "",
                "The documented v25 `/video_insights` Reel metrics require "
                "`read_insights` plus Page permissions. Direct Video "
                "`views`/`likes`/`comments` and associated Page-post `shares.count` "
                "remain the fallback; omission remains null. Facebook does not "
                "document saves, per-Reel profile visits, unique non-follower reach, "
                "returning viewers, or an Instagram-style direct 3-second skip rate.",
            ]
        )
    lines.extend(
        [
            "",
            "Manual annotations are read from `data/reel_annotations.json` and are never "
            "rewritten by report generation. Per-field provenance distinguishes manual, "
            "generation-pipeline, ledger, and inferred values.",
            "",
        ]
    )
    return "\n".join(lines)


CSV_COLUMNS = (
    "platform",
    "account",
    "media_id",
    "content_hash",
    "permalink",
    "paired_instagram_media_id",
    "paired_instagram_permalink",
    "published_at",
    "maturity_window",
    "target_age_hours",
    "actual_age_hours",
    "captured_at",
    "series",
    "content_goal",
    "topic",
    "source",
    "hook_style",
    "hook_text",
    "format",
    "duration_bucket",
    "posting_window",
    "trial_reel",
    "experiment_id",
    "experiment_variant",
    "production_minutes",
    "direct_cost_jpy",
    *CANONICAL_RAW_METRICS,
    "watch_depth",
    "total_watch_hours",
    "interactions_per_1000_reach",
    "interactions_per_1000_views",
    "interactions_per_1000_unique_media_viewers",
    "engagement_rate_by_reach",
    "views_per_reached_account",
    "shares_per_1000_reach",
    "shares_per_1000_views",
    "shares_per_1000_unique_media_viewers",
    "reposts_per_1000_reach",
    "reposts_per_1000_views",
    "sends_per_1000_reach",
    "sends_per_1000_views",
    "saves_per_1000_reach",
    "saves_per_1000_views",
    "saves_per_1000_unique_media_viewers",
    "comments_per_1000_reach",
    "comments_per_1000_views",
    "comments_per_1000_unique_media_viewers",
    "likes_per_1000_reach",
    "likes_per_1000_views",
    "likes_per_1000_unique_media_viewers",
    "reactions_per_1000_reach",
    "reactions_per_1000_views",
    "reactions_per_1000_unique_media_viewers",
    "intent_actions_per_1000_reach",
    "intent_actions_per_1000_views",
    "satisfaction_denominator_type",
    "follow_conversion",
    "follow_conversion_denominator_type",
    "follows_per_1000_unique_media_viewers",
    "profile_visit_to_follow_rate",
    "follows_per_production_hour",
    "shares_per_production_hour",
    "saves_per_production_hour",
    "watch_hours_per_production_hour",
    "reach_per_production_hour",
    "views_per_production_hour",
    "follows_per_1000_jpy",
    "classifications",
    "funnel_diagnostics",
)


def render_moneyball_csv(report: Mapping[str, Any]) -> str:
    handle = io.StringIO(newline="")
    writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for post in sorted(
        report.get("posts", []),
        key=lambda row: (
            str(row.get("identity", {}).get("published_at") or ""),
            str(row.get("identity", {}).get("media_id") or ""),
        ),
    ):
        identity = post.get("identity", {})
        paired = identity.get("paired_instagram")
        paired = paired if isinstance(paired, Mapping) else {}
        metadata = post.get("content_metadata", {})
        labels = ";".join(
            sorted(
                str(item.get("label"))
                for item in post.get("classifications", [])
                if isinstance(item, Mapping)
            )
        )
        diagnostics = ";".join(
            sorted(
                str(item.get("diagnostic"))
                for item in post.get("funnel_diagnostics", [])
                if isinstance(item, Mapping)
            )
        )
        for window in (*WINDOW_ORDER, "latest"):
            observation = post.get("maturity_windows", {}).get(window)
            if not isinstance(observation, Mapping):
                continue
            raw = observation.get("raw_metrics", {})
            derived = observation.get("derived_metrics", {})
            follow = derived.get("follow_conversion", {})
            satisfaction = derived.get("satisfaction_rate", {})
            row = {
                **identity,
                "paired_instagram_media_id": paired.get("media_id"),
                "paired_instagram_permalink": paired.get("permalink"),
                **metadata,
                **raw,
                **derived,
                "maturity_window": window,
                "target_age_hours": observation.get("target_age_hours"),
                "actual_age_hours": observation.get("actual_age_hours"),
                "captured_at": observation.get("captured_at"),
                "satisfaction_denominator_type": satisfaction.get("denominator_type"),
                "follow_conversion": follow.get("value"),
                "follow_conversion_denominator_type": follow.get("denominator_type"),
                "classifications": labels,
                "funnel_diagnostics": diagnostics,
            }
            writer.writerow(
                {
                    key: (
                        json.dumps(value, ensure_ascii=False, sort_keys=True)
                        if isinstance(value, (dict, list))
                        else value
                    )
                    for key, value in row.items()
                }
            )
    return handle.getvalue()


def render_facebook_moneyball_csv(report: Mapping[str, Any]) -> str:
    """Render the independent Facebook lane with the same flat schema."""
    platforms = report.get("platform_analytics")
    platforms = platforms if isinstance(platforms, Mapping) else {}
    facebook = platforms.get("facebook")
    facebook = facebook if isinstance(facebook, Mapping) else {}
    return render_moneyball_csv({"posts": facebook.get("posts", [])})


def _html_text(value: Any) -> str:
    return html.escape(str(value if value is not None else "Unavailable"), quote=True)


def _html_metric(value: Any, *, decimals: int = 0) -> str:
    number = numeric(value)
    if number is None:
        return "Unavailable"
    return f"{float(number):,.{decimals}f}"


def _html_percent(value: Any, *, value_is_ratio: bool = True) -> str:
    number = numeric(value)
    if number is None:
        return "Unavailable"
    percentage = float(number) * 100.0 if value_is_ratio else float(number)
    return f"{percentage:,.1f}%"


def _html_duration(value: Any) -> str:
    seconds = numeric(value)
    if seconds is None:
        return "Unavailable"
    seconds = float(seconds)
    if seconds >= 3600:
        return f"{seconds / 3600:,.2f}h"
    if seconds >= 60:
        return f"{seconds / 60:,.1f}m"
    return f"{seconds:,.1f}s"


def _html_href(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text.startswith(("https://", "http://")):
        return None
    return html.escape(text, quote=True)


def _html_count_rate(
    count: Any,
    rate_per_1000: Any,
    *,
    unavailable_note: str | None = None,
) -> str:
    count_text = _html_metric(count)
    rate_text = _html_metric(rate_per_1000, decimals=2)
    note = (
        f"{rate_text} /1k reach"
        if numeric(rate_per_1000) is not None
        else unavailable_note or "rate unavailable"
    )
    return (
        f'<span class="metric-main">{count_text}</span>'
        f"<small>{_html_text(note)}</small>"
    )


def _sortable_cell(
    content: str,
    sort_value: Any,
    *,
    sort_type: str,
    css_class: str | None = None,
) -> str:
    """Render visible cell content with a separate, unformatted sort key."""
    if sort_type in {"number", "date"}:
        normalized: Any = numeric(sort_value)
    else:
        text = str(sort_value or "").strip()
        normalized = text if text else None
    missing = normalized is None
    value = "" if missing else str(normalized)
    missing_flag = "1" if missing else "0"
    class_attribute = (
        f' class="{html.escape(css_class, quote=True)}"' if css_class else ""
    )
    return (
        f"<td{class_attribute} data-sort-value=\"{html.escape(value, quote=True)}\" "
        f'data-sort-missing="{missing_flag}">{content}</td>'
    )


def _sortable_header(
    label: str,
    *,
    sort_type: str,
    css_class: str | None = None,
    initial_direction: str | None = None,
) -> str:
    direction = (
        initial_direction
        if initial_direction in {"ascending", "descending"}
        else "none"
    )
    indicator = "▲" if direction == "ascending" else "▼" if direction == "descending" else "↕"
    classes = "sortable-header"
    if css_class:
        classes += f" {css_class}"
    escaped_label = _html_text(label)
    return (
        f'<th scope="col" class="{html.escape(classes, quote=True)}" '
        f'data-sort-type="{html.escape(sort_type, quote=True)}" '
        f'aria-sort="{direction}"><button type="button" class="sort-button" '
        f'aria-label="Sort by {escaped_label}" title="Sort ascending or descending">'
        f'<span>{escaped_label}</span><span class="sort-indicator" aria-hidden="true">'
        f"{indicator}</span></button></th>"
    )


SORTABLE_TABLE_SCRIPT = """<script id="moneyball-table-sorter">
(() => {
  const collator = new Intl.Collator("ja", {numeric: true, sensitivity: "base"});
  const tables = document.querySelectorAll('table[data-sortable="true"]');
  tables.forEach((table) => {
    const body = table.tBodies[0];
    const headers = Array.from(table.tHead?.rows[0]?.cells || []);
    if (!body || !headers.length) return;
    headers.forEach((header, columnIndex) => {
      const button = header.querySelector(".sort-button");
      if (!button) return;
      button.addEventListener("click", () => {
        const current = header.getAttribute("aria-sort") || "none";
        const direction = current === "ascending" ? "descending" : "ascending";
        headers.forEach((candidate) => {
          candidate.setAttribute("aria-sort", "none");
          const indicator = candidate.querySelector(".sort-indicator");
          if (indicator) indicator.textContent = "↕";
        });
        header.setAttribute("aria-sort", direction);
        const activeIndicator = header.querySelector(".sort-indicator");
        if (activeIndicator) {
          activeIndicator.textContent = direction === "ascending" ? "▲" : "▼";
        }
        const type = header.dataset.sortType || "text";
        const factor = direction === "ascending" ? 1 : -1;
        const rows = Array.from(body.rows).map((row, order) => {
          const cell = row.cells[columnIndex];
          const missing = !cell || cell.dataset.sortMissing === "1";
          const raw = cell?.dataset.sortValue || "";
          const value = type === "number" || type === "date" ? Number(raw) : raw;
          const stableIndex = Number(row.dataset.rowIndex ?? order);
          return {row, stableIndex, missing, value};
        });
        rows.sort((left, right) => {
          if (left.missing !== right.missing) return left.missing ? 1 : -1;
          if (left.missing && right.missing) return left.stableIndex - right.stableIndex;
          let comparison = 0;
          if (type === "number" || type === "date") {
            comparison = left.value < right.value ? -1 : left.value > right.value ? 1 : 0;
          } else {
            comparison = collator.compare(String(left.value), String(right.value));
          }
          return comparison === 0
            ? left.stableIndex - right.stableIndex
            : comparison * factor;
        });
        rows.forEach(({row}) => body.append(row));
        const label = button.querySelector("span")?.textContent?.trim() || "column";
        const status = table.parentElement?.querySelector(".sort-status");
        if (status) {
          status.textContent = `Sorted ${label} ${direction}; unavailable values last.`;
        }
      });
    });
  });
})();
</script>"""


def _latest_post_observation(post: Mapping[str, Any]) -> Mapping[str, Any]:
    windows = post.get("maturity_windows")
    windows = windows if isinstance(windows, Mapping) else {}
    latest = windows.get("latest")
    return latest if isinstance(latest, Mapping) else {}


def _svg_empty(*, chart_id: str, title: str, message: str) -> str:
    return (
        f'<svg id="{_html_text(chart_id)}" class="chart" viewBox="0 0 760 180" '
        f'role="img" aria-label="{_html_text(title)}">'
        f'<title>{_html_text(title)}</title>'
        '<rect width="760" height="180" rx="18" fill="#101c25"/>'
        f'<text x="380" y="84" text-anchor="middle" class="svg-title">'
        f"{_html_text(title)}</text>"
        f'<text x="380" y="112" text-anchor="middle" class="svg-muted">'
        f"{_html_text(message)}</text></svg>"
    )


def _daily_flow_svg(rows: Sequence[Mapping[str, Any]]) -> str:
    values = [
        row
        for row in sorted(
            rows,
            key=lambda item: (
                str(item.get("observed_since") or ""),
                str(item.get("observed_until") or ""),
            ),
        )
        if numeric(row.get("follows")) is not None
        or numeric(row.get("unfollows")) is not None
    ]
    if not values:
        return _svg_empty(
            chart_id="daily-flow-chart",
            title="Daily follows and unfollows",
            message="No valid daily account flow intervals",
        )
    width, height = 760.0, 280.0
    left, right, top, bottom = 48.0, 18.0, 36.0, 56.0
    inner_width = width - left - right
    inner_height = height - top - bottom
    maximum = max(
        1.0,
        max(
            float(numeric(row.get(field)) or 0)
            for row in values
            for field in ("follows", "unfollows")
        ),
    )
    group_width = inner_width / len(values)
    bar_width = min(18.0, max(3.0, group_width * 0.28))
    label_every = max(1, math.ceil(len(values) / 8))
    parts = [
        '<svg id="daily-flow-chart" class="chart" viewBox="0 0 760 280" '
        'role="img" aria-label="Daily account follows and unfollows">',
        "<title>Daily account follows and unfollows</title>",
        '<rect width="760" height="280" rx="18" fill="#101c25"/>',
        '<line x1="48" y1="224" x2="742" y2="224" stroke="#35505f"/>',
        '<text x="48" y="22" class="svg-title">Daily account flows</text>',
        '<circle cx="574" cy="18" r="5" fill="#2dd4bf"/>',
        '<text x="585" y="22" class="svg-legend">follows</text>',
        '<circle cx="662" cy="18" r="5" fill="#fb923c"/>',
        '<text x="673" y="22" class="svg-legend">unfollows</text>',
    ]
    for index, row in enumerate(values):
        center = left + group_width * (index + 0.5)
        follows = float(numeric(row.get("follows")) or 0)
        unfollows = float(numeric(row.get("unfollows")) or 0)
        follow_height = inner_height * follows / maximum
        unfollow_height = inner_height * unfollows / maximum
        opacity = "0.55" if row.get("preliminary") else "1"
        context = row.get("publication_context")
        context = context if isinstance(context, Mapping) else {}
        tooltip = (
            f"{str(row.get('observed_since') or '')[:10]}: "
            f"{_html_metric(follows)} follows, {_html_metric(unfollows)} unfollows; "
            f"{int(context.get('published_post_count') or 0)} posts published "
            "(time context only)"
        )
        parts.extend(
            [
                f'<g opacity="{opacity}"><title>{_html_text(tooltip)}</title>',
                f'<rect x="{center - bar_width - 1:.2f}" '
                f'y="{top + inner_height - follow_height:.2f}" '
                f'width="{bar_width:.2f}" height="{follow_height:.2f}" '
                'rx="3" fill="#2dd4bf"/>',
                f'<rect x="{center + 1:.2f}" '
                f'y="{top + inner_height - unfollow_height:.2f}" '
                f'width="{bar_width:.2f}" height="{unfollow_height:.2f}" '
                'rx="3" fill="#fb923c"/></g>',
            ]
        )
        if index % label_every == 0 or index == len(values) - 1:
            day = str(row.get("observed_since") or "")[:10]
            parts.append(
                f'<text x="{center:.2f}" y="244" text-anchor="middle" '
                f'class="svg-axis">{_html_text(day[5:])}</text>'
            )
        post_count = int(context.get("published_post_count") or 0)
        if post_count:
            parts.append(
                f'<circle cx="{center:.2f}" cy="257" r="7" fill="#38bdf8"/>'
                f'<text x="{center:.2f}" y="260" text-anchor="middle" '
                f'class="svg-badge">{post_count}</text>'
            )
    parts.extend(
        [
            '<text x="742" y="273" text-anchor="end" class="svg-muted">'
            "blue badge = posts published (context only)</text>",
            "</svg>",
        ]
    )
    return "".join(parts)


def _stock_svg(rows: Sequence[Mapping[str, Any]]) -> str:
    points = [
        row
        for row in sorted(
            rows,
            key=lambda item: str(item.get("fetched_at") or ""),
        )
        if numeric(row.get("followers_count")) is not None
    ]
    if not points:
        return _svg_empty(
            chart_id="follower-stock-chart",
            title="Follower stock snapshots",
            message="No follower-stock snapshots",
        )
    width, height = 760.0, 240.0
    left, right, top, bottom = 54.0, 24.0, 38.0, 42.0
    inner_width = width - left - right
    inner_height = height - top - bottom
    counts = [float(row["followers_count"]) for row in points]
    minimum, maximum = min(counts), max(counts)
    span = maximum - minimum
    coordinates: list[tuple[float, float]] = []
    for index, count in enumerate(counts):
        x = (
            left + inner_width / 2
            if len(points) == 1
            else left + inner_width * index / (len(points) - 1)
        )
        y = (
            top + inner_height / 2
            if span == 0
            else top + inner_height * (maximum - count) / span
        )
        coordinates.append((x, y))
    path = " ".join(
        f"{'M' if index == 0 else 'L'} {x:.2f} {y:.2f}"
        for index, (x, y) in enumerate(coordinates)
    )
    parts = [
        '<svg id="follower-stock-chart" class="chart" viewBox="0 0 760 240" '
        'role="img" aria-label="Follower stock snapshots">',
        "<title>Follower stock snapshots</title>",
        '<rect width="760" height="240" rx="18" fill="#101c25"/>',
        '<text x="54" y="24" class="svg-title">Follower stock snapshots</text>',
        '<line x1="54" y1="198" x2="736" y2="198" stroke="#35505f"/>',
        f'<path d="{path}" fill="none" stroke="#38bdf8" stroke-width="3"/>',
    ]
    for index, (row, (x, y)) in enumerate(zip(points, coordinates)):
        tooltip = (
            f"{row.get('fetched_at')}: "
            f"{_html_metric(row.get('followers_count'))} followers"
        )
        parts.append(
            f'<circle cx="{x:.2f}" cy="{y:.2f}" r="5" fill="#e0f2fe">'
            f"<title>{_html_text(tooltip)}</title></circle>"
        )
        if index in {0, len(points) - 1}:
            parts.append(
                f'<text x="{x:.2f}" y="{max(35.0, y - 10):.2f}" '
                f'text-anchor="middle" class="svg-value">'
                f"{_html_metric(row.get('followers_count'))}</text>"
            )
    first_day = str(points[0].get("fetched_at") or "")[:10]
    last_day = str(points[-1].get("fetched_at") or "")[:10]
    parts.extend(
        [
            f'<text x="54" y="221" class="svg-axis">{_html_text(first_day)}</text>',
            f'<text x="736" y="221" text-anchor="end" class="svg-axis">'
            f"{_html_text(last_day)}</text>",
            "</svg>",
        ]
    )
    return "".join(parts)


def _maturity_coverage_svg(
    coverage: Mapping[str, Any],
    *,
    chart_id: str = "maturity-coverage-chart",
    title: str = "Maturity coverage",
) -> str:
    maturity = coverage.get("snapshot_maturity")
    maturity = maturity if isinstance(maturity, Mapping) else {}
    rows = [(window, maturity.get(window, {})) for window in WINDOW_ORDER]
    parts = [
        f'<svg id="{html.escape(chart_id, quote=True)}" class="chart" '
        'viewBox="0 0 760 250" '
        'role="img" aria-label="Fixed maturity snapshot coverage">',
        f"<title>{_html_text(title)}</title>",
        '<rect width="760" height="250" rx="18" fill="#101c25"/>',
        f'<text x="42" y="28" class="svg-title">{_html_text(title)}</text>',
    ]
    for index, (window, raw) in enumerate(rows):
        entry = raw if isinstance(raw, Mapping) else {}
        count = int(entry.get("count") or 0)
        total = int(entry.get("total") or 0)
        ratio = min(1.0, max(0.0, count / total)) if total else 0.0
        y = 58 + index * 45
        parts.extend(
            [
                f'<text x="42" y="{y + 15}" class="svg-axis">{window}</text>',
                f'<rect x="96" y="{y}" width="560" height="20" rx="10" fill="#203541"/>',
                f'<rect x="96" y="{y}" width="{560 * ratio:.2f}" height="20" '
                'rx="10" fill="#a3e635"/>',
                f'<text x="716" y="{y + 15}" text-anchor="end" class="svg-value">'
                f"{count}/{total} ({ratio * 100:.0f}%)</text>",
            ]
        )
    parts.append("</svg>")
    return "".join(parts)


def _reach_intent_svg(posts: Sequence[Mapping[str, Any]]) -> str:
    points: list[tuple[float, float, str]] = []
    for post in posts:
        observation = post.get("maturity_windows", {}).get("latest")
        if not isinstance(observation, Mapping):
            continue
        reach = numeric(metric_value(observation, "reach"))
        intent = numeric(metric_value(observation, "intent_actions_per_1000_reach"))
        if reach is None or intent is None or float(reach) < 0:
            continue
        identity = post.get("identity")
        identity = identity if isinstance(identity, Mapping) else {}
        points.append(
            (float(reach), float(intent), str(identity.get("media_id") or "unknown"))
        )
    points.sort(key=lambda row: (row[0], row[1], row[2]))
    if not points:
        return _svg_empty(
            chart_id="reach-intent-scatter",
            title="Reach versus intent",
            message="No latest observations have both reach and intent",
        )
    width, height = 760.0, 300.0
    left, right, top, bottom = 58.0, 28.0, 38.0, 50.0
    inner_width = width - left - right
    inner_height = height - top - bottom
    log_reaches = [math.log1p(row[0]) for row in points]
    intents = [row[1] for row in points]
    min_x, max_x = min(log_reaches), max(log_reaches)
    min_y, max_y = min(intents), max(intents)
    span_x, span_y = max_x - min_x, max_y - min_y
    parts = [
        '<svg id="reach-intent-scatter" class="chart" viewBox="0 0 760 300" '
        'role="img" aria-label="Latest Reel reach versus intent actions per 1000 reach">',
        "<title>Latest Reel reach versus intent actions per 1,000 reach</title>",
        '<rect width="760" height="300" rx="18" fill="#101c25"/>',
        '<text x="58" y="25" class="svg-title">Reach vs intent · latest</text>',
        '<line x1="58" y1="250" x2="732" y2="250" stroke="#35505f"/>',
        '<line x1="58" y1="38" x2="58" y2="250" stroke="#35505f"/>',
    ]
    for (reach, intent, media_id), log_reach in zip(points, log_reaches):
        x = (
            left + inner_width / 2
            if span_x == 0
            else left + inner_width * (log_reach - min_x) / span_x
        )
        y = (
            top + inner_height / 2
            if span_y == 0
            else top + inner_height * (max_y - intent) / span_y
        )
        parts.append(
            f'<circle cx="{x:.2f}" cy="{y:.2f}" r="5" fill="#c084fc" '
            'fill-opacity="0.8">'
            f"<title>{_html_text(media_id)}: {_html_metric(reach)} reach, "
            f"{_html_metric(intent, decimals=2)} intent/1k reach</title></circle>"
        )
    parts.extend(
        [
            '<text x="395" y="286" text-anchor="middle" class="svg-axis">'
            "Reach (log scale)</text>",
            '<text x="16" y="145" text-anchor="middle" class="svg-axis" '
            'transform="rotate(-90 16 145)">Intent actions / 1k reach</text>',
            "</svg>",
        ]
    )
    return "".join(parts)


def _funnel_svg(funnel: Mapping[str, Any]) -> str:
    stage_counts = funnel.get("stage_counts")
    stage_counts = stage_counts if isinstance(stage_counts, Mapping) else {}
    stages = [
        (stage, counts)
        for stage, counts in stage_counts.items()
        if isinstance(counts, Mapping)
    ]
    if not stages:
        return _svg_empty(
            chart_id="funnel-stage-chart",
            title="Funnel stage diagnostics",
            message="No comparable stage diagnostics",
        )
    statuses = (
        ("SUCCEEDED", "#2dd4bf"),
        ("AT_BASELINE", "#38bdf8"),
        ("WEAK", "#fb923c"),
        ("UNKNOWN", "#64748b"),
    )
    height = 58 + 38 * len(stages)
    parts = [
        f'<svg id="funnel-stage-chart" class="chart" viewBox="0 0 760 {height}" '
        'role="img" aria-label="Funnel stage diagnostic counts">',
        f"<title>Funnel stage diagnostic counts</title>"
        f'<rect width="760" height="{height}" rx="18" fill="#101c25"/>',
        '<text x="34" y="26" class="svg-title">Funnel stage counts</text>',
    ]
    for index, (stage, counts) in enumerate(stages):
        y = 43 + index * 38
        total = sum(int(counts.get(name) or 0) for name, _ in statuses)
        cursor = 170.0
        parts.append(
            f'<text x="34" y="{y + 15}" class="svg-axis">'
            f"{_html_text(stage)}</text>"
        )
        if total == 0:
            parts.append(
                f'<rect x="170" y="{y}" width="540" height="20" rx="10" '
                'fill="#203541"/>'
            )
            continue
        for name, color in statuses:
            count = int(counts.get(name) or 0)
            segment = 540.0 * count / total
            if segment:
                parts.append(
                    f'<rect x="{cursor:.2f}" y="{y}" width="{segment:.2f}" '
                    f'height="20" fill="{color}"><title>'
                    f"{_html_text(stage)} · {name}: {count}</title></rect>"
                )
            cursor += segment
        parts.append(
            f'<text x="728" y="{y + 15}" text-anchor="end" class="svg-value">'
            f"n={total}</text>"
        )
    parts.append("</svg>")
    return "".join(parts)


def _per_reel_table(posts: Sequence[Mapping[str, Any]]) -> str:
    rows: list[str] = []
    sorted_posts = sorted(
        (post for post in posts if isinstance(post, Mapping)),
        key=lambda post: (
            str(post.get("identity", {}).get("published_at") or ""),
            str(post.get("identity", {}).get("media_id") or ""),
        ),
        reverse=True,
    )
    for row_index, post in enumerate(sorted_posts):
        identity = post.get("identity")
        identity = identity if isinstance(identity, Mapping) else {}
        metadata = post.get("content_metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        observation = _latest_post_observation(post)
        raw = observation.get("raw_metrics")
        raw = raw if isinstance(raw, Mapping) else {}
        derived = observation.get("derived_metrics")
        derived = derived if isinstance(derived, Mapping) else {}

        media_id = str(identity.get("media_id") or "Unavailable")
        permalink = _html_href(identity.get("permalink"))
        hook = metadata.get("hook_text") or identity.get("caption") or media_id
        reel_link = (
            f'<a href="{permalink}" target="_blank" rel="noreferrer">Open Reel ↗</a>'
            if permalink
            else '<span class="muted">Link unavailable</span>'
        )
        reel_cell = (
            f'<div class="reel-id"><strong>{_html_text(media_id)}</strong>{reel_link}</div>'
            f'<small class="hook">{_html_text(hook)}</small>'
        )
        content_bits = [
            f"series: {_html_text(metadata.get('series') or 'Unavailable')}",
            f"goal: {_html_text(metadata.get('content_goal') or 'Unavailable')}",
            f"format: {_html_text(metadata.get('format') or 'Unavailable')}",
        ]
        content_cell = "<br>".join(content_bits)

        published = parse_datetime(identity.get("published_at"))
        published_text = (
            published.astimezone(JST).strftime("%Y-%m-%d %H:%M JST")
            if published is not None
            else "Unavailable"
        )
        actual_age = numeric(observation.get("actual_age_hours"))
        age_text = (
            f"{float(actual_age):,.1f}h" if actual_age is not None else "Unavailable"
        )
        comparison_window = post.get("analysis_maturity_window") or "Unavailable"
        age_cell = (
            f'<span class="metric-main">{_html_text(age_text)}</span>'
            f"<small>comparison: {_html_text(comparison_window)}</small>"
        )

        meta_view_bits = [f"total {_html_metric(raw.get('total_views'))}"]
        if raw.get("facebook_views") is not None:
            meta_view_bits.append(f"FB {_html_metric(raw.get('facebook_views'))}")
        if raw.get("crossposted_views") is not None:
            meta_view_bits.append(
                f"crossposted {_html_metric(raw.get('crossposted_views'))}"
            )
        meta_views_cell = "<br>".join(_html_text(bit) for bit in meta_view_bits)
        instagram_views_cell = (
            f'<span class="metric-main">{_html_metric(raw.get("views"))}</span>'
            f'<small>{_html_metric(derived.get("views_per_reached_account"), decimals=2)}'
            " views / reached account</small>"
        )
        engagement_cell = (
            f'<span class="metric-main">{_html_percent(derived.get("engagement_rate_by_reach"))}</span>'
            "<small>total interactions / reach</small>"
        )

        follow = derived.get("follow_conversion")
        follow = follow if isinstance(follow, Mapping) else {}
        follow_rate = follow.get("value")
        follow_denominator = follow.get("denominator_type")
        follow_cell = _html_count_rate(
            raw.get("follows"),
            follow_rate,
            unavailable_note="post attribution unavailable",
        )
        if numeric(follow_rate) is not None:
            follow_cell += (
                f"<small>denominator: {_html_text(follow_denominator)}</small>"
            )

        production_minutes = numeric(metadata.get("production_minutes"))
        if production_minutes is None:
            production_cell = "Unavailable"
        else:
            production_bits = [f"{float(production_minutes):,.0f} min"]
            for label, key in (
                ("reach/h", "reach_per_production_hour"),
                ("follows/h", "follows_per_production_hour"),
                ("watch h/h", "watch_hours_per_production_hour"),
            ):
                value = derived.get(key)
                if numeric(value) is not None:
                    production_bits.append(
                        f"{label} {_html_metric(value, decimals=2)}"
                    )
            production_cell = "<br>".join(
                _html_text(value) for value in production_bits
            )

        classification_labels = sorted(
            str(item.get("label"))
            for item in post.get("classifications", [])
            if isinstance(item, Mapping) and item.get("label")
        )
        diagnostic_labels = sorted(
            str(item.get("diagnostic"))
            for item in post.get("funnel_diagnostics", [])
            if isinstance(item, Mapping) and item.get("diagnostic")
        )
        evidence_bits = [
            *(
                f'<span class="pill">{_html_text(label)}</span>'
                for label in classification_labels
            ),
            *(
                f'<span class="pill diagnostic">{_html_text(label)}</span>'
                for label in diagnostic_labels
            ),
        ]
        evidence_cell = " ".join(evidence_bits) if evidence_bits else "No label"
        content_sort = " | ".join(
            str(value)
            for value in (
                metadata.get("series"),
                metadata.get("content_goal"),
                metadata.get("format"),
            )
            if value is not None and str(value).strip()
        )
        evidence_sort = " | ".join(
            (*classification_labels, *diagnostic_labels)
        )

        cells = (
            _sortable_cell(
                reel_cell,
                identity.get("media_id"),
                sort_type="text",
                css_class="sticky-col",
            ),
            _sortable_cell(content_cell, content_sort, sort_type="text"),
            _sortable_cell(
                _html_text(published_text),
                published.timestamp() if published is not None else None,
                sort_type="date",
            ),
            _sortable_cell(age_cell, actual_age, sort_type="number"),
            _sortable_cell(
                _html_metric(raw.get("reach")),
                raw.get("reach"),
                sort_type="number",
            ),
            _sortable_cell(
                instagram_views_cell,
                raw.get("views"),
                sort_type="number",
            ),
            _sortable_cell(
                meta_views_cell,
                raw.get("total_views"),
                sort_type="number",
            ),
            _sortable_cell(
                _html_count_rate(
                    raw.get("interactions"),
                    derived.get("interactions_per_1000_reach"),
                ),
                derived.get("interactions_per_1000_reach"),
                sort_type="number",
            ),
            _sortable_cell(
                engagement_cell,
                derived.get("engagement_rate_by_reach"),
                sort_type="number",
            ),
            _sortable_cell(
                _html_count_rate(
                    raw.get("likes"),
                    derived.get("likes_per_1000_reach"),
                ),
                derived.get("likes_per_1000_reach"),
                sort_type="number",
            ),
            _sortable_cell(
                _html_count_rate(
                    raw.get("comments"),
                    derived.get("comments_per_1000_reach"),
                ),
                derived.get("comments_per_1000_reach"),
                sort_type="number",
            ),
            _sortable_cell(
                _html_count_rate(
                    raw.get("shares"),
                    derived.get("shares_per_1000_reach"),
                ),
                derived.get("shares_per_1000_reach"),
                sort_type="number",
            ),
            _sortable_cell(
                _html_count_rate(
                    raw.get("reposts"),
                    derived.get("reposts_per_1000_reach"),
                ),
                derived.get("reposts_per_1000_reach"),
                sort_type="number",
            ),
            _sortable_cell(
                _html_count_rate(
                    raw.get("saves"),
                    derived.get("saves_per_1000_reach"),
                ),
                derived.get("saves_per_1000_reach"),
                sort_type="number",
            ),
            _sortable_cell(
                _html_metric(
                    derived.get("intent_actions_per_1000_reach"),
                    decimals=2,
                ),
                derived.get("intent_actions_per_1000_reach"),
                sort_type="number",
            ),
            _sortable_cell(
                _html_duration(raw.get("total_watch_time_seconds")),
                raw.get("total_watch_time_seconds"),
                sort_type="number",
            ),
            _sortable_cell(
                _html_duration(raw.get("duration_seconds")),
                raw.get("duration_seconds"),
                sort_type="number",
            ),
            _sortable_cell(
                _html_duration(derived.get("average_watch_time_seconds")),
                derived.get("average_watch_time_seconds"),
                sort_type="number",
            ),
            _sortable_cell(
                _html_percent(derived.get("watch_depth")),
                derived.get("watch_depth"),
                sort_type="number",
            ),
            _sortable_cell(
                _html_percent(
                    raw.get("reels_skip_rate"),
                    value_is_ratio=False,
                ),
                raw.get("reels_skip_rate"),
                sort_type="number",
            ),
            _sortable_cell(follow_cell, follow_rate, sort_type="number"),
            _sortable_cell(
                production_cell,
                production_minutes,
                sort_type="number",
            ),
            _sortable_cell(evidence_cell, evidence_sort, sort_type="text"),
        )
        rows.append(
            f'<tr data-row-index="{row_index}">{"".join(cells)}</tr>'
        )
    if not rows:
        rows.append(
            '<tr><td colspan="23" class="empty">No Reel observations are available.</td></tr>'
        )
    headers = (
        _sortable_header("Reel", sort_type="text", css_class="sticky-col"),
        _sortable_header("Content", sort_type="text"),
        _sortable_header(
            "Published",
            sort_type="date",
            initial_direction="descending",
        ),
        _sortable_header("Latest age", sort_type="number"),
        _sortable_header("Reach", sort_type="number"),
        _sortable_header("IG views", sort_type="number"),
        _sortable_header("Meta views", sort_type="number"),
        _sortable_header("Interactions /1k", sort_type="number"),
        _sortable_header("Engagement / reach", sort_type="number"),
        _sortable_header("Likes /1k", sort_type="number"),
        _sortable_header("Comments /1k", sort_type="number"),
        _sortable_header("Shares /1k", sort_type="number"),
        _sortable_header("Reposts /1k", sort_type="number"),
        _sortable_header("Saves /1k", sort_type="number"),
        _sortable_header("Intent /1k reach", sort_type="number"),
        _sortable_header("Total watch", sort_type="number"),
        _sortable_header("Duration", sort_type="number"),
        _sortable_header("Avg watch", sort_type="number"),
        _sortable_header("Watch depth", sort_type="number"),
        _sortable_header("3s skip", sort_type="number"),
        _sortable_header("Follows", sort_type="number"),
        _sortable_header("Production", sort_type="number"),
        _sortable_header("Evidence", sort_type="text"),
    )
    return (
        '<div class="reel-table-wrap" tabindex="0">'
        '<table id="per-reel-table" data-testid="per-reel-table" '
        'class="evidence-table" data-sortable="true" '
        'aria-describedby="per-reel-sort-status">'
        '<caption class="sr-only">Per-Reel metrics. Select a column heading to '
        "sort; unavailable values remain last.</caption>"
        f"<thead><tr>{''.join(headers)}</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
        '<p id="per-reel-sort-status" class="sort-status sr-only" role="status" '
        'aria-live="polite"></p></div>'
    )


def _html_count_rate_for_denominator(
    count: Any,
    rate_per_1000: Any,
    *,
    denominator: str,
    unavailable_note: str = "rate unavailable",
) -> str:
    note = (
        f"{_html_metric(rate_per_1000, decimals=2)} /1k {denominator}"
        if numeric(rate_per_1000) is not None
        else unavailable_note
    )
    return (
        f'<span class="metric-main">{_html_metric(count)}</span>'
        f"<small>{_html_text(note)}</small>"
    )


def _facebook_per_reel_table(posts: Sequence[Mapping[str, Any]]) -> str:
    """Render the independent Facebook-native lane with explicit denominators."""
    rows: list[str] = []
    sorted_posts = sorted(
        (post for post in posts if isinstance(post, Mapping)),
        key=lambda post: (
            str(post.get("identity", {}).get("published_at") or ""),
            str(post.get("identity", {}).get("media_id") or ""),
        ),
        reverse=True,
    )
    for row_index, post in enumerate(sorted_posts):
        identity = post.get("identity")
        identity = identity if isinstance(identity, Mapping) else {}
        metadata = post.get("content_metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        observation = _latest_post_observation(post)
        raw = observation.get("raw_metrics")
        raw = raw if isinstance(raw, Mapping) else {}
        derived = observation.get("derived_metrics")
        derived = derived if isinstance(derived, Mapping) else {}
        provenance = observation.get("metric_provenance")
        provenance = provenance if isinstance(provenance, Mapping) else {}
        canonical_provenance = provenance.get("canonical_fields")
        canonical_provenance = (
            canonical_provenance
            if isinstance(canonical_provenance, Mapping)
            else {}
        )

        media_id = str(identity.get("media_id") or "Unavailable")
        permalink = _html_href(identity.get("permalink"))
        reel_link = (
            f'<a href="{permalink}" target="_blank" rel="noreferrer">Open Facebook Reel ↗</a>'
            if permalink
            else '<span class="muted">Link unavailable</span>'
        )
        hook = metadata.get("hook_text") or identity.get("caption") or media_id
        reel_cell = (
            f'<div class="reel-id"><strong>{_html_text(media_id)}</strong>'
            f"{reel_link}</div><small class=\"hook\">{_html_text(hook)}</small>"
        )

        paired = identity.get("paired_instagram")
        paired = paired if isinstance(paired, Mapping) else {}
        paired_href = _html_href(paired.get("permalink"))
        paired_cell = (
            f'<a href="{paired_href}" target="_blank" rel="noreferrer">'
            f"{_html_text(paired.get('media_id'))} ↗</a>"
            if paired_href
            else _html_text(paired.get("media_id"))
            if paired.get("media_id")
            else "Unavailable"
        )
        content_sort = " | ".join(
            str(value)
            for value in (
                metadata.get("series"),
                metadata.get("content_goal"),
                metadata.get("format"),
            )
            if value is not None and str(value).strip()
        )
        content_cell = "<br>".join(
            (
                f"series: {_html_text(metadata.get('series'))}",
                f"goal: {_html_text(metadata.get('content_goal'))}",
                f"format: {_html_text(metadata.get('format'))}",
            )
        )

        published = parse_datetime(identity.get("published_at"))
        published_text = (
            published.astimezone(JST).strftime("%Y-%m-%d %H:%M JST")
            if published is not None
            else "Unavailable"
        )
        actual_age = numeric(observation.get("actual_age_hours"))
        age_cell = (
            f'<span class="metric-main">'
            f"{_html_metric(actual_age, decimals=1) if actual_age is not None else 'Unavailable'}"
            f"{'h' if actual_age is not None else ''}</span>"
            f"<small>comparison: {_html_text(post.get('analysis_maturity_window'))}</small>"
        )

        views_cell = (
            f'<span class="metric-main">{_html_metric(raw.get("views"))}</span>'
            f"<small>plays {_html_metric(raw.get('plays'))} · "
            f"initial {_html_metric(raw.get('initial_plays'))} · "
            f"replays {_html_metric(raw.get('replays'))}</small>"
        )
        has_unique_viewers = numeric(raw.get("reach")) is not None
        denominator = "unique viewers" if has_unique_viewers else "views"

        def rate_for(action: str) -> Any:
            return derived.get(
                f"{action}_per_1000_reach"
                if has_unique_viewers
                else f"{action}_per_1000_views"
            )

        interaction_rate = (
            derived.get("interactions_per_1000_reach")
            if has_unique_viewers
            else derived.get("interactions_per_1000_views")
        )
        follow = derived.get("follow_conversion")
        follow = follow if isinstance(follow, Mapping) else {}
        follow_cell = _html_count_rate_for_denominator(
            raw.get("follows"),
            follow.get("value"),
            denominator="unique viewers",
            unavailable_note="Reel attribution unavailable",
        )

        direct_skip = numeric(raw.get("reels_skip_rate"))
        dropoff = numeric(derived.get("three_second_dropoff_rate"))
        if direct_skip is not None:
            skip_cell = (
                f'<span class="metric-main">{_html_percent(direct_skip, value_is_ratio=False)}</span>'
                "<small>direct Meta skip metric</small>"
            )
            skip_sort = direct_skip
        elif dropoff is not None:
            skip_cell = (
                f'<span class="metric-main">{_html_percent(dropoff)}</span>'
                "<small>derived exact 3s retention drop-off; not Meta skip rate</small>"
            )
            skip_sort = dropoff
        else:
            skip_cell = (
                '<span class="metric-main">Unavailable</span>'
                "<small>Facebook exposes no direct 3s skip metric</small>"
            )
            skip_sort = None

        production_minutes = numeric(metadata.get("production_minutes"))
        production_cell = (
            "Unavailable"
            if production_minutes is None
            else (
                f'<span class="metric-main">{_html_metric(production_minutes)} min</span>'
                f"<small>{_html_metric(derived.get('views_per_production_hour'), decimals=2)} "
                "views/hour · "
                f"{_html_metric(derived.get('follows_per_production_hour'), decimals=2)} "
                "follows/hour</small>"
            )
        )
        rich_sources = sorted(
            {
                str(entry.get("source_field"))
                for entry in canonical_provenance.values()
                if isinstance(entry, Mapping)
                and str(entry.get("source") or "") == "facebook_video_insights"
                and entry.get("source_field")
            }
        )
        evidence_cell = (
            '<span class="pill">rich video_insights</span><br>'
            f"<small>{_html_text(', '.join(rich_sources))}</small>"
            if rich_sources
            else '<span class="pill">direct fallback</span><br>'
            "<small>views/likes/comments + Page-post shares when returned</small>"
        )
        evidence_sort = "rich video_insights" if rich_sources else "direct fallback"

        cells = (
            _sortable_cell(
                reel_cell,
                media_id,
                sort_type="text",
                css_class="sticky-col",
            ),
            _sortable_cell(
                paired_cell,
                paired.get("media_id"),
                sort_type="text",
            ),
            _sortable_cell(content_cell, content_sort, sort_type="text"),
            _sortable_cell(
                _html_text(published_text),
                published.timestamp() if published is not None else None,
                sort_type="date",
            ),
            _sortable_cell(age_cell, actual_age, sort_type="number"),
            _sortable_cell(views_cell, raw.get("views"), sort_type="number"),
            _sortable_cell(
                _html_metric(raw.get("reach")),
                raw.get("reach"),
                sort_type="number",
            ),
            _sortable_cell(
                _html_count_rate_for_denominator(
                    raw.get("interactions"),
                    interaction_rate,
                    denominator=denominator,
                    unavailable_note="requires reactions + comments + shares",
                ),
                interaction_rate,
                sort_type="number",
            ),
            _sortable_cell(
                _html_count_rate_for_denominator(
                    raw.get("likes"),
                    rate_for("likes"),
                    denominator=denominator,
                ),
                rate_for("likes"),
                sort_type="number",
            ),
            _sortable_cell(
                _html_count_rate_for_denominator(
                    raw.get("reactions"),
                    rate_for("reactions"),
                    denominator=denominator,
                ),
                rate_for("reactions"),
                sort_type="number",
            ),
            _sortable_cell(
                _html_count_rate_for_denominator(
                    raw.get("comments"),
                    rate_for("comments"),
                    denominator=denominator,
                ),
                rate_for("comments"),
                sort_type="number",
            ),
            _sortable_cell(
                _html_count_rate_for_denominator(
                    raw.get("shares"),
                    rate_for("shares"),
                    denominator=denominator,
                ),
                rate_for("shares"),
                sort_type="number",
            ),
            _sortable_cell(
                _html_count_rate_for_denominator(
                    raw.get("saves"),
                    rate_for("saves"),
                    denominator=denominator,
                    unavailable_note="not exposed for Facebook Reels",
                ),
                rate_for("saves"),
                sort_type="number",
            ),
            _sortable_cell(follow_cell, follow.get("value"), sort_type="number"),
            _sortable_cell(
                _html_duration(raw.get("total_watch_time_seconds")),
                raw.get("total_watch_time_seconds"),
                sort_type="number",
            ),
            _sortable_cell(
                _html_duration(derived.get("average_watch_time_seconds")),
                derived.get("average_watch_time_seconds"),
                sort_type="number",
            ),
            _sortable_cell(
                _html_percent(derived.get("watch_depth")),
                derived.get("watch_depth"),
                sort_type="number",
            ),
            _sortable_cell(skip_cell, skip_sort, sort_type="number"),
            _sortable_cell(
                production_cell,
                production_minutes,
                sort_type="number",
            ),
            _sortable_cell(evidence_cell, evidence_sort, sort_type="text"),
        )
        rows.append(f'<tr data-row-index="{row_index}">{"".join(cells)}</tr>')

    if not rows:
        rows.append(
            '<tr><td colspan="20" class="empty">No Facebook Reel observations are available.</td></tr>'
        )
    headers = (
        _sortable_header(
            "Facebook Reel",
            sort_type="text",
            css_class="sticky-col",
        ),
        _sortable_header("Paired Instagram", sort_type="text"),
        _sortable_header("Content", sort_type="text"),
        _sortable_header(
            "Published",
            sort_type="date",
            initial_direction="descending",
        ),
        _sortable_header("Latest age", sort_type="number"),
        _sortable_header("Views / plays", sort_type="number"),
        _sortable_header("Unique viewers", sort_type="number"),
        _sortable_header("Total engagement /1k", sort_type="number"),
        _sortable_header("Likes /1k", sort_type="number"),
        _sortable_header("All reactions /1k", sort_type="number"),
        _sortable_header("Comments /1k", sort_type="number"),
        _sortable_header("Shares /1k", sort_type="number"),
        _sortable_header("Saves /1k", sort_type="number"),
        _sortable_header("Follows /1k unique viewers", sort_type="number"),
        _sortable_header("Total watch", sort_type="number"),
        _sortable_header("Avg watch", sort_type="number"),
        _sortable_header("Watch depth", sort_type="number"),
        _sortable_header("3s skip / drop-off", sort_type="number"),
        _sortable_header("Production", sort_type="number"),
        _sortable_header("Evidence source", sort_type="text"),
    )
    return (
        '<div class="facebook-table-wrap" tabindex="0">'
        '<table id="facebook-per-reel-table" '
        'data-testid="facebook-per-reel-table" class="evidence-table '
        'facebook-evidence-table" data-sortable="true" '
        'aria-describedby="facebook-per-reel-sort-status">'
        '<caption class="sr-only">Facebook-native per-Reel metrics. Select a '
        "column heading to sort; unavailable values remain last.</caption>"
        f"<thead><tr>{''.join(headers)}</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
        '<p id="facebook-per-reel-sort-status" class="sort-status sr-only" '
        'role="status" aria-live="polite"></p></div>'
    )


def _publication_day_table(
    daily: Sequence[Mapping[str, Any]],
    posts: Sequence[Mapping[str, Any]],
) -> str:
    identities: dict[str, Mapping[str, Any]] = {}
    for post in posts:
        if not isinstance(post, Mapping):
            continue
        identity = post.get("identity")
        if isinstance(identity, Mapping) and identity.get("media_id"):
            identities[str(identity.get("media_id"))] = identity

    rows: list[str] = []
    for row_index, day in enumerate(
        reversed([row for row in daily if isinstance(row, Mapping)])
    ):
        context = day.get("publication_context")
        context = context if isinstance(context, Mapping) else {}
        media_ids = [
            str(value)
            for value in context.get("media_ids", [])
            if str(value or "").strip()
        ]
        if not media_ids:
            continue
        links: list[str] = []
        for index, media_id in enumerate(media_ids, start=1):
            identity = identities.get(media_id, {})
            href = _html_href(identity.get("permalink"))
            label = f"Reel {index} ↗"
            links.append(
                f'<a href="{href}" target="_blank" rel="noreferrer">{label}</a>'
                if href
                else _html_text(media_id)
            )
        interval = (
            f"{str(day.get('observed_since') or '')[:10]} UTC"
            if day.get("observed_since")
            else "Unavailable"
        )
        observed_since = parse_datetime(day.get("observed_since"))
        status = "preliminary" if day.get("preliminary") else "finalized"
        activity_cell = (
            f"views {_html_metric(day.get('reel_views'))}<br>"
            f"interactions {_html_metric(day.get('reel_total_interactions'))}<br>"
            f"{_html_metric(day.get('reel_interactions_per_1000_reel_reach'), decimals=2)} "
            "/1k Reel reach"
        )
        cells = (
            _sortable_cell(
                _html_text(interval),
                observed_since.timestamp() if observed_since is not None else None,
                sort_type="date",
            ),
            _sortable_cell(
                " ".join(links),
                context.get("published_post_count"),
                sort_type="number",
                css_class="reel-links",
            ),
            _sortable_cell(
                _html_metric(day.get("follows")),
                day.get("follows"),
                sort_type="number",
            ),
            _sortable_cell(
                _html_metric(day.get("unfollows")),
                day.get("unfollows"),
                sort_type="number",
            ),
            _sortable_cell(
                _html_metric(day.get("net_growth")),
                day.get("net_growth"),
                sort_type="number",
            ),
            _sortable_cell(
                _html_metric(day.get("account_reach")),
                day.get("account_reach"),
                sort_type="number",
            ),
            _sortable_cell(
                _html_metric(
                    day.get("gross_follows_per_1000_account_reach"),
                    decimals=2,
                ),
                day.get("gross_follows_per_1000_account_reach"),
                sort_type="number",
            ),
            _sortable_cell(
                _html_metric(day.get("reel_reach")),
                day.get("reel_reach"),
                sort_type="number",
            ),
            _sortable_cell(
                _html_metric(day.get("reel_non_follower_reach")),
                day.get("reel_non_follower_reach"),
                sort_type="number",
            ),
            _sortable_cell(
                _html_metric(
                    day.get("gross_follows_per_1000_reel_reach"),
                    decimals=2,
                ),
                day.get("gross_follows_per_1000_reel_reach"),
                sort_type="number",
            ),
            _sortable_cell(
                _html_metric(
                    day.get(
                        "gross_follows_per_1000_reel_non_follower_reach"
                    ),
                    decimals=2,
                ),
                day.get(
                    "gross_follows_per_1000_reel_non_follower_reach"
                ),
                sort_type="number",
            ),
            _sortable_cell(
                activity_cell,
                day.get("reel_interactions_per_1000_reel_reach"),
                sort_type="number",
            ),
            _sortable_cell(
                _html_text(status),
                status,
                sort_type="text",
            ),
        )
        rows.append(
            f'<tr data-row-index="{row_index}">{"".join(cells)}</tr>'
        )
    if not rows:
        rows.append(
            '<tr><td colspan="13" class="empty">No publication-day account intervals are available.</td></tr>'
        )
    headers = (
        _sortable_header(
            "Account day",
            sort_type="date",
            initial_direction="descending",
        ),
        _sortable_header("Reels published in interval", sort_type="number"),
        _sortable_header("Account follows", sort_type="number"),
        _sortable_header("Unfollows", sort_type="number"),
        _sortable_header("Net", sort_type="number"),
        _sortable_header("Account reach", sort_type="number"),
        _sortable_header("Follows /1k account reach", sort_type="number"),
        _sortable_header("All Reel reach", sort_type="number"),
        _sortable_header("Non-follower Reel reach", sort_type="number"),
        _sortable_header("Follows /1k Reel reach", sort_type="number"),
        _sortable_header(
            "Follows /1k non-follower Reel reach",
            sort_type="number",
        ),
        _sortable_header("All-Reel activity /1k", sort_type="number"),
        _sortable_header("Status", sort_type="text"),
    )
    return (
        '<div class="day-table-wrap" tabindex="0"><table '
        'id="publication-day-table" data-testid="publication-day-table" '
        'class="day-table" data-sortable="true" '
        'aria-describedby="publication-day-sort-status">'
        '<caption class="sr-only">Publication-day account context. Select a '
        "column heading to sort; unavailable values remain last.</caption>"
        f"<thead><tr>{''.join(headers)}</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
        '<p id="publication-day-sort-status" class="sort-status sr-only" '
        'role="status" aria-live="polite"></p></div>'
    )


def render_moneyball_html(report: Mapping[str, Any]) -> str:
    """Render a deterministic, self-contained Moneyball dashboard."""
    metadata = report.get("report_metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    coverage = report.get("data_coverage")
    coverage = coverage if isinstance(coverage, Mapping) else {}
    growth = report.get("account_growth")
    growth = growth if isinstance(growth, Mapping) else {}
    stock = growth.get("follower_stock")
    stock = stock if isinstance(stock, Mapping) else {}
    daily = growth.get("daily_intervals")
    daily = daily if isinstance(daily, list) else []
    stock_rows = growth.get("stock_snapshots")
    stock_rows = stock_rows if isinstance(stock_rows, list) else []
    funnel = report.get("funnel_diagnostics")
    funnel = funnel if isinstance(funnel, Mapping) else {}
    posts = report.get("posts")
    posts = posts if isinstance(posts, list) else []
    platforms = report.get("platform_analytics")
    platforms = platforms if isinstance(platforms, Mapping) else {}
    facebook = platforms.get("facebook")
    facebook = facebook if isinstance(facebook, Mapping) else {}
    facebook_status = str(facebook.get("status") or "UNAVAILABLE")
    facebook_posts = facebook.get("posts")
    facebook_posts = facebook_posts if isinstance(facebook_posts, list) else []
    facebook_coverage = facebook.get("data_coverage")
    facebook_coverage = (
        facebook_coverage if isinstance(facebook_coverage, Mapping) else {}
    )
    facebook_summary = facebook.get("account_summary")
    facebook_summary = (
        facebook_summary if isinstance(facebook_summary, Mapping) else {}
    )
    roster = report.get("series")
    roster = roster if isinstance(roster, list) else []
    warnings = growth.get("warnings")
    warnings = warnings if isinstance(warnings, list) else [ACCOUNT_ATTRIBUTION_WARNING]

    growth_coverage = growth.get("coverage")
    growth_coverage = (
        growth_coverage if isinstance(growth_coverage, Mapping) else {}
    )

    def kpi_metric(
        *,
        complete_key: str,
        known_key: str | None = None,
        coverage_key: str | None = None,
        complete_note: str,
        partial_label: str,
    ) -> tuple[Any, str]:
        value = growth.get(complete_key)
        if value is not None or known_key is None:
            return value, complete_note
        known_value = growth.get(known_key)
        if known_value is None:
            return None, complete_note
        metric_coverage = growth_coverage.get(coverage_key or complete_key)
        metric_coverage = (
            metric_coverage if isinstance(metric_coverage, Mapping) else {}
        )
        count = metric_coverage.get("count", 0)
        total = metric_coverage.get("total", 0)
        return known_value, f"{partial_label} · {count}/{total} days"

    gross_kpi = kpi_metric(
        complete_key="gross_follows",
        known_key="known_gross_follows",
        coverage_key="follows",
        complete_note="all covered daily intervals",
        partial_label="partial known total",
    )
    unfollows_kpi = kpi_metric(
        complete_key="unfollows",
        known_key="known_unfollows",
        coverage_key="unfollows",
        complete_note="all covered daily intervals",
        partial_label="partial known total",
    )
    net_kpi = kpi_metric(
        complete_key="net_growth",
        known_key="known_net_growth",
        coverage_key="follows",
        complete_note="gross − unfollows",
        partial_label="partial known net",
    )
    reach_coverage = growth_coverage.get("account_reach")
    reach_coverage = reach_coverage if isinstance(reach_coverage, Mapping) else {}
    reach_note = (
        f"{reach_coverage.get('count', 0)}/{reach_coverage.get('total', 0)} days"
    )
    reel_reach_coverage = growth_coverage.get("reel_reach")
    reel_reach_coverage = (
        reel_reach_coverage
        if isinstance(reel_reach_coverage, Mapping)
        else {}
    )
    reel_reach_note = (
        f"{reel_reach_coverage.get('count', 0)}/"
        f"{reel_reach_coverage.get('total', 0)} days · all Reels viewed"
    )
    kpis = (
        ("Follower stock", stock.get("latest"), "point-in-time"),
        ("Gross follows", gross_kpi[0], gross_kpi[1]),
        ("Unfollows", unfollows_kpi[0], unfollows_kpi[1]),
        ("Net growth", net_kpi[0], net_kpi[1]),
        ("Account reach", growth.get("account_reach"), reach_note),
        ("Reel reach-days", growth.get("reel_reach"), reel_reach_note),
        (
            "Net / 1k reach",
            growth.get("net_follows_per_1000_account_reach"),
            "requires matched flow coverage",
        ),
        (
            "Gross / 1k Reel reach",
            growth.get("gross_follows_per_1000_reel_reach"),
            "observational account-day ratio",
        ),
    )
    cards = "".join(
        '<article class="kpi">'
        f'<span class="eyebrow">{_html_text(label)}</span>'
        f'<strong>{_html_metric(value, decimals=2 if "1k" in label else 0)}</strong>'
        f'<small>{_html_text(note)}</small></article>'
        for label, value, note in kpis
    )
    warning_html = "".join(f"<li>{_html_text(value)}</li>" for value in warnings)

    facebook_html = ""
    if facebook_status in {"AVAILABLE", "NO_PUBLISHED_POSTS"}:
        facebook_api = facebook.get("api_semantics")
        facebook_api = (
            facebook_api if isinstance(facebook_api, Mapping) else {}
        )
        rich_api = facebook_api.get("rich_video_insights")
        rich_api = rich_api if isinstance(rich_api, Mapping) else {}
        latest_post_metrics = facebook_coverage.get("latest_post_metrics")
        latest_post_metrics = (
            latest_post_metrics
            if isinstance(latest_post_metrics, Mapping)
            else {}
        )
        has_facebook_unique_viewers = (
            numeric(facebook_summary.get("median_unique_media_viewers"))
            is not None
        )
        facebook_rate_suffix = (
            "unique" if has_facebook_unique_viewers else "views"
        )
        facebook_rate_key_suffix = (
            "unique_media_viewers"
            if has_facebook_unique_viewers
            else "views"
        )
        facebook_kpis = (
            (
                "Published Reels",
                facebook_coverage.get("published_posts"),
                "independent Facebook uploads",
            ),
            (
                "Latest snapshots",
                facebook_coverage.get("latest_snapshot_posts"),
                f"of {facebook_coverage.get('published_posts', 0)} Reels",
            ),
            (
                "Median views",
                facebook_summary.get("median_views"),
                "latest snapshots · playback count",
            ),
            (
                "Median unique viewers",
                facebook_summary.get("median_unique_media_viewers"),
                "requires read_insights",
            ),
            (
                f"Likes / 1k {facebook_rate_suffix}",
                facebook_summary.get(
                    f"median_likes_per_1000_{facebook_rate_key_suffix}"
                ),
                (
                    "unique media viewers"
                    if has_facebook_unique_viewers
                    else "view fallback; never mixed with unique viewers"
                ),
            ),
            (
                f"Comments / 1k {facebook_rate_suffix}",
                facebook_summary.get(
                    f"median_comments_per_1000_{facebook_rate_key_suffix}"
                ),
                (
                    "unique media viewers"
                    if has_facebook_unique_viewers
                    else "view fallback; never mixed with unique viewers"
                ),
            ),
            (
                f"Shares / 1k {facebook_rate_suffix}",
                facebook_summary.get(
                    f"median_shares_per_1000_{facebook_rate_key_suffix}"
                ),
                (
                    f"{format_coverage(latest_post_metrics.get('shares'))} "
                    "share coverage"
                ),
            ),
            (
                "Follows / 1k unique",
                facebook_summary.get("median_follow_conversion"),
                "Reel-attributed; requires read_insights",
            ),
        )
        facebook_cards = "".join(
            '<article class="kpi facebook-kpi">'
            f'<span class="eyebrow">{_html_text(label)}</span>'
            f'<strong>{_html_metric(value, decimals=2 if "1k" in label else 0)}</strong>'
            f'<small>{_html_text(note)}</small></article>'
            for label, value, note in facebook_kpis
        )
        facebook_gaps = facebook.get("data_gaps")
        facebook_gaps = (
            facebook_gaps if isinstance(facebook_gaps, list) else []
        )
        gap_items = "".join(
            "<li>"
            f"<strong>{_html_text(gap.get('field'))}</strong> · "
            f"{_html_text(format_coverage(gap.get('coverage')))} · "
            f"{_html_text(gap.get('limitation'))}"
            "</li>"
            for gap in facebook_gaps[:6]
            if isinstance(gap, Mapping)
        )
        facebook_html = (
            '<section id="facebook-native-overview" '
            'data-testid="facebook-native-overview" '
            'class="panel full platform-divider facebook-divider">'
            '<span class="eyebrow">Independent platform lane</span>'
            "<h2>Facebook-native Reel analytics</h2>"
            "<p>These are separately uploaded Facebook Reels with their own video "
            "IDs, publication times, snapshots, and cohorts. Instagram and Facebook "
            "totals and rankings are never combined.</p>"
            f'<span class="status">Rich video_insights: '
            f"<strong>{_html_text(rich_api.get('status'))}</strong></span></section>"
            '<section id="facebook-kpis" data-testid="facebook-kpis" '
            f'class="kpis facebook-kpis">{facebook_cards}</section>'
            '<section class="panel span-6"><h2>Facebook maturity coverage</h2>'
            f"{_maturity_coverage_svg(facebook_coverage, chart_id='facebook-maturity-coverage-chart', title='Facebook maturity coverage')}</section>"
            '<section class="panel span-6 facebook-gap-panel">'
            "<h2>API coverage boundary</h2>"
            "<p>The dashboard uses unique media viewers when the documented "
            "video_insights edge is authorized; otherwise each fallback rate says "
            "views. Saves and a direct Facebook 3-second skip metric stay unavailable.</p>"
            f"<ul>{gap_items or '<li>No measured gap.</li>'}</ul></section>"
            '<section id="facebook-per-reel-evidence" '
            'data-testid="facebook-per-reel-evidence" class="panel full">'
            "<h2>Facebook per-Reel evidence table</h2>"
            '<p class="section-note">Every row links to the native Facebook Reel '
            "and, when matched by content hash, its independently published Instagram "
            "counterpart. Each action rate uses unique media viewers when available; "
            "otherwise it is explicitly labeled per 1,000 views. Select any heading "
            "to sort and select it again to reverse the order. Unavailable values stay last.</p>"
            f"{_facebook_per_reel_table(facebook_posts)}</section>"
        )

    roster_rows = []
    for row in sorted(
        (item for item in roster if isinstance(item, Mapping)),
        key=lambda item: str(item.get("series") or ""),
    ):
        roster_rows.append(
            "<tr>"
            f"<td>{_html_text(row.get('series'))}</td>"
            f"<td>{_html_metric(row.get('post_count'))}</td>"
            f"<td>{_html_metric(row.get('total_reach'))}</td>"
            f"<td>{_html_metric(row.get('median_watch_depth'), decimals=2)}</td>"
            f"<td>{_html_metric(row.get('median_shares_per_1000_reach'), decimals=2)}</td>"
            f"<td>{_html_metric(row.get('median_saves_per_1000_reach'), decimals=2)}</td>"
            f"<td><span class=\"pill\">{_html_text(row.get('recommendation'))}</span></td>"
            f"<td>{_html_text(row.get('evidence_status'))}</td>"
            "</tr>"
        )
    if not roster_rows:
        roster_rows.append(
            '<tr><td colspan="8" class="empty">No recurring series has enough '
            "metadata for a roster row.</td></tr>"
        )

    generated = metadata.get("generated_at_jst") or metadata.get("generated_at")
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>Moneyball · {_html_text(metadata.get('account'))}</title>"
        "<style>"
        ":root{color-scheme:dark;--bg:#07131a;--panel:#101c25;--line:#263d49;"
        "--ink:#edf7f7;--muted:#93a9b4;--lime:#a3e635;--cyan:#38bdf8;"
        "--teal:#2dd4bf;--orange:#fb923c}"
        "*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at "
        "10% 0,#15313c 0,transparent 32%),var(--bg);color:var(--ink);font:15px/1.5 "
        "ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,\"Segoe UI\",sans-serif}"
        "main{width:min(1240px,calc(100% - 32px));margin:auto;padding:36px 0 64px}"
        "header{display:flex;justify-content:space-between;gap:24px;align-items:end;"
        "margin-bottom:28px}.eyebrow{color:var(--lime);font-size:11px;font-weight:800;"
        "letter-spacing:.13em;text-transform:uppercase}h1{font-size:clamp(30px,5vw,58px);"
        "line-height:1;margin:8px 0}h2{font-size:20px;margin:0 0 14px}p,small{color:var(--muted)}"
        ".status{border:1px solid var(--line);border-radius:999px;padding:8px 13px;"
        "white-space:nowrap}.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:16px}"
        ".panel{background:rgba(16,28,37,.92);border:1px solid var(--line);border-radius:20px;"
        "padding:18px;box-shadow:0 16px 48px #0003}.kpis{grid-column:1/-1;display:grid;"
        "grid-template-columns:repeat(6,1fr);gap:12px}.kpi{background:#0b1820;border:1px solid "
        "var(--line);border-radius:16px;padding:15px;min-width:0}.kpi strong{display:block;"
        "font-size:clamp(22px,2vw,34px);line-height:1.1;margin:9px 0 4px;"
        "overflow-wrap:anywhere}.kpi small{display:block}"
        ".span-7{grid-column:span 7}.span-5{grid-column:span 5}.span-6{grid-column:span 6}"
        ".full{grid-column:1/-1}.chart{display:block;width:100%;height:auto}.warning{border-color:"
        "#8a5429;background:#2b1b13}.warning h2{color:#fdba74}.warning li{margin:6px 0;color:#fed7aa}"
        "a{color:var(--cyan);text-underline-offset:3px}.section-note{margin:-6px 0 14px;"
        "max-width:980px}.muted{color:var(--muted)}"
        "table{width:100%;border-collapse:collapse;min-width:820px}th,td{text-align:left;"
        "padding:11px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);"
        "font-size:11px;letter-spacing:.08em;text-transform:uppercase}.table-wrap{overflow:auto}"
        ".pill{display:inline-block;border:1px solid #42606e;border-radius:999px;padding:3px 8px;"
        "font-size:11px}.pill.diagnostic{border-color:#76523a;color:#fdba74}.empty{text-align:center;"
        "color:var(--muted);padding:28px}.reel-table-wrap,.facebook-table-wrap{"
        "max-height:760px;overflow:auto;"
        "border:1px solid var(--line);border-radius:14px}.day-table-wrap{max-height:520px;"
        "overflow:auto;border:1px solid var(--line);border-radius:14px}.evidence-table{"
        "min-width:3160px;font-size:13px}.facebook-evidence-table{min-width:2860px}"
        ".day-table{min-width:1900px}.evidence-table th,"
        ".day-table th{position:sticky;top:0;z-index:3;background:#162630}.evidence-table "
        ".sticky-col{position:sticky;left:0;z-index:2;background:#101c25;min-width:270px;"
        "max-width:320px}.evidence-table th.sticky-col{z-index:4;background:#162630}"
        ".evidence-table tr:hover td{background:#13242e}.evidence-table tr:hover "
        "td.sticky-col{background:#162a35}.metric-main{display:block;color:var(--ink);"
        "font-weight:750;white-space:nowrap}.reel-id{display:flex;justify-content:space-between;"
        "align-items:center;gap:10px}.reel-id strong{font-size:12px}.hook{display:block;"
        "margin-top:7px;line-height:1.35;white-space:normal}.reel-links{display:flex;gap:10px;"
        "flex-wrap:wrap;min-width:260px}.evidence-table td:nth-child(n+4):nth-child(-n+22){"
        "white-space:nowrap}"
        ".sort-button{appearance:none;border:0;background:transparent;color:inherit;font:inherit;"
        "letter-spacing:inherit;text-transform:inherit;display:flex;align-items:center;"
        "justify-content:space-between;gap:8px;width:100%;padding:0;cursor:pointer;text-align:left}"
        ".sort-button:hover{color:var(--ink)}.sort-button:focus-visible{outline:2px solid "
        "var(--cyan);outline-offset:4px;border-radius:3px}.sort-indicator{color:#64748b;"
        "font-size:13px;line-height:1}th[aria-sort=ascending] .sort-indicator,"
        "th[aria-sort=descending] .sort-indicator{color:var(--lime)}.sr-only{position:absolute;"
        "width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);"
        "white-space:nowrap;border:0}"
        ".svg-title{fill:#edf7f7;font-size:14px;font-weight:700}.svg-axis,.svg-muted,"
        ".svg-legend{fill:#93a9b4;font-size:10px}.svg-value{fill:#dff5f2;font-size:11px;"
        "font-weight:700}.svg-badge{fill:#07131a;font-size:8px;font-weight:900}"
        ".platform-divider{display:flex;align-items:center;gap:14px;flex-wrap:wrap;"
        "border-color:#315e70;background:linear-gradient(120deg,#102a35,#101c25)}"
        ".platform-divider h2{font-size:27px;margin:0}.platform-divider p{width:100%;"
        "max-width:940px;margin:0}.facebook-kpi{border-color:#315e70}"
        ".facebook-gap-panel li{margin:6px 0;color:var(--muted)}"
        "@media(max-width:900px){header{align-items:start;flex-direction:column}.kpis{"
        "grid-template-columns:repeat(2,1fr)}.span-7,.span-5,.span-6{grid-column:1/-1}}"
        "@media(max-width:520px){main{width:min(100% - 20px,1240px);padding-top:22px}"
        ".kpis{grid-template-columns:1fr}.panel{padding:13px}}"
        "</style></head><body>"
        '<main id="moneyball-dashboard" data-testid="moneyball-dashboard">'
        "<header><div><span class=\"eyebrow\">AI Brief · Moneyball analytics</span>"
        f"<h1>{_html_text(metadata.get('account'))}</h1>"
        f"<p>Generated {_html_text(generated)} · evidence over spectacle</p></div>"
        f'<div class="status">Account growth: <strong>{_html_text(growth.get("status"))}</strong></div>'
        "</header><div class=\"grid\">"
        '<section id="account-growth-kpis" data-testid="account-growth-kpis" '
        f'class="kpis">{cards}</section>'
        '<section class="panel span-7"><h2>Daily flows</h2>'
        f"{_daily_flow_svg(daily)}</section>"
        '<section class="panel span-5"><h2>Follower stock</h2>'
        f"{_stock_svg(stock_rows)}</section>"
        '<section id="publication-day-cohorts" data-testid="publication-day-cohorts" '
        'class="panel full"><h2>Publication-day follower context</h2>'
        '<p class="section-note">Each row uses an exact UTC account-insight interval. '
        "Account-wide follows are shown against both account reach and Graph's "
        "REEL-filtered reach for that day. Reel reach includes older Reels viewed "
        "during the interval; linked newly published Reels are timing context only "
        "and do not receive attributed follows. Select any column heading to sort; "
        "select it again to reverse the order. Unavailable values stay last.</p>"
        f"{_publication_day_table(daily, posts)}</section>"
        '<section class="panel span-6"><h2>Snapshot maturity</h2>'
        f"{_maturity_coverage_svg(coverage)}</section>"
        '<section class="panel span-6"><h2>Distribution vs intent</h2>'
        f"{_reach_intent_svg(posts)}</section>"
        '<section id="per-reel-evidence" data-testid="per-reel-evidence" '
        'class="panel full"><h2>Per-Reel evidence table</h2>'
        '<p class="section-note">Latest lifetime snapshot for each Reel, with actual '
        "age shown. Action rates use Reel reach and are displayed per 1,000 reached "
        "accounts. Fixed-window comparisons remain separate elsewhere in the report. "
        "Select any column heading to sort; select it again to reverse the order. "
        "Unavailable values stay last.</p>"
        f"{_per_reel_table(posts)}</section>"
        f"{facebook_html}"
        '<section class="panel full"><h2>Funnel diagnostics</h2>'
        f"{_funnel_svg(funnel)}</section>"
        '<section id="content-roster" data-testid="content-roster" class="panel full">'
        "<h2>Content roster</h2><div class=\"table-wrap\"><table><thead><tr>"
        "<th>Series</th><th>n</th><th>Reach</th><th>Watch depth</th>"
        "<th>Shares/1k</th><th>Saves/1k</th><th>Recommendation</th><th>Evidence</th>"
        f"</tr></thead><tbody>{''.join(roster_rows)}</tbody></table></div></section>"
        '<aside id="attribution-warning" data-testid="attribution-warning" '
        f'class="panel warning full"><h2>Attribution boundary</h2><ul>{warning_html}</ul>'
        "<p>Publication counts beside daily flows are time-overlap context only. "
        "No account follows are divided among posts.</p></aside>"
        f"</div></main>{SORTABLE_TABLE_SCRIPT}</body></html>\n"
    )


def atomic_write_text(path: Path, text: str) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def write_moneyball_outputs(
    report: Mapping[str, Any],
    *,
    markdown_path: Path,
    json_path: Path,
    csv_path: Path,
    audit_path: Path,
    html_path: Path | None = None,
    facebook_csv_path: Path | None = None,
) -> None:
    json_text = json.dumps(
        report,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    )
    atomic_write_text(json_path, json_text + "\n")
    atomic_write_text(markdown_path, render_moneyball_markdown(report))
    atomic_write_text(csv_path, render_moneyball_csv(report))
    if facebook_csv_path is not None:
        platforms = report.get("platform_analytics")
        platforms = platforms if isinstance(platforms, Mapping) else {}
        facebook = platforms.get("facebook")
        facebook = facebook if isinstance(facebook, Mapping) else {}
        if facebook.get("status") in {"AVAILABLE", "NO_PUBLISHED_POSTS"}:
            atomic_write_text(
                facebook_csv_path,
                render_facebook_moneyball_csv(report),
            )
    atomic_write_text(audit_path, render_data_audit_markdown(report))
    if html_path is not None:
        atomic_write_text(html_path, render_moneyball_html(report))
