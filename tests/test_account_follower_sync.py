import argparse
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

import reel_ledger
import reel_scheduler


class AccountFollowerParsingTests(unittest.TestCase):
    def test_parses_account_identity_flow_breakdown_and_reach(self) -> None:
        identity = reel_scheduler.parse_instagram_account_identity(
            {
                "username": "aibrief.jp",
                "followers_count": 220,
                "media_count": 131,
            }
        )
        flow = reel_scheduler.parse_follows_and_unfollows(
            {
                "data": [
                    {
                        "name": "follows_and_unfollows",
                        "total_value": {
                            "breakdowns": [
                                {
                                    "dimension_keys": ["follow_type"],
                                    "results": [
                                        {
                                            "dimension_values": ["FOLLOWER"],
                                            "value": 12,
                                        },
                                        {
                                            "dimension_values": ["NON_FOLLOWER"],
                                            "value": 3,
                                        },
                                        {
                                            "dimension_values": ["UNKNOWN"],
                                            "value": 1,
                                        },
                                    ],
                                }
                            ]
                        },
                    }
                ]
            }
        )
        reach = reel_scheduler.parse_account_total_metric(
            {
                "data": [
                    {
                        "name": "reach",
                        "total_value": {"value": 987},
                    }
                ]
            },
            "reach",
        )
        reel_content = reel_scheduler.parse_account_reel_content_breakdown(
            {
                "data": [
                    {
                        "name": "reach",
                        "total_value": {
                            "value": 832,
                            "breakdowns": [
                                {
                                    "dimension_keys": ["media_product_type"],
                                    "results": [
                                        {"dimension_values": ["AD"], "value": 1},
                                        {"dimension_values": ["REEL"], "value": 831},
                                    ],
                                }
                            ],
                        },
                    },
                    {
                        "name": "views",
                        "total_value": {
                            "value": 1101,
                            "breakdowns": [
                                {
                                    "dimension_keys": ["media_product_type"],
                                    "results": [
                                        {"dimension_values": ["REEL"], "value": 1100}
                                    ],
                                }
                            ],
                        },
                    },
                    {
                        "name": "comments",
                        "total_value": {
                            "value": 0,
                            "breakdowns": [
                                {"dimension_keys": ["media_product_type"]}
                            ],
                        },
                    },
                ]
            }
        )
        reel_audience = reel_scheduler.parse_account_reel_audience_breakdown(
            {
                "data": [
                    {
                        "name": "reach",
                        "total_value": {
                            "value": 832,
                            "breakdowns": [
                                {
                                    # Parser should follow dimension names, not
                                    # rely on the order used in the request.
                                    "dimension_keys": [
                                        "follow_type",
                                        "media_product_type",
                                    ],
                                    "results": [
                                        {
                                            "dimension_values": [
                                                "NON_FOLLOWER",
                                                "REEL",
                                            ],
                                            "value": 785,
                                        },
                                        {
                                            "dimension_values": ["FOLLOWER", "REEL"],
                                            "value": 48,
                                        },
                                    ],
                                }
                            ],
                        },
                    }
                ]
            }
        )

        self.assertEqual(identity["username"], "aibrief.jp")
        self.assertEqual(identity["followers_count"], 220)
        self.assertEqual(identity["media_count"], 131)
        self.assertEqual(
            flow,
            {"follows": 12, "unfollows": 3, "unknown": 1},
        )
        self.assertEqual(reach, 987)
        self.assertEqual(reel_content["reel_reach"], 831)
        self.assertEqual(reel_content["reel_views"], 1100)
        self.assertIsNone(reel_content["reel_comments"])
        self.assertEqual(reel_audience["reel_non_follower_reach"], 785)
        self.assertEqual(reel_audience["reel_follower_reach"], 48)

    def test_missing_flow_values_remain_unavailable(self) -> None:
        self.assertEqual(
            reel_scheduler.parse_follows_and_unfollows({"data": []}),
            {"follows": None, "unfollows": None, "unknown": None},
        )
        self.assertIsNone(
            reel_scheduler.parse_account_total_metric({"data": []}, "reach")
        )
        self.assertTrue(
            all(
                value is None
                for value in reel_scheduler.parse_account_reel_content_breakdown(
                    {"data": []}
                ).values()
            )
        )
        self.assertTrue(
            all(
                value is None
                for value in reel_scheduler.parse_account_reel_audience_breakdown(
                    {"data": []}
                ).values()
            )
        )

    def test_sanitizes_tokens_and_paging_urls_recursively(self) -> None:
        secret = "EAAB-secret-token"
        sanitized = reel_scheduler.sanitize_graph_payload(
            {
                "access_token": secret,
                "paging": {
                    "cursors": {"before": "cursor-before", "after": "cursor-after"},
                    "next": (
                        "https://graph.facebook.com/v25.0/123/insights"
                        f"?access_token={secret}&after=cursor-after"
                    ),
                },
                "message": f"request access_token={secret} failed",
            },
            secrets=(secret,),
        )
        serialized = json.dumps(sanitized)

        self.assertNotIn(secret, serialized)
        self.assertNotIn("cursor-before", serialized)
        self.assertNotIn("cursor-after", serialized)
        self.assertIn("[REDACTED]", serialized)


