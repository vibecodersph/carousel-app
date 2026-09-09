import json
import unittest

import moneyball_analytics as moneyball


METRIC_KEYS = (
    "three_second_skip_rate",
    "views_per_reached_account",
    "saves_per_reach",
    "shares_per_view",
    "reach",
)


def make_ranking_observation(
    media_id,
    *,
    reach=100,
    views=100,
    interactions=10,
    saves=1,
    shares=1,
    average_watch_time_seconds=5,
    duration_seconds=10,
    skip_rate=50,
    maturity_window="24h",
    actual_age_hours=24.5,
):
    raw_metrics = {
        "reach": reach,
        "views": views,
        "interactions": interactions,
        "saves": saves,
        "shares": shares,
        "average_watch_time_seconds": average_watch_time_seconds,
        "duration_seconds": duration_seconds,
        "reels_skip_rate": skip_rate,
    }
    metadata = {
        "hook_text": f"Hook {media_id}",
        "format": "reel",
        "duration_bucket": "0–15 seconds",
    }
    return {
        "identity": {
            "media_id": media_id,
            "permalink": f"https://www.instagram.com/reel/{media_id}/",
            "published_at": "2026-07-20T00:00:00+00:00",
        },
        "content_metadata": metadata,
        "maturity_window": maturity_window,
        "actual_age_hours": actual_age_hours,
        "raw_metrics": raw_metrics,
        "derived_metrics": moneyball.compute_post_metrics(raw_metrics, metadata),
    }


def make_ranking_post(media_id, **observation_kwargs):
    maturity_window = observation_kwargs.get("maturity_window", "24h")
    observation = make_ranking_observation(media_id, **observation_kwargs)
    return {
        "identity": dict(observation["identity"]),
        "content_metadata": dict(observation["content_metadata"]),
        "maturity_windows": {maturity_window: observation},
    }


def ranking_rows(result, metric):
    bucket = result["metric_rankings"][metric]
    if isinstance(bucket, list):
        return bucket
    return bucket.get("top_10") or bucket.get("rows") or bucket.get("rankings") or []


def aggregate_rows(result):
    bucket = result["aggregate_top_10"]
    if isinstance(bucket, list):
        return bucket
    return bucket.get("rows") or bucket.get("rankings") or []


