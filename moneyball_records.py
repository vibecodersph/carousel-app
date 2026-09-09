"""Account-scoped records from real, comparable Moneyball observations.

No database or filesystem writes occur here. A caller may persist
``current_records`` and ``observations`` and supply them as ``previous_history``
on the next run. All-time means all eligible observations in the tracked ledger,
not a complete account history or a cross-account Instagram record.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from moneyball_analytics import (
    PERFORMANCE_RANKING_METRICS,
    compute_post_metrics,
    isoformat_seconds,
    metric_value,
    numeric,
    parse_datetime,
    percentile_rank,
)


WINDOW_BOUNDS = {"24h": (24.0, 28.0), "72h": (72.0, 96.0), "7d": (168.0, 192.0)}
LIFETIME_METRICS = tuple(
    {
        "key": key, "metric": key, "label": key.title(), "short_label": key.title(),
        "direction": "higher", "format": "count", "source": f"Instagram {key}",
    }
    for key in ("reach",)
)
RECORD_METRICS = PERFORMANCE_RANKING_METRICS
RATE_ELIGIBILITY = {
    "three_second_skip_rate": {"base_metric": "reach", "minimum_base": 100},
    "views_per_reached_account": {"base_metric": "reach", "minimum_base": 100},
    "saves_per_reach": {
        "base_metric": "reach", "minimum_base": 100, "count_metric": "saves", "minimum_count": 5,
    },
    "shares_per_view": {
        "base_metric": "views", "minimum_base": 100, "count_metric": "shares", "minimum_count": 5,
    },
}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                   allow_nan=False).encode("utf-8")
    ).hexdigest()


def _identity(post: Mapping[str, Any]) -> dict[str, Any]:
    identity = _mapping(post.get("identity"))
    metadata = _mapping(post.get("content_metadata"))
    media_id = str(identity.get("media_id") or "")
    return {
        "media_id": media_id,
        "permalink": identity.get("permalink"),
        "published_at": identity.get("published_at"),
        "title": metadata.get("hook_text") or identity.get("caption") or media_id,
        "series": metadata.get("series"),
        "hook_text": metadata.get("hook_text"),
    }


def _window_bounds(report: Mapping[str, Any], window: str) -> tuple[float, float]:
    default_start, default_end = WINDOW_BOUNDS[window]
    target = _mapping(_mapping(_mapping(report.get("maturity_windows")).get(window)).get("target"))
    start = numeric(target.get("target_hours"))
    tolerance = numeric(target.get("max_hours_after_target"))
    if start is not None and tolerance is not None and start >= 0 and tolerance >= 0:
        return float(start), float(start + tolerance)
    return default_start, default_end


def _ranking_policy(report: Mapping[str, Any]) -> dict[str, Any]:
    """Version the full comparison rules so history can baseline policy changes."""
    metrics = []
    for specification in RECORD_METRICS:
        key = specification["key"]
        eligibility = {"minimum_value": 0, **RATE_ELIGIBILITY.get(key, {})}
        if key == "three_second_skip_rate":
            eligibility["maximum_value"] = 100
        metrics.append({
            "key": key, "metric": specification["metric"],
            "direction": specification["direction"], "eligibility": eligibility,
        })
    policy = {
        "version": 2,
        "metrics": metrics,
        "lifetime_metrics": [specification["key"] for specification in LIFETIME_METRICS],
        "fixed_windows": {
            window: {"minimum_age_hours": _window_bounds(report, window)[0],
                     "maximum_age_hours": _window_bounds(report, window)[1]}
            for window in WINDOW_BOUNDS
        },
        "rank_method": "competition ties: 1, 1, 3",
        "percentile_method": "directional midrank among the named cohort; higher is stronger",
    }
    return {**policy, "signature": _fingerprint(policy)}


def _observation(
    post: Mapping[str, Any], window: str, report: Mapping[str, Any], as_of: Any,
) -> dict[str, Any] | None:
    source_window = "latest" if window == "lifetime" else window
    candidate = _mapping(_mapping(post.get("maturity_windows")).get(source_window))
    if not candidate or candidate.get("maturity_window", source_window) != source_window:
        return None
    captured = parse_datetime(candidate.get("captured_at"))
    published = parse_datetime(_mapping(post.get("identity")).get("published_at"))
    if captured is None or published is None or captured > as_of or captured < published:
        return None
    provenance = _mapping(candidate.get("metric_provenance"))
    if provenance.get("source_platform", "instagram") != "instagram":
        return None
    age = (captured - published).total_seconds() / 3600
    if window != "lifetime":
        low, high = _window_bounds(report, window)
        if not low <= age <= high:
            return None
    raw = {key: numeric(value) for key, value in _mapping(candidate.get("raw_metrics")).items()}
    derived = _mapping(candidate.get("derived_metrics"))
    metrics = compute_post_metrics(
        raw, _mapping(post.get("content_metadata")),
        plays_semantics_verified=(
            derived.get("average_watch_time_source") == "total_watch_time_seconds / verified_plays"
        ),
    )
    return {
        **_identity(post), "actual_age_hours": age,
        "captured_at": isoformat_seconds(captured), "raw_metrics": raw,
        "derived_metrics": metrics,
    }


def _metric_row(observation: Mapping[str, Any], specification: Mapping[str, Any]) -> dict[str, Any] | None:
    key = specification["key"]
    raw = _mapping(observation.get("raw_metrics"))
    value = numeric(metric_value(observation, specification["metric"]))
    if value is None or value < 0:
        return None
    if key == "three_second_skip_rate" and value > 100:
        return None
    supporting_keys = {
        "three_second_skip_rate": ("reels_skip_rate", "reach"),
        "views_per_reached_account": ("views", "reach"),
        "saves_per_reach": ("saves", "reach"),
        "shares_per_view": ("shares", "views"),
        "reach": ("reach",),
        "views": ("views",),
    }
    supporting = {field: raw.get(field) for field in supporting_keys.get(key, ())}
    denominator = {
        "views_per_reached_account": "reach", "saves_per_reach": "reach", "shares_per_view": "views",
    }.get(key)
    if denominator:
        supporting["denominator_type"] = denominator
    flags = []
    rule = RATE_ELIGIBILITY.get(key)
    if rule:
        base = numeric(raw.get(rule["base_metric"]))
        supporting["eligibility_base"] = rule["base_metric"]
        if base is None:
            flags.append("MISSING_BASE")
        elif base < rule["minimum_base"]:
            flags.append("LOW_BASE")
        count_key = rule.get("count_metric")
        if count_key:
            count = numeric(raw.get(count_key))
            if count is None:
                flags.append("MISSING_COUNT")
            elif count < rule["minimum_count"]:
                flags.append("LOW_COUNT")
    return {
        key: value for key, value in observation.items() if key != "derived_metrics"
    } | {
        "value": float(value), "eligible": not flags, "eligibility_flags": flags,
        "supporting_metrics": supporting,
    }


def _rank(rows: list[dict[str, Any]], direction: str, *, measured: bool) -> None:
    prefix = "measured_" if measured else ""
    values = [row["value"] for row in rows]
    rank, previous_value = 0, None
    for position, row in enumerate(rows, 1):
        if previous_value is None or row["value"] != previous_value:
            rank = position
        previous_value = row["value"]
        percentile = percentile_rank(values, row["value"])
        row[f"{prefix}rank"] = rank
        row[f"{prefix}cohort_size"] = len(rows)
        row[f"{prefix}directional_percentile"] = (
            100.0 - percentile if direction == "lower" else percentile
        )


def _board(observations: list[dict[str, Any]], specification: Mapping[str, Any]) -> dict[str, Any]:
    rows = [row for observation in observations if (row := _metric_row(observation, specification)) is not None]
    direction = specification["direction"]
    rows.sort(key=lambda row: (
        -row["value"] if direction == "higher" else row["value"], row["media_id"],
    ))
    eligible = [row for row in rows if row["eligible"]]
    _rank(rows, direction, measured=True)
    for row in rows:
        row.update(rank=None, cohort_size=len(eligible), directional_percentile=None)
    _rank(eligible, direction, measured=False)
    leaders = [row for row in eligible if row["rank"] == 1]
    return {
        **dict(specification), "source_metric": specification["metric"],
        "measured_count": len(rows), "eligible_count": len(eligible),
        "unavailable_count": len(observations) - len(rows),
        "rows": rows, "top_10": eligible[:10], "record_leaders": leaders,
    }


def _record_events(
    current: Mapping[str, Any], observations: Mapping[str, str], previous: Mapping[str, Any], as_of: str,
) -> list[dict[str, Any]]:
    prior_records = _mapping(previous.get("current_records"))
    prior_observations = _mapping(previous.get("observations"))
    prior_time = parse_datetime(previous.get("last_recorded_at") or previous.get("as_of"))
    events = []
    for record_key, record in current.items():
        prior = _mapping(prior_records.get(record_key))
        if not record["leaders"] and not prior.get("leaders"):
            continue
        old_value = numeric(prior.get("value"))
        value = record["value"]
        leaders, previous_leaders = record["leaders"], prior.get("leaders", [])
        new_ids = {row["media_id"] for row in leaders}
        old_ids = {row["media_id"] for row in previous_leaders}
        delta = value - old_value if old_value is not None and value is not None else None
        improvement = (-delta if record["direction"] == "lower" else delta) if delta is not None else None
        changed_existing = [row for row in leaders if (
            (observation_key := f"{record['window']}:{row['media_id']}") in prior_observations
            and observations.get(observation_key) != prior_observations[observation_key]
        )]
        if not leaders:
            status = "RECORD_REVISED"
        elif old_value is None:
            status = "BASELINE_ESTABLISHED"
        elif improvement == 0:
            if new_ids - old_ids:
                status = "RECORD_TIED"
            elif old_ids - new_ids:
                status = "RECORD_REVISED"
            else:
                continue
        elif improvement < 0:
            status = "RECORD_REVISED"
        elif record["window"] != "lifetime" and (changed_existing or new_ids & old_ids):
            status = "RECORD_REVISED"
        elif prior_time is not None and all(
            parse_datetime(row["captured_at"]) <= prior_time for row in leaders
        ):
            status = "HISTORICAL_RECORD_DISCOVERED"
        else:
            status = "NEW_RECORD"
        event = {
            "status": status, "window": record["window"], "metric_key": record["metric_key"],
            "label": record["label"], "direction": record["direction"], "format": record["format"],
            "detected_at": as_of, "previous_value": old_value, "value": value,
            "delta": delta, "improvement_percent": (
                improvement / abs(old_value) * 100 if old_value and improvement is not None else None
            ),
            "previous_leaders": previous_leaders, "leaders": leaders,
        }
        event["event_id"] = _fingerprint({
            "record": record_key, "status": status, "detected_at": as_of,
            "before": {"value": old_value, "ids": sorted(old_ids)},
            "after": {"value": value, "ids": sorted(new_ids),
                      "captures": [row["captured_at"] for row in leaders]},
        })
        events.append(event)
    return events


def build_account_records(
    report: Mapping[str, Any], *, previous_history: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Rank all tracked history and compare eligible champions with saved history.

    Eligible ranks use competition ties (1, 1, 3). Percentiles use the same
    directional midrank method as Moneyball's rolling rankings. Raw measurable
    standings remain visible even when a low base or action count prevents a
    confident record. Missing checkpoints are never substituted with latest.
    """
    metadata = _mapping(report.get("report_metadata"))
    account = str(metadata.get("account") or "")
    as_of_dt = parse_datetime(metadata.get("as_of"))
    if not account or as_of_dt is None:
        raise ValueError("Account records require report account and valid as_of")
    as_of = isoformat_seconds(as_of_dt)
    if previous_history is not None:
        if not isinstance(previous_history, Mapping):
            raise ValueError("Account record history must be a mapping")
        if previous_history.get("account") != account or previous_history.get("platform") != "instagram":
            raise ValueError("Account record history account/platform mismatch")
        prior_time = parse_datetime(previous_history.get("last_recorded_at") or previous_history.get("as_of"))
        if prior_time is not None and as_of_dt < prior_time:
            raise ValueError("Cannot compare account records against future history")
    posts, seen = [], set()
    for post in report.get("posts", []):
        identity = _mapping(post.get("identity"))
        if identity.get("platform", "instagram") != "instagram":
            continue
        if identity.get("account", account) != account:
            raise ValueError("Post account does not match report account")
        media_id = str(identity.get("media_id") or "")
        if not media_id:
            continue
        if media_id in seen:
            raise ValueError(f"Duplicate Instagram media_id: {media_id}")
        seen.add(media_id)
        published = parse_datetime(identity.get("published_at"))
        if published is not None and published <= as_of_dt:
            posts.append(post)
    posts.sort(key=lambda post: str(_mapping(post.get("identity")).get("media_id")))
    windows, current_records, fingerprints = {}, {}, {}
    for window in (*WINDOW_BOUNDS, "lifetime"):
        observations = [
            observation for post in posts
            if (observation := _observation(post, window, report, as_of_dt)) is not None
        ]
        for observation in observations:
            fingerprints[f"{window}:{observation['media_id']}"] = _fingerprint({
                key: observation[key] for key in ("captured_at", "actual_age_hours", "raw_metrics")
            })
        boards = {}
        for specification in (LIFETIME_METRICS if window == "lifetime" else RECORD_METRICS):
            key = specification["key"]
            board = _board(observations, specification)
            boards[key] = board
            current_records[f"{window}:{key}"] = {
                "window": window, "metric_key": key, "label": specification["label"],
                "direction": specification["direction"], "format": specification["format"],
                "value": board["record_leaders"][0]["value"] if board["record_leaders"] else None,
                "leaders": board["record_leaders"],
            }
        windows[window] = {
            "cohort_size": len(observations), "missing_checkpoint_count": len(posts) - len(observations),
            "metric_rankings": boards,
        }
    start, end = as_of_dt - timedelta(hours=100), as_of_dt - timedelta(hours=28)
    fresh = []
    by_metric = {
        key: {row["media_id"]: row for row in board["rows"]}
        for key, board in windows["24h"]["metric_rankings"].items()
    }
    for post in posts:
        published = parse_datetime(_mapping(post.get("identity")).get("published_at"))
        if not start < published <= end:
            continue
        item = {**_identity(post), "metrics": {}}
        for key, lookup in by_metric.items():
            row = lookup.get(item["media_id"])
            if row is None:
                continue
            record = current_records[f"24h:{key}"]
            difference = record["value"] - row["value"] if record["value"] is not None else None
            item["metrics"][key] = {
                **row, "record_value": record["value"], "record_leaders": record["leaders"],
                "gap_to_record": (-difference if record["direction"] == "lower" else difference)
                if difference is not None else None,
            }
        item["has_24h_checkpoint"] = f"24h:{item['media_id']}" in fingerprints
        fresh.append(item)
    fresh.sort(key=lambda item: (item["published_at"], item["media_id"]), reverse=True)
    dates = [parse_datetime(_mapping(post.get("identity")).get("published_at")) for post in posts]
    ranking_policy = _ranking_policy(report)
    return {
        "schema_version": 1, "account": account, "platform": "instagram", "as_of": as_of,
        "status": "BASELINE_ESTABLISHED" if previous_history is None else "UPDATED",
        "scope": "Best among this account's tracked, eligible Instagram observations; no cross-account records.",
        "tracked_post_count": len(posts), "history_start": isoformat_seconds(min(dates)) if dates else None,
        "ranking_policy": ranking_policy,
        "methodology": {
            "fixed_windows": ranking_policy["fixed_windows"],
            "minimum_rate_reach": 100, "minimum_rate_views": 100,
            "minimum_saves": 5, "minimum_shares": 5,
            "eligibility_rules": {key: dict(value) for key, value in RATE_ELIGIBILITY.items()},
            "rank_method": ranking_policy["rank_method"],
            "percentile_method": ranking_policy["percentile_method"],
            "lifetime_note": "Latest recorded Instagram reach; unequal ages, separate from fixed-window records. Raw views accompany the reach-selected leader as context and are never ranked separately.",
        },
        "windows": {window: windows[window] for window in WINDOW_BOUNDS},
        "lifetime": windows["lifetime"],
        "fresh": {
            "start_exclusive": isoformat_seconds(start), "end_inclusive": isoformat_seconds(end),
            "missing_checkpoint_count": sum(not item["has_24h_checkpoint"] for item in fresh),
            "posts": fresh,
        },
        "current_records": current_records, "observations": fingerprints,
        "events": _record_events(current_records, fingerprints, previous_history, as_of)
        if previous_history is not None else [],
    }