class AccountFollowerWindowTests(unittest.TestCase):
    def test_windows_are_completed_non_overlapping_days_and_refetch_recent(self) -> None:
        windows = reel_scheduler.account_follow_windows(
            as_of=datetime(2026, 7, 29, 8, 0, tzinfo=timezone.utc),
            backfill_days=5,
            refetch_days=2,
            existing_days={
                "2026-07-24",
                "2026-07-25",
                "2026-07-26",
                "2026-07-27",
                "2026-07-28",
            },
        )

        self.assertEqual(
            [window.day for window in windows],
            [date(2026, 7, 27), date(2026, 7, 28)],
        )
        for current, following in zip(windows, windows[1:]):
            self.assertEqual(current.until, following.since)
        self.assertTrue(all(window.until.date() <= date(2026, 7, 29) for window in windows))

    def test_missing_old_day_is_backfilled_without_requerying_other_old_days(self) -> None:
        windows = reel_scheduler.account_follow_windows(
            as_of=datetime(2026, 7, 29, tzinfo=timezone.utc),
            backfill_days=4,
            refetch_days=1,
            existing_days={"2026-07-25", "2026-07-27", "2026-07-28"},
        )

        self.assertEqual(
            [window.day.isoformat() for window in windows],
            ["2026-07-26", "2026-07-28"],
        )

    def test_rejects_more_than_ninety_backfill_days(self) -> None:
        with self.assertRaisesRegex(ValueError, "between 0 and 90"):
            reel_scheduler.account_follow_windows(backfill_days=91)

    def test_missing_reel_breakdown_coverage_is_backfilled_with_recent_refetch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "reels.db"
            with reel_ledger.connect(db_path) as conn:
                for day, covered in (
                    ("2026-07-25", False),
                    ("2026-07-26", True),
                    ("2026-07-27", True),
                    ("2026-07-28", True),
                ):
                    reel_ledger.record_account_follow_flow(
                        conn,
                        channel_id="aibrief_jp",
                        ig_user_id="1784",
                        day=day,
                        follows=1,
                        unfollows=0,
                        reach=100,
                        reel_content_breakdown_fetched=covered,
                        reel_audience_breakdown_fetched=covered,
                        fetched_at=f"{day}T23:00:00Z",
                    )
                # A later failed optional fetch must make the day eligible
                # again even if an older revision had both breakdowns.
                reel_ledger.record_account_follow_flow(
                    conn,
                    channel_id="aibrief_jp",
                    ig_user_id="1784",
                    day="2026-07-26",
                    follows=1,
                    unfollows=0,
                    reach=100,
                    reel_content_breakdown_fetched=False,
                    reel_audience_breakdown_fetched=False,
                    fetched_at="2026-07-26T23:30:00Z",
                )
                covered_days = reel_ledger.account_follow_flow_days(
                    conn,
                    "aibrief_jp",
                    "2026-07-25",
                    "2026-07-28",
                    require_reel_breakdowns=True,
                )

            windows = reel_scheduler.account_follow_windows(
                as_of=datetime(2026, 7, 29, 8, 0, tzinfo=timezone.utc),
                backfill_days=4,
                refetch_days=1,
                existing_days=covered_days,
            )

        self.assertEqual(
            [window.day.isoformat() for window in windows],
            ["2026-07-25", "2026-07-26", "2026-07-28"],
        )


