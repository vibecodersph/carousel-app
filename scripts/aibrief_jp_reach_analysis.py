#!/usr/bin/env python3
"""Deterministic, read-only reach analysis for the AI Brief JP Reel account.

The module deliberately keeps the Instagram/base ``views`` metric separate from
Meta's cross-surface ``total_views`` metric.  It reads snapshots without
mutating the ledger and writes only the two explicitly requested output files.
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import random
import sqlite3
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import quote
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
CHANNEL = "aibrief_jp"
JST = ZoneInfo("Asia/Tokyo")
SLOTS = ("09", "13", "18", "21")
SLOT_TARGET_MINUTES = {"09": 9 * 60, "13": 13 * 60, "18": 18 * 60, "21": 21 * 60}
MIN_COVERAGE = 0.90
MIN_MATCHED_DATES = 8
PERMUTATIONS = 10_000
PERMUTATION_SEED = 20_260_713
GROWTH_PERMUTATION_SEED = 20_260_715
MATRIX_RANDOMIZATION_SEED = 20_260_714
DEFAULT_VARIANT_A = "CURRENT_TRANSLATED_CLIP"
DEFAULT_VARIANT_B = "EDITORIAL_TRANSFORM"
DIAGNOSTIC_METRICS = (
    "ig_reels_video_view_total_time",
    "ig_reels_avg_watch_time",
    "reels_skip_rate",
    "clips_replays_count",
    "facebook_views",
    "crossposted_views",
    "follows",
)

OBSERVATIONAL_CAVEATS = (
    "This analysis is observational. Post-level outcomes do not establish that a hook, "
    "topic, treatment, or posting slot caused reach or engagement.",
    "Engagement is conditional on the reached cohort. A high rate on limited reach is a "
    "retest signal, not proof of broad audience fit or algorithmic suppression.",
    "Watch time and first-three-second skip rate are diagnostics. The average-watch-time "
    "to estimated-duration ratio is not a completion rate or a retention curve.",
    "Meta all-surface and explicit Instagram-plus-Facebook crossposted views describe "
    "measured distribution surfaces; they do not identify the recommendation mechanism "
    "that produced them.",
)

CLASSIFICATION_LANGUAGE = {
    "COMPLETE_WINNER": (
        "Observed high Instagram distribution and high engagement density; replicate as "
        "a controlled retest, not as proof that the treatment caused the result."
    ),
    "DISTRIBUTION_WINNER": (
        "Observed high Instagram distribution without the engagement-density signal; "
        "this is descriptive and does not diagnose the CTA or payoff."
    ),
    "AUDIENCE_FIT_WINNER": (
        "High-engagement/limited-distribution retest signal; not proof of broad audience "
        "fit, suppressed distribution, or a causal hook effect."
    ),
    "NO_WINNER": (
        "The post did not cross the frozen observational thresholds; this does not prove "
        "that its hook, topic, or slot caused the result."
    ),
    "MONITOR_EARLY": "Under 24 hours; monitor only and make no performance inference.",
    "DATA_HOLD": "Required data is incomplete or invalid; make no performance inference.",
}

PERFORMANCE_BANDS: dict[str, tuple[float, float, float]] = {
    "combined_views": (164, 398, 750),
    "base_views": (151, 192, 250),
    "reach": (125, 150, 185),
    "total_interactions": (1, 4, 7),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build deterministic aibrief_jp reach classifications and slot analysis"
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=ROOT / "out" / "reel_report.insights.json",
    )
    parser.add_argument(
        "--source-report-label",
        type=Path,
        default=None,
        help=(
            "Durable report path to record in output provenance when --report is "
            "a temporary staged copy"
        ),
    )
    parser.add_argument("--db", type=Path, default=ROOT / "state" / "reels.db")
    parser.add_argument(
        "--json-out",
        type=Path,
        default=ROOT / "out" / "aibrief_jp_reach_analysis.json",
    )
    parser.add_argument(
        "--markdown-out",
        type=Path,
        default=ROOT / "out" / "aibrief_jp_reach_analysis.md",
    )
    parser.add_argument(
        "--matrix-start-date",
        help="First JST date for the non-mutating 14-day assignment matrix (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--matrix-seed",
        type=int,
        default=MATRIX_RANDOMIZATION_SEED,
        help="Deterministic seed for the constrained randomized assignment matrix",
    )
    parser.add_argument("--variant-a", default=DEFAULT_VARIANT_A)
    parser.add_argument("--variant-b", default=DEFAULT_VARIANT_B)
    return parser


def parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def numeric(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return int(value) if float(value).is_integer() else float(value)
    return None


def raw_metric_map(payload: Any) -> dict[str, int | float]:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return {}
    if not isinstance(payload, Mapping):
        return {}
    data = payload.get("data")
    if not isinstance(data, list):
        return {}
    result: dict[str, int | float] = {}
    for item in data:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        value = None
        values = item.get("values")
        if isinstance(values, list) and values:
            latest = values[-1]
            if isinstance(latest, Mapping):
                value = numeric(latest.get("value"))
        if value is None:
            total_value = item.get("total_value")
            if isinstance(total_value, Mapping):
                value = numeric(total_value.get("value"))
        if value is not None:
            result[name] = value
    return result


def _snapshot_from_report(item: Mapping[str, Any]) -> dict[str, Any] | None:
    insights = item.get("insights")
    if not isinstance(insights, Mapping) or not insights.get("has_snapshot"):
        return None
    metrics = insights.get("metrics")
    return {
        "id": -1,
        "captured_at": str(insights.get("captured_at") or ""),
        "columns": dict(metrics) if isinstance(metrics, Mapping) else {},
        "raw_api_payload": insights.get("raw_api_payload"),
        "source": "report",
    }


def load_report(path: Path) -> dict[str, Any]:
    payload = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("report JSON root must be an object")
    items = payload.get("items")
    if not isinstance(items, list):
        raise ValueError("report JSON items must be a list")
    return payload


def _readonly_connection(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"ledger database not found: {resolved}")
    # A normal read-only connection sees committed WAL frames while preventing
    # both database creation and writes; query_only adds a second guardrail.
    uri = f"file:{quote(str(resolved), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def load_reels(report: Mapping[str, Any], db_path: Path) -> list[dict[str, Any]]:
    report_items = [
        item
        for item in report.get("items", [])
        if isinstance(item, Mapping) and item.get("channel_id") == CHANNEL
    ]
    report_by_media = {
        str(item.get("media_id") or ""): item
        for item in report_items
        if str(item.get("media_id") or "")
    }
    report_by_hash = {
        str(item.get("content_hash") or ""): item
        for item in report_items
        if str(item.get("content_hash") or "")
    }

    with _readonly_connection(db_path) as connection:
        insight_schema = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(insights)").fetchall()
        }
        optional_insight_columns = [
            name
            for name in (
                "total_views",
                "total_likes",
                "total_comments",
                *DIAGNOSTIC_METRICS,
            )
            if name in insight_schema
        ]
        reel_rows = connection.execute(
            """
            SELECT content_hash, channel_id, title, published_at, scheduled_at,
                   media_id, permalink, status
            FROM reels
            WHERE channel_id=? AND status='published'
            ORDER BY published_at, content_hash
            """,
            (CHANNEL,),
        ).fetchall()
        optional_select = "".join(f", {name}" for name in optional_insight_columns)
        insight_rows = connection.execute(
            f"""
            SELECT id, content_hash, channel_id, media_id, captured_at,
                   views, reach, likes, comments, saved, shares,
                   total_interactions, raw{optional_select}
            FROM insights
            WHERE channel_id=?
            ORDER BY captured_at, id
            """,
            (CHANNEL,),
        ).fetchall()

    snapshots_by_hash: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in insight_rows:
        snapshots_by_hash[str(row["content_hash"] or "")].append(
            {
                "id": int(row["id"]),
                "captured_at": str(row["captured_at"] or ""),
                "columns": {
                    "views": row["views"],
                    "reach": row["reach"],
                    "likes": row["likes"],
                    "comments": row["comments"],
                    "saved": row["saved"],
                    "shares": row["shares"],
                    "total_interactions": row["total_interactions"],
                    **{name: row[name] for name in optional_insight_columns},
                },
                "raw_api_payload": row["raw"],
                "source": "sqlite",
            }
        )

    records: list[dict[str, Any]] = []
    consumed_report_keys: set[tuple[str, str]] = set()
    for row in reel_rows:
        media_id = str(row["media_id"] or "")
        content_hash = str(row["content_hash"] or "")
        report_item = report_by_media.get(media_id) or report_by_hash.get(content_hash) or {}
        report_segment = report_item.get("segment")
        report_segment = report_segment if isinstance(report_segment, Mapping) else {}
        consumed_report_keys.add((media_id, content_hash))
        snapshots = list(snapshots_by_hash.get(content_hash, []))
        report_snapshot = _snapshot_from_report(report_item)
        if report_snapshot and report_snapshot["captured_at"] not in {
            snapshot["captured_at"] for snapshot in snapshots
        }:
            snapshots.append(report_snapshot)
        records.append(
            {
                "content_hash": content_hash,
                "media_id": media_id,
                "title": str(row["title"] or report_item.get("title") or ""),
                "published_at": str(row["published_at"] or report_item.get("published_at") or ""),
                "scheduled_at": str(row["scheduled_at"] or report_item.get("scheduled_at") or ""),
                "permalink": str(row["permalink"] or report_item.get("permalink") or ""),
                "duration_seconds": numeric(report_segment.get("duration")),
                "reel_transcript": str(report_segment.get("reel_transcript") or "").strip(),
                "snapshots": snapshots,
            }
        )

    # A validated report should be a subset of the ledger.  Keeping report-only
    # rows makes failures explicit rather than silently dropping data.
    for item in report_items:
        media_id = str(item.get("media_id") or "")
        content_hash = str(item.get("content_hash") or "")
        if (media_id, content_hash) in consumed_report_keys:
            continue
        snapshot = _snapshot_from_report(item)
        report_segment = item.get("segment")
        report_segment = report_segment if isinstance(report_segment, Mapping) else {}
        records.append(
            {
                "content_hash": content_hash,
                "media_id": media_id,
                "title": str(item.get("title") or ""),
                "published_at": str(item.get("published_at") or ""),
                "scheduled_at": str(item.get("scheduled_at") or ""),
                "permalink": str(item.get("permalink") or ""),
                "duration_seconds": numeric(report_segment.get("duration")),
                "reel_transcript": str(report_segment.get("reel_transcript") or "").strip(),
                "snapshots": [snapshot] if snapshot else [],
                "report_only": True,
            }
        )
    return records


def select_snapshot(
    snapshots: Sequence[Mapping[str, Any]], published_at: datetime | None
) -> tuple[dict[str, Any] | None, float | None, list[str]]:
    warnings: list[str] = []
    candidates: list[tuple[float | None, datetime, int, dict[str, Any]]] = []
    for snapshot in snapshots:
        captured_at = parse_datetime(snapshot.get("captured_at"))
        if captured_at is None:
            continue
        age = (
            (captured_at - published_at).total_seconds() / 3600
            if published_at is not None
            else None
        )
        candidates.append((age, captured_at, int(snapshot.get("id") or -1), dict(snapshot)))
    if not candidates:
        return None, None, warnings
    candidates.sort(key=lambda item: (item[1], item[2]))
    if published_at is None:
        selected = candidates[-1]
        return selected[3], None, warnings

    preferred = [item for item in candidates if item[0] is not None and 72 <= item[0] <= 96]
    if preferred:
        selected = min(preferred, key=lambda item: (float(item[0]), item[1], item[2]))
        return selected[3], float(selected[0]), warnings
    mature = [item for item in candidates if item[0] is not None and item[0] >= 72]
    if mature:
        selected = min(mature, key=lambda item: (float(item[0]), item[1], item[2]))
        if float(selected[0]) > 96:
            warnings.append("LATE_SNAPSHOT")
        return selected[3], float(selected[0]), warnings
    selected = max(candidates, key=lambda item: (float(item[0] or -math.inf), item[1], item[2]))
    return selected[3], float(selected[0]) if selected[0] is not None else None, warnings


def _first_snapshot_in_age_window(
    snapshots: Sequence[Mapping[str, Any]],
    published_at: datetime,
    minimum_age_hours: float,
    maximum_age_hours: float,
) -> tuple[dict[str, Any] | None, float | None]:
    candidates: list[tuple[float, datetime, int, dict[str, Any]]] = []
    for snapshot in snapshots:
        captured_at = parse_datetime(snapshot.get("captured_at"))
        if captured_at is None:
            continue
        age_hours = (captured_at - published_at).total_seconds() / 3600
        if minimum_age_hours <= age_hours <= maximum_age_hours:
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


def latest_inventory_summary(
    reels: Sequence[Mapping[str, Any]],
    *,
    reference_time: datetime | None = None,
    freshness_hours: int = 13,
) -> dict[str, Any]:
    """Count current metrics from each reel's newest snapshot without mixing scopes."""
    latest_rows: list[dict[str, Any]] = []
    for reel in reels:
        snapshots = reel.get("snapshots")
        snapshots = snapshots if isinstance(snapshots, Sequence) else []
        candidates: list[tuple[datetime, int, Mapping[str, Any]]] = []
        for snapshot in snapshots:
            if not isinstance(snapshot, Mapping):
                continue
            captured_at = parse_datetime(snapshot.get("captured_at"))
            if captured_at is None:
                continue
            candidates.append(
                (captured_at, int(snapshot.get("id") or -1), snapshot)
            )
        if not candidates:
            continue
        captured_at, _, snapshot = max(
            candidates, key=lambda candidate: (candidate[0], candidate[1])
        )
        raw = raw_metric_map(snapshot.get("raw_api_payload"))
        columns = snapshot.get("columns")
        columns = columns if isinstance(columns, Mapping) else {}

        def raw_or_column(name: str) -> int | float | None:
            return raw.get(name, numeric(columns.get(name)))

        # Older ledgers used columns.views for an all-surface total. It is a safe
        # Instagram/base fallback only when a separate total_views column proves
        # that this is the newer split schema.
        base_views = raw.get("views")
        if base_views is None and numeric(columns.get("total_views")) is not None:
            base_views = numeric(columns.get("views"))
        latest_rows.append(
            {
                "captured_at": captured_at,
                "base_views": base_views,
                "total_views": raw.get(
                    "total_views", numeric(columns.get("total_views"))
                ),
                "saved": raw_or_column("saved"),
                "shares": raw_or_column("shares"),
                "total_interactions": raw_or_column("total_interactions"),
                "facebook_views": raw_or_column("facebook_views"),
                "crossposted_views": raw_or_column("crossposted_views"),
            }
        )

    def available(name: str) -> list[int | float]:
        return [
            value
            for row in latest_rows
            if (value := numeric(row.get(name))) is not None
        ]

    base_views = available("base_views")
    total_views = available("total_views")
    saved = available("saved")
    shares = available("shares")
    interactions = available("total_interactions")
    facebook_views = available("facebook_views")
    crossposted_views = available("crossposted_views")
    captures = sorted(row["captured_at"] for row in latest_rows)
    freshness_reference = reference_time or (captures[-1] if captures else None)
    freshness_cutoff = (
        freshness_reference - timedelta(hours=freshness_hours)
        if freshness_reference is not None
        else None
    )
    fresh_n = (
        sum(row["captured_at"] >= freshness_cutoff for row in latest_rows)
        if freshness_cutoff is not None
        else 0
    )
    transcript_n = sum(bool(str(reel.get("reel_transcript") or "").strip()) for reel in reels)
    return {
        "label": "CURRENT_LATEST_INVENTORY",
        "evidence_type": "DESCRIPTIVE_NEWEST_SNAPSHOT_COUNTS",
        "snapshot_rule": "newest valid captured_at per published reel; snapshot id breaks ties",
        "published_reels": len(reels),
        "synced_n": len(latest_rows),
        "unsynced_n": len(reels) - len(latest_rows),
        "latest_capture_range": {
            "oldest": captures[0].isoformat() if captures else None,
            "newest": captures[-1].isoformat() if captures else None,
        },
        "freshness": {
            "reference_time": (
                freshness_reference.isoformat() if freshness_reference else None
            ),
            "maximum_age_hours": freshness_hours,
            "fresh_n": fresh_n,
            "stale_n": len(latest_rows) - fresh_n,
            "unsynced_n": len(reels) - len(latest_rows),
            "all_synced_rows_fresh": fresh_n == len(latest_rows),
        },
        "transcript_coverage": {
            "available_n": transcript_n,
            "missing_n": len(reels) - transcript_n,
            "published_reels": len(reels),
        },
        "counts": {
            "meta_all_surface_total_views_gte_500": sum(
                value >= 500 for value in total_views
            ),
            "instagram_base_views_gte_500": sum(value >= 500 for value in base_views),
            "ig_facebook_crossposted_views_gte_500": sum(
                value >= 500 for value in crossposted_views
            ),
            "zero_share": sum(value == 0 for value in shares),
            "zero_save": sum(value == 0 for value in saved),
            "total_interactions_gte_7": sum(value >= 7 for value in interactions),
        },
        "availability": {
            "meta_all_surface_total_views_n": len(total_views),
            "instagram_base_views_n": len(base_views),
            "shares_n": len(shares),
            "saved_n": len(saved),
            "total_interactions_n": len(interactions),
            "facebook_views_n": len(facebook_views),
            "crossposted_views_n": len(crossposted_views),
        },
        "scope_guardrail": (
            "Meta all-surface total_views and Instagram/base views are counted "
            "separately and are never added together. crossposted_views explicitly "
            "aggregates Instagram and Facebook plays; facebook_views and "
            "crossposted_views availability is reported, not imputed, and overlapping "
            "view metrics are never summed."
        ),
    }


