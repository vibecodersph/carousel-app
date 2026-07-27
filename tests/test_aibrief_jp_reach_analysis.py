from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from scripts import aibrief_jp_reach_analysis as reach_analysis


def raw_payload(
    *,
    base_views: int,
    combined_views: int | None,
    reach: int,
    interactions: int,
    saved: int,
    shares: int,
    skip_rate: float | None = None,
    avg_watch_time_ms: int | None = None,
) -> dict[str, object]:
    values = {
        "views": base_views,
        "reach": reach,
        "likes": max(0, interactions - saved - shares),
        "comments": 0,
        "saved": saved,
        "shares": shares,
        "total_interactions": interactions,
    }
    if combined_views is not None:
        values["total_views"] = combined_views
    if skip_rate is not None:
        values["reels_skip_rate"] = skip_rate
    if avg_watch_time_ms is not None:
        values["ig_reels_avg_watch_time"] = avg_watch_time_ms
    return {
        "data": [
            {"name": name, "period": "lifetime", "values": [{"value": value}]}
            for name, value in values.items()
        ]
    }


def initialize_db(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE reels (
              content_hash TEXT NOT NULL,
              channel_id TEXT NOT NULL,
              title TEXT,
              published_at TEXT,
              scheduled_at TEXT,
              media_id TEXT,
              permalink TEXT,
              status TEXT NOT NULL,
              PRIMARY KEY (content_hash, channel_id)
            );
            CREATE TABLE insights (
              id INTEGER PRIMARY KEY,
              content_hash TEXT NOT NULL,
              channel_id TEXT NOT NULL,
              media_id TEXT NOT NULL,
              captured_at TEXT NOT NULL,
              views INTEGER,
              reach INTEGER,
              likes INTEGER,
              comments INTEGER,
              saved INTEGER,
              shares INTEGER,
              total_interactions INTEGER,
              raw TEXT
            );
            """
        )


def insert_reel(
    db_path: Path,
    *,
    index: int,
    published_at: datetime,
    base_views: int,
    combined_views: int | None,
    reach: int,
    interactions: int,
    saved: int,
    shares: int,
    age_hours: float = 80,
    title: str | None = None,
) -> dict[str, object]:
    content_hash = f"hash-{index}"
    media_id = f"media-{index}"
    captured_at = published_at + timedelta(hours=age_hours)
    raw = raw_payload(
        base_views=base_views,
        combined_views=combined_views,
        reach=reach,
        interactions=interactions,
        saved=saved,
        shares=shares,
    )
    visible_views = combined_views if combined_views is not None else base_views
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO reels (
              content_hash, channel_id, title, published_at, scheduled_at,
              media_id, permalink, status
            ) VALUES (?, 'aibrief_jp', ?, ?, ?, ?, ?, 'published')
            """,
            (
                content_hash,
                title or f"Reel {index}",
                published_at.isoformat(),
                published_at.isoformat(),
                media_id,
                f"https://example.com/{media_id}",
            ),
        )
        connection.execute(
            """
            INSERT INTO insights (
              content_hash, channel_id, media_id, captured_at, views, reach,
              likes, comments, saved, shares, total_interactions, raw
            ) VALUES (?, 'aibrief_jp', ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
            """,
            (
                content_hash,
                media_id,
                captured_at.isoformat(),
                visible_views,
                reach,
                max(0, interactions - saved - shares),
                saved,
                shares,
                interactions,
                json.dumps(raw),
            ),
        )
    return {
        "content_hash": content_hash,
        "channel_id": "aibrief_jp",
        "media_id": media_id,
        "title": title or f"Reel {index}",
        "published_at": published_at.isoformat(),
        "insights": {
            "captured_at": captured_at.isoformat(),
            "has_snapshot": True,
            "metrics": {
                "views": visible_views,
                "reach": reach,
                "saved": saved,
                "shares": shares,
                "total_interactions": interactions,
            },
            "raw_api_payload": raw,
        },
    }


def write_report(path: Path, items: list[dict[str, object]]) -> None:
    path.write_text(
        json.dumps(
            {
                "generated_at": "2026-07-13T09:21:42+00:00",
                "platform": "instagram",
                "channel_filter": "",
                "items": items,
            }
        ),
        encoding="utf-8",
    )


