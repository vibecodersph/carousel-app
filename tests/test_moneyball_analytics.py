import csv
import io
import json
import math
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import moneyball_analytics as moneyball
import reel_ledger


FIXED_NOW = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)


def make_observation(
    media_id,
    *,
    reach=None,
    views=None,
    follows=None,
    shares=None,
    sends=None,
    saves=None,
    likes=None,
    comments=None,
    profile_visits=None,
    returning_viewers=None,
    average_watch_time_seconds=None,
    total_watch_time_seconds=None,
    duration_seconds=10,
    production_minutes=None,
    maturity_window="24h",
    published_at="2026-07-20T00:00:00+00:00",
):
    raw_metrics = {
        "reach": reach,
        "views": views,
        "follows": follows,
        "shares": shares,
        "sends": sends,
        "saves": saves,
        "likes": likes,
        "comments": comments,
        "profile_visits": profile_visits,
        "returning_viewers": returning_viewers,
        "average_watch_time_seconds": average_watch_time_seconds,
        "total_watch_time_seconds": total_watch_time_seconds,
        "duration_seconds": duration_seconds,
    }
    metadata = {
        "format": "reel",
        "duration_bucket": "0–15 seconds",
        "production_minutes": production_minutes,
    }
    return {
        "identity": {
            "media_id": media_id,
            "content_hash": f"hash-{media_id}",
            "published_at": published_at,
            "permalink": f"https://www.instagram.com/reel/{media_id}/",
        },
        "content_metadata": metadata,
        "maturity_window": maturity_window,
        "actual_age_hours": 24.5,
        "raw_metrics": raw_metrics,
        "derived_metrics": moneyball.compute_post_metrics(raw_metrics, metadata),
    }


def make_baseline(observations):
    baseline = moneyball.summarize_cohort(
        observations,
        maturity_window="24h",
        dimension="account",
        value="all",
        quartiles_min_n=4,
    )
    baseline["_observations"] = list(observations)
    return baseline


def make_experiment_post(
    media_id,
    *,
    experiment_id="exp-1",
    variant="control",
    changed_variable="hook_style",
    follows=2,
    reach=100,
):
    observation = make_observation(
        media_id,
        reach=reach,
        follows=follows,
        shares=2,
        saves=2,
    )
    metadata = {
        "experiment_id": experiment_id,
        "experiment_variant": variant,
        "changed_variable": changed_variable,
        "series": "AI brief",
        "content_goal": "utility",
        "topic": "agents",
        "source": "research",
        "format": "reel",
        "duration_bucket": "0–15 seconds",
        "posting_window": "09:00 JST",
        "trial_reel": False,
    }
    observation["content_metadata"] = metadata
    return {
        "identity": {
            "media_id": media_id,
            "content_hash": f"hash-{media_id}",
            "published_at": "2026-07-20T00:00:00+00:00",
        },
        "content_metadata": metadata,
        "maturity_windows": {"24h": observation},
        "trial_experiment": {},
    }


def make_series_post(
    media_id,
    *,
    series,
    follows,
    reach=100,
    production_minutes=30,
    diagnostic=None,
    day=20,
):
    observation = make_observation(
        media_id,
        reach=reach,
        follows=follows,
        shares=0,
        saves=0,
        average_watch_time_seconds=4,
        duration_seconds=10,
        production_minutes=production_minutes,
        maturity_window="72h",
        published_at=f"2026-07-{day:02d}T00:00:00+00:00",
    )
    metadata = dict(
        observation["content_metadata"],
        series=series,
        content_goal="utility",
    )
    observation["content_metadata"] = metadata
    post = {
        "identity": dict(observation["identity"]),
        "content_metadata": metadata,
        "maturity_windows": {"72h": observation},
        "classifications": [],
        "funnel_diagnostics": [],
    }
    if diagnostic:
        post["funnel_diagnostics"] = [{"diagnostic": diagnostic}]
    return post


def make_series_account_baseline():
    return {
        "dimension": "account",
        "value": "all",
        "post_count": 20,
        "metrics": {
            "follow_conversion": {
                "reach": {
                    "n": 20,
                    "median": 10,
                    "p25": 5,
                    "p75": 15,
                    "min": 0,
                    "max": 30,
                },
                "non_follower_reach": {
                    "n": 0,
                    "median": None,
                    "p25": None,
                    "p75": None,
                    "min": None,
                    "max": None,
                },
            },
            "follows_per_production_hour": {
                "n": 20,
                "median": 2,
                "p25": 1,
                "p75": 3,
                "min": 0,
                "max": 5,
            },
            "watch_depth": {
                "n": 20,
                "median": 0.5,
                "p25": 0.3,
                "p75": 0.9,
                "min": 0.1,
                "max": 1.2,
            },
            "shares_per_1000_reach": {
                "n": 20,
                "median": 10,
                "p25": 5,
                "p75": 50,
                "min": 0,
                "max": 100,
            },
            "saves_per_1000_reach": {
                "n": 20,
                "median": 10,
                "p25": 5,
                "p75": 50,
                "min": 0,
                "max": 100,
            },
        },
    }