class AccountFollowerFetchTests(unittest.TestCase):
    def test_flow_and_reach_requests_use_exact_same_daily_interval(self) -> None:
        window = reel_scheduler.AccountFollowWindow(
            day=date(2026, 7, 27),
            since=datetime(2026, 7, 27, tzinfo=timezone.utc),
            until=datetime(2026, 7, 28, tzinfo=timezone.utc),
        )
        with patch(
            "instagram_publish.graph_request",
            return_value={"data": []},
        ) as graph_request:
            reel_scheduler.fetch_instagram_account_flow(
                ig_user_id="1784",
                window=window,
                access_token="secret",
                graph_version="v25.0",
                graph_api_root="https://graph.facebook.com",
            )
            flow_params = graph_request.call_args.kwargs["params"]
            reel_scheduler.fetch_instagram_account_reach(
                ig_user_id="1784",
                window=window,
                access_token="secret",
                graph_version="v25.0",
                graph_api_root="https://graph.facebook.com",
            )
            reach_params = graph_request.call_args.kwargs["params"]
            reel_scheduler.fetch_instagram_account_reel_content_breakdown(
                ig_user_id="1784",
                window=window,
                access_token="secret",
                graph_version="v25.0",
                graph_api_root="https://graph.facebook.com",
            )
            content_params = graph_request.call_args.kwargs["params"]
            reel_scheduler.fetch_instagram_account_reel_audience_breakdown(
                ig_user_id="1784",
                window=window,
                access_token="secret",
                graph_version="v25.0",
                graph_api_root="https://graph.facebook.com",
            )
            audience_params = graph_request.call_args.kwargs["params"]

        self.assertEqual(flow_params["metric_type"], "total_value")
        self.assertEqual(flow_params["breakdown"], "follow_type")
        self.assertEqual(flow_params["since"], reach_params["since"])
        self.assertEqual(flow_params["until"], reach_params["until"])
        self.assertEqual(content_params["breakdown"], "media_product_type")
        self.assertEqual(
            content_params["metric"],
            ",".join(reel_scheduler.INSTAGRAM_ACCOUNT_REEL_CONTENT_METRICS),
        )
        self.assertEqual(
            audience_params["breakdown"],
            "media_product_type,follow_type",
        )
        self.assertEqual(audience_params["metric"], "reach")
        for params in (reach_params, content_params, audience_params):
            self.assertEqual(flow_params["since"], params["since"])
            self.assertEqual(flow_params["until"], params["until"])
        self.assertEqual(
            flow_params["until"] - flow_params["since"],
            24 * 60 * 60,
        )

    def test_reach_failure_does_not_block_follow_flow_storage_or_leak_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "reels.db"
            secret = "EAAB-do-not-store"
            flow_payload = {
                "data": [
                    {
                        "name": "follows_and_unfollows",
                        "total_value": {
                            "breakdowns": [
                                {
                                    "dimension_keys": ["follow_type"],
                                    "results": [
                                        {
                                            "dimension_values": ["FOLLOWER"],
                                            "value": 5,
                                        },
                                        {
                                            "dimension_values": ["NON_FOLLOWER"],
                                            "value": 2,
                                        },
                                    ],
                                }
                            ]
                        },
                    }
                ],
                "paging": {
                    "next": f"https://graph.facebook.com/?access_token={secret}"
                },
            }
            with reel_ledger.connect(db_path):
                pass
            with patch.object(
                reel_scheduler,
                "fetch_instagram_account_identity",
                return_value={
                    "username": "aibrief.jp",
                    "followers_count": 220,
                    "media_count": 131,
                },
            ), patch.object(
                reel_scheduler,
                "fetch_instagram_account_flow",
                return_value=flow_payload,
            ), patch.object(
                reel_scheduler,
                "fetch_instagram_account_reach",
                side_effect=SystemExit(f"request access_token={secret} failed"),
            ), patch.object(
                reel_scheduler,
                "fetch_instagram_account_reel_content_breakdown",
                return_value={
                    "data": [
                        {
                            "name": "reach",
                            "total_value": {
                                "breakdowns": [
                                    {
                                        "dimension_keys": ["media_product_type"],
                                        "results": [
                                            {
                                                "dimension_values": ["REEL"],
                                                "value": 900,
                                            }
                                        ],
                                    }
                                ]
                            },
                        }
                    ]
                },
            ), patch.object(
                reel_scheduler,
                "fetch_instagram_account_reel_audience_breakdown",
                return_value={
                    "data": [
                        {
                            "name": "reach",
                            "total_value": {
                                "breakdowns": [
                                    {
                                        "dimension_keys": [
                                            "media_product_type",
                                            "follow_type",
                                        ],
                                        "results": [
                                            {
                                                "dimension_values": [
                                                    "REEL",
                                                    "NON_FOLLOWER",
                                                ],
                                                "value": 850,
                                            }
                                        ],
                                    }
                                ]
                            },
                        }
                    ]
                },
            ):
                output = io.StringIO()
                with redirect_stdout(output):
                    result = reel_scheduler.sync_instagram_account_insights_for_channel(
                        db_path=db_path,
                        channel_id="aibrief_jp",
                        ig_user_id="1784",
                        access_token=secret,
                        token_source="test",
                        graph_version="v25.0",
                        graph_api_root="https://graph.facebook.com",
                        backfill_days=1,
                        refetch_days=1,
                        captured_at="2026-07-29T08:00:00+00:00",
                    )

            self.assertEqual(result["flow_windows"], 1)
            self.assertNotIn(secret, output.getvalue())
            with reel_ledger.connect(db_path) as conn:
                rows = reel_ledger.latest_account_follow_flows(
                    conn,
                    "aibrief_jp",
                )
                snapshot = reel_ledger.latest_account_insight_snapshot(
                    conn,
                    "aibrief_jp",
                )
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["follows"], 5)
            self.assertEqual(rows[0]["unfollows"], 2)
            self.assertIsNone(rows[0]["reach"])
            self.assertEqual(rows[0]["reel_reach"], 900)
            self.assertEqual(rows[0]["reel_non_follower_reach"], 850)
            self.assertEqual(rows[0]["reel_content_breakdown_fetched"], 1)
            self.assertEqual(rows[0]["reel_audience_breakdown_fetched"], 1)
            self.assertNotIn(secret, rows[0]["raw"])
            self.assertEqual(snapshot["account"], "aibrief.jp")
            self.assertNotIn(secret, snapshot["raw"])

    def test_reel_breakdown_failures_do_not_suppress_existing_account_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "reels.db"
            secret = "EAAB-breakdown-secret"
            with patch.object(
                reel_scheduler,
                "fetch_instagram_account_identity",
                return_value={
                    "username": "aibrief.jp",
                    "followers_count": 220,
                    "media_count": 131,
                },
            ), patch.object(
                reel_scheduler,
                "fetch_instagram_account_flow",
                return_value={"data": []},
            ), patch.object(
                reel_scheduler,
                "fetch_instagram_account_reach",
                return_value={
                    "data": [
                        {
                            "name": "reach",
                            "total_value": {"value": 700},
                        }
                    ]
                },
            ), patch.object(
                reel_scheduler,
                "fetch_instagram_account_reel_content_breakdown",
                side_effect=SystemExit(f"request access_token={secret} failed"),
            ), patch.object(
                reel_scheduler,
                "fetch_instagram_account_reel_audience_breakdown",
                side_effect=SystemExit(f"request access_token={secret} failed"),
            ):
                output = io.StringIO()
                with redirect_stdout(output):
                    result = reel_scheduler.sync_instagram_account_insights_for_channel(
                        db_path=db_path,
                        channel_id="aibrief_jp",
                        ig_user_id="1784",
                        access_token=secret,
                        token_source="test",
                        graph_version="v25.0",
                        graph_api_root="https://graph.facebook.com",
                        backfill_days=1,
                        refetch_days=1,
                        captured_at="2026-07-29T08:00:00+00:00",
                    )

            with reel_ledger.connect(db_path) as conn:
                row = reel_ledger.latest_account_follow_flows(
                    conn,
                    "aibrief_jp",
                )[0]

        self.assertEqual(result["flow_windows"], 1)
        self.assertEqual(result["account_errors"], 2)
        self.assertEqual(row["reach"], 700)
        self.assertIsNone(row["reel_reach"])
        self.assertEqual(row["reel_content_breakdown_fetched"], 0)
        self.assertEqual(row["reel_audience_breakdown_fetched"], 0)
        self.assertNotIn(secret, output.getvalue())
        self.assertNotIn(secret, row["raw"])

    def test_optional_account_api_failure_does_not_break_media_sync(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "reels.db"
            with reel_ledger.connect(db_path) as conn:
                reel_ledger.upsert_imported(
                    conn,
                    content_hash="media-row",
                    channel_id="aibrief_jp",
                    lang="ja",
                    clip_dir="/clip",
                    media_path="/clip/reel.mp4",
                    status=reel_ledger.STATUS_PUBLISHED,
                    published_at="2026-07-28T00:00:00+00:00",
                    media_id="178900002",
                )
            args = argparse.Namespace(
                platform="instagram",
                channel="aibrief_jp",
                db=db_path,
                limit=None,
                media_id=None,
                dry_run=False,
                metrics="views,reach",
                access_token="",
                graph_api_version="v25.0",
                graph_api_root="https://graph.facebook.com",
                account_insights=True,
                account_follow_backfill_days=1,
                account_follow_refetch_days=1,
            )
            secret = "EAAB-output-secret"
            with patch("instagram_publish.load_env_file"), patch(
                "instagram_publish.resolve_instagram_access_token",
                return_value=(secret, "test"),
            ), patch(
                "instagram_publish.resolve_instagram_user_id",
                return_value=("1784", "test"),
            ), patch.object(
                reel_scheduler,
                "fetch_instagram_insights_resilient",
                return_value=(
                    {
                        "data": [
                            {"name": "views", "values": [{"value": 120}]},
                            {"name": "reach", "values": [{"value": 90}]},
                        ]
                    },
                    [],
                ),
            ), patch.object(
                reel_scheduler,
                "fetch_instagram_account_identity",
                side_effect=SystemExit(f"request access_token={secret} failed"),
            ), patch.object(
                reel_scheduler,
                "fetch_instagram_account_flow",
                side_effect=SystemExit(f"request access_token={secret} failed"),
            ), patch.object(
                reel_scheduler,
                "fetch_instagram_account_reach",
                side_effect=SystemExit(f"request access_token={secret} failed"),
            ), patch.object(
                reel_scheduler,
                "fetch_instagram_account_reel_content_breakdown",
                side_effect=SystemExit(f"request access_token={secret} failed"),
            ), patch.object(
                reel_scheduler,
                "fetch_instagram_account_reel_audience_breakdown",
                side_effect=SystemExit(f"request access_token={secret} failed"),
            ):
                output = io.StringIO()
                with redirect_stdout(output):
                    result = reel_scheduler.sync_instagram_insights(args, db_path)

            self.assertEqual(result, 0)
            self.assertNotIn(secret, output.getvalue())
            with reel_ledger.connect(db_path) as conn:
                media = conn.execute(
                    "SELECT views, reach FROM insights WHERE media_id='178900002'"
                ).fetchone()
            self.assertEqual(media["views"], 120)
            self.assertEqual(media["reach"], 90)

    def test_account_sync_is_invoked_once_for_two_reels_in_same_channel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "reels.db"
            with reel_ledger.connect(db_path) as conn:
                for index in range(2):
                    reel_ledger.upsert_imported(
                        conn,
                        content_hash=f"media-{index}",
                        channel_id="aibrief_jp",
                        lang="ja",
                        clip_dir=f"/clip-{index}",
                        media_path=f"/clip-{index}/reel.mp4",
                        status=reel_ledger.STATUS_PUBLISHED,
                        published_at=f"2026-07-{27 + index}T00:00:00+00:00",
                        media_id=f"17890000{index}",
                    )
            args = argparse.Namespace(
                channel="aibrief_jp",
                limit=None,
                media_id=None,
                dry_run=False,
                metrics="views",
                access_token="",
                graph_api_version="v25.0",
                graph_api_root="https://graph.facebook.com",
                account_insights=True,
                account_follow_backfill_days=0,
                account_follow_refetch_days=0,
            )
            with patch("instagram_publish.load_env_file"), patch(
                "instagram_publish.resolve_instagram_access_token",
                return_value=("token", "test"),
            ), patch(
                "instagram_publish.resolve_instagram_user_id",
                return_value=("1784", "test"),
            ), patch.object(
                reel_scheduler,
                "fetch_instagram_insights_resilient",
                return_value=(
                    {"data": [{"name": "views", "values": [{"value": 1}]}]},
                    [],
                ),
            ), patch.object(
                reel_scheduler,
                "sync_instagram_account_insights_for_channel",
                return_value={
                    "account_snapshots": 1,
                    "flow_windows": 0,
                    "account_errors": 0,
                },
            ) as account_sync:
                result = reel_scheduler.sync_instagram_insights(args, db_path)

            self.assertEqual(result, 0)
            account_sync.assert_called_once()

    def test_sync_parser_enables_account_collection_with_configurable_windows(self) -> None:
        args = reel_scheduler.build_parser().parse_args(
            [
                "sync-insights",
                "--channel",
                "aibrief_jp",
                "--account-follow-backfill-days",
                "45",
                "--account-follow-refetch-days",
                "5",
            ]
        )

        self.assertTrue(args.account_insights)
        self.assertEqual(args.account_follow_backfill_days, 45)
        self.assertEqual(args.account_follow_refetch_days, 5)


if __name__ == "__main__":
    unittest.main()