class MetricAndClassificationTests(unittest.TestCase):
    def test_preserves_base_and_combined_scopes_and_mutually_exclusive_winners(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db_path = root / "reels.db"
            report_path = root / "report.json"
            initialize_db(db_path)
            published = datetime(2026, 7, 1, tzinfo=timezone.utc)
            specs = [
                # Complete: native breakout plus efficient high-intent engagement.
                dict(base_views=300, combined_views=900, reach=200, interactions=8, saved=4, shares=1),
                # Distribution only: broad reach, weak engagement quality.
                dict(base_views=300, combined_views=800, reach=200, interactions=4, saved=1, shares=0),
                # Audience fit only: ordinary distribution, dense saves/shares.
                dict(base_views=211, combined_views=218, reach=160, interactions=10, saved=5, shares=2),
                # No winner.
                dict(base_views=170, combined_views=180, reach=130, interactions=2, saved=1, shares=0),
                # Combined-only amplification must not become a distribution winner.
                dict(base_views=170, combined_views=800, reach=130, interactions=2, saved=1, shares=0),
            ]
            items = [
                insert_reel(db_path, index=index, published_at=published, **spec)
                for index, spec in enumerate(specs, start=1)
            ]
            write_report(report_path, items)

            result = reach_analysis.build_analysis(
                report_path=report_path,
                db_path=db_path,
                matrix_start=date(2026, 7, 14),
            )

            self.assertEqual(result["coverage"]["ratio"], 1.0)
            inventory = result["latest_inventory"]
            self.assertEqual(
                inventory["counts"]["meta_all_surface_total_views_gte_500"], 3
            )
            self.assertEqual(
                inventory["counts"]["instagram_base_views_gte_500"], 0
            )
            by_media = {row["media_id"]: row for row in result["reels"]}
            self.assertTrue(all("latest_diagnostics" in row for row in result["reels"]))
            self.assertEqual(by_media["media-1"]["metrics"]["base_views"], 300)
            self.assertEqual(by_media["media-1"]["metrics"]["combined_views"], 900)
            self.assertEqual(by_media["media-1"]["classification"], "COMPLETE_WINNER")
            self.assertEqual(by_media["media-2"]["classification"], "DISTRIBUTION_WINNER")
            self.assertEqual(by_media["media-3"]["classification"], "AUDIENCE_FIT_WINNER")
            self.assertEqual(by_media["media-4"]["classification"], "NO_WINNER")
            self.assertEqual(by_media["media-5"]["classification"], "NO_WINNER")
            self.assertEqual(by_media["media-5"]["amplification"], "AMPLIFICATION_ONLY")
            self.assertEqual(result["evidence_type"], "OBSERVATIONAL_POST_LEVEL_ANALYSIS")
            self.assertFalse(result["causal_effect_estimated"])
            self.assertTrue(result["observational_caveats"])
            self.assertIn(
                "High-engagement/limited-distribution retest signal",
                result["classification_language"]["AUDIENCE_FIT_WINNER"],
            )

    def test_trial_launches_are_excluded_from_regular_baseline_and_monitored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db_path = root / "reels.db"
            report_path = root / "report.json"
            initialize_db(db_path)
            published = datetime(2026, 7, 1, tzinfo=timezone.utc)
            items = [
                insert_reel(
                    db_path,
                    index=index,
                    published_at=published,
                    base_views=300,
                    combined_views=900,
                    reach=200,
                    interactions=8,
                    saved=4,
                    shares=1,
                )
                for index in range(1, 5)
            ]
            with sqlite3.connect(db_path) as connection:
                connection.executescript(
                    """
                    ALTER TABLE reels ADD COLUMN trial_reel INTEGER NOT NULL DEFAULT 0;
                    ALTER TABLE reels ADD COLUMN trial_graduation_strategy TEXT;
                    CREATE TABLE trial_experiments (
                      experiment_id TEXT PRIMARY KEY,
                      content_hash TEXT NOT NULL,
                      channel_id TEXT NOT NULL,
                      case_type TEXT NOT NULL,
                      parent_media_id TEXT,
                      asset_family_id TEXT NOT NULL,
                      baseline_hook TEXT,
                      variant_hook TEXT NOT NULL,
                      changed_variables_json TEXT NOT NULL,
                      state TEXT NOT NULL,
                      graduated_at TEXT,
                      UNIQUE(content_hash, channel_id)
                    );
                    """
                )
                connection.execute(
                    "UPDATE reels SET trial_reel=1, "
                    "trial_graduation_strategy='MANUAL' "
                    "WHERE content_hash IN ('hash-3', 'hash-4')"
                )
                connection.execute(
                    """
                    INSERT INTO trial_experiments (
                      experiment_id, content_hash, channel_id, case_type,
                      parent_media_id, asset_family_id, baseline_hook, variant_hook,
                      changed_variables_json, state, graduated_at
                    ) VALUES (
                      'TRIAL-003', 'hash-3', 'aibrief_jp',
                      'successful_post_variant', 'parent-3', 'family-3',
                      'old 3', 'new 3', '["hook"]', 'active', NULL
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO trial_experiments (
                      experiment_id, content_hash, channel_id, case_type,
                      parent_media_id, asset_family_id, baseline_hook, variant_hook,
                      changed_variables_json, state, graduated_at
                    ) VALUES (
                      'TRIAL-004', 'hash-4', 'aibrief_jp',
                      'scheduled_conversion', NULL, 'family-4',
                      'old 4', 'new 4', '["hook"]', 'graduated',
                      '2026-07-02T16:00:00+00:00'
                    )
                    """
                )
            write_report(report_path, items)

            result = reach_analysis.build_analysis(
                report_path=report_path,
                db_path=db_path,
                matrix_start=date(2026, 7, 14),
            )

            self.assertEqual(result["coverage"]["scope"], "REGULAR_BASELINE_ONLY")
            self.assertEqual(result["coverage"]["published_reels"], 2)
            self.assertEqual(result["coverage"]["all_published_reels"], 4)
            self.assertEqual(result["coverage"]["excluded_trial_reels"], 2)
            self.assertEqual(result["latest_inventory"]["published_reels"], 2)
            self.assertEqual(
                sum(result["counts"]["classification"].values()),
                2,
            )
            monitoring = result["trial_monitoring"]
            self.assertEqual(monitoring["published_reels"], 2)
            self.assertEqual(
                monitoring["by_distribution_cohort"],
                {"GRADUATED_TRIAL": 1, "TRIAL_ACTIVE": 1},
            )
            by_media = {row["media_id"]: row for row in monitoring["reels"]}
            self.assertEqual(
                by_media["media-3"]["trial_experiment"]["experiment_id"],
                "TRIAL-003",
            )
            self.assertEqual(by_media["media-3"]["trial_phase"], "PRE_GRADUATION")
            self.assertEqual(by_media["media-4"]["trial_phase"], "POST_GRADUATION")
            self.assertTrue(
                all(row["trial_reel"] for row in monitoring["reels"])
            )
            markdown = reach_analysis.render_markdown(result)
            self.assertIn("## Trial Reel monitoring", markdown)
            self.assertIn("TRIAL-003", markdown)
            self.assertIn(
                "never enters the regular baseline, including after graduation",
                markdown,
            )

    def test_total_views_falls_back_to_raw_base_without_losing_scope(self) -> None:
        snapshot = {
            "raw_api_payload": raw_payload(
                base_views=190,
                combined_views=None,
                reach=150,
                interactions=4,
                saved=4,
                shares=0,
            ),
            "columns": {"views": 999, "reach": 150, "saved": 4, "shares": 0},
        }

        metrics, warnings = reach_analysis.extract_snapshot_metrics(snapshot)

        self.assertEqual(metrics["base_views"], 190)
        self.assertEqual(metrics["combined_views"], 190)
        self.assertIn("TOTAL_VIEWS_FALLBACK_TO_BASE", warnings)

    def test_total_value_diagnostics_and_v3_columns_are_parsed_without_summing(self) -> None:
        payload = {
            "data": [
                {
                    "name": "ig_reels_avg_watch_time",
                    "period": "lifetime",
                    "total_value": {"value": 4321},
                },
                {
                    "name": "facebook_views",
                    "period": "lifetime",
                    "total_value": {"value": 700},
                },
                {
                    "name": "crossposted_views",
                    "period": "lifetime",
                    "total_value": {"value": 650},
                },
            ]
        }
        snapshot = {
            "id": 1,
            "captured_at": "2026-07-04T08:00:00+00:00",
            "source": "sqlite",
            "raw_api_payload": payload,
            "columns": {
                "views": 1200,
                "total_views": 2200,
                "reach": 900,
                "saved": 4,
                "shares": 2,
                "total_interactions": 8,
            },
        }

        metrics, warnings = reach_analysis.extract_snapshot_metrics(snapshot)

        self.assertEqual(metrics["base_views"], 1200)
        self.assertEqual(metrics["combined_views"], 2200)
        self.assertEqual(metrics["diagnostics"]["ig_reels_avg_watch_time"], 4321)
        self.assertEqual(metrics["diagnostics"]["facebook_views"], 700)
        self.assertEqual(metrics["diagnostics"]["crossposted_views"], 650)
        self.assertNotIn(1350, metrics["diagnostics"].values())
        self.assertIn("BASE_VIEWS_FROM_SEPARATE_V3_COLUMN", warnings)

        classified = reach_analysis.classify_reel(
            {
                "content_hash": "v3",
                "media_id": "v3",
                "title": "V3 retention",
                "published_at": "2026-07-01T00:00:00+00:00",
                "duration_seconds": 10,
                "snapshots": [snapshot],
            },
            coverage=1.0,
        )
        latest = classified["latest_diagnostics"]
        self.assertAlmostEqual(
            latest["avg_watch_to_estimated_duration_ratio_percent"],
            43.21,
        )
        self.assertEqual(latest["captured_at"], snapshot["captured_at"])
        self.assertFalse(latest["is_completion_rate"])
        self.assertIn("not a completion rate", latest["interpretation"])
        self.assertNotIn("avg_watch_percentage", classified["metrics"]["diagnostics"])

    def test_latest_diagnostics_are_separate_from_fixed_performance_snapshot(self) -> None:
        published = datetime(2026, 7, 1, tzinfo=timezone.utc)
        performance = {
            "id": 1,
            "captured_at": (published + timedelta(hours=80)).isoformat(),
            "source": "sqlite",
            "columns": {},
            "raw_api_payload": raw_payload(
                base_views=170,
                combined_views=180,
                reach=130,
                interactions=4,
                saved=3,
                shares=0,
            ),
        }
        latest_payload = raw_payload(
            base_views=999,
            combined_views=1500,
            reach=800,
            interactions=40,
            saved=20,
            shares=5,
        )
        latest_payload["data"].extend(
            [
                {
                    "name": "ig_reels_avg_watch_time",
                    "values": [{"value": 5000}],
                },
                {"name": "reels_skip_rate", "values": [{"value": 42.5}]},
                {"name": "facebook_views", "values": [{"value": 501}]},
                {"name": "crossposted_views", "values": [{"value": 1500}]},
            ]
        )
        latest = {
            "id": 2,
            "captured_at": (published + timedelta(hours=120)).isoformat(),
            "source": "report",
            "columns": {},
            "raw_api_payload": latest_payload,
        }

        classified = reach_analysis.classify_reel(
            {
                "content_hash": "separate",
                "media_id": "separate",
                "title": "Separate snapshots",
                "published_at": published.isoformat(),
                "duration_seconds": 20,
                "snapshots": [performance, latest],
            },
            coverage=1.0,
        )

        self.assertEqual(classified["snapshot_age_hours"], 80)
        self.assertEqual(classified["metrics"]["base_views"], 170)
        self.assertEqual(classified["metrics"]["reach"], 130)
        self.assertEqual(classified["classification"], "AUDIENCE_FIT_WINNER")
        diagnostics = classified["latest_diagnostics"]
        self.assertEqual(diagnostics["snapshot_age_hours"], 120)
        self.assertEqual(diagnostics["captured_at"], latest["captured_at"])
        self.assertEqual(diagnostics["snapshot_source"], "report")
        self.assertEqual(diagnostics["metrics"]["reels_skip_rate"], 42.5)
        self.assertEqual(diagnostics["metrics"]["facebook_views"], 501)
        self.assertAlmostEqual(
            diagnostics["avg_watch_to_estimated_duration_ratio_percent"], 25.0
        )
        self.assertFalse(diagnostics["is_completion_rate"])

    def test_data_hold_for_inverted_scope_and_low_coverage(self) -> None:
        published = datetime(2026, 7, 1, tzinfo=timezone.utc)
        raw = raw_payload(
            base_views=200,
            combined_views=150,
            reach=140,
            interactions=2,
            saved=1,
            shares=0,
        )
        reel = {
            "content_hash": "bad",
            "media_id": "bad",
            "title": "Bad scope",
            "published_at": published.isoformat(),
            "snapshots": [
                {
                    "id": 1,
                    "captured_at": (published + timedelta(hours=80)).isoformat(),
                    "columns": {},
                    "raw_api_payload": raw,
                    "source": "sqlite",
                }
            ],
        }

        inverted = reach_analysis.classify_reel(reel, coverage=1.0)
        low_coverage = reach_analysis.classify_reel(reel, coverage=0.89)

        self.assertEqual(inverted["classification"], "DATA_HOLD")
        self.assertIn("COMBINED_VIEWS_LOWER_THAN_BASE", inverted["data_errors"])
        self.assertIn("COVERAGE_BELOW_90_PERCENT", low_coverage["data_errors"])

    def test_age_selection_and_gates_are_deterministic(self) -> None:
        published = datetime(2026, 7, 1, tzinfo=timezone.utc)

        def snapshot(index: int, age: float) -> dict[str, object]:
            return {
                "id": index,
                "captured_at": (published + timedelta(hours=age)).isoformat(),
                "columns": {},
                "raw_api_payload": raw_payload(
                    base_views=170,
                    combined_views=180,
                    reach=130,
                    interactions=2,
                    saved=1,
                    shares=0,
                ),
                "source": "sqlite",
            }

        selected, age, warnings = reach_analysis.select_snapshot(
            [snapshot(1, 20), snapshot(2, 75), snapshot(3, 90), snapshot(4, 120)],
            published,
        )
        late, late_age, late_warnings = reach_analysis.select_snapshot(
            [snapshot(1, 20), snapshot(4, 120)], published
        )

        self.assertEqual(selected["id"], 2)
        self.assertEqual(age, 75)
        self.assertEqual(warnings, [])
        self.assertEqual(late["id"], 4)
        self.assertEqual(late_age, 120)
        self.assertIn("LATE_SNAPSHOT", late_warnings)

        early_reel = {
            "content_hash": "early",
            "media_id": "early",
            "title": "Early",
            "published_at": published.isoformat(),
            "snapshots": [snapshot(1, 20)],
        }
        provisional_reel = {**early_reel, "snapshots": [snapshot(1, 48)]}
        self.assertEqual(
            reach_analysis.classify_reel(early_reel, coverage=1.0)["classification"],
            "MONITOR_EARLY",
        )
        self.assertTrue(
            reach_analysis.classify_reel(provisional_reel, coverage=1.0)[
                "classification"
            ].startswith("PROVISIONAL_")
        )

    def test_exact_age_and_performance_band_boundaries(self) -> None:
        published = datetime(2026, 7, 1, tzinfo=timezone.utc)

        def classify_at(age: float) -> dict[str, object]:
            return reach_analysis.classify_reel(
                {
                    "content_hash": str(age),
                    "media_id": str(age),
                    "title": str(age),
                    "published_at": published.isoformat(),
                    "snapshots": [{
                        "id": 1,
                        "captured_at": (published + timedelta(hours=age)).isoformat(),
                        "columns": {},
                        "raw_api_payload": raw_payload(
                            base_views=170,
                            combined_views=180,
                            reach=130,
                            interactions=2,
                            saved=1,
                            shares=0,
                        ),
                        "source": "sqlite",
                    }],
                },
                coverage=1.0,
            )

        self.assertEqual(classify_at(23.999)["stage"], "MONITOR_EARLY")
        self.assertEqual(classify_at(24)["stage"], "PROVISIONAL")
        self.assertEqual(classify_at(71.999)["stage"], "PROVISIONAL")
        self.assertEqual(classify_at(72)["stage"], "DECISION_READY")
        self.assertNotIn("LATE_SNAPSHOT", classify_at(96)["warnings"])
        self.assertIn("LATE_SNAPSHOT", classify_at(96.001)["warnings"])

        boundaries = {
            "combined_views": ((163, "below"), (164, "normal"), (398, "strong"), (750, "breakout")),
            "base_views": ((150, "below"), (151, "normal"), (192, "strong"), (250, "breakout")),
            "reach": ((124, "below"), (125, "normal"), (150, "strong"), (185, "breakout")),
            "total_interactions": ((0, "below"), (1, "normal"), (4, "strong"), (7, "breakout")),
        }
        for name, cases in boundaries.items():
            for value, expected in cases:
                self.assertEqual(reach_analysis.metric_tier(name, value), expected)

    def test_audience_fit_rate_and_denominator_gates(self) -> None:
        def candidate(*, reach: int, interactions: int, saved: int) -> dict[str, object]:
            return reach_analysis.winner_candidate(
                {
                    "base_views": 170,
                    "combined_views": 180,
                    "reach": reach,
                    "total_interactions": interactions,
                    "saved": saved,
                    "shares": 0,
                }
            )

        self.assertTrue(candidate(reach=130, interactions=4, saved=3)["audience_fit"])
        self.assertFalse(candidate(reach=131, interactions=4, saved=3)["audience_fit"])
        self.assertTrue(candidate(reach=1_000, interactions=14, saved=14)["audience_fit"])
        self.assertFalse(candidate(reach=1_000, interactions=14, saved=13)["audience_fit"])
        self.assertFalse(candidate(reach=99, interactions=7, saved=3)["audience_fit"])


class GrowthAnalysisTests(unittest.TestCase):
    @staticmethod
    def reel(index: int, *, include_retention: bool = False) -> dict[str, object]:
        published = datetime(2026, 7, 1, tzinfo=timezone.utc)

        def snapshot(
            snapshot_id: int,
            age_hours: float,
            *,
            base_views: int,
            reach: int,
            interactions: int,
            saved: int,
            shares: int,
            selected_early: bool = False,
        ) -> dict[str, object]:
            return {
                "id": snapshot_id,
                "captured_at": (published + timedelta(hours=age_hours)).isoformat(),
                "source": "sqlite",
                "columns": {},
                "raw_api_payload": raw_payload(
                    base_views=base_views,
                    combined_views=10_000 + index,
                    reach=reach,
                    interactions=interactions,
                    saved=saved,
                    shares=shares,
                    skip_rate=(35.0 + index if include_retention and selected_early else None),
                    avg_watch_time_ms=(2_000 + 100 * index if include_retention and selected_early else None),
                ),
            }

        early_base = 90 + 6 * index
        early_reach = 70 + 5 * index
        return {
            "content_hash": f"growth-{index}",
            "channel_id": "aibrief_jp",
            "media_id": f"growth-media-{index}",
            "title": f"Growth reel {index}",
            "published_at": published.isoformat(),
            # Deliberately out of order: the audit must use the first snapshot
            # in each strict age window, not the latest snapshot in that window.
            "snapshots": [
                snapshot(
                    4,
                    90,
                    base_views=180 + 11 * index,
                    reach=140 + 9 * index,
                    interactions=8 + index,
                    saved=3,
                    shares=1,
                ),
                snapshot(
                    2,
                    20,
                    base_views=130 + 7 * index,
                    reach=100 + 6 * index,
                    interactions=5 + index,
                    saved=2,
                    shares=1,
                ),
                snapshot(
                    3,
                    75,
                    base_views=145 + 9 * index,
                    reach=112 + 7 * index,
                    interactions=7 + index,
                    saved=3,
                    shares=1,
                ),
                snapshot(
                    1,
                    5,
                    base_views=early_base,
                    reach=early_reach,
                    interactions=2 + index,
                    saved=1 + index % 3,
                    shares=index % 2,
                    selected_early=True,
                ),
            ],
        }

    def test_latest_inventory_uses_newest_snapshot_and_never_mixes_view_scopes(self) -> None:
        published = datetime(2026, 7, 1, tzinfo=timezone.utc)

        def snapshot(
            snapshot_id: int,
            age_hours: int,
            *,
            base_views: int,
            combined_views: int | None,
            interactions: int,
            saved: int,
            shares: int,
            include_surface_fields: bool = False,
        ) -> dict[str, object]:
            payload = raw_payload(
                base_views=base_views,
                combined_views=combined_views,
                reach=400,
                interactions=interactions,
                saved=saved,
                shares=shares,
            )
            if include_surface_fields:
                payload["data"].extend(
                    [
                        {"name": "facebook_views", "values": [{"value": 250}]},
                        {"name": "crossposted_views", "values": [{"value": 600}]},
                    ]
                )
            return {
                "id": snapshot_id,
                "captured_at": (published + timedelta(hours=age_hours)).isoformat(),
                "columns": {},
                "raw_api_payload": payload,
                "source": "sqlite",
            }

        reels = [
            {
                "media_id": "latest-1",
                "reel_transcript": "冒頭の日本語トランスクリプト",
                "snapshots": [
                    snapshot(
                        1,
                        80,
                        base_views=800,
                        combined_views=900,
                        interactions=9,
                        saved=2,
                        shares=1,
                    ),
                    snapshot(
                        2,
                        120,
                        base_views=499,
                        combined_views=600,
                        interactions=7,
                        saved=0,
                        shares=0,
                        include_surface_fields=True,
                    ),
                ],
            },
            {
                "media_id": "latest-2",
                "reel_transcript": "",
                "snapshots": [
                    snapshot(
                        3,
                        100,
                        base_views=550,
                        combined_views=None,
                        interactions=6,
                        saved=1,
                        shares=2,
                    )
                ],
            },
        ]

        result = reach_analysis.latest_inventory_summary(
            reels,
            reference_time=published + timedelta(hours=130),
        )

        self.assertEqual(result["label"], "CURRENT_LATEST_INVENTORY")
        self.assertEqual(result["synced_n"], 2)
        self.assertEqual(result["counts"]["meta_all_surface_total_views_gte_500"], 1)
        self.assertEqual(result["counts"]["instagram_base_views_gte_500"], 1)
        self.assertEqual(
            result["counts"]["ig_facebook_crossposted_views_gte_500"], 1
        )
        self.assertEqual(result["availability"]["meta_all_surface_total_views_n"], 1)
        self.assertEqual(result["counts"]["zero_share"], 1)
        self.assertEqual(result["counts"]["zero_save"], 1)
        self.assertEqual(result["counts"]["total_interactions_gte_7"], 1)
        self.assertEqual(result["availability"]["facebook_views_n"], 1)
        self.assertEqual(result["availability"]["crossposted_views_n"], 1)
        self.assertEqual(result["freshness"]["fresh_n"], 1)
        self.assertEqual(result["freshness"]["stale_n"], 1)
        self.assertEqual(result["transcript_coverage"]["available_n"], 1)
        self.assertEqual(result["transcript_coverage"]["missing_n"], 1)
        self.assertIn("never added together", result["scope_guardrail"])
        self.assertIn("explicitly aggregates Instagram and Facebook", result["scope_guardrail"])

    def test_small_pair_set_uses_exact_test_and_blocks_inference(self) -> None:
        result = reach_analysis.early_to_fixed_growth_analysis(
            [self.reel(index) for index in range(1, 8)]
        )

        self.assertEqual(result["status"], "INSUFFICIENT_PAIRED_SAMPLE")
        self.assertFalse(result["inference_allowed"])
        self.assertFalse(result["causal_effect_estimated"])
        self.assertEqual(result["paired_n"], 7)
        self.assertEqual(result["age_hours"]["early"]["min"], 5)
        self.assertEqual(result["age_hours"]["fixed"]["max"], 75)
        self.assertFalse(result["selection_rules"]["combined_total_views_used"])

        pairs = {
            pair["media_id"]: pair
            for pair in result["ranked_pairs_by_later_reach"]
        }
        first = pairs["growth-media-1"]
        self.assertEqual(first["early_age_hours"], 5)
        self.assertEqual(first["fixed_age_hours"], 75)
        self.assertEqual(first["early_base_views"], 96)
        self.assertEqual(first["later_base_views"], 154)

        retention = result["correlations"]["early_skip_rate_percent"]
        self.assertEqual(retention["n"], 0)
        self.assertEqual(retention["status"], "UNAVAILABLE_OR_INSUFFICIENT")
        association = result["correlations"]["early_reach"]["outcomes"][
            "later_reach"
        ]
        self.assertEqual(
            association["pearson"]["permutation_method"],
            "EXACT_ALL_LABEL_PERMUTATIONS",
        )
        self.assertEqual(association["pearson"]["permutations"], 5_040)
        self.assertTrue(any("cumulative" in caveat for caveat in result["caveats"]))
        self.assertTrue(any("strict windows" in caveat for caveat in result["caveats"]))

    def test_twelve_pairs_enable_only_deterministic_association_inference(self) -> None:
        reels = [self.reel(index, include_retention=True) for index in range(1, 13)]
        first = reach_analysis.early_to_fixed_growth_analysis(
            reels,
            permutations=100,
            seed=1234,
        )
        second = reach_analysis.early_to_fixed_growth_analysis(
            reels,
            permutations=100,
            seed=1234,
        )

        self.assertEqual(first, second)
        self.assertEqual(first["status"], "READY_FOR_ASSOCIATION_INFERENCE")
        self.assertTrue(first["inference_allowed"])
        self.assertFalse(first["causal_effect_estimated"])
        self.assertEqual(first["paired_n"], 12)
        self.assertEqual(first["correlations"]["early_skip_rate_percent"]["n"], 12)
        self.assertEqual(first["correlations"]["early_avg_watch_time_ms"]["n"], 12)

        association = first["correlations"]["early_avg_watch_time_ms"]["outcomes"][
            "later_base_views"
        ]
        self.assertEqual(
            association["spearman"]["permutation_method"],
            "FIXED_SEED_MONTE_CARLO",
        )
        self.assertEqual(association["spearman"]["permutations"], 100)
        self.assertIsNotNone(association["spearman"]["seed"])


class SlotAndMatrixTests(unittest.TestCase):
    def test_derives_canonical_slots_from_published_at(self) -> None:
        self.assertEqual(reach_analysis.canonical_slot("2026-07-01T08:00:00+09:00"), "09")
        self.assertEqual(reach_analysis.canonical_slot("2026-07-01T10:29:00+09:00"), "09")
        self.assertEqual(reach_analysis.canonical_slot("2026-07-01T13:15:00+09:00"), "13")
        self.assertEqual(reach_analysis.canonical_slot("2026-07-01T19:29:00+09:00"), "18")
        self.assertEqual(reach_analysis.canonical_slot("2026-07-01T22:29:00+09:00"), "21")
        self.assertIsNone(reach_analysis.canonical_slot("2026-07-01T11:00:00+09:00"))

    def test_matched_date_permutation_is_deterministic_and_finds_large_slot_lift(self) -> None:
        records = []
        start = date(2026, 7, 1)
        values = {
            "09": (260, 200),
            "13": (180, 140),
            "18": (170, 130),
            "21": (160, 120),
        }
        for day_index in range(8):
            current = start + timedelta(days=day_index)
            for slot in reach_analysis.SLOTS:
                hour = int(slot)
                published = datetime(
                    current.year,
                    current.month,
                    current.day,
                    hour,
                    tzinfo=reach_analysis.JST,
                )
                base_views, reel_reach = values[slot]
                records.append(
                    {
                        "media_id": f"{current}-{slot}",
                        "title": f"{current} {slot}",
                        "published_at": published.isoformat(),
                        "snapshot_age_hours": 75,
                        "data_errors": [],
                        "metrics": {
                            "base_views": base_views,
                            "reach": reel_reach,
                        },
                    }
                )

        first = reach_analysis.analyze_slots(records)
        second = reach_analysis.analyze_slots(records)

        self.assertEqual(first, second)
        self.assertEqual(first["status"], "READY")
        self.assertEqual(first["estimability"], "OBSERVATIONAL_ASSOCIATION_ONLY")
        self.assertFalse(first["causal_effect_estimated"])
        self.assertIn("not causal", first["interpretation"])
        self.assertEqual(first["complete_date_count"], 8)
        self.assertIn("09", first["favorable_slots"])
        self.assertIn("09", first["associated_higher_reach_slots"])
        self.assertLess(
            first["slot_metrics"]["09"]["reach"]["one_sided_permutation_p"],
            0.05,
        )
        self.assertEqual(first["slot_metrics"]["09"]["reach"]["win_rate"], 1.0)

    def test_insufficient_slots_are_explicitly_not_estimable(self) -> None:
        result = reach_analysis.analyze_slots([])

        self.assertEqual(result["status"], "INSUFFICIENT_MATCHED_DATES")
        self.assertEqual(result["estimability"], "NOT_ESTIMABLE")
        self.assertIn("not estimable", result["interpretation"])
        self.assertEqual(result["associated_higher_reach_slots"], [])
        self.assertFalse(result["causal_effect_estimated"])

    def test_four_slot_ab_matrix_is_balanced_by_slot_day_and_weekday(self) -> None:
        matrix = reach_analysis.build_ab_matrix(date(2026, 7, 14))
        same_seed = reach_analysis.build_ab_matrix(date(2026, 7, 14))
        other_seed = reach_analysis.build_ab_matrix(date(2026, 7, 14), seed=12345)

        variant_a = reach_analysis.DEFAULT_VARIANT_A
        variant_b = reach_analysis.DEFAULT_VARIANT_B

        self.assertEqual(matrix, same_seed)
        self.assertNotEqual(matrix["days"], other_seed["days"])
        self.assertEqual(matrix["randomization_seed"], reach_analysis.MATRIX_RANDOMIZATION_SEED)
        self.assertTrue(matrix["assignment_randomized"])
        self.assertTrue(matrix["treatment_is_bundle"])
        self.assertFalse(matrix["causal_effect_estimated"])
        self.assertIn("quasi-experiment", matrix["causal_interpretation"])
        self.assertTrue(matrix["matching_requirements"])
        self.assertEqual(len(matrix["days"]), 14)
        for slot in reach_analysis.SLOTS:
            self.assertEqual(
                matrix["balance_by_slot"][slot], {variant_a: 7, variant_b: 7}
            )
        for day in matrix["days"]:
            self.assertEqual(
                Counter(day["assignments"].values()),
                Counter({variant_a: 2, variant_b: 2}),
            )
        for slots in matrix["balance_by_weekday_and_slot"].values():
            for counts in slots.values():
                self.assertEqual(counts, {variant_a: 1, variant_b: 1})
        self.assertIn("Original Japanese opening", matrix["variant_definitions"][variant_b])


class CliTests(unittest.TestCase):
    def test_cli_writes_requested_json_and_markdown_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db_path = root / "reels.db"
            report_path = root / "report.json"
            json_out = root / "nested" / "analysis.json"
            markdown_out = root / "nested" / "analysis.md"
            initialize_db(db_path)
            item = insert_reel(
                db_path,
                index=1,
                published_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
                base_views=300,
                combined_views=900,
                reach=200,
                interactions=8,
                saved=4,
                shares=1,
            )
            write_report(report_path, [item])

            rc = reach_analysis.main(
                [
                    "--report",
                    str(report_path),
                    "--db",
                    str(db_path),
                    "--json-out",
                    str(json_out),
                    "--markdown-out",
                    str(markdown_out),
                    "--matrix-start-date",
                    "2026-07-14",
                ]
            )

            self.assertEqual(rc, 0)
            self.assertTrue(json_out.is_file())
            self.assertTrue(markdown_out.is_file())
            payload = json.loads(json_out.read_text(encoding="utf-8"))
            self.assertEqual(payload["reels"][0]["classification"], "COMPLETE_WINNER")
            self.assertIn("latest_inventory", payload)
            self.assertIn("early_to_fixed_growth_analysis", payload)
            markdown = markdown_out.read_text(encoding="utf-8")
            self.assertIn("# AI Brief JP Reach Analysis", markdown)
            self.assertIn("## Interpretation guardrails", markdown)
            self.assertIn("## Current/latest inventory", markdown)
            self.assertIn("## Early-to-fixed growth analysis", markdown)
            self.assertIn("INSUFFICIENT_PAIRED_SAMPLE", markdown)
            self.assertIn("not completion", markdown)
            self.assertIn("quasi-experiment matrix", markdown)
            self.assertIn("not estimable", markdown)


if __name__ == "__main__":
    unittest.main()