def _percentile(values: Sequence[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _numeric_summary(values: Sequence[int | float]) -> dict[str, Any]:
    clean = [float(value) for value in values]
    if not clean:
        return {
            "n": 0,
            "min": None,
            "p25": None,
            "median": None,
            "mean": None,
            "p75": None,
            "max": None,
        }
    return {
        "n": len(clean),
        "min": min(clean),
        "p25": _percentile(clean, 0.25),
        "median": median(clean),
        "mean": mean(clean),
        "p75": _percentile(clean, 0.75),
        "max": max(clean),
    }


def _midranks(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda index: values[index])
    result = [0.0] * len(values)
    cursor = 0
    while cursor < len(order):
        end = cursor + 1
        while end < len(order) and values[order[end]] == values[order[cursor]]:
            end += 1
        average_rank = (cursor + end + 1) / 2
        for position in range(cursor, end):
            result[order[position]] = average_rank
        cursor = end
    return result


def _pearson(values_x: Sequence[float], values_y: Sequence[float]) -> float | None:
    if len(values_x) != len(values_y) or len(values_x) < 2:
        return None
    mean_x = mean(values_x)
    mean_y = mean(values_y)
    sum_x = sum((value - mean_x) ** 2 for value in values_x)
    sum_y = sum((value - mean_y) ** 2 for value in values_y)
    denominator = math.sqrt(sum_x * sum_y)
    if denominator == 0:
        return None
    return sum(
        (value_x - mean_x) * (value_y - mean_y)
        for value_x, value_y in zip(values_x, values_y)
    ) / denominator


def _spearman(values_x: Sequence[float], values_y: Sequence[float]) -> float | None:
    return _pearson(_midranks(values_x), _midranks(values_y))


def _permutation_correlation(
    values_x: Sequence[float],
    values_y: Sequence[float],
    *,
    statistic_name: str,
    permutations: int,
    seed: int,
) -> dict[str, Any]:
    statistic = _pearson if statistic_name == "pearson" else _spearman
    observed = statistic(values_x, values_y)
    n = len(values_x)
    if observed is None:
        return {
            "coefficient": None,
            "two_sided_permutation_p": None,
            "status": "CONSTANT_OR_INSUFFICIENT_INPUT",
            "permutation_method": None,
            "permutations": 0,
            "seed": None,
        }

    threshold = abs(observed) - 1e-12
    greater_or_equal = 0
    if n <= 8:
        permutation_count = math.factorial(n)
        for permuted in itertools.permutations(values_y):
            permuted_statistic = statistic(values_x, permuted)
            if permuted_statistic is not None and abs(permuted_statistic) >= threshold:
                greater_or_equal += 1
        p_value = greater_or_equal / permutation_count
        method = "EXACT_ALL_LABEL_PERMUTATIONS"
        result_seed: int | None = None
    else:
        permutation_count = permutations
        rng = random.Random(seed)
        for _ in range(permutation_count):
            permuted = list(values_y)
            rng.shuffle(permuted)
            permuted_statistic = statistic(values_x, permuted)
            if permuted_statistic is not None and abs(permuted_statistic) >= threshold:
                greater_or_equal += 1
        p_value = (greater_or_equal + 1) / (permutation_count + 1)
        method = "FIXED_SEED_MONTE_CARLO"
        result_seed = seed
    return {
        "coefficient": observed,
        "two_sided_permutation_p": p_value,
        "status": "DESCRIPTIVE_ASSOCIATION",
        "permutation_method": method,
        "permutations": permutation_count,
        "seed": result_seed,
    }


def _paired_correlation_result(
    pairs: Sequence[Mapping[str, Any]],
    predictor: str,
    outcome: str,
    *,
    permutations: int,
    seed: int,
) -> dict[str, Any]:
    complete = [
        pair
        for pair in pairs
        if numeric(pair.get(predictor)) is not None and numeric(pair.get(outcome)) is not None
    ]
    values_x = [float(pair[predictor]) for pair in complete]
    values_y = [float(pair[outcome]) for pair in complete]
    if len(complete) < 3:
        return {
            "n": len(complete),
            "status": "UNAVAILABLE_OR_INSUFFICIENT",
            "pearson": None,
            "spearman": None,
        }
    return {
        "n": len(complete),
        "status": "DESCRIPTIVE_ONLY",
        "pearson": _permutation_correlation(
            values_x,
            values_y,
            statistic_name="pearson",
            permutations=permutations,
            seed=seed,
        ),
        "spearman": _permutation_correlation(
            values_x,
            values_y,
            statistic_name="spearman",
            permutations=permutations,
            seed=seed + 1,
        ),
    }


def early_to_fixed_growth_analysis(
    reels: Sequence[Mapping[str, Any]],
    *,
    minimum_pairs: int = 12,
    permutations: int = PERMUTATIONS,
    seed: int = GROWTH_PERMUTATION_SEED,
) -> dict[str, Any]:
    """Compare first 1--24h raw Instagram metrics with first 72--96h outcomes."""
    with_early = 0
    with_fixed = 0
    window_pairs = 0
    excluded: list[dict[str, Any]] = []
    pairs: list[dict[str, Any]] = []
    required_early = ("views", "reach", "total_interactions", "saved", "shares")
    required_fixed = ("views", "reach")

    for reel in reels:
        published_at = parse_datetime(reel.get("published_at"))
        if published_at is None:
            continue
        snapshots = reel.get("snapshots")
        snapshots = snapshots if isinstance(snapshots, Sequence) else []
        early_snapshot, early_age = _first_snapshot_in_age_window(
            snapshots, published_at, 1, 24
        )
        fixed_snapshot, fixed_age = _first_snapshot_in_age_window(
            snapshots, published_at, 72, 96
        )
        with_early += early_snapshot is not None
        with_fixed += fixed_snapshot is not None
        if early_snapshot is None or fixed_snapshot is None:
            continue
        window_pairs += 1
        early = raw_metric_map(early_snapshot.get("raw_api_payload"))
        fixed = raw_metric_map(fixed_snapshot.get("raw_api_payload"))
        missing = [f"early.{name}" for name in required_early if numeric(early.get(name)) is None]
        missing.extend(
            f"fixed.{name}" for name in required_fixed if numeric(fixed.get(name)) is None
        )
        if missing:
            excluded.append(
                {
                    "media_id": str(reel.get("media_id") or ""),
                    "title": str(reel.get("title") or ""),
                    "reason": "MISSING_RAW_INSTAGRAM_METRICS",
                    "missing": missing,
                }
            )
            continue
        early_reach = float(early["reach"])
        early_base_views = float(early["views"])
        fixed_reach = float(fixed["reach"])
        fixed_base_views = float(fixed["views"])
        if early_reach <= 0 or min(early_base_views, fixed_reach, fixed_base_views) < 0:
            excluded.append(
                {
                    "media_id": str(reel.get("media_id") or ""),
                    "title": str(reel.get("title") or ""),
                    "reason": "INVALID_RAW_INSTAGRAM_METRICS",
                }
            )
            continue
        interactions = float(early["total_interactions"])
        save_share_count = float(early["saved"]) + float(early["shares"])
        pairs.append(
            {
                "media_id": str(reel.get("media_id") or ""),
                "title": str(reel.get("title") or ""),
                "early_age_hours": early_age,
                "fixed_age_hours": fixed_age,
                "early_raw_interactions": interactions,
                "early_interaction_rate_per_1000_reached": 1000 * interactions / early_reach,
                "early_saves_plus_shares": save_share_count,
                "early_reach": early_reach,
                "early_base_views": early_base_views,
                "early_skip_rate_percent": numeric(early.get("reels_skip_rate")),
                "early_avg_watch_time_ms": numeric(early.get("ig_reels_avg_watch_time")),
                "later_reach": fixed_reach,
                "later_base_views": fixed_base_views,
                "reach_gain": fixed_reach - early_reach,
                "base_views_gain": fixed_base_views - early_base_views,
            }
        )

    predictors = (
        "early_raw_interactions",
        "early_interaction_rate_per_1000_reached",
        "early_saves_plus_shares",
        "early_reach",
        "early_base_views",
        "early_skip_rate_percent",
        "early_avg_watch_time_ms",
    )
    outcomes = ("later_reach", "later_base_views", "reach_gain", "base_views_gain")
    correlations: dict[str, Any] = {}
    correlation_index = 0
    for predictor in predictors:
        predictor_n = sum(numeric(pair.get(predictor)) is not None for pair in pairs)
        correlations[predictor] = {
            "n": predictor_n,
            "status": "AVAILABLE" if predictor_n >= 3 else "UNAVAILABLE_OR_INSUFFICIENT",
            "outcomes": {},
        }
        for outcome in outcomes:
            correlations[predictor]["outcomes"][outcome] = _paired_correlation_result(
                pairs,
                predictor,
                outcome,
                permutations=permutations,
                seed=seed + correlation_index * 2,
            )
            correlation_index += 1

    paired_n = len(pairs)
    inference_allowed = paired_n >= minimum_pairs
    status = "READY_FOR_ASSOCIATION_INFERENCE" if inference_allowed else "INSUFFICIENT_PAIRED_SAMPLE"
    caveats = [
        "All metrics are cumulative lifetime values; engagement partly reflects exposure already received.",
        "Reels without both strict windows are excluded, so irregular sync timing can create selection bias and omit breakouts.",
        "Retention predictors are unavailable when the first early snapshot predates those API metrics; later diagnostics are not substituted.",
        "Correlations are observational and do not identify a causal recommendation or hook effect.",
        "Multiple predictor/outcome correlations are shown without multiplicity adjustment.",
    ]
    ranked_pairs = sorted(
        pairs,
        key=lambda pair: (-float(pair["later_reach"]), str(pair["media_id"])),
    )
    return {
        "status": status,
        "inference_allowed": inference_allowed,
        "causal_effect_estimated": False,
        "minimum_pairs_for_inference": minimum_pairs,
        "paired_n": paired_n,
        "selection_rules": {
            "channel_id": CHANNEL,
            "early_snapshot": "first snapshot with 1 <= age_hours <= 24",
            "fixed_snapshot": "first snapshot with 72 <= age_hours <= 96",
            "tie_break": "lowest age_hours, then captured_at, then snapshot id",
            "metric_source": "raw_api_payload Instagram views/reach and engagement metrics only",
            "combined_total_views_used": False,
        },
        "availability": {
            "published_reels": len(reels),
            "with_early_window_snapshot": with_early,
            "with_fixed_window_snapshot": with_fixed,
            "with_both_windows_before_metric_validation": window_pairs,
            "valid_pairs": paired_n,
            "excluded_pairs": excluded,
        },
        "age_hours": {
            "early": _numeric_summary([pair["early_age_hours"] for pair in pairs]),
            "fixed": _numeric_summary([pair["fixed_age_hours"] for pair in pairs]),
        },
        "outcomes": {
            outcome: _numeric_summary([pair[outcome] for pair in pairs])
            for outcome in outcomes
        },
        "correlations": correlations,
        "ranked_pairs_by_later_reach": [
            {"rank": rank, **pair}
            for rank, pair in enumerate(ranked_pairs, start=1)
        ],
        "permutation_policy": {
            "n_at_or_below_8": "exact two-sided all-label permutations",
            "n_above_8": f"fixed-seed two-sided {permutations:,}-permutation Monte Carlo",
            "seed": seed,
        },
        "interpretation": (
            "Descriptive metrics only; prohibit predictive inference until at least "
            f"{minimum_pairs} valid pairs."
            if not inference_allowed
            else "Association inference is permitted, but causal interpretation remains prohibited."
        ),
        "caveats": caveats,
    }


def extract_snapshot_metrics(snapshot: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    raw = raw_metric_map(snapshot.get("raw_api_payload"))
    columns = snapshot.get("columns")
    columns = columns if isinstance(columns, Mapping) else {}
    warnings: list[str] = []

    # Older ledgers store combined total_views in columns.views.  A columns.views
    # fallback is safe only when a distinct columns.total_views proves the newer
    # schema and therefore preserves the base/combined distinction.
    base_views = raw.get("views")
    column_total_views = numeric(columns.get("total_views"))
    if base_views is None and column_total_views is not None:
        base_views = numeric(columns.get("views"))
        if base_views is not None:
            warnings.append("BASE_VIEWS_FROM_SEPARATE_V3_COLUMN")
    combined_views = raw.get("total_views", column_total_views)
    if combined_views is None and base_views is not None:
        combined_views = base_views
        warnings.append("TOTAL_VIEWS_FALLBACK_TO_BASE")

    def raw_or_column(name: str, column_name: str | None = None) -> int | float | None:
        value = raw.get(name)
        if value is not None:
            return value
        return numeric(columns.get(column_name or name))

    combined_likes = raw.get("total_likes")
    if combined_likes is None:
        combined_likes = numeric(columns.get("total_likes"))
    if combined_likes is None:
        combined_likes = raw_or_column("likes")
    metrics = {
        "base_views": base_views,
        "combined_views": combined_views,
        "reach": raw_or_column("reach"),
        "base_likes": raw_or_column("likes"),
        "combined_likes": combined_likes,
        "comments": raw_or_column("comments"),
        "saved": raw_or_column("saved"),
        "shares": raw_or_column("shares"),
        "total_interactions": raw_or_column("total_interactions"),
        "diagnostics": {
            name: raw.get(name, numeric(columns.get(name)))
            for name in DIAGNOSTIC_METRICS
        },
    }
    return metrics, warnings


def extract_snapshot_diagnostics(snapshot: Mapping[str, Any]) -> dict[str, int | float | None]:
    """Return diagnostics without mixing them into the classification snapshot."""
    raw = raw_metric_map(snapshot.get("raw_api_payload"))
    columns = snapshot.get("columns")
    columns = columns if isinstance(columns, Mapping) else {}
    return {
        name: raw.get(name, numeric(columns.get(name)))
        for name in DIAGNOSTIC_METRICS
    }


def select_latest_diagnostic_snapshot(
    snapshots: Sequence[Mapping[str, Any]], published_at: datetime | None
) -> tuple[dict[str, Any] | None, float | None, dict[str, int | float | None], list[str]]:
    """Select the newest snapshot carrying diagnostics, independently of performance.

    Historical 72--96 hour snapshots remain the source of classification metrics.
    Diagnostics were added later, so choosing them separately prevents a comparable
    historical performance snapshot from hiding the newest retention data.
    """
    candidates: list[
        tuple[
            bool,
            datetime,
            int,
            float | None,
            dict[str, Any],
            dict[str, int | float | None],
        ]
    ] = []
    for snapshot in snapshots:
        captured_at = parse_datetime(snapshot.get("captured_at"))
        if captured_at is None:
            continue
        diagnostics = extract_snapshot_diagnostics(snapshot)
        has_diagnostics = any(value is not None for value in diagnostics.values())
        age = (
            (captured_at - published_at).total_seconds() / 3600
            if published_at is not None
            else None
        )
        candidates.append(
            (
                has_diagnostics,
                captured_at,
                int(snapshot.get("id") or -1),
                age,
                dict(snapshot),
                diagnostics,
            )
        )

    empty = {name: None for name in DIAGNOSTIC_METRICS}
    if not candidates:
        return None, None, empty, ["MISSING_DIAGNOSTIC_SNAPSHOT"]
    with_diagnostics = [candidate for candidate in candidates if candidate[0]]
    pool = with_diagnostics or candidates
    selected = max(pool, key=lambda candidate: (candidate[1], candidate[2]))
    warnings: list[str] = []
    if not selected[0]:
        warnings.append("DIAGNOSTICS_UNAVAILABLE")
    if selected[3] is not None and float(selected[3]) < 0:
        warnings.append("NEGATIVE_DIAGNOSTIC_SNAPSHOT_AGE")
    return selected[4], selected[3], selected[5], warnings


def classification_language(classification: str) -> str:
    if classification.startswith("PROVISIONAL_"):
        candidate = classification.removeprefix("PROVISIONAL_")
        detail = CLASSIFICATION_LANGUAGE.get(candidate, "Observational candidate only.")
        return f"Provisional 24--72 hour signal. {detail}"
    return CLASSIFICATION_LANGUAGE.get(
        classification,
        "Internal observational classification; do not interpret it as a causal effect.",
    )


def metric_tier(name: str, value: int | float | None) -> str:
    if value is None:
        return "missing"
    normal, strong, breakout = PERFORMANCE_BANDS[name]
    if value < normal:
        return "below"
    if value < strong:
        return "normal"
    if value < breakout:
        return "strong"
    return "breakout"


def winner_candidate(metrics: Mapping[str, Any]) -> dict[str, Any]:
    base_views = float(metrics["base_views"])
    combined_views = float(metrics["combined_views"])
    reach = float(metrics["reach"])
    interactions = int(metrics["total_interactions"])
    saved = int(metrics["saved"])
    shares = int(metrics["shares"])
    save_share_count = saved + shares
    save_share_rate = 1000 * save_share_count / reach
    interaction_rate = 1000 * interactions / reach

    native_breakout = base_views >= 250 or reach >= 185
    audience_fit = (
        reach >= 100
        and interactions >= 4
        and save_share_count >= 3
        and (
            save_share_rate >= 23
            or (interactions >= 7 and save_share_rate >= 14)
        )
    )
    if native_breakout and audience_fit:
        winner = "COMPLETE_WINNER"
    elif native_breakout:
        winner = "DISTRIBUTION_WINNER"
    elif audience_fit:
        winner = "AUDIENCE_FIT_WINNER"
    else:
        winner = "NO_WINNER"

    if combined_views >= 750:
        amplification = "META_AMPLIFIED" if native_breakout else "AMPLIFICATION_ONLY"
    else:
        amplification = "NONE"

    return {
        "winner": winner,
        "native_breakout": native_breakout,
        "audience_fit": audience_fit,
        "amplification": amplification,
        "save_share_count": save_share_count,
        "save_share_rate_per_1000": save_share_rate,
        "interaction_rate_per_1000": interaction_rate,
    }


def action_for(candidate: Mapping[str, Any], metrics: Mapping[str, Any]) -> str:
    winner = candidate["winner"]
    interactions = int(metrics["total_interactions"])
    if winner == "COMPLETE_WINNER":
        return "SCALE_COMPLETE"
    if winner == "DISTRIBUTION_WINNER":
        return "SCALE_DISTRIBUTION" if interactions >= 4 else "ITERATE_CTA"
    if winner == "AUDIENCE_FIT_WINNER":
        return "SCALE_AUDIENCE_FIT" if interactions >= 7 else "ITERATE_DISTRIBUTION"
    if candidate["amplification"] == "AMPLIFICATION_ONLY":
        return "ITERATE_AMPLIFICATION_ONLY"
    below_count = sum(
        (
            float(metrics["combined_views"]) < 164,
            float(metrics["base_views"]) < 151,
            float(metrics["reach"]) < 125,
        )
    )
    if interactions == 0 and below_count >= 2:
        return "STOP_EXACT_TREATMENT"
    return "ITERATE"


def classify_reel(
    reel: Mapping[str, Any], *, coverage: float
) -> dict[str, Any]:
    published_at = parse_datetime(reel.get("published_at"))
    snapshots = reel.get("snapshots", [])
    snapshot, age_hours, selection_warnings = select_snapshot(snapshots, published_at)
    (
        diagnostic_snapshot,
        diagnostic_age_hours,
        diagnostic_metrics,
        diagnostic_warnings,
    ) = select_latest_diagnostic_snapshot(snapshots, published_at)
    duration_seconds = numeric(reel.get("duration_seconds"))
    average_watch_ms = numeric(diagnostic_metrics.get("ig_reels_avg_watch_time"))
    avg_watch_to_duration_ratio = None
    if (
        duration_seconds is not None
        and duration_seconds > 0
        and average_watch_ms is not None
    ):
        avg_watch_to_duration_ratio = (
            float(average_watch_ms) / (float(duration_seconds) * 1000) * 100
        )
    latest_diagnostics = {
        "captured_at": (
            str(diagnostic_snapshot.get("captured_at") or "")
            if diagnostic_snapshot
            else ""
        ),
        "snapshot_age_hours": diagnostic_age_hours,
        "snapshot_source": (
            str(diagnostic_snapshot.get("source") or "")
            if diagnostic_snapshot
            else ""
        ),
        "metrics": diagnostic_metrics,
        "estimated_duration_seconds": duration_seconds,
        "avg_watch_to_estimated_duration_ratio_percent": avg_watch_to_duration_ratio,
        "is_completion_rate": False,
        "interpretation": (
            "Average watch time divided by estimated Reel duration; this is not a "
            "completion rate or a retention curve."
        ),
        "warnings": sorted(set(diagnostic_warnings)),
    }
    errors: list[str] = []
    warnings = list(selection_warnings)
    if coverage < MIN_COVERAGE:
        errors.append("COVERAGE_BELOW_90_PERCENT")
    if published_at is None:
        errors.append("INVALID_PUBLISHED_AT")
    if snapshot is None:
        errors.append("MISSING_SNAPSHOT")
        metrics: dict[str, Any] = {}
    else:
        metrics, metric_warnings = extract_snapshot_metrics(snapshot)
        warnings.extend(metric_warnings)
        if parse_datetime(snapshot.get("captured_at")) is None:
            errors.append("INVALID_CAPTURED_AT")
    if age_hours is not None and age_hours < 0:
        errors.append("NEGATIVE_SNAPSHOT_AGE")

    required = (
        "base_views",
        "combined_views",
        "reach",
        "saved",
        "shares",
        "total_interactions",
    )
    for name in required:
        value = metrics.get(name)
        if value is None:
            errors.append(f"MISSING_{name.upper()}")
        elif float(value) < 0:
            errors.append(f"NEGATIVE_{name.upper()}")
    if not errors:
        if float(metrics["combined_views"]) < float(metrics["base_views"]):
            errors.append("COMBINED_VIEWS_LOWER_THAN_BASE")
        if float(metrics["reach"]) <= 0:
            errors.append("NON_POSITIVE_REACH")
    if metrics.get("reach") is not None and 0 < float(metrics["reach"]) < 100:
        warnings.append("INSUFFICIENT_REACH_FOR_AUDIENCE_FIT")

    result: dict[str, Any] = {
        "content_hash": str(reel.get("content_hash") or ""),
        "media_id": str(reel.get("media_id") or ""),
        "title": str(reel.get("title") or ""),
        "published_at": str(reel.get("published_at") or ""),
        "permalink": str(reel.get("permalink") or ""),
        "duration_seconds": duration_seconds,
        "snapshot_captured_at": str(snapshot.get("captured_at") or "") if snapshot else "",
        "snapshot_age_hours": age_hours,
        "snapshot_source": str(snapshot.get("source") or "") if snapshot else "",
        "metrics": metrics,
        "latest_diagnostics": latest_diagnostics,
        "warnings": sorted(set(warnings)),
        "data_errors": sorted(set(errors)),
    }
    if errors:
        result.update(
            {
                "stage": "DATA_HOLD",
                "classification": "DATA_HOLD",
                "candidate_classification": None,
                "amplification": "UNKNOWN",
                "action": "DATA_HOLD",
                "tiers": {},
                "classification_interpretation": classification_language("DATA_HOLD"),
            }
        )
        return result

    candidate = winner_candidate(metrics)
    candidate_action = action_for(candidate, metrics)
    if age_hours is None:
        stage = "DATA_HOLD"
        classification = "DATA_HOLD"
        errors.append("MISSING_SNAPSHOT_AGE")
        action = "DATA_HOLD"
    elif age_hours < 24:
        stage = "MONITOR_EARLY"
        classification = "MONITOR_EARLY"
        action = "MONITOR_EARLY"
    elif age_hours < 72:
        stage = "PROVISIONAL"
        classification = f"PROVISIONAL_{candidate['winner']}"
        action = "PROVISIONAL"
    else:
        stage = "DECISION_READY"
        classification = str(candidate["winner"])
        action = candidate_action

    result.update(
        {
            "stage": stage,
            "classification": classification,
            "candidate_classification": candidate["winner"],
            "amplification": candidate["amplification"],
            "action": action,
            "classification_interpretation": classification_language(classification),
            "save_share_count": candidate["save_share_count"],
            "save_share_rate_per_1000": candidate["save_share_rate_per_1000"],
            "interaction_rate_per_1000": candidate["interaction_rate_per_1000"],
            "tiers": {
                "combined_views": metric_tier("combined_views", metrics["combined_views"]),
                "base_views": metric_tier("base_views", metrics["base_views"]),
                "reach": metric_tier("reach", metrics["reach"]),
                "total_interactions": metric_tier(
                    "total_interactions", metrics["total_interactions"]
                ),
            },
        }
    )
    result["data_errors"] = sorted(set(errors))
    return result


def canonical_slot(published_at: Any) -> str | None:
    parsed = parse_datetime(published_at)
    if parsed is None:
        return None
    local = parsed.astimezone(JST)
    minute = local.hour * 60 + local.minute
    if 8 * 60 <= minute < 10 * 60 + 30:
        return "09"
    if 12 * 60 <= minute < 14 * 60 + 30:
        return "13"
    if 17 * 60 <= minute < 19 * 60 + 30:
        return "18"
    if 20 * 60 <= minute < 22 * 60 + 30:
        return "21"
    return None


def _minutes_from_midnight(published_at: str) -> int:
    parsed = parse_datetime(published_at)
    if parsed is None:
        return -1
    local = parsed.astimezone(JST)
    return local.hour * 60 + local.minute


def _matched_date_rows(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, list[Mapping[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for record in records:
        age = record.get("snapshot_age_hours")
        if record.get("data_errors") or not isinstance(age, (int, float)) or not (72 <= age <= 96):
            continue
        slot = canonical_slot(record.get("published_at"))
        published = parse_datetime(record.get("published_at"))
        if slot is None or published is None:
            continue
        grouped[published.astimezone(JST).date().isoformat()][slot].append(record)

    matched: list[dict[str, Any]] = []
    for local_date in sorted(grouped):
        per_slot = grouped[local_date]
        if any(not per_slot.get(slot) for slot in SLOTS):
            continue
        chosen: dict[str, Mapping[str, Any]] = {}
        for slot in SLOTS:
            chosen[slot] = min(
                per_slot[slot],
                key=lambda record: (
                    abs(
                        _minutes_from_midnight(str(record.get("published_at") or ""))
                        - SLOT_TARGET_MINUTES[slot]
                    ),
                    str(record.get("published_at") or ""),
                    str(record.get("media_id") or ""),
                ),
            )
        matched.append(
            {
                "date": local_date,
                "slots": {
                    slot: {
                        "media_id": str(chosen[slot].get("media_id") or ""),
                        "title": str(chosen[slot].get("title") or ""),
                        "base_views": chosen[slot]["metrics"]["base_views"],
                        "reach": chosen[slot]["metrics"]["reach"],
                    }
                    for slot in SLOTS
                },
            }
        )
    return matched


def _permutation_results(
    matched: Sequence[Mapping[str, Any]], metric: str, *, permutations: int, seed: int
) -> dict[str, Any]:
    observed_values: dict[str, list[float]] = {slot: [] for slot in SLOTS}
    wins = Counter({slot: 0 for slot in SLOTS})
    for day in matched:
        values = [float(day["slots"][slot][metric]) for slot in SLOTS]
        daily_median = median(values)
        for slot, value in zip(SLOTS, values):
            observed_values[slot].append(value / daily_median if daily_median else 0.0)
            if value == max(values):
                wins[slot] += 1
    observed = {slot: median(observed_values[slot]) for slot in SLOTS}

    rng = random.Random(seed)
    greater_or_equal = Counter({slot: 0 for slot in SLOTS})
    for _ in range(permutations):
        permuted_values: dict[str, list[float]] = {slot: [] for slot in SLOTS}
        for day in matched:
            values = [float(day["slots"][slot][metric]) for slot in SLOTS]
            daily_median = median(values)
            rng.shuffle(values)
            for slot, value in zip(SLOTS, values):
                permuted_values[slot].append(value / daily_median if daily_median else 0.0)
        for slot in SLOTS:
            statistic = median(permuted_values[slot])
            if statistic >= observed[slot] - 1e-12:
                greater_or_equal[slot] += 1
    return {
        slot: {
            "median_within_date_ratio": observed[slot],
            "one_sided_permutation_p": (greater_or_equal[slot] + 1) / (permutations + 1),
            "win_rate": wins[slot] / len(matched),
        }
        for slot in SLOTS
    }


def analyze_slots(
    records: Sequence[Mapping[str, Any]],
    *,
    min_dates: int = MIN_MATCHED_DATES,
    permutations: int = PERMUTATIONS,
    seed: int = PERMUTATION_SEED,
) -> dict[str, Any]:
    matched = _matched_date_rows(records)
    eligible_counts = Counter()
    for record in records:
        age = record.get("snapshot_age_hours")
        slot = canonical_slot(record.get("published_at"))
        if (
            slot
            and not record.get("data_errors")
            and isinstance(age, (int, float))
            and 72 <= age <= 96
        ):
            eligible_counts[slot] += 1
    result: dict[str, Any] = {
        "method": "complete JST dates; fixed 72-96h snapshots; within-date median ratios",
        "causal_effect_estimated": False,
        "slot_windows": {
            "09": "08:00-10:29 JST",
            "13": "12:00-14:29 JST",
            "18": "17:00-19:29 JST",
            "21": "20:00-22:29 JST",
        },
        "eligible_by_slot": {slot: eligible_counts[slot] for slot in SLOTS},
        "complete_date_count": len(matched),
        "minimum_complete_dates": min_dates,
        "matched_dates": [day["date"] for day in matched],
        "permutations": permutations,
        "seed": seed,
    }
    if len(matched) < min_dates:
        result.update(
            {
                "status": "INSUFFICIENT_MATCHED_DATES",
                "estimability": "NOT_ESTIMABLE",
                "interpretation": (
                    "Posting-slot performance is not estimable from the available matched "
                    "dates; no slot should be described as favorable or unfavorable."
                ),
                "slot_metrics": {},
                "favorable_slots": [],
                "associated_higher_reach_slots": [],
            }
        )
        return result

    reach_results = _permutation_results(
        matched, "reach", permutations=permutations, seed=seed
    )
    base_results = _permutation_results(
        matched, "base_views", permutations=permutations, seed=seed + 1
    )
    slot_metrics: dict[str, Any] = {}
    favorable: list[str] = []
    for slot in SLOTS:
        slot_metrics[slot] = {
            "reach": reach_results[slot],
            "base_views": base_results[slot],
        }
        if (
            reach_results[slot]["median_within_date_ratio"] >= 1.15
            and base_results[slot]["median_within_date_ratio"] > 1.0
            and reach_results[slot]["one_sided_permutation_p"] < 0.05
            and reach_results[slot]["win_rate"] >= 0.60
        ):
            favorable.append(slot)
    result.update(
        {
            "status": "READY",
            "estimability": "OBSERVATIONAL_ASSOCIATION_ONLY",
            "interpretation": (
                "Flagged slots are historical within-date associations, not causal posting-"
                "time effects; content, topic, duration, and treatment may still confound them."
            ),
            "slot_metrics": slot_metrics,
            "favorable_slots": favorable,
            "associated_higher_reach_slots": favorable,
        }
    )
    return result


def build_ab_matrix(
    start: date,
    *,
    variant_a: str = DEFAULT_VARIANT_A,
    variant_b: str = DEFAULT_VARIANT_B,
    seed: int = MATRIX_RANDOMIZATION_SEED,
) -> dict[str, Any]:
    if not variant_a or not variant_b or variant_a == variant_b:
        raise ValueError("matrix variant labels must be non-empty and distinct")
    days: list[dict[str, Any]] = []
    by_slot = {slot: Counter({variant_a: 0, variant_b: 0}) for slot in SLOTS}
    by_weekday: dict[str, dict[str, Counter[str]]] = defaultdict(
        lambda: {slot: Counter({variant_a: 0, variant_b: 0}) for slot in SLOTS}
    )
    rng = random.Random(seed)
    first_week_variant_a_slots = [
        frozenset(rng.sample(list(SLOTS), 2))
        for _ in range(7)
    ]
    for day_index in range(14):
        current = start + timedelta(days=day_index)
        first_week_slots = first_week_variant_a_slots[day_index % 7]
        variant_a_slots = (
            first_week_slots
            if day_index < 7
            else frozenset(slot for slot in SLOTS if slot not in first_week_slots)
        )
        assignments: dict[str, str] = {}
        for slot in SLOTS:
            variant = variant_a if slot in variant_a_slots else variant_b
            assignments[slot] = variant
            by_slot[slot][variant] += 1
            by_weekday[current.strftime("%A")][slot][variant] += 1
        days.append(
            {
                "date": current.isoformat(),
                "weekday": current.strftime("%A"),
                "assignments": assignments,
            }
        )
    return {
        "status": "NON_MUTATING_BALANCED_QUASI_EXPERIMENT_PLAN",
        "design": "BALANCED_CONSTRAINED_RANDOMIZED_ASSIGNMENT",
        "randomization_seed": seed,
        "assignment_randomized": True,
        "treatment_is_bundle": True,
        "causal_effect_estimated": False,
        "matching_requirements": [
            "Match or block posts by topic family.",
            "Match or block posts by Reel duration.",
            "Keep source and production-quality mix balanced where practical.",
        ],
        "causal_interpretation": (
            "This is a balanced, constrained-randomized assignment proposal for a bundled "
            "treatment. Until matched content is assigned and the plan is executed, it is a "
            "quasi-experiment plan, not evidence of a causal effect. Any later contrast would "
            "estimate the bundle, not its individual opening, narration, evidence, or takeaway "
            "components."
        ),
        "start_date": start.isoformat(),
        "end_date": (start + timedelta(days=13)).isoformat(),
        "variants": [variant_a, variant_b],
        "variant_definitions": {
            variant_a: "Current translated-clip workflow used as the control.",
            variant_b: (
                "Original Japanese opening and narration, explicit source evidence, "
                "and the account's own editorial takeaway."
            ),
        },
        "days": days,
        "balance_by_slot": {
            slot: dict(by_slot[slot])
            for slot in SLOTS
        },
        "balance_by_weekday_and_slot": {
            weekday: {slot: dict(counts) for slot, counts in slots.items()}
            for weekday, slots in sorted(by_weekday.items())
        },
    }


def default_matrix_start(report: Mapping[str, Any]) -> date:
    generated_at = parse_datetime(report.get("generated_at"))
    if generated_at is not None:
        return generated_at.astimezone(JST).date() + timedelta(days=1)
    return datetime.now(JST).date() + timedelta(days=1)


def build_analysis(
    *,
    report_path: Path,
    db_path: Path,
    source_report_label: Path | None = None,
    matrix_start: date | None = None,
    variant_a: str = DEFAULT_VARIANT_A,
    variant_b: str = DEFAULT_VARIANT_B,
    slot_min_dates: int = MIN_MATCHED_DATES,
    permutations: int = PERMUTATIONS,
    permutation_seed: int = PERMUTATION_SEED,
    matrix_seed: int = MATRIX_RANDOMIZATION_SEED,
) -> dict[str, Any]:
    report = load_report(report_path)
    reels = load_reels(report, db_path)
    with_snapshots = sum(bool(reel.get("snapshots")) for reel in reels)
    coverage = with_snapshots / len(reels) if reels else 0.0
    classified = [classify_reel(reel, coverage=coverage) for reel in reels]
    stage_counts = Counter(record["stage"] for record in classified)
    classification_counts = Counter(record["classification"] for record in classified)
    action_counts = Counter(record["action"] for record in classified)
    start = matrix_start or default_matrix_start(report)
    growth_analysis = early_to_fixed_growth_analysis(
        reels,
        permutations=permutations,
        seed=GROWTH_PERMUTATION_SEED,
    )
    latest_inventory = latest_inventory_summary(
        reels,
        reference_time=parse_datetime(report.get("generated_at")),
    )
    classification_language_map = dict(CLASSIFICATION_LANGUAGE)
    for candidate in (
        "COMPLETE_WINNER",
        "DISTRIBUTION_WINNER",
        "AUDIENCE_FIT_WINNER",
        "NO_WINNER",
    ):
        provisional = f"PROVISIONAL_{candidate}"
        classification_language_map[provisional] = classification_language(provisional)
    return {
        "schema_version": 2,
        "channel_id": CHANNEL,
        "report_generated_at": str(report.get("generated_at") or ""),
        "source_report": str(
            (source_report_label or report_path).expanduser().resolve()
        ),
        "source_db": str(db_path.expanduser().resolve()),
        "read_only": True,
        "evidence_type": "OBSERVATIONAL_POST_LEVEL_ANALYSIS",
        "causal_effect_estimated": False,
        "observational_caveats": list(OBSERVATIONAL_CAVEATS),
        "classification_language": classification_language_map,
        "coverage": {
            "published_reels": len(reels),
            "reels_with_snapshots": with_snapshots,
            "ratio": coverage,
            "minimum_required": MIN_COVERAGE,
            "healthy": coverage >= MIN_COVERAGE,
        },
        "thresholds": {
            "performance_bands": {
                name: {
                    "normal": bounds[0],
                    "strong": bounds[1],
                    "breakout": bounds[2],
                }
                for name, bounds in PERFORMANCE_BANDS.items()
            },
            "audience_fit": {
                "minimum_reach": 100,
                "minimum_total_interactions": 4,
                "minimum_save_share_count": 3,
                "elite_save_share_rate_per_1000": 23,
                "interaction_breakout": 7,
                "strong_save_share_rate_per_1000": 14,
            },
        },
        "counts": {
            "stage": dict(sorted(stage_counts.items())),
            "classification": dict(sorted(classification_counts.items())),
            "action": dict(sorted(action_counts.items())),
        },
        "reels": classified,
        "latest_inventory": latest_inventory,
        "early_to_fixed_growth_analysis": growth_analysis,
        "slot_analysis": analyze_slots(
            classified,
            min_dates=slot_min_dates,
            permutations=permutations,
            seed=permutation_seed,
        ),
        "ab_matrix": build_ab_matrix(
            start,
            variant_a=variant_a,
            variant_b=variant_b,
            seed=matrix_seed,
        ),
    }


def _markdown_cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def _fmt_number(value: Any, digits: int = 0) -> str:
    if not isinstance(value, (int, float)):
        return ""
    return f"{value:,.{digits}f}" if digits else f"{value:,.0f}"


def _growth_correlation_cell(result: Any) -> str:
    if not isinstance(result, Mapping) or result.get("n", 0) < 3:
        return "unavailable"
    pearson = result.get("pearson")
    spearman = result.get("spearman")
    if not isinstance(pearson, Mapping) or not isinstance(spearman, Mapping):
        return "unavailable"
    coefficient_r = pearson.get("coefficient")
    coefficient_rho = spearman.get("coefficient")
    p_value = spearman.get("two_sided_permutation_p")
    if not all(isinstance(value, (int, float)) for value in (coefficient_r, coefficient_rho, p_value)):
        return "constant/insufficient"
    return f"r={coefficient_r:.3f}; rho={coefficient_rho:.3f} (p={p_value:.3f})"


def render_markdown(analysis: Mapping[str, Any]) -> str:
    coverage = analysis["coverage"]
    inventory = analysis.get("latest_inventory", {})
    inventory = inventory if isinstance(inventory, Mapping) else {}
    inventory_counts = inventory.get("counts", {})
    inventory_counts = (
        inventory_counts if isinstance(inventory_counts, Mapping) else {}
    )
    inventory_availability = inventory.get("availability", {})
    inventory_availability = (
        inventory_availability
        if isinstance(inventory_availability, Mapping)
        else {}
    )
    capture_range = inventory.get("latest_capture_range", {})
    capture_range = capture_range if isinstance(capture_range, Mapping) else {}
    inventory_freshness = inventory.get("freshness", {})
    inventory_freshness = (
        inventory_freshness if isinstance(inventory_freshness, Mapping) else {}
    )
    transcript_coverage = inventory.get("transcript_coverage", {})
    transcript_coverage = (
        transcript_coverage if isinstance(transcript_coverage, Mapping) else {}
    )
    lines = [
        "# AI Brief JP Reach Analysis",
        "",
        f"- Report generated: {_markdown_cell(analysis.get('report_generated_at'))}",
        f"- Coverage: {coverage['reels_with_snapshots']}/{coverage['published_reels']} ({coverage['ratio']:.1%})",
        "- Metric scopes: Instagram/base `views`, Meta all-surface `total_views`, explicit Instagram + Facebook `crossposted_views`, and engagement are kept separate.",
        "",
        "## Interpretation guardrails",
        "",
        *[f"- {_markdown_cell(caveat)}" for caveat in analysis.get("observational_caveats", [])],
        "",
        "### Classification language",
        "",
        *[
            f"- `{label}`: {_markdown_cell(analysis['classification_language'][label])}"
            for label in (
                "COMPLETE_WINNER",
                "DISTRIBUTION_WINNER",
                "AUDIENCE_FIT_WINNER",
                "NO_WINNER",
            )
        ],
        "",
        "## Current/latest inventory",
        "",
        f"- Synced reels: {inventory.get('synced_n', 0)}/{inventory.get('published_reels', 0)}",
        f"- Per-reel latest capture range: {_markdown_cell(capture_range.get('oldest'))} to {_markdown_cell(capture_range.get('newest'))}",
        f"- Snapshot freshness at report time: {inventory_freshness.get('fresh_n', 0)} fresh, {inventory_freshness.get('stale_n', 0)} older than {inventory_freshness.get('maximum_age_hours', 13)}h, {inventory_freshness.get('unsynced_n', 0)} unsynced",
        f"- Reel transcript coverage: {transcript_coverage.get('available_n', 0)}/{transcript_coverage.get('published_reels', 0)}; opening diagnostics are unavailable for {transcript_coverage.get('missing_n', 0)}",
        f"- Meta all-surface `total_views >= 500`: {inventory_counts.get('meta_all_surface_total_views_gte_500', 0)}/{inventory_availability.get('meta_all_surface_total_views_n', 0)} available",
        f"- Instagram/base `views >= 500`: {inventory_counts.get('instagram_base_views_gte_500', 0)}/{inventory_availability.get('instagram_base_views_n', 0)} available",
        f"- Explicit Instagram + Facebook `crossposted_views >= 500`: {inventory_counts.get('ig_facebook_crossposted_views_gte_500', 0)}/{inventory_availability.get('crossposted_views_n', 0)} available",
        f"- Zero shares: {inventory_counts.get('zero_share', 0)}/{inventory_availability.get('shares_n', 0)} available; zero saves: {inventory_counts.get('zero_save', 0)}/{inventory_availability.get('saved_n', 0)} available",
        f"- Total interactions >= 7: {inventory_counts.get('total_interactions_gte_7', 0)}/{inventory_availability.get('total_interactions_n', 0)} available",
        f"- Explicit surface fields available: `facebook_views` {inventory_availability.get('facebook_views_n', 0)}; `crossposted_views` {inventory_availability.get('crossposted_views_n', 0)}",
        f"- Scope guardrail: {_markdown_cell(inventory.get('scope_guardrail'))}",
        "",
        "## Reel classifications",
        "",
        "| Title | Performance age h | Diagnostic age h | Instagram/base views | Meta all-surface views | Instagram reach | Interactions | Save+share / 1k | Avg watch / estimated duration % (not completion) | First-3s skip % | Classification | Action | Warnings |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | --- |",
    ]
    for reel in analysis.get("reels", []):
        metrics = reel.get("metrics", {})
        latest_diagnostics = reel.get("latest_diagnostics", {})
        latest_diagnostics = (
            latest_diagnostics if isinstance(latest_diagnostics, Mapping) else {}
        )
        diagnostic_metrics = latest_diagnostics.get("metrics", {})
        diagnostic_metrics = (
            diagnostic_metrics if isinstance(diagnostic_metrics, Mapping) else {}
        )
        warnings = [*reel.get("warnings", []), *reel.get("data_errors", [])]
        warnings.extend(latest_diagnostics.get("warnings", []))
        lines.append(
            "| "
            + " | ".join(
                (
                    _markdown_cell(reel.get("title")),
                    _fmt_number(reel.get("snapshot_age_hours"), 1),
                    _fmt_number(latest_diagnostics.get("snapshot_age_hours"), 1),
                    _fmt_number(metrics.get("base_views")),
                    _fmt_number(metrics.get("combined_views")),
                    _fmt_number(metrics.get("reach")),
                    _fmt_number(metrics.get("total_interactions")),
                    _fmt_number(reel.get("save_share_rate_per_1000"), 1),
                    _fmt_number(
                        latest_diagnostics.get(
                            "avg_watch_to_estimated_duration_ratio_percent"
                        ),
                        1,
                    ),
                    _fmt_number(diagnostic_metrics.get("reels_skip_rate"), 1),
                    _markdown_cell(reel.get("classification")),
                    _markdown_cell(reel.get("action")),
                    _markdown_cell(", ".join(warnings)),
                )
            )
            + " |"
        )

    growth = analysis.get("early_to_fixed_growth_analysis", {})
    if isinstance(growth, Mapping):
        early_age = growth.get("age_hours", {}).get("early", {})
        fixed_age = growth.get("age_hours", {}).get("fixed", {})
        later_reach = growth.get("outcomes", {}).get("later_reach", {})
        later_base = growth.get("outcomes", {}).get("later_base_views", {})
        lines.extend(
            [
                "",
                "## Early-to-fixed growth analysis",
                "",
                f"- Status: {growth.get('status')}",
                f"- Valid pairs: {growth.get('paired_n', 0)} (minimum {growth.get('minimum_pairs_for_inference', 12)} for inference)",
                f"- Inference allowed: {str(bool(growth.get('inference_allowed'))).lower()}",
                f"- Early ages: {_fmt_number(early_age.get('min'), 1)}--{_fmt_number(early_age.get('max'), 1)}h; median {_fmt_number(early_age.get('median'), 1)}h",
                f"- Fixed ages: {_fmt_number(fixed_age.get('min'), 1)}--{_fmt_number(fixed_age.get('max'), 1)}h; median {_fmt_number(fixed_age.get('median'), 1)}h",
                f"- Outcome ranges: reach {_fmt_number(later_reach.get('min'))}--{_fmt_number(later_reach.get('max'))}; base views {_fmt_number(later_base.get('min'))}--{_fmt_number(later_base.get('max'))}",
                f"- Interpretation: {_markdown_cell(growth.get('interpretation'))}",
                "",
                "| Early predictor | N | Later reach | Later base views | Reach gain | Base-view gain |",
                "| --- | ---: | --- | --- | --- | --- |",
            ]
        )
        predictor_labels = {
            "early_raw_interactions": "Raw interactions",
            "early_interaction_rate_per_1000_reached": "Interactions / 1,000 reached",
            "early_saves_plus_shares": "Saves + shares",
            "early_reach": "Early reach",
            "early_base_views": "Early base views",
            "early_skip_rate_percent": "First-3s skip rate",
            "early_avg_watch_time_ms": "Average watch time",
        }
        correlations = growth.get("correlations", {})
        correlations = correlations if isinstance(correlations, Mapping) else {}
        for predictor, label in predictor_labels.items():
            details = correlations.get(predictor, {})
            details = details if isinstance(details, Mapping) else {}
            outcomes = details.get("outcomes", {})
            outcomes = outcomes if isinstance(outcomes, Mapping) else {}
            lines.append(
                "| "
                + " | ".join(
                    (
                        label,
                        str(details.get("n", 0)),
                        _growth_correlation_cell(outcomes.get("later_reach")),
                        _growth_correlation_cell(outcomes.get("later_base_views")),
                        _growth_correlation_cell(outcomes.get("reach_gain")),
                        _growth_correlation_cell(outcomes.get("base_views_gain")),
                    )
                )
                + " |"
            )
        lines.extend(["", *[f"- {_markdown_cell(caveat)}" for caveat in growth.get("caveats", [])]])

    slots = analysis["slot_analysis"]
    lines.extend(
        [
            "",
            "## Matched-date JST slot analysis",
            "",
            f"- Status: {slots['status']}",
            f"- Complete dates: {slots['complete_date_count']} (minimum {slots['minimum_complete_dates']})",
            f"- Interpretation: {_markdown_cell(slots.get('interpretation'))}",
        ]
    )
    if slots.get("estimability") == "NOT_ESTIMABLE":
        lines.append("- Observed higher-reach slot associations: not estimable")
    else:
        lines.append(
            "- Observed higher-reach slot associations: "
            + (", ".join(slots.get("associated_higher_reach_slots", [])) or "none detected")
        )
    if slots.get("slot_metrics"):
        lines.extend(
            [
                "",
                "| Slot | Reach ratio | Reach p | Reach win rate | Base ratio |",
                "| --- | ---: | ---: | ---: | ---: |",
            ]
        )
        for slot in SLOTS:
            values = slots["slot_metrics"][slot]
            lines.append(
                f"| {slot}:00 | {values['reach']['median_within_date_ratio']:.3f} "
                f"| {values['reach']['one_sided_permutation_p']:.4f} "
                f"| {values['reach']['win_rate']:.1%} "
                f"| {values['base_views']['median_within_date_ratio']:.3f} |"
            )

    matrix = analysis["ab_matrix"]
    lines.extend(
        [
            "",
            "## Non-mutating balanced 14-day quasi-experiment matrix",
            "",
            f"- Window: {matrix['start_date']} to {matrix['end_date']}",
            f"- Design: {matrix['design']}; deterministic seed `{matrix['randomization_seed']}`.",
            "- Treatment: bundled editorial transformation; component effects are not identifiable.",
            f"- Causal interpretation: {_markdown_cell(matrix['causal_interpretation'])}",
            "- Matching required: topic family, Reel duration, and source/production-quality mix.",
            *[
                f"- `{variant}`: {_markdown_cell(definition)}"
                for variant, definition in matrix["variant_definitions"].items()
            ],
            "",
            "| Date | Weekday | 09:00 | 13:00 | 18:00 | 21:00 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for day in matrix["days"]:
        assignments = day["assignments"]
        lines.append(
            f"| {day['date']} | {day['weekday']} | {assignments['09']} | {assignments['13']} "
            f"| {assignments['18']} | {assignments['21']} |"
        )
    return "\n".join(lines) + "\n"


def write_outputs(analysis: Mapping[str, Any], json_out: Path, markdown_out: Path) -> None:
    json_path = json_out.expanduser().resolve()
    markdown_path = markdown_out.expanduser().resolve()
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(analysis, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(render_markdown(analysis), encoding="utf-8")


def parse_matrix_start(value: str | None, report: Mapping[str, Any]) -> date:
    if not value:
        return default_matrix_start(report)
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise SystemExit(f"invalid --matrix-start-date: {value}") from exc


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = load_report(args.report)
    matrix_start = parse_matrix_start(args.matrix_start_date, report)
    analysis = build_analysis(
        report_path=args.report,
        db_path=args.db,
        source_report_label=args.source_report_label,
        matrix_start=matrix_start,
        variant_a=args.variant_a,
        variant_b=args.variant_b,
        matrix_seed=args.matrix_seed,
    )
    write_outputs(analysis, args.json_out, args.markdown_out)
    print(
        "[aibrief-jp-reach-analysis] "
        f"published={analysis['coverage']['published_reels']} "
        f"coverage={analysis['coverage']['ratio']:.1%} "
        f"json={args.json_out.expanduser().resolve()} "
        f"markdown={args.markdown_out.expanduser().resolve()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