class MoneyballTopRankingTests(unittest.TestCase):
    def test_small_nonzero_action_rates_remain_visible_in_markdown_and_html(self):
        source = make_ranking_post("small-share", shares=1, views=32069, saves=1, reach=32069)
        rankings = moneyball.build_top_rankings([source])
        markdown = "\n".join(moneyball.performance_rankings_markdown_lines(rankings, platform_label="Instagram"))
        rendered = moneyball._performance_rankings_html(rankings, platform_label="Instagram")
        self.assertIn("0.0031%", markdown)
        self.assertIn("0.0031%", rendered)
        self.assertEqual(moneyball._format_ranking_value(1 / 2372, "percent_ratio"), "0.0422%")
        self.assertEqual(moneyball._format_ranking_value(0, "percent_ratio"), "0.0%")
        self.assertEqual(moneyball._html_ranking_value(1e-9, "percent_ratio"), "&lt;0.0001%")

    def test_selected_five_are_ordered_and_watch_depth_and_interactions_have_no_weight(self):
        self.assertEqual(tuple(item["key"] for item in moneyball.PERFORMANCE_RANKING_METRICS), METRIC_KEYS)
        a = make_ranking_post("a", reach=100, views=200, saves=10, shares=20)
        b = make_ranking_post("b", reach=200, views=400, saves=20, shares=40)
        baseline = moneyball.build_top_rankings([a, b])
        changed = make_ranking_post(
            "a", reach=100, views=200, saves=10, shares=20,
            average_watch_time_seconds=999, duration_seconds=1, interactions=999999,
        )
        self.assertEqual(baseline, moneyball.build_top_rankings([changed, b]))
        # All four rates tie; raw reach alone changes the equal-weight result.
        rows = aggregate_rows(baseline)
        self.assertEqual([row["media_id"] for row in rows], ["b", "a"])
        self.assertEqual([row["average_directional_percentile"] for row in rows], [55, 45])

    def test_share_rate_uses_views_even_when_reach_present_and_preserves_support(self):
        source = make_ranking_post("source", reach=100, views=400, shares=20, saves=10)
        result = moneyball.build_top_rankings([source])
        share = ranking_rows(result, "shares_per_view")[0]
        self.assertEqual(share["value"], 0.05)
        self.assertEqual(share["supporting_metrics"], {
            "shares": 20, "views": 400, "denominator_type": "views",
        })
        self.assertEqual(ranking_rows(result, "saves_per_reach")[0]["value"], 0.1)
        self.assertEqual(result["metric_rankings"]["shares_per_view"]["format"], "percent_ratio")

    def test_missing_shares_or_views_excludes_share_rate_and_combined_rank(self):
        posts = [make_ranking_post("no-shares", shares=None),
                 make_ranking_post("no-views", views=None),
                 make_ranking_post("zero-shares", shares=0)]
        result = moneyball.build_top_rankings(posts)
        self.assertEqual([row["media_id"] for row in ranking_rows(result, "shares_per_view")], ["zero-shares"])
        self.assertEqual(ranking_rows(result, "shares_per_view")[0]["value"], 0)
        self.assertEqual([row["media_id"] for row in aggregate_rows(result)], ["zero-shares"])
        self.assertEqual(result["metric_rankings"]["reach"]["coverage"]["count"], 3)

    def test_five_top_tens_are_linked_limited_and_directionally_correct(self):
        posts = [
            make_ranking_post(
                f"reel-{index:02d}",
                reach=100 + index,
                views=100 + index * 10,
                interactions=index,
                saves=index,
                shares=index * 3,
                average_watch_time_seconds=1 + index,
                duration_seconds=10,
                skip_rate=20 + index,
            )
            for index in range(12)
        ]

        result = moneyball.build_top_rankings(
            posts,
            platform="instagram",
            maturity_window="24h",
            limit=10,
        )

        self.assertEqual(result["status"], "AVAILABLE")
        self.assertEqual(result["platform"], "instagram")
        self.assertEqual(result["maturity_window"], "24h")
        self.assertEqual(set(result["metric_rankings"]), set(METRIC_KEYS))
        self.assertEqual(result["cohort_size"], 12)

        for metric in METRIC_KEYS:
            with self.subTest(metric=metric):
                rows = ranking_rows(result, metric)
                self.assertEqual(len(rows), 10)
                self.assertEqual([row["rank"] for row in rows], list(range(1, 11)))
                self.assertTrue(
                    all(
                        row["permalink"]
                        == f"https://www.instagram.com/reel/{row['media_id']}/"
                        for row in rows
                    )
                )
                self.assertTrue(
                    all(row["actual_age_hours"] == 24.5 for row in rows)
                )

        self.assertEqual(
            ranking_rows(result, "shares_per_view")[0]["media_id"],
            "reel-11",
        )
        self.assertEqual(
            ranking_rows(result, "reach")[0]["media_id"],
            "reel-11",
        )
        self.assertEqual(
            ranking_rows(result, "saves_per_reach")[0]["media_id"],
            "reel-11",
        )
        self.assertEqual(
            ranking_rows(result, "views_per_reached_account")[0]["media_id"],
            "reel-11",
        )
        self.assertEqual(
            ranking_rows(result, "three_second_skip_rate")[0]["media_id"],
            "reel-00",
        )

    def test_missing_values_are_excluded_without_view_denominator_substitution(self):
        complete = make_ranking_post(
            "complete",
            reach=100,
            views=200,
            interactions=10,
            saves=2,
            average_watch_time_seconds=6,
            skip_rate=25,
        )
        missing_reach = make_ranking_post(
            "missing-reach",
            reach=None,
            views=1_000,
            interactions=900,
            saves=500,
            shares=900,
            average_watch_time_seconds=9,
            skip_rate=5,
        )
        missing_all = make_ranking_post(
            "missing-all",
            reach=None,
            views=None,
            interactions=None,
            saves=None,
            average_watch_time_seconds=None,
            duration_seconds=None,
            skip_rate=None,
        )

        result = moneyball.build_top_rankings(
            [missing_all, missing_reach, complete],
            platform="instagram",
            maturity_window="24h",
        )

        for metric in (
            "reach",
            "saves_per_reach",
            "views_per_reached_account",
        ):
            with self.subTest(metric=metric):
                self.assertEqual(
                    [row["media_id"] for row in ranking_rows(result, metric)],
                    ["complete"],
                )
                self.assertIn(
                    "reach",
                    result["metric_rankings"][metric]["source"].lower(),
                )
        self.assertIn(
            "denominators are never substituted",
            result["methodology"]["denominator_rule"].lower(),
        )
        self.assertEqual(
            [row["media_id"] for row in ranking_rows(result, "shares_per_view")],
            ["missing-reach", "complete"],
        )
        self.assertEqual(
            [
                row["media_id"]
                for row in ranking_rows(result, "three_second_skip_rate")
            ],
            ["missing-reach", "complete"],
        )
        serialized = json.dumps(result, allow_nan=False)
        self.assertNotIn("NaN", serialized)
        self.assertNotIn("Infinity", serialized)

    def test_rankings_use_only_the_requested_fixed_maturity_window(self):
        low_24h = make_ranking_observation(
            "two-window",
            interactions=1,
            saves=1,
    shares=1,
            views=100,
            maturity_window="24h",
            actual_age_hours=25,
        )
        high_latest = make_ranking_observation(
            "two-window",
            interactions=100,
            saves=100,
            views=1_000,
            maturity_window="latest",
            actual_age_hours=300,
        )
        post = {
            "identity": dict(low_24h["identity"]),
            "content_metadata": dict(low_24h["content_metadata"]),
            "maturity_windows": {
                "24h": low_24h,
                "latest": high_latest,
            },
        }
        latest_only = make_ranking_post(
            "latest-only",
            interactions=500,
            maturity_window="latest",
            actual_age_hours=500,
        )

        result = moneyball.build_top_rankings(
            [latest_only, post],
            platform="instagram",
            maturity_window="24h",
        )

        self.assertEqual(result["cohort_size"], 1)
        for metric in METRIC_KEYS:
            rows = ranking_rows(result, metric)
            self.assertEqual([row["media_id"] for row in rows], ["two-window"])
            self.assertEqual(rows[0]["actual_age_hours"], 25)
        self.assertEqual(
            ranking_rows(result, "shares_per_view")[0]["value"],
            0.01,
        )

    def test_ties_are_deterministic_and_zero_is_not_treated_as_missing(self):
        posts = [
            make_ranking_post(
                media_id,
                interactions=0,
                saves=0,
                views=100,
                shares=0,
                average_watch_time_seconds=0,
                skip_rate=0,
            )
            for media_id in ("tie-c", "tie-a", "tie-b")
        ]

        first = moneyball.build_top_rankings(
            posts,
            platform="instagram",
            maturity_window="24h",
        )
        second = moneyball.build_top_rankings(
            list(reversed(posts)),
            platform="instagram",
            maturity_window="24h",
        )

        self.assertEqual(first, second)
        for metric in METRIC_KEYS:
            self.assertEqual(
                [row["media_id"] for row in ranking_rows(first, metric)],
                ["tie-a", "tie-b", "tie-c"],
            )
            self.assertTrue(
                    all(row["value"] == {"reach": 100, "views_per_reached_account": 1}.get(metric, 0)
                        for row in ranking_rows(first, metric))
            )
        self.assertEqual(
            [row["media_id"] for row in aggregate_rows(first)],
            ["tie-a", "tie-b", "tie-c"],
        )

    def test_aggregate_is_transparent_eligible_and_has_strong_point_labels(self):
        posts = [
            make_ranking_post(
                f"aggregate-{index:02d}",
                reach=100 + index,
                views=100 + index * 25,
                interactions=index * 2,
                saves=index,
                shares=index * 3,
                average_watch_time_seconds=2 + index,
                duration_seconds=20,
                skip_rate=80 - index * 4,
            )
            for index in range(12)
        ]
        only_two_metrics = make_ranking_post(
            "only-two-metrics",
            reach=None,
            views=None,
            interactions=None,
            saves=None,
            average_watch_time_seconds=20,
            duration_seconds=10,
            skip_rate=0,
        )

        result = moneyball.build_top_rankings(
            [only_two_metrics, *posts],
            platform="instagram",
            maturity_window="24h",
        )
        rows = aggregate_rows(result)

        self.assertLessEqual(len(rows), 10)
        self.assertNotIn("only-two-metrics", {row["media_id"] for row in rows})
        self.assertEqual([row["rank"] for row in rows], list(range(1, len(rows) + 1)))
        for row in rows:
            with self.subTest(media_id=row["media_id"]):
                self.assertEqual(row["metric_count"], 5)
                self.assertIn("average_directional_percentile", row)
                self.assertIsInstance(row["components"], dict)
                self.assertEqual(set(row["components"]), set(METRIC_KEYS))
                for component in row["components"].values():
                    self.assertIn("value", component)
                    self.assertIn("rank", component)
                    self.assertIn("metric_leaderboard_rank", component)
                    self.assertIn("directional_percentile", component)
                self.assertIsInstance(row["strong_points"], list)
                self.assertTrue(row["permalink"].startswith("https://"))
                expected_average = sum(
                    component["directional_percentile"]
                    for component in row["components"].values()
                ) / 5
                self.assertAlmostEqual(
                    row["average_directional_percentile"],
                    expected_average,
                )
                for strong_point in row["strong_points"]:
                    self.assertIn("label", strong_point)
                    self.assertGreaterEqual(
                        strong_point["directional_percentile"],
                        75,
                    )

        self.assertEqual(rows[0]["media_id"], "aggregate-11")
        self.assertEqual(
            {point["metric"] for point in rows[0]["strong_points"]},
            set(METRIC_KEYS),
        )
        self.assertEqual(
            [row["average_directional_percentile"] for row in rows],
            sorted(
                (
                    row["average_directional_percentile"]
                    for row in rows
                ),
                reverse=True,
            ),
        )

        recursively_serialized = json.dumps(result, sort_keys=True)
        for opaque_key in (
            '"score"',
            '"weighted_score"',
            '"engagement_score"',
            '"composite_score"',
        ):
            self.assertNotIn(opaque_key, recursively_serialized)
        methodology = json.dumps(result["methodology"]).lower()
        self.assertIn("unweighted", methodology)
        self.assertIn("five", methodology)
        self.assertIn("75", methodology)

    def test_facebook_does_not_fabricate_reach_rankings_from_view_fallbacks(self):
        facebook_post = make_ranking_post(
            "facebook-view-only",
            shares=None,
            reach=None,
            views=10_000,
            interactions=100,
            saves=None,
            average_watch_time_seconds=None,
            skip_rate=None,
        )
        facebook_post["identity"][
            "permalink"
        ] = "https://www.facebook.com/reel/facebook-view-only/"

        result = moneyball.build_top_rankings(
            [facebook_post],
            platform="facebook",
            maturity_window="24h",
        )

        self.assertEqual(result["status"], "INSUFFICIENT_DATA")
        self.assertEqual(result["platform"], "facebook")
        self.assertEqual(aggregate_rows(result), [])
        for metric in METRIC_KEYS:
            self.assertEqual(ranking_rows(result, metric), [])

    def test_ranking_html_has_five_linked_lists_and_a_labeled_aggregate(self):
        posts = [
            make_ranking_post(
                f"linked-{index:02d}",
                reach=100 + index,
                views=100 + index * 10,
                interactions=index,
                saves=index,
                shares=index * 3,
                average_watch_time_seconds=index + 1,
                duration_seconds=20,
                skip_rate=50 - index,
            )
            for index in range(12)
        ]
        rankings = moneyball.build_top_rankings(
            posts,
            platform="instagram",
            maturity_window="24h",
        )

        dashboard_section = moneyball._performance_rankings_html(
            rankings,
            platform_label="Instagram",
        )

        self.assertEqual(
            dashboard_section,
            moneyball._performance_rankings_html(
                rankings,
                platform_label="Instagram",
            ),
        )
        self.assertIn('id="instagram-performance-rankings"', dashboard_section)
        self.assertIn("Five Moneyball Top 10s", dashboard_section)
        self.assertIn("Aggregate Top 10", dashboard_section)
        self.assertIn("Strong points", dashboard_section)
        self.assertIn(
            "Unweighted mean of all five directional cohort percentiles",
            dashboard_section,
        )
        self.assertIn("lower wins", dashboard_section)
        for metric in METRIC_KEYS:
            with self.subTest(metric=metric):
                self.assertIn(
                    f'data-testid="instagram-ranking-{metric}"',
                    dashboard_section,
                )
        for row in aggregate_rows(rankings):
            with self.subTest(reel_link=row["media_id"]):
                self.assertIn(
                    f'href="https://www.instagram.com/reel/{row["media_id"]}/"',
                    dashboard_section,
                )
        self.assertEqual(
            dashboard_section.count('class="rank-reel-link"'),
            len(aggregate_rows(rankings)),
        )
        self.assertNotIn("NaN", dashboard_section)
        self.assertNotIn("Infinity", dashboard_section)

        full_dashboard = moneyball.render_moneyball_html(
            {
                "report_metadata": {
                    "account": "aibrief_jp",
                    "generated_at_jst": "2026-07-29T12:00:00+09:00",
                },
                "data_coverage": {},
                "account_growth": {},
                "funnel_diagnostics": {},
                "posts": [],
                "series": [],
                "platform_analytics": {
                    "facebook": {"status": "UNAVAILABLE"},
                },
                "performance_rankings": {
                    "instagram": rankings,
                    "facebook": {},
                },
            }
        )
        self.assertEqual(
            full_dashboard.count(
                '<section id="instagram-performance-rankings"'
            ),
            1,
        )
        self.assertIn(
            'href="https://www.instagram.com/reel/linked-11/"',
            full_dashboard,
        )

    def test_ranking_html_states_insufficient_coverage_instead_of_empty_winner(self):
        rankings = moneyball.build_top_rankings(
            [
                make_ranking_post(
                    "views-only",
                    shares=None,
                    reach=None,
                    views=10_000,
                    interactions=None,
                    saves=None,
                    average_watch_time_seconds=None,
                    skip_rate=None,
                )
            ],
            platform="facebook",
            maturity_window="24h",
        )

        dashboard_section = moneyball._performance_rankings_html(
            rankings,
            platform_label="Facebook",
        )

        self.assertIn('id="facebook-performance-rankings"', dashboard_section)
        self.assertIn("Top 10 rankings unavailable", dashboard_section)
        self.assertIn("Missing fields remain unavailable", dashboard_section)
        self.assertNotIn("Aggregate Top 10", dashboard_section)


if __name__ == "__main__":
    unittest.main()