class MoneyballMathTests(unittest.TestCase):
    def test_safe_divide_rejects_missing_zero_negative_nonfinite_and_bool(self):
        self.assertEqual(moneyball.safe_divide(9, 3), 3.0)
        for numerator, denominator in (
            (None, 3),
            (3, None),
            (3, 0),
            (3, -1),
            (math.nan, 3),
            (math.inf, 3),
            (3, math.nan),
            (3, math.inf),
            (True, 1),
            (1, False),
        ):
            with self.subTest(numerator=numerator, denominator=denominator):
                self.assertIsNone(moneyball.safe_divide(numerator, denominator))

    def test_direct_average_watch_time_and_uncapped_watch_depth(self):
        metrics = moneyball.compute_post_metrics(
            {
                "average_watch_time_seconds": 12,
                "total_watch_time_seconds": 120,
                "duration_seconds": 10,
            },
            {},
        )
        self.assertEqual(metrics["average_watch_time_source"], "direct_meta_metric")
        self.assertEqual(metrics["average_watch_time_seconds"], 12)
        self.assertAlmostEqual(metrics["watch_depth"], 1.2)
        self.assertAlmostEqual(metrics["total_watch_hours"], 120 / 3600)

    def test_average_watch_fallback_requires_verified_plays_semantics(self):
        raw = {
            "total_watch_time_seconds": 30,
            "plays": 3,
            "duration_seconds": 5,
        }
        unavailable = moneyball.compute_post_metrics(
            raw, {}, plays_semantics_verified=False
        )
        self.assertIsNone(unavailable["average_watch_time_seconds"])
        self.assertIsNone(unavailable["watch_depth"])
        self.assertEqual(unavailable["average_watch_time_source"], "unavailable")

        verified = moneyball.compute_post_metrics(
            raw, {}, plays_semantics_verified=True
        )
        self.assertEqual(verified["average_watch_time_seconds"], 10)
        self.assertEqual(verified["watch_depth"], 2)
        self.assertEqual(
            verified["average_watch_time_source"],
            "total_watch_time_seconds / verified_plays",
        )

    def test_graph_watch_time_milliseconds_are_normalized_to_seconds(self):
        raw_payload = {
            "data": [
                {
                    "name": "ig_reels_video_view_total_time",
                    "values": [{"value": 60000}],
                },
                {
                    "name": "ig_reels_avg_watch_time",
                    "values": [{"value": 12000}],
                },
                {
                    "name": "reels_skip_rate",
                    "values": [{"value": 56.3}],
                },
                {
                    "name": "total_views",
                    "values": [{"value": 504}],
                },
                {
                    "name": "facebook_views",
                    "values": [{"value": 314}],
                },
                {
                    "name": "crossposted_views",
                    "values": [{"value": 504}],
                },
                {
                    "name": "clips_replays_count",
                    "values": [{"value": 7}],
                },
                {
                    "name": "reposts",
                    "values": [{"value": 3}],
                },
            ]
        }
        canonical, provenance = moneyball.canonical_snapshot_metrics(
            {"raw": raw_payload}, duration_seconds=10
        )
        self.assertEqual(canonical["total_watch_time_seconds"], 60)
        self.assertEqual(canonical["average_watch_time_seconds"], 12)
        self.assertEqual(canonical["reels_skip_rate"], 56.3)
        self.assertEqual(canonical["total_views"], 504)
        self.assertEqual(canonical["facebook_views"], 314)
        self.assertEqual(canonical["crossposted_views"], 504)
        self.assertEqual(canonical["replays"], 7)
        self.assertEqual(canonical["reposts"], 3)
        self.assertEqual(
            provenance["canonical_fields"]["average_watch_time_seconds"][
                "source_unit"
            ],
            "milliseconds",
        )
        derived = moneyball.compute_post_metrics(canonical, {})
        self.assertEqual(derived["watch_depth"], 1.2)

    def test_total_interactions_per_reach_is_transparent_and_safe(self):
        metrics = moneyball.compute_post_metrics(
            {
                "reach": 250,
                "views": 400,
                "interactions": 20,
                "reposts": 2,
            },
            {},
        )
        self.assertEqual(metrics["interactions_per_1000_reach"], 80)
        self.assertAlmostEqual(metrics["engagement_rate_by_reach"], 0.08)
        self.assertEqual(metrics["views_per_reached_account"], 1.6)
        self.assertEqual(metrics["reposts_per_1000_reach"], 8)
        self.assertIsNone(metrics["interactions_per_1000_views"])

        missing = moneyball.compute_post_metrics(
            {"reach": None, "views": 400, "interactions": 20},
            {},
        )
        self.assertIsNone(missing["interactions_per_1000_reach"])
        self.assertIsNone(missing["engagement_rate_by_reach"])
        self.assertIsNone(missing["views_per_reached_account"])
        self.assertEqual(missing["interactions_per_1000_views"], 50)

    def test_missing_reach_uses_labeled_view_rates_but_not_reach_rates(self):
        metrics = moneyball.compute_post_metrics(
            {
                "reach": None,
                "views": 100,
                "shares": 2,
                "saves": 3,
            },
            {},
        )
        self.assertIsNone(metrics["shares_per_1000_reach"])
        self.assertEqual(metrics["shares_per_1000_views"], 20)
        self.assertIsNone(metrics["intent_actions_per_1000_reach"])
        self.assertEqual(metrics["intent_actions_per_1000_views"], 50)
        self.assertEqual(metrics["satisfaction_rate"]["denominator_type"], "views")
        self.assertTrue(
            any("excluded from reach-based rankings" in warning for warning in metrics["warnings"])
        )

    def test_follow_conversion_prefers_non_follower_reach_and_keeps_buckets_separate(self):
        preferred = make_observation(
            "preferred",
            reach=1000,
            follows=5,
        )
        preferred["raw_metrics"]["non_follower_reach"] = 500
        preferred["derived_metrics"] = moneyball.compute_post_metrics(
            preferred["raw_metrics"], preferred["content_metadata"]
        )
        fallback = make_observation("fallback", reach=1000, follows=5)

        preferred_metrics = preferred["derived_metrics"]
        self.assertEqual(
            preferred_metrics["follows_per_1000_non_follower_reach"], 10
        )
        self.assertEqual(preferred_metrics["follows_per_1000_reach"], 5)
        self.assertEqual(
            preferred_metrics["follow_conversion"]["denominator_type"],
            "non_follower_reach",
        )
        self.assertEqual(
            fallback["derived_metrics"]["follow_conversion"]["denominator_type"],
            "reach",
        )

        cohort = moneyball.summarize_cohort(
            [preferred, fallback],
            maturity_window="24h",
            dimension="account",
            value="all",
            quartiles_min_n=4,
        )
        conversion = cohort["metrics"]["follow_conversion"]
        self.assertEqual(conversion["non_follower_reach"]["n"], 1)
        self.assertEqual(conversion["reach"]["n"], 1)
        self.assertEqual(conversion["non_follower_reach"]["median"], 10)
        self.assertEqual(conversion["reach"]["median"], 5)

    def test_shares_and_sends_are_never_double_counted(self):
        both = moneyball.compute_post_metrics(
            {
                "reach": 100,
                "shares": 4,
                "sends": 7,
                "saves": 3,
            },
            {},
        )
        self.assertEqual(both["intent_actions"], 7)
        self.assertEqual(both["intent_actions_per_1000_reach"], 70)
        self.assertEqual(both["intent_action_source"], "shares_plus_saves")
        self.assertTrue(any("not added" in warning for warning in both["warnings"]))

        sends_only = moneyball.compute_post_metrics(
            {"reach": 100, "shares": None, "sends": 7, "saves": 3}, {}
        )
        self.assertEqual(sends_only["intent_actions"], 10)
        self.assertEqual(sends_only["intent_action_source"], "sends_plus_saves")

    def test_missing_or_zero_production_time_keeps_efficiency_unavailable(self):
        raw = {
            "views": 125,
            "reach": 100,
            "follows": 2,
            "shares": 4,
            "saves": 3,
            "total_watch_time_seconds": 360,
        }
        for production_minutes in (None, 0):
            with self.subTest(production_minutes=production_minutes):
                metrics = moneyball.compute_post_metrics(
                    raw, {"production_minutes": production_minutes}
                )
                if production_minutes is None:
                    self.assertIsNone(metrics["production_hours"])
                else:
                    self.assertEqual(metrics["production_hours"], 0)
                self.assertIsNone(metrics["follows_per_production_hour"])
                self.assertIsNone(metrics["shares_per_production_hour"])
                self.assertIsNone(metrics["saves_per_production_hour"])
                self.assertIsNone(metrics["watch_hours_per_production_hour"])
                self.assertIsNone(metrics["reach_per_production_hour"])
                self.assertIsNone(metrics["views_per_production_hour"])

    def test_missing_follows_is_unavailable_while_measured_zero_remains_zero(self):
        missing = moneyball.compute_post_metrics({"reach": 100, "follows": None}, {})
        measured_zero = moneyball.compute_post_metrics(
            {"reach": 100, "follows": 0}, {}
        )
        self.assertIsNone(missing["follow_conversion"]["value"])
        self.assertIsNone(missing["follow_conversion"]["denominator_type"])
        self.assertEqual(measured_zero["follow_conversion"]["value"], 0)
        self.assertEqual(
            measured_zero["follow_conversion"]["denominator_type"], "reach"
        )

    def test_linear_percentiles_and_tied_midranks(self):
        values = [1, 2, 3, 4]
        self.assertEqual(moneyball.percentile(values, 0.25), 1.75)
        self.assertEqual(moneyball.percentile(values, 0.5), 2.5)
        self.assertEqual(moneyball.percentile(values, 0.75), 3.25)
        self.assertEqual(moneyball.percentile_rank([1, 2, 2, 4], 2), 50)
        self.assertIsNone(moneyball.percentile([], 0.5))
        self.assertIsNone(moneyball.percentile(values, 1.1))


class MoneyballSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.published = FIXED_NOW

    def snapshot(self, snapshot_id, age_hours, media_id="target", reach=10):
        return {
            "id": snapshot_id,
            "media_id": media_id,
            "captured_at": (
                self.published + timedelta(hours=age_hours)
            ).isoformat(),
            "raw_metrics": {"reach": reach, "views": reach},
        }

    def test_window_match_is_at_or_after_nearest_within_tolerance_and_same_media(self):
        snapshots = [
            self.snapshot(1, 1.99),
            self.snapshot(2, 2.01, media_id="other"),
            self.snapshot(3, 2.40),
            self.snapshot(4, 2.20),
            self.snapshot(5, 3.01),
        ]
        window = {"target_hours": 2, "max_hours_after_target": 1}
        selected = moneyball.select_window_snapshot(
            snapshots,
            self.published.isoformat(),
            window,
            media_id="target",
        )
        self.assertEqual(selected["insight_id"], 4)
        self.assertAlmostEqual(selected["actual_age_hours"], 2.2)
        self.assertEqual(selected["target_age_hours"], 2)

        excluded_by_as_of = moneyball.select_window_snapshot(
            snapshots,
            self.published.isoformat(),
            window,
            media_id="target",
            as_of=self.published + timedelta(hours=2.1),
        )
        self.assertIsNone(excluded_by_as_of)

    def test_window_tolerance_and_missing_snapshot_do_not_fabricate_history(self):
        too_late = [self.snapshot(1, 30)]
        selected = moneyball.select_window_snapshot(
            too_late,
            self.published.isoformat(),
            {"target_hours": 24, "max_hours_after_target": 4},
            media_id="target",
        )
        self.assertIsNone(selected)

        growth = moneyball.growth_curve_metrics(
            {
                "latest": make_observation("latest", reach=999, follows=99),
                "72h": make_observation("72h", reach=300, follows=6),
            }
        )
        self.assertIsNone(growth["reach_delta_2h_to_24h"])
        self.assertIsNone(growth["reach_delta_24h_to_72h"])
        self.assertIsNone(growth["reach_7d_vs_24h_ratio"])
        self.assertIsNone(growth["follow_7d_vs_24h_ratio"])

    def test_latest_snapshot_respects_as_of_and_media_identity(self):
        snapshots = [
            self.snapshot(1, 2, media_id="target"),
            self.snapshot(2, 3, media_id="other"),
            self.snapshot(3, 4, media_id="target"),
        ]
        selected = moneyball.latest_snapshot(
            snapshots,
            media_id="target",
            as_of=self.published + timedelta(hours=3),
        )
        self.assertEqual(selected["insight_id"], 1)

    def test_exact_duplicate_collapse_is_idempotent_but_later_fetches_survive(self):
        first = self.snapshot(1, 2)
        exact_duplicate = {**first, "id": 2}
        later_same_values = self.snapshot(3, 3)
        other_media = self.snapshot(4, 2, media_id="other")
        collapsed, duplicate_count = moneyball.collapse_exact_snapshot_duplicates(
            [later_same_values, exact_duplicate, other_media, first]
        )
        self.assertEqual(duplicate_count, 1)
        self.assertEqual(len(collapsed), 3)
        self.assertEqual(
            {(row["media_id"], row["captured_at"]) for row in collapsed},
            {
                ("target", first["captured_at"]),
                ("target", later_same_values["captured_at"]),
                ("other", other_media["captured_at"]),
            },
        )

    def test_growth_curve_uses_only_real_window_observations(self):
        windows = {
            "2h": make_observation("2h", reach=10, follows=1),
            "24h": make_observation("24h", reach=20, follows=3),
            "72h": make_observation("72h", reach=50, follows=4),
            "7d": make_observation("7d", reach=100, follows=10),
        }
        growth = moneyball.growth_curve_metrics(windows)
        self.assertEqual(growth["reach_delta_2h_to_24h"], 10)
        self.assertEqual(growth["reach_delta_24h_to_72h"], 30)
        self.assertEqual(growth["reach_delta_72h_to_7d"], 50)
        self.assertEqual(growth["follow_delta_2h_to_24h"], 2)
        self.assertEqual(growth["follow_delta_24h_to_72h"], 1)
        self.assertEqual(growth["follow_delta_72h_to_7d"], 6)
        self.assertEqual(growth["reach_72h_vs_24h_ratio"], 2.5)
        self.assertEqual(growth["reach_7d_vs_24h_ratio"], 5)
        self.assertAlmostEqual(growth["follow_7d_vs_24h_ratio"], 10 / 3)


class MoneyballAccountGrowthTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db = self.root / "reels.db"
        self.annotations = self.root / "annotations.json"
        self.annotations.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "annotations": [
                        {
                            "account": "aibrief_jp",
                            "media_id": "media-context",
                            "series": "Context series",
                            "content_goal": "utility",
                            "metadata_source": "manual",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_daily_reel_breakdowns_use_same_interval_and_remain_account_level(self):
        as_of = datetime(2026, 7, 5, 0, 0, tzinfo=timezone.utc)
        rows, invalid = moneyball._normalize_account_flow_rows(
            [
                {
                    "id": 1,
                    "observed_since": "2026-07-03T00:00:00+00:00",
                    "observed_until": "2026-07-04T00:00:00+00:00",
                    "fetched_at": "2026-07-04T12:00:00+00:00",
                    "follows": 10,
                    "unfollows": 2,
                    "reach": 1_000,
                    "reel_reach": 800,
                    "reel_non_follower_reach": 700,
                    "reel_follower_reach": 105,
                    "reel_views": 1_200,
                    "reel_total_interactions": 32,
                }
            ],
            table="account_follow_flows",
            config=moneyball.load_config(),
            as_of=as_of,
        )
        self.assertEqual(invalid, 0)
        self.assertEqual(len(rows), 1)
        day = rows[0]
        self.assertEqual(day["gross_follows_per_1000_reel_reach"], 12.5)
        self.assertAlmostEqual(
            day["gross_follows_per_1000_reel_non_follower_reach"],
            10 / 700 * 1_000,
        )
        self.assertEqual(day["reel_interactions_per_1000_reel_reach"], 40)
        self.assertEqual(day["reel_views_per_reached_account"], 1.5)
        growth = moneyball.summarize_account_growth(
            snapshots=[],
            flow_rows=rows,
            config=moneyball.load_config(),
            as_of=as_of,
            snapshot_table=None,
            flow_table="account_follow_flows",
        )
        self.assertEqual(growth["reel_reach"], 800)
        self.assertEqual(growth["reel_non_follower_reach"], 700)
        self.assertEqual(growth["gross_follows_per_1000_reel_reach"], 12.5)
        self.assertIn(
            "observational",
            growth["denominator_labels"]["gross_follows_per_1000_reel_reach"],
        )

    def _seed_growth_ledger(self):
        with reel_ledger.connect(self.db) as conn:
            reel_ledger.upsert_imported(
                conn,
                content_hash="context-hash",
                channel_id="aibrief_jp",
                lang="ja",
                clip_dir=self.root / "clip",
                media_path=self.root / "clip" / "reel.mp4",
                status=reel_ledger.STATUS_PUBLISHED,
                title="Publication context only",
                published_at="2026-07-03T12:00:00+00:00",
                media_id="media-context",
            )
            reel_ledger.record_insight(
                conn,
                content_hash="context-hash",
                channel_id="aibrief_jp",
                media_id="media-context",
                captured_at="2026-07-04T12:30:00+00:00",
                metrics={
                    "views": 200,
                    "reach": 150,
                    "saved": 5,
                    "shares": 2,
                    # A legacy/custom value must not become post attribution.
                    "follows": 99,
                },
            )
            reel_ledger.record_insight(
                conn,
                content_hash="context-hash",
                channel_id="aibrief_jp",
                media_id="media-context",
                captured_at="2026-07-06T12:30:00+00:00",
                metrics={
                    "views": 300,
                    "reach": 220,
                    "saved": 7,
                    "shares": 3,
                    "follows": 99,
                },
            )
            reel_ledger.record_account_insight_snapshot(
                conn,
                channel_id="aibrief_jp",
                account="aibrief.jp",
                ig_user_id="ig-user",
                followers_count=200,
                media_count=100,
                fetched_at="2026-07-01T12:00:00+00:00",
            )
            reel_ledger.record_account_insight_snapshot(
                conn,
                channel_id="aibrief_jp",
                account="aibrief.jp",
                ig_user_id="ig-user",
                followers_count=230,
                media_count=108,
                fetched_at="2026-07-10T00:00:00+00:00",
            )
            for index in range(8):
                reel_ledger.record_account_follow_flow(
                    conn,
                    channel_id="aibrief_jp",
                    account="aibrief.jp",
                    ig_user_id="ig-user",
                    day=f"2026-07-{index + 1:02d}",
                    follows=10 + index,
                    unfollows=2,
                    unknown=0,
                    reach=1_000,
                    fetched_at="2026-07-10T00:00:00+00:00",
                )
            # A later revision supersedes rather than double-counts the first fetch.
            reel_ledger.record_account_follow_flow(
                conn,
                channel_id="aibrief_jp",
                account="aibrief.jp",
                ig_user_id="ig-user",
                day="2026-07-01",
                follows=11,
                unfollows=2,
                unknown=0,
                reach=1_000,
                fetched_at="2026-07-10T01:00:00+00:00",
            )

    def test_account_growth_is_separate_age_labeled_and_non_attributing(self):
        self._seed_growth_ledger()
        report = moneyball.build_moneyball_report(
            db_path=self.db,
            channel="aibrief_jp",
            config_path=moneyball.DEFAULT_CONFIG_PATH,
            annotations_path=self.annotations,
            generated_at=datetime(2026, 7, 11, 0, 0, tzinfo=timezone.utc),
        )

        growth = report["account_growth"]
        self.assertEqual(growth["status"], "AVAILABLE")
        self.assertEqual(growth["follower_stock"]["latest"], 230)
        self.assertEqual(growth["follower_stock"]["snapshot_change"], 30)
        self.assertEqual(growth["gross_follows"], 109)
        self.assertEqual(growth["unfollows"], 16)
        self.assertEqual(growth["net_growth"], 93)
        self.assertEqual(growth["account_reach"], 8_000)
        self.assertAlmostEqual(
            growth["gross_follows_per_1000_account_reach"],
            109 / 8_000 * 1_000,
        )
        self.assertAlmostEqual(
            growth["net_follows_per_1000_account_reach"],
            93 / 8_000 * 1_000,
        )
        self.assertEqual(growth["coverage"]["daily_intervals"]["count"], 8)
        self.assertEqual(growth["coverage"]["daily_intervals"]["total"], 8)
        self.assertFalse(growth["preliminary"]["status"])
        self.assertEqual(growth["trend"]["status"], "AVAILABLE")
        self.assertEqual(len(growth["daily_intervals"]), 8)
        self.assertIn(
            "never Reel reach",
            growth["denominator_labels"][
                "gross_follows_per_1000_account_reach"
            ],
        )

        context_day = next(
            row
            for row in growth["daily_intervals"]
            if row["observed_since"].startswith("2026-07-03")
        )
        self.assertEqual(
            context_day["publication_context"],
            {
                "published_post_count": 1,
                "media_ids": ["media-context"],
                "evidence_status": "time_overlap_context_not_attribution",
                "warning": moneyball.ACCOUNT_ATTRIBUTION_WARNING,
            },
        )

        post = report["posts"][0]
        for observation in post["maturity_windows"].values():
            if observation is not None:
                self.assertIsNone(observation["raw_metrics"]["follows"])
                self.assertIsNone(
                    observation["derived_metrics"]["follow_conversion"]["value"]
                )
        self.assertIsNone(report["series"][0]["total_follows"])
        account_recommendation = next(
            row
            for row in report["recommendations"]
            if row["entity_type"] == "account"
        )
        self.assertEqual(
            account_recommendation["confidence_or_evidence_status"],
            "descriptive_account_level_not_causal",
        )
        self.assertNotIn("media-context", json.dumps(account_recommendation))

        markdown = moneyball.render_moneyball_markdown(report)
        audit = moneyball.render_data_audit_markdown(report)
        self.assertIn("Account growth (account-wide)", markdown)
        self.assertIn("context only", markdown)
        self.assertIn(moneyball.ACCOUNT_ATTRIBUTION_WARNING, markdown)
        self.assertIn("Account-growth source coverage", audit)
        self.assertIn("explicitly REEL-filtered reach", audit)
        self.assertIn("never copied into a post or series", audit)

        dashboard = moneyball.render_moneyball_html(report)
        self.assertEqual(dashboard, moneyball.render_moneyball_html(report))
        for marker in (
            'id="moneyball-dashboard"',
            'id="account-growth-kpis"',
            'id="daily-flow-chart"',
            'id="follower-stock-chart"',
            'id="maturity-coverage-chart"',
            'id="reach-intent-scatter"',
            'id="publication-day-table"',
            'id="per-reel-table"',
            'id="funnel-stage-chart"',
            'id="content-roster"',
            'id="attribution-warning"',
        ):
            with self.subTest(html_marker=marker):
                self.assertIn(marker, dashboard)
        self.assertIn("<style>", dashboard)
        self.assertIn("Interactions", dashboard)
        self.assertIn("Engagement / reach", dashboard)
        self.assertIn("Reposts /1k", dashboard)
        self.assertIn("views / reached account", dashboard)
        self.assertIn("3s skip", dashboard)
        self.assertIn("post attribution unavailable", dashboard)
        self.assertIn("Follows /1k account reach", dashboard)
        self.assertIn("Follows /1k Reel reach", dashboard)
        self.assertIn("Non-follower Reel reach", dashboard)
        self.assertEqual(
            dashboard.count('data-sortable="true" aria-describedby='),
            2,
        )
        self.assertEqual(dashboard.count('class="sort-button"'), 36)
        self.assertEqual(dashboard.count('aria-sort="descending"'), 2)
        self.assertIn('id="moneyball-table-sorter"', dashboard)
        self.assertIn('data-row-index="0"', dashboard)
        self.assertIn('data-sort-missing="1"', dashboard)
        self.assertIn("unavailable values remain last", dashboard)
        self.assertIn("left.stableIndex - right.stableIndex", dashboard)
        linked_report = json.loads(json.dumps(report))
        linked_report["posts"][0]["identity"][
            "permalink"
        ] = "https://www.instagram.com/reel/example/"
        linked_dashboard = moneyball.render_moneyball_html(linked_report)
        self.assertIn("Open Reel ↗", linked_dashboard)
        self.assertIn(
            'href="https://www.instagram.com/reel/example/"',
            linked_dashboard,
        )
        self.assertNotIn("NaN", dashboard)
        self.assertNotIn("Infinity", dashboard)
        partial_report = json.loads(json.dumps(report))
        partial_growth = partial_report["account_growth"]
        partial_growth["gross_follows"] = None
        partial_growth["unfollows"] = None
        partial_growth["net_growth"] = None
        partial_growth["known_gross_follows"] = 220
        partial_growth["known_unfollows"] = 4
        partial_growth["known_net_growth"] = 216
        partial_growth["coverage"]["follows"] = {
            "count": 40,
            "total": 90,
            "percentage": 44.44444444444444,
        }
        partial_growth["coverage"]["unfollows"] = {
            "count": 40,
            "total": 90,
            "percentage": 44.44444444444444,
        }
        partial_dashboard = moneyball.render_moneyball_html(partial_report)
        self.assertIn("<strong>220</strong>", partial_dashboard)
        self.assertIn("partial known total · 40/90 days", partial_dashboard)
        self.assertIn("partial known net · 40/90 days", partial_dashboard)
        partial_markdown = moneyball.render_moneyball_markdown(partial_report)
        self.assertIn("partial known total; 40/90 (44.4%)", partial_markdown)
        self.assertIn("partial known net; 40/90 (44.4%)", partial_markdown)
        report["report_metadata"]["account"] = '<img src=x onerror="boom">'
        escaped = moneyball.render_moneyball_html(report)
        self.assertNotIn('<img src=x onerror="boom">', escaped)
        self.assertIn("&lt;img src=x onerror=&quot;boom&quot;&gt;", escaped)

    def test_missing_account_tables_are_backward_compatible(self):
        with reel_ledger.connect(self.db) as conn:
            conn.execute("DROP TABLE account_follow_flows")
            conn.execute("DROP TABLE account_insight_snapshots")

        report = moneyball.build_moneyball_report(
            db_path=self.db,
            channel="aibrief_jp",
            config_path=moneyball.DEFAULT_CONFIG_PATH,
            annotations_path=self.annotations,
            generated_at=datetime(2026, 7, 11, 0, 0, tzinfo=timezone.utc),
        )
        growth = report["account_growth"]
        self.assertEqual(growth["status"], "UNAVAILABLE")
        self.assertIsNone(growth["follower_stock"]["latest"])
        self.assertIsNone(growth["gross_follows"])
        self.assertIsNone(growth["unfollows"])
        self.assertIsNone(growth["net_growth"])
        self.assertEqual(growth["daily_intervals"], [])
        self.assertTrue(
            any("table is unavailable" in warning for warning in growth["warnings"])
        )


class MoneyballFacebookPlatformAnalyticsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.instagram_db = self.root / "reels.db"
        self.facebook_db = self.root / "facebook.db"
        self.annotations = self.root / "annotations.json"
        (self.root / "clip").mkdir()
        (self.root / "clip" / "notes.json").write_text(
            json.dumps({"duration": 10}),
            encoding="utf-8",
        )
        self.annotations.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "annotations": [
                        {
                            "account": "aibrief_jp",
                            "content_hash": "shared-content",
                            "series": "Shared utility",
                            "content_goal": "utility",
                            "format": "talking-head",
                            "production_minutes": 30,
                            "metadata_source": "manual",
                            "metadata_confidence": "high",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.instagram_published = datetime(
            2026, 7, 1, 0, 0, tzinfo=timezone.utc
        )
        self.facebook_published = datetime(
            2026, 7, 1, 6, 0, tzinfo=timezone.utc
        )
        self._seed_ledgers()

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def _insight_payload(values):
        return {
            "data": [
                {
                    "name": name,
                    "period": "lifetime",
                    "values": [{"value": value}],
                }
                for name, value in values.items()
            ]
        }

    def _seed_ledgers(self):
        with reel_ledger.connect(self.instagram_db) as connection:
            reel_ledger.upsert_imported(
                connection,
                content_hash="shared-content",
                channel_id="aibrief_jp",
                lang="ja",
                clip_dir=self.root / "clip",
                media_path=self.root / "clip" / "reel.mp4",
                status=reel_ledger.STATUS_PUBLISHED,
                title="Same creative, Instagram-native upload",
                published_at=self.instagram_published.isoformat(),
                media_id="ig-media-independent",
                permalink="https://www.instagram.com/reel/ig-media-independent/",
            )
            reel_ledger.record_insight(
                connection,
                content_hash="shared-content",
                channel_id="aibrief_jp",
                media_id="ig-media-independent",
                captured_at=(
                    self.instagram_published + timedelta(hours=24.25)
                ).isoformat(),
                metrics={
                    "views": 500,
                    "reach": 400,
                    "likes": 20,
                    "comments": 4,
                    "saved": 5,
                    "shares": 3,
                    "total_interactions": 32,
                },
            )

        with reel_ledger.connect(self.facebook_db) as connection:
            reel_ledger.upsert_imported(
                connection,
                content_hash="shared-content",
                channel_id="aibrief_jp",
                lang="ja",
                clip_dir=self.root / "clip",
                media_path=self.root / "clip" / "reel.mp4",
                status=reel_ledger.STATUS_PUBLISHED,
                title="Same creative, Facebook-native upload",
                published_at=self.facebook_published.isoformat(),
                media_id="fb-video-independent",
                permalink="/reel/fb-video-independent/",
            )

            direct_fallback = {
                "views": 100,
                "likes": 5,
                "comments": 2,
            }
            reel_ledger.record_insight(
                connection,
                content_hash="shared-content",
                channel_id="aibrief_jp",
                media_id="fb-video-independent",
                captured_at=(
                    self.facebook_published + timedelta(hours=2.25)
                ).isoformat(),
                metrics=direct_fallback,
                raw=json.dumps(
                    self._insight_payload(direct_fallback),
                    ensure_ascii=False,
                ),
            )

            rich_video_insights = {
                "fb_reels_total_plays": 240,
                "blue_reels_play_count": 160,
                "fb_reels_replay_count": 80,
                "post_total_media_view_unique": 150,
                "post_video_likes_by_reaction_type": {
                    "LIKE": 6,
                    "LOVE": 3,
                    "WOW": 1,
                },
                "post_video_social_actions": {
                    "COMMENT": 4,
                    "SHARE": 5,
                },
                "post_video_followers": 3,
                "post_video_view_time": 3_600_000,
                "post_video_avg_time_watched": 12_000,
                "post_video_retention_graph": {
                    "0": 100,
                    "1": 90,
                    "3": 70,
                    "5": 55,
                },
            }
            reel_ledger.record_insight(
                connection,
                content_hash="shared-content",
                channel_id="aibrief_jp",
                media_id="fb-video-independent",
                captured_at=(
                    self.facebook_published + timedelta(hours=24.5)
                ).isoformat(),
                metrics={"views": 240},
                raw=json.dumps(
                    self._insight_payload(rich_video_insights),
                    ensure_ascii=False,
                ),
            )

    def _build_report(self):
        return moneyball.build_moneyball_report(
            db_path=self.instagram_db,
            facebook_db_path=self.facebook_db,
            channel="aibrief_jp",
            config_path=moneyball.DEFAULT_CONFIG_PATH,
            annotations_path=self.annotations,
            generated_at=datetime(2026, 7, 4, 0, 0, tzinfo=timezone.utc),
            as_of=datetime(2026, 7, 4, 0, 0, tzinfo=timezone.utc),
        )

    def test_facebook_lane_keeps_ids_clocks_windows_and_denominators_separate(self):
        report = self._build_report()
        facebook = report["platform_analytics"]["facebook"]

        self.assertEqual(report["report_metadata"]["platforms"], ["instagram", "facebook"])
        self.assertEqual(facebook["status"], "AVAILABLE")
        self.assertEqual(facebook["paired_instagram_posts"]["count"], 1)
        self.assertEqual(facebook["paired_instagram_posts"]["total"], 1)
        self.assertEqual(facebook["data_coverage"]["published_posts"], 1)

        instagram = report["posts"][0]
        post = facebook["posts"][0]
        identity = post["identity"]
        self.assertEqual(identity["platform"], "facebook")
        self.assertEqual(identity["media_id"], "fb-video-independent")
        self.assertEqual(
            identity["permalink"],
            "https://www.facebook.com/reel/fb-video-independent/",
        )
        self.assertEqual(
            identity["paired_instagram"],
            {
                "media_id": "ig-media-independent",
                "permalink": "https://www.instagram.com/reel/ig-media-independent/",
                "published_at": self.instagram_published.isoformat(),
            },
        )
        self.assertEqual(identity["published_at"], self.facebook_published.isoformat())
        self.assertEqual(instagram["identity"]["published_at"], self.instagram_published.isoformat())

        facebook_2h = post["maturity_windows"]["2h"]
        facebook_24h = post["maturity_windows"]["24h"]
        self.assertAlmostEqual(facebook_2h["actual_age_hours"], 2.25)
        self.assertAlmostEqual(facebook_24h["actual_age_hours"], 24.5)
        self.assertIsNone(instagram["maturity_windows"]["2h"])
        self.assertAlmostEqual(
            instagram["maturity_windows"]["24h"]["actual_age_hours"],
            24.25,
        )
        self.assertEqual(facebook["maturity_windows"]["2h"]["post_count"], 1)
        self.assertEqual(facebook["maturity_windows"]["24h"]["post_count"], 1)
        self.assertEqual(facebook["maturity_windows"]["72h"]["post_count"], 0)
        self.assertEqual(facebook["maturity_windows"]["7d"]["post_count"], 0)

        fallback_raw = facebook_2h["raw_metrics"]
        fallback_derived = facebook_2h["derived_metrics"]
        self.assertEqual(fallback_raw["views"], 100)
        self.assertIsNone(fallback_raw["reach"])
        self.assertIsNone(fallback_raw["follows"])
        self.assertIsNone(fallback_raw["shares"])
        self.assertIsNone(fallback_raw["saves"])
        self.assertIsNone(fallback_raw["total_watch_time_seconds"])
        self.assertIsNone(fallback_raw["average_watch_time_seconds"])
        self.assertIsNone(fallback_raw["reels_skip_rate"])
        self.assertIsNone(fallback_derived["three_second_dropoff_rate"])
        self.assertEqual(fallback_derived["distribution_denominator_type"], "views")
        self.assertEqual(fallback_derived["likes_per_1000_views"], 50)
        self.assertEqual(fallback_derived["comments_per_1000_views"], 20)
        self.assertIsNone(fallback_derived["shares_per_1000_views"])
        self.assertIsNone(fallback_derived["saves_per_1000_views"])
        self.assertIsNone(fallback_derived["likes_per_1000_reach"])
        self.assertIsNone(fallback_derived["follow_conversion"]["value"])
        self.assertIsNone(fallback_derived["watch_depth"])
        self.assertIsNone(fallback_derived["interactions_per_1000_views"])

    def test_rich_v25_metrics_keep_exact_semantics_and_uncapped_watch_depth(self):
        report = self._build_report()
        post = report["platform_analytics"]["facebook"]["posts"][0]
        observation = post["maturity_windows"]["24h"]
        raw = observation["raw_metrics"]
        derived = observation["derived_metrics"]
        provenance = observation["metric_provenance"]["canonical_fields"]

        self.assertEqual(raw["plays"], 240)
        self.assertEqual(raw["views"], 240)
        self.assertEqual(raw["initial_plays"], 160)
        self.assertEqual(raw["replays"], 80)
        self.assertEqual(raw["reach"], 150)
        self.assertEqual(raw["likes"], 6)
        self.assertEqual(raw["reactions"], 10)
        self.assertEqual(raw["comments"], 4)
        self.assertEqual(raw["shares"], 5)
        self.assertEqual(raw["interactions"], 19)
        self.assertEqual(raw["follows"], 3)
        self.assertEqual(raw["total_watch_time_seconds"], 3_600)
        self.assertEqual(raw["average_watch_time_seconds"], 12)
        self.assertEqual(provenance["likes"]["source_field"], "post_video_likes_by_reaction_type.LIKE")
        self.assertEqual(provenance["reach"]["denominator_type"], "unique_media_viewers")

        self.assertEqual(derived["distribution_denominator_type"], "unique_media_viewers")
        self.assertEqual(derived["likes_per_1000_unique_media_viewers"], 40)
        self.assertEqual(derived["reactions_per_1000_unique_media_viewers"], 10 / 150 * 1_000)
        self.assertEqual(derived["follow_conversion"]["denominator_type"], "unique_media_viewers")
        self.assertEqual(derived["follow_conversion"]["value"], 20)
        self.assertEqual(derived["watch_depth"], 1.2)
        self.assertEqual(derived["three_second_retention_rate"], 0.7)
        self.assertAlmostEqual(derived["three_second_dropoff_rate"], 0.3)
        self.assertIn(
            "exact three-second retention",
            provenance["three_second_dropoff_rate"]["formula"],
        )
        self.assertIn(
            "not Meta's Instagram reels_skip_rate",
            provenance["three_second_dropoff_rate"]["warning"],
        )

    def test_facebook_html_and_csv_are_linked_sortable_finite_and_deterministic(self):
        first = self._build_report()
        second = self._build_report()
        self.assertEqual(first, second)

        dashboard = moneyball.render_moneyball_html(first)
        self.assertEqual(dashboard, moneyball.render_moneyball_html(second))
        self.assertIn('id="facebook-per-reel-table"', dashboard)
        self.assertIn('data-testid="facebook-per-reel-table"', dashboard)
        self.assertIn(
            'href="https://www.facebook.com/reel/fb-video-independent/"',
            dashboard,
        )
        self.assertIn(
            'href="https://www.instagram.com/reel/ig-media-independent/"',
            dashboard,
        )
        for heading in (
            "Views / plays",
            "Unique viewers",
            "Total engagement /1k",
            "Likes /1k",
            "All reactions /1k",
            "Comments /1k",
            "Shares /1k",
            "Saves /1k",
            "Follows /1k unique viewers",
            "Total watch",
            "Avg watch",
            "Watch depth",
            "3s skip / drop-off",
            "Production",
            "Evidence source",
        ):
            with self.subTest(sortable_heading=heading):
                self.assertIn(f'aria-label="Sort by {heading}', dashboard)
        self.assertIn("derived exact 3s retention drop-off; not Meta skip rate", dashboard)
        self.assertIn("rich video_insights", dashboard)

        facebook_csv = moneyball.render_facebook_moneyball_csv(first)
        self.assertEqual(
            facebook_csv,
            moneyball.render_facebook_moneyball_csv(second),
        )
        facebook_rows = list(csv.DictReader(io.StringIO(facebook_csv)))
        self.assertEqual(
            {row["maturity_window"] for row in facebook_rows},
            {"2h", "24h", "latest"},
        )
        self.assertTrue(
            all(row["platform"] == "facebook" for row in facebook_rows)
        )
        self.assertTrue(
            all(row["media_id"] == "fb-video-independent" for row in facebook_rows)
        )
        self.assertTrue(
            all(
                row["paired_instagram_media_id"] == "ig-media-independent"
                for row in facebook_rows
            )
        )
        instagram_csv = moneyball.render_moneyball_csv(first)
        self.assertIn("ig-media-independent", instagram_csv)
        self.assertNotIn("fb-video-independent", instagram_csv)

        for rendered in (
            json.dumps(first, ensure_ascii=False, sort_keys=True, allow_nan=False),
            dashboard,
            facebook_csv,
        ):
            self.assertNotIn("NaN", rendered)
            self.assertNotIn("Infinity", rendered)


class MoneyballClassificationAndFunnelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = moneyball.load_config()

    def test_hidden_gem_requires_measured_growth_or_efficiency_and_explains_values(self):
        hidden = make_observation(
            "hidden", reach=50, follows=5, shares=2, saves=2
        )
        peers = [
            make_observation("peer-1", reach=100, follows=1, shares=2, saves=2),
            make_observation("peer-2", reach=120, follows=1, shares=2, saves=2),
            make_observation("peer-3", reach=140, follows=1, shares=2, saves=2),
        ]
        classifications = moneyball.classify_post(
            hidden, make_baseline([hidden, *peers]), self.config
        )
        result = next(item for item in classifications if item["label"] == "Hidden Gem")
        self.assertIn("reach 50", result["reason"])
        self.assertIn("cohort median 110", result["reason"])
        self.assertIn("follow conversion 100", result["reason"])
        self.assertEqual(result["comparison_cohort"]["n"], 4)
        self.assertEqual(result["supporting_metrics"]["reach"], 50)

        missing_follow = make_observation(
            "missing", reach=50, follows=None, shares=2, saves=2
        )
        labels = {
            item["label"]
            for item in moneyball.classify_post(
                missing_follow,
                make_baseline([missing_follow, *peers]),
                self.config,
            )
        }
        self.assertNotIn("Hidden Gem", labels)

    def test_vanity_winner_needs_high_reach_and_measured_weak_durable_outcomes(self):
        vanity = make_observation(
            "vanity", reach=400, follows=1, shares=0, saves=0
        )
        peers = [
            make_observation("peer-1", reach=100, follows=5, shares=5, saves=5),
            make_observation("peer-2", reach=120, follows=5, shares=5, saves=5),
            make_observation("peer-3", reach=140, follows=5, shares=5, saves=5),
        ]
        classifications = moneyball.classify_post(
            vanity, make_baseline([vanity, *peers]), self.config
        )
        result = next(
            item for item in classifications if item["label"] == "Vanity Winner"
        )
        self.assertIn("reach 400", result["reason"])
        self.assertIn("at/above cohort p75", result["reason"])
        self.assertIn("intent actions 0", result["reason"])
        self.assertEqual(
            result["supporting_metrics"]["follow_conversion_denominator_type"],
            "reach",
        )

        unavailable = make_observation(
            "unavailable", reach=400, follows=None, shares=0, saves=0
        )
        labels = {
            item["label"]
            for item in moneyball.classify_post(
                unavailable,
                make_baseline([unavailable, *peers]),
                self.config,
            )
        }
        self.assertNotIn("Vanity Winner", labels)

    def test_tiny_comparison_sample_cannot_create_an_established_label(self):
        candidate = make_observation(
            "candidate", reach=50, follows=5, shares=0, saves=0
        )
        peers = [
            make_observation("peer-1", reach=100, follows=1, shares=5, saves=5),
            make_observation("peer-2", reach=120, follows=1, shares=5, saves=5),
        ]
        self.assertEqual(
            moneyball.classify_post(
                candidate, make_baseline([candidate, *peers]), self.config
            ),
            [],
        )

    def test_funnel_labels_packaging_diagnosis_as_hypothesis_and_unknown_stages(self):
        candidate = make_observation(
            "candidate",
            reach=200,
            shares=2,
            saves=2,
            average_watch_time_seconds=2,
            duration_seconds=10,
        )
        peers = [
            make_observation(
                "peer-1",
                reach=80,
                shares=2,
                saves=2,
                average_watch_time_seconds=8,
                duration_seconds=10,
            ),
            make_observation(
                "peer-2",
                reach=100,
                shares=2,
                saves=2,
                average_watch_time_seconds=6,
                duration_seconds=10,
            ),
        ]
        baseline = make_baseline([candidate, *peers])
        diagnostics = moneyball.diagnose_funnel(candidate, baseline, self.config)
        packaging = next(
            item
            for item in diagnostics
            if item["diagnostic"] == "PACKAGING_WON_CONTENT_LOST"
        )
        self.assertEqual(
            packaging["evidence_status"], "diagnostic_hypothesis_not_causal"
        )
        self.assertIn("inspect", packaging["hypothesis"].lower())

        stages = moneyball.stage_statuses(candidate, baseline)
        for stage in (
            "curiosity",
            "conversion",
            "production_efficiency",
            "retention",
        ):
            with self.subTest(stage=stage):
                self.assertEqual(stages[stage]["status"], "UNKNOWN")

    def test_strong_content_weak_distribution_does_not_make_hook_causal_claim(self):
        candidate = make_observation(
            "candidate",
            reach=50,
            shares=5,
            saves=5,
            average_watch_time_seconds=9,
            duration_seconds=10,
        )
        peers = [
            make_observation(
                "peer-1",
                reach=100,
                shares=1,
                saves=1,
                average_watch_time_seconds=4,
                duration_seconds=10,
            ),
            make_observation(
                "peer-2",
                reach=120,
                shares=1,
                saves=1,
                average_watch_time_seconds=5,
                duration_seconds=10,
            ),
        ]
        diagnostics = moneyball.diagnose_funnel(
            candidate, make_baseline([candidate, *peers]), self.config
        )
        diagnosis = next(
            item
            for item in diagnostics
            if item["diagnostic"] == "STRONG_CONTENT_WEAK_INITIAL_DISTRIBUTION"
        )
        self.assertIn("does not prove the hook", diagnosis["hypothesis"])


class MoneyballSeriesRecommendationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = moneyball.load_config()

    def test_scale_hold_revise_pause_and_small_sample_rules(self):
        posts = []
        posts.extend(
            make_series_post(
                f"scale-{index}",
                series="Scale",
                follows=2,
                day=10 + index,
            )
            for index in range(5)
        )
        posts.extend(
            make_series_post(
                f"pause-{index}",
                series="Pause",
                follows=0,
                day=10 + index,
            )
            for index in range(5)
        )
        hold_follows = [2, 2, 0, 0, 1]
        posts.extend(
            make_series_post(
                f"hold-{index}",
                series="Hold",
                follows=follows,
                day=10 + index,
            )
            for index, follows in enumerate(hold_follows)
        )
        posts.extend(
            make_series_post(
                f"revise-{index}",
                series="Revise",
                follows=1,
                diagnostic=(
                    "PACKAGING_WON_CONTENT_LOST" if index < 3 else None
                ),
                day=10 + index,
            )
            for index in range(5)
        )
        posts.extend(
            make_series_post(
                f"tiny-{index}",
                series="Tiny",
                follows=2,
                day=10 + index,
            )
            for index in range(4)
        )

        summaries, workhorses = moneyball.analyze_series(
            posts,
            {"72h": make_series_account_baseline()},
            self.config,
        )
        recommendations = {
            row["series"]: row["recommendation"] for row in summaries
        }
        self.assertEqual(recommendations["Scale"], "SCALE")
        self.assertEqual(recommendations["Hold"], "HOLD")
        self.assertEqual(recommendations["Revise"], "REVISE")
        self.assertEqual(recommendations["Pause"], "PAUSE")
        self.assertEqual(recommendations["Tiny"], "INSUFFICIENT DATA")
        self.assertEqual(
            [row["series"] for row in workhorses],
            ["Scale"],
        )
        scale = next(row for row in summaries if row["series"] == "Scale")
        self.assertEqual(
            scale["consistency_last_five"]["above_both_primary_medians"], 5
        )
        pause = next(row for row in summaries if row["series"] == "Pause")
        self.assertEqual(
            pause["consistency_last_five"]["below_both_primary_medians"], 5
        )

    def test_scale_at_exactly_three_of_five_above_both_same_denominator_medians(self):
        posts = [
            make_series_post(
                f"exact-scale-{index}",
                series="Exact Scale",
                follows=follows,
                day=10 + index,
            )
            for index, follows in enumerate([2, 2, 2, 1, 1])
        ]

        summaries, workhorses = moneyball.analyze_series(
            posts,
            {"72h": make_series_account_baseline()},
            self.config,
        )

        summary = summaries[0]
        consistency = summary["consistency_last_five"]
        self.assertEqual(summary["recommendation"], "SCALE")
        self.assertEqual(consistency["above_both_primary_medians"], 3)
        self.assertEqual(consistency["complete_comparisons"], 5)
        self.assertTrue(
            all(
                detail["follow_conversion_denominator_type"] == "reach"
                and detail["follow_conversion_baseline_median"] == 10
                for detail in consistency["details"]
            )
        )
        self.assertEqual([row["series"] for row in workhorses], ["Exact Scale"])

    def test_incomplete_production_coverage_blocks_series_decision(self):
        posts = [
            make_series_post(
                f"missing-production-{index}",
                series="Incomplete",
                follows=2,
                production_minutes=None if index == 0 else 30,
                day=10 + index,
            )
            for index in range(5)
        ]
        summaries, workhorses = moneyball.analyze_series(
            posts,
            {"72h": make_series_account_baseline()},
            self.config,
        )
        self.assertEqual(summaries[0]["recommendation"], "INSUFFICIENT DATA")
        self.assertIn("4/5", summaries[0]["recommendation_reason"])
        self.assertEqual(workhorses, [])

    def test_missing_follow_coverage_blocks_series_decision_and_workhorse(self):
        posts = [
            make_series_post(
                f"missing-follow-{index}",
                series="Missing Follow",
                follows=None if index == 0 else 2,
                day=10 + index,
            )
            for index in range(5)
        ]

        summaries, workhorses = moneyball.analyze_series(
            posts,
            {"72h": make_series_account_baseline()},
            self.config,
        )

        self.assertEqual(summaries[0]["recommendation"], "INSUFFICIENT DATA")
        self.assertEqual(
            summaries[0]["follows_coverage"],
            {"count": 4, "total": 5},
        )
        self.assertIn("4/5", summaries[0]["recommendation_reason"])
        self.assertEqual(workhorses, [])

    def test_pause_at_exactly_four_of_five_below_both_with_no_top_secondary(self):
        posts = [
            make_series_post(
                f"exact-pause-{index}",
                series="Exact Pause",
                follows=follows,
                day=10 + index,
            )
            for index, follows in enumerate([0, 0, 0, 0, 1])
        ]

        summaries, workhorses = moneyball.analyze_series(
            posts,
            {"72h": make_series_account_baseline()},
            self.config,
        )

        summary = summaries[0]
        self.assertEqual(summary["recommendation"], "PAUSE")
        self.assertEqual(
            summary["consistency_last_five"]["below_both_primary_medians"],
            4,
        )
        self.assertIn(
            "no secondary metric in the top quartile",
            summary["recommendation_reason"],
        )
        self.assertEqual(workhorses, [])

    def test_hold_with_complete_data_when_no_other_threshold_is_met(self):
        posts = [
            make_series_post(
                f"exact-hold-{index}",
                series="Exact Hold",
                follows=follows,
                day=10 + index,
            )
            for index, follows in enumerate([2, 2, 0, 1, 1])
        ]

        summaries, workhorses = moneyball.analyze_series(
            posts,
            {"72h": make_series_account_baseline()},
            self.config,
        )

        summary = summaries[0]
        self.assertEqual(summary["recommendation"], "HOLD")
        self.assertEqual(
            summary["consistency_last_five"]["complete_comparisons"], 5
        )
        self.assertEqual(
            summary["consistency_last_five"]["above_both_primary_medians"], 2
        )
        self.assertEqual(
            summary["consistency_last_five"]["below_both_primary_medians"], 1
        )
        self.assertEqual(workhorses, [])

    def test_revise_at_exactly_three_shared_split_funnel_diagnostics(self):
        posts = [
            make_series_post(
                f"exact-revise-{index}",
                series="Exact Revise",
                follows=1,
                diagnostic=(
                    "STRONG_CONTENT_WEAK_INITIAL_DISTRIBUTION"
                    if index < 3
                    else None
                ),
                day=10 + index,
            )
            for index in range(5)
        ]

        summaries, workhorses = moneyball.analyze_series(
            posts,
            {"72h": make_series_account_baseline()},
            self.config,
        )

        self.assertEqual(summaries[0]["recommendation"], "REVISE")
        self.assertIn(
            "3/5 recent posts share the "
            "STRONG_CONTENT_WEAK_INITIAL_DISTRIBUTION",
            summaries[0]["recommendation_reason"],
        )
        self.assertEqual(workhorses, [])

    def test_one_viral_outlier_alone_cannot_trigger_scale(self):
        posts = [
            make_series_post(
                "viral-outlier",
                series="Outlier Guard",
                reach=1_000_000,
                follows=20_000,
                day=10,
            )
        ]
        posts.extend(
            make_series_post(
                f"viral-peer-{index}",
                series="Outlier Guard",
                follows=1,
                day=11 + index,
            )
            for index in range(4)
        )

        summaries, workhorses = moneyball.analyze_series(
            posts,
            {"72h": make_series_account_baseline()},
            self.config,
        )

        summary = summaries[0]
        self.assertEqual(
            summary["consistency_last_five"]["above_both_primary_medians"], 1
        )
        self.assertEqual(summary["recommendation"], "HOLD")
        self.assertEqual(workhorses, [])


class MoneyballExperimentAndRenderingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = moneyball.load_config()

    def test_exactly_one_changed_variable_at_same_window_is_eligible(self):
        control = make_experiment_post(
            "control", variant="control", changed_variable="hook_style", follows=2
        )
        variant = make_experiment_post(
            "variant", variant="short_hook", changed_variable="hook_style", follows=4
        )
        result = moneyball.analyze_experiments(
            [variant, control], self.config
        )[0]
        self.assertEqual(
            result["comparison_type"], "controlled_one_variable_comparison"
        )
        self.assertEqual(result["evidence_status"], "eligible")
        self.assertEqual(result["changed_variable"], "hook_style")
        self.assertEqual(result["maturity_window"], "24h")
        self.assertEqual(result["metric"], "follow_conversion")
        self.assertEqual(result["control_metric"], 20)
        self.assertEqual(result["variant_metric"], 40)
        self.assertEqual(result["absolute_difference"], 20)
        self.assertEqual(result["percentage_lift"], 1)
        self.assertIn(
            "no statistical significance is claimed",
            result["uncertainty_warning"].lower(),
        )

    def test_multiple_changed_variables_are_flagged_as_uncontrolled(self):
        control = make_experiment_post(
            "control", variant="control", changed_variable="hook_style"
        )
        variant = make_experiment_post(
            "variant", variant="variant", changed_variable="caption_style"
        )
        result = moneyball.analyze_experiments([control, variant], self.config)[0]
        self.assertEqual(result["comparison_type"], "uncontrolled_comparison")
        self.assertEqual(result["evidence_status"], "uncontrolled")
        self.assertIsNone(result["changed_variable"])
        self.assertEqual(
            result["declared_changed_variables"],
            ["caption_style", "hook_style"],
        )
        self.assertIn(
            "Exactly one changed variable is required; found 2.",
            result["uncertainty_warning"],
        )

    def test_sortable_cells_use_raw_keys_and_distinguish_zero_from_missing(self):
        numeric_cell = moneyball._sortable_cell(
            "1,000",
            1_000,
            sort_type="number",
        )
        zero_cell = moneyball._sortable_cell("0", 0, sort_type="number")
        missing_cell = moneyball._sortable_cell(
            "Unavailable",
            None,
            sort_type="number",
        )
        header = moneyball._sortable_header(
            "Published",
            sort_type="date",
            initial_direction="descending",
        )

        self.assertIn('data-sort-value="1000"', numeric_cell)
        self.assertNotIn('data-sort-value="1,000"', numeric_cell)
        self.assertIn('data-sort-value="0"', zero_cell)
        self.assertIn('data-sort-missing="0"', zero_cell)
        self.assertIn('data-sort-value=""', missing_cell)
        self.assertIn('data-sort-missing="1"', missing_cell)
        self.assertIn('aria-sort="descending"', header)
        self.assertIn('type="button"', header)
        self.assertIn("▼", header)

    def test_fixed_inputs_render_deterministically_without_nonfinite_json(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            database = root / "reels.db"
            annotations = root / "annotations.json"
            annotations.write_text(
                '{"schema_version": 1, "annotations": []}\n',
                encoding="utf-8",
            )
            with reel_ledger.connect(database):
                pass

            kwargs = {
                "db_path": database,
                "channel": "aibrief_jp",
                "config_path": moneyball.DEFAULT_CONFIG_PATH,
                "annotations_path": annotations,
                "generated_at": FIXED_NOW,
                "as_of": FIXED_NOW,
            }
            first = moneyball.build_moneyball_report(**kwargs)
            second = moneyball.build_moneyball_report(**kwargs)
            self.assertEqual(first, second)

            first_markdown = moneyball.render_moneyball_markdown(first)
            second_markdown = moneyball.render_moneyball_markdown(second)
            self.assertEqual(first_markdown, second_markdown)
            self.assertEqual(
                moneyball.render_moneyball_csv(first),
                moneyball.render_moneyball_csv(second),
            )

            serialized = json.dumps(
                first,
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
            )
            self.assertNotIn("NaN", serialized)
            self.assertNotIn("Infinity", serialized)
            self.assertNotIn("NaN", first_markdown)
            self.assertNotIn("Infinity", first_markdown)


if __name__ == "__main__":
    unittest.main()
