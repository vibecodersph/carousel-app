import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

import reel_ledger


class ReelLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "reels.db"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_discovered_rescan_preserves_lifecycle_but_refreshes_metadata(self) -> None:
        with reel_ledger.connect(self.db) as conn:
            result = reel_ledger.upsert_discovered(
                conn,
                content_hash="h1",
                channel_id="aibrief_jp",
                lang="ja",
                clip_dir="/clips/001",
                media_path="/clips/001/reel.ja.aibrief_jp.mp4",
                source_video="VID",
                title="original",
            )
            self.assertEqual(result, "inserted")
            reel_ledger.set_status(
                conn, "h1", "aibrief_jp", reel_ledger.STATUS_SCHEDULED,
                scheduled_at="2026-06-24T09:00:00+09:00",
            )

        # A later scan of the same clip (moved folder, new title) must not undo scheduling.
        with reel_ledger.connect(self.db) as conn:
            result = reel_ledger.upsert_discovered(
                conn,
                content_hash="h1",
                channel_id="aibrief_jp",
                lang="ja",
                clip_dir="/clips/001-renamed",
                media_path="/clips/001-renamed/reel.ja.aibrief_jp.mp4",
                source_video="VID",
                title="updated",
            )
            self.assertEqual(result, "updated")
            row = reel_ledger.get_reel(conn, "h1", "aibrief_jp")
            self.assertEqual(row["status"], reel_ledger.STATUS_SCHEDULED)
            self.assertEqual(row["scheduled_at"], "2026-06-24T09:00:00+09:00")
            self.assertEqual(row["clip_dir"], "/clips/001-renamed")
            self.assertEqual(row["title"], "updated")

    def test_imported_precedence_never_downgrades_published(self) -> None:
        with reel_ledger.connect(self.db) as conn:
            reel_ledger.upsert_imported(
                conn,
                content_hash="h2",
                channel_id="aibrief_jp",
                lang="ja",
                clip_dir="/clips/002",
                media_path="/clips/002/reel.ja.aibrief_jp.mp4",
                status=reel_ledger.STATUS_PUBLISHED,
                published_at="2026-06-23T00:00:00+00:00",
                permalink="https://instagram.com/p/abc",
            )
            # A duplicate schedule (e.g. the _attributed copy) calls it 'scheduled'.
            result = reel_ledger.upsert_imported(
                conn,
                content_hash="h2",
                channel_id="aibrief_jp",
                lang="ja",
                clip_dir="/clips/002",
                media_path="/clips/002/reel.ja.aibrief_jp.mp4",
                status=reel_ledger.STATUS_SCHEDULED,
            )
            self.assertEqual(result, "kept")
            row = reel_ledger.get_reel(conn, "h2", "aibrief_jp")
            self.assertEqual(row["status"], reel_ledger.STATUS_PUBLISHED)
            self.assertEqual(row["permalink"], "https://instagram.com/p/abc")

    def test_counts_and_upcoming_order_across_timezones(self) -> None:
        with reel_ledger.connect(self.db) as conn:
            reel_ledger.upsert_discovered(
                conn, content_hash="a", channel_id="aibrief_jp", lang="ja",
                clip_dir="/c/a", media_path="/c/a/x.mp4",
            )
            reel_ledger.upsert_imported(
                conn, content_hash="b", channel_id="aibrief_jp", lang="ja",
                clip_dir="/c/b", media_path="/c/b/x.mp4",
                status=reel_ledger.STATUS_SCHEDULED, scheduled_at="2026-06-24T09:00:00+09:00",
            )
            reel_ledger.upsert_imported(
                conn, content_hash="c", channel_id="vibecodersph", lang="en",
                clip_dir="/c/c", media_path="/c/c/x.mp4",
                status=reel_ledger.STATUS_SCHEDULED, scheduled_at="2026-06-23T12:00:00+08:00",
            )
            reel_ledger.upsert_imported(
                conn, content_hash="d", channel_id="aibrief_jp", lang="ja",
                clip_dir="/c/d", media_path="/c/d/x.mp4",
                status=reel_ledger.STATUS_PREVIEWED, scheduled_at="2026-06-23T20:00:00+09:00",
            )

        with reel_ledger.connect(self.db) as conn:
            counts = reel_ledger.status_counts(conn)
            self.assertEqual(counts["aibrief_jp"][reel_ledger.STATUS_NEW], 1)
            self.assertEqual(counts["aibrief_jp"][reel_ledger.STATUS_SCHEDULED], 1)
            self.assertEqual(counts["aibrief_jp"][reel_ledger.STATUS_PREVIEWED], 1)
            self.assertEqual(counts["vibecodersph"][reel_ledger.STATUS_SCHEDULED], 1)

            # vibecodersph @ 2026-06-23T12:00+08:00 (04:00Z) precedes
            # aibrief_jp @ 2026-06-24T09:00+09:00 (00:00Z next day), despite the
            # later wall-clock date string.
            order = [(row["channel_id"], row["status"]) for row in reel_ledger.upcoming(conn)]
            self.assertEqual(
                order,
                [
                    ("vibecodersph", reel_ledger.STATUS_SCHEDULED),
                    ("aibrief_jp", reel_ledger.STATUS_PREVIEWED),
                    ("aibrief_jp", reel_ledger.STATUS_SCHEDULED),
                ],
            )
            self.assertEqual(len(reel_ledger.upcoming(conn, "aibrief_jp")), 2)

    def test_record_and_read_insight_snapshot(self) -> None:
        with reel_ledger.connect(self.db) as conn:
            reel_ledger.upsert_imported(
                conn, content_hash="m", channel_id="aibrief_jp", lang="ja",
                clip_dir="/c/m", media_path="/c/m/x.mp4",
                status=reel_ledger.STATUS_PUBLISHED, media_id="178000",
            )
            inserted = reel_ledger.record_insight(
                conn, content_hash="m", channel_id="aibrief_jp", media_id="178000",
                metrics={
                    "views": 1200,
                    "total_views": 2200,
                    "reach": 900,
                    "likes": 12,
                    "total_likes": 18,
                    "comments": 2,
                    "total_comments": 3,
                    "saved": 40,
                    "shares": 5,
                    "total_interactions": 75,
                    "ig_reels_video_view_total_time": 987654,
                    "ig_reels_avg_watch_time": 4321.5,
                    "reels_skip_rate": 0.375,
                    "reposts": 6,
                    "clips_replays_count": 321,
                    "facebook_views": 700,
                    "crossposted_views": 1900,
                    "follows": 9,
                },
            )
            self.assertTrue(inserted)
            row = conn.execute("SELECT * FROM insights WHERE media_id=?", ("178000",)).fetchone()
            self.assertEqual(row["views"], 1200)
            self.assertEqual(row["total_views"], 2200)
            self.assertEqual(row["likes"], 12)
            self.assertEqual(row["total_likes"], 18)
            self.assertEqual(row["comments"], 2)
            self.assertEqual(row["total_comments"], 3)
            self.assertEqual(row["saved"], 40)
            self.assertEqual(row["ig_reels_video_view_total_time"], 987654)
            self.assertEqual(row["ig_reels_avg_watch_time"], 4321.5)
            self.assertEqual(row["reels_skip_rate"], 0.375)
            self.assertEqual(row["reposts"], 6)
            self.assertEqual(row["clips_replays_count"], 321)
            self.assertEqual(row["facebook_views"], 700)
            self.assertEqual(row["crossposted_views"], 1900)
            self.assertEqual(row["follows"], 9)

            latest = reel_ledger.latest_insight_rows(conn, "aibrief_jp", limit=None)[0]
            self.assertEqual(latest["views"], 1200)
            self.assertEqual(latest["total_views"], 2200)
            self.assertEqual(latest["ig_reels_avg_watch_time"], 4321.5)
            self.assertEqual(latest["reels_skip_rate"], 0.375)
            self.assertEqual(latest["reposts"], 6)
            self.assertEqual(latest["facebook_views"], 700)
            self.assertEqual(latest["crossposted_views"], 1900)
            self.assertEqual(latest["follows"], 9)

    def test_record_insight_is_idempotent_only_for_an_exact_observation(self) -> None:
        metrics = {
            "views": 1200,
            "reach": 900,
            "saved": 40,
            "shares": 5,
            "ig_reels_avg_watch_time": 4321.5,
        }
        captured_at = "2026-07-20T00:00:00+00:00"

        with reel_ledger.connect(self.db) as conn:
            reel_ledger.upsert_imported(
                conn,
                content_hash="same",
                channel_id="aibrief_jp",
                lang="ja",
                clip_dir="/c/same",
                media_path="/c/same/x.mp4",
                status=reel_ledger.STATUS_PUBLISHED,
                media_id="178001",
            )
            self.assertTrue(
                reel_ledger.record_insight(
                    conn,
                    content_hash="same",
                    channel_id="aibrief_jp",
                    media_id="178001",
                    metrics=metrics,
                    raw='{"source":"graph"}',
                    captured_at=captured_at,
                )
            )
            self.assertFalse(
                reel_ledger.record_insight(
                    conn,
                    content_hash="same",
                    channel_id="aibrief_jp",
                    media_id="178001",
                    metrics=dict(metrics),
                    raw='{"source":"graph"}',
                    captured_at=captured_at,
                )
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM insights").fetchone()[0],
                1,
            )

            # An unchanged observation at a later fetch remains append-only.
            self.assertTrue(
                reel_ledger.record_insight(
                    conn,
                    content_hash="same",
                    channel_id="aibrief_jp",
                    media_id="178001",
                    metrics=metrics,
                    raw='{"source":"graph"}',
                    captured_at="2026-07-20T01:00:00+00:00",
                )
            )

            # The same key and timestamp is also distinct when a persisted value
            # or the raw source payload differs.
            changed_metrics = dict(metrics, reach=901)
            self.assertTrue(
                reel_ledger.record_insight(
                    conn,
                    content_hash="same",
                    channel_id="aibrief_jp",
                    media_id="178001",
                    metrics=changed_metrics,
                    raw='{"source":"graph"}',
                    captured_at=captured_at,
                )
            )
            self.assertTrue(
                reel_ledger.record_insight(
                    conn,
                    content_hash="same",
                    channel_id="aibrief_jp",
                    media_id="178001",
                    metrics=metrics,
                    raw='{"source":"graph","retry":true}',
                    captured_at=captured_at,
                )
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM insights").fetchone()[0],
                4,
            )

    def test_connect_migrates_existing_insights_table_without_losing_data(self) -> None:
        with sqlite3.connect(self.db) as conn:
            conn.executescript(
                """
                CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT);
                INSERT INTO schema_meta(key, value) VALUES ('schema_version', '2');
                CREATE TABLE insights (
                  id INTEGER PRIMARY KEY,
                  content_hash TEXT NOT NULL,
                  channel_id TEXT NOT NULL,
                  media_id TEXT NOT NULL,
                  captured_at TEXT NOT NULL,
                  views INTEGER, reach INTEGER, likes INTEGER, comments INTEGER,
                  saved INTEGER, shares INTEGER, total_interactions INTEGER,
                  raw TEXT
                );
                INSERT INTO insights (
                  content_hash, channel_id, media_id, captured_at, views, reach, saved, raw
                ) VALUES (
                  'legacy-hash', 'aibrief_jp', 'legacy-media',
                  '2026-07-01T00:00:00+00:00', 456, 321, 12, '{"legacy": true}'
                );
                """
            )

        with reel_ledger.connect(self.db) as conn:
            columns = {
                str(row[1]): str(row[2]).upper()
                for row in conn.execute("PRAGMA table_info(insights)").fetchall()
            }
            self.assertEqual(columns["total_views"], "INTEGER")
            self.assertEqual(columns["total_likes"], "INTEGER")
            self.assertEqual(columns["total_comments"], "INTEGER")
            self.assertEqual(columns["ig_reels_video_view_total_time"], "INTEGER")
            self.assertEqual(columns["ig_reels_avg_watch_time"], "REAL")
            self.assertEqual(columns["reels_skip_rate"], "REAL")
            self.assertEqual(columns["reposts"], "INTEGER")
            self.assertEqual(columns["clips_replays_count"], "INTEGER")
            self.assertEqual(columns["facebook_views"], "INTEGER")
            self.assertEqual(columns["crossposted_views"], "INTEGER")
            self.assertEqual(columns["follows"], "INTEGER")
            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            self.assertIn("account_insight_snapshots", tables)
            self.assertIn("account_follow_flows", tables)
            flow_columns = {
                str(row[1])
                for row in conn.execute(
                    "PRAGMA table_info(account_follow_flows)"
                ).fetchall()
            }
            for column in (
                "reach",
                "reel_reach",
                "reel_non_follower_reach",
                "reel_follower_reach",
                "reel_views",
                "reel_likes",
                "reel_comments",
                "reel_saves",
                "reel_shares",
                "reel_total_interactions",
                "reel_content_breakdown_fetched",
                "reel_audience_breakdown_fetched",
            ):
                self.assertIn(column, flow_columns)
            self.assertIn("observation_key", flow_columns)

            legacy = conn.execute(
                "SELECT * FROM insights WHERE media_id='legacy-media'"
            ).fetchone()
            self.assertEqual(legacy["views"], 456)
            self.assertEqual(legacy["reach"], 321)
            self.assertEqual(legacy["saved"], 12)
            self.assertIsNone(legacy["total_views"])
            self.assertIsNone(legacy["reposts"])
            self.assertEqual(legacy["raw"], '{"legacy": true}')
            schema_version = conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()[0]
            self.assertEqual(schema_version, str(reel_ledger.SCHEMA_VERSION))

    def test_connect_adds_account_metric_columns_to_legacy_flow_table(self) -> None:
        with sqlite3.connect(self.db) as conn:
            conn.executescript(
                """
                CREATE TABLE account_follow_flows (
                  id INTEGER PRIMARY KEY,
                  channel_id TEXT NOT NULL,
                  account TEXT,
                  ig_user_id TEXT NOT NULL,
                  observed_since TEXT NOT NULL,
                  observed_until TEXT NOT NULL,
                  fetched_at TEXT NOT NULL,
                  graph_api_version TEXT,
                  graph_api_root TEXT,
                  login_type TEXT,
                  token_source TEXT,
                  follows INTEGER,
                  unfollows INTEGER,
                  unknown INTEGER,
                  raw TEXT,
                  observation_key TEXT NOT NULL UNIQUE
                );
                INSERT INTO account_follow_flows (
                  channel_id, account, ig_user_id, observed_since, observed_until,
                  fetched_at, follows, unfollows, raw, observation_key
                ) VALUES (
                  'aibrief_jp', 'aibrief.jp', '17841411137200252',
                  '2026-07-20T00:00:00+00:00', '2026-07-21T00:00:00+00:00',
                  '2026-07-22T00:00:00+00:00', 7, 1, '{"legacy":true}', 'legacy-flow'
                );
                """
            )

        with reel_ledger.connect(self.db) as conn:
            columns = {
                str(row[1])
                for row in conn.execute(
                    "PRAGMA table_info(account_follow_flows)"
                ).fetchall()
            }
            for column in (
                "reach",
                "reel_reach",
                "reel_non_follower_reach",
                "reel_follower_reach",
                "reel_views",
                "reel_likes",
                "reel_comments",
                "reel_saves",
                "reel_shares",
                "reel_total_interactions",
                "reel_content_breakdown_fetched",
                "reel_audience_breakdown_fetched",
            ):
                self.assertIn(column, columns)
            legacy = conn.execute(
                "SELECT follows, unfollows, reach, reel_reach, "
                "reel_content_breakdown_fetched, raw "
                "FROM account_follow_flows WHERE observation_key='legacy-flow'"
            ).fetchone()
            self.assertEqual(legacy["follows"], 7)
            self.assertEqual(legacy["unfollows"], 1)
            self.assertIsNone(legacy["reach"])
            self.assertIsNone(legacy["reel_reach"])
            self.assertIsNone(legacy["reel_content_breakdown_fetched"])
            self.assertEqual(legacy["raw"], '{"legacy":true}')

    def test_account_insight_snapshots_are_append_only_and_exactly_idempotent(self) -> None:
        common = {
            "channel_id": "aibrief_jp",
            "account": "aibrief.jp",
            "ig_user_id": "17841411137200252",
            "followers_count": 220,
            "media_count": 131,
            "graph_api_version": "v25.0",
            "graph_api_root": "https://graph.facebook.com",
            "login_type": "facebook_login",
            "token_source": "META_SYSTEM_USER_ACCESS_TOKEN_AIBRIEF_JP",
            "raw": '{"followers_count":220,"media_count":131}',
        }
        with reel_ledger.connect(self.db) as conn:
            self.assertTrue(
                reel_ledger.record_account_insight_snapshot(
                    conn,
                    **common,
                    captured_at="2026-07-28T12:00:00Z",
                )
            )
            # Equivalent UTC spellings are the same exact observation.
            self.assertFalse(
                reel_ledger.record_account_insight_snapshot(
                    conn,
                    **common,
                    fetched_at="2026-07-28T12:00:00+00:00",
                )
            )
            # An unchanged later fetch is still retained.
            self.assertTrue(
                reel_ledger.record_account_insight_snapshot(
                    conn,
                    **common,
                    fetched_at="2026-07-28T13:00:00+00:00",
                )
            )
            # A revision at the same instant is retained when a value changes.
            revised = dict(common, followers_count=221)
            self.assertTrue(
                reel_ledger.record_account_insight_snapshot(
                    conn,
                    **revised,
                    fetched_at="2026-07-28T13:00:00+00:00",
                )
            )

            rows = conn.execute(
                "SELECT * FROM account_insight_snapshots ORDER BY id"
            ).fetchall()
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[0]["account"], "aibrief.jp")
            self.assertEqual(rows[0]["followers_count"], 220)
            self.assertEqual(rows[0]["media_count"], 131)
            self.assertEqual(rows[0]["graph_api_version"], "v25.0")
            self.assertEqual(
                rows[0]["token_source"],
                "META_SYSTEM_USER_ACCESS_TOKEN_AIBRIEF_JP",
            )
            self.assertNotIn(
                "access_token",
                {
                    str(row[1])
                    for row in conn.execute(
                        "PRAGMA table_info(account_insight_snapshots)"
                    ).fetchall()
                },
            )
            latest = reel_ledger.latest_account_insight_snapshot(
                conn,
                "aibrief_jp",
            )
            self.assertEqual(latest["followers_count"], 221)

    def test_account_follow_flows_keep_interval_revisions_and_nullable_reach(self) -> None:
        common = {
            "channel_id": "aibrief_jp",
            "account": "aibrief.jp",
            "ig_user_id": "17841411137200252",
            "day": "2026-07-27",
            "follows": 7,
            "unfollows": 1,
            "unknown": 0,
            "graph_api_version": "v25.0",
            "graph_api_root": "https://graph.facebook.com",
            "login_type": "facebook_login",
            "token_source": "channel:aibrief_jp",
            "raw": '{"follows":7,"unfollows":1}',
        }
        with reel_ledger.connect(self.db) as conn:
            self.assertTrue(
                reel_ledger.record_account_follow_flow(
                    conn,
                    **common,
                    reach=None,
                    fetched_at="2026-07-28T00:10:00Z",
                )
            )
            self.assertFalse(
                reel_ledger.record_account_follow_flow(
                    conn,
                    **common,
                    reach=None,
                    fetched_at="2026-07-28T00:10:00+00:00",
                )
            )
            # The same interval can be revised later, including newly available reach.
            revised = dict(
                common,
                follows=8,
                reach=1800,
                reel_reach=1700,
                reel_non_follower_reach=1600,
                reel_follower_reach=120,
                reel_views=2200,
                reel_likes=30,
                reel_comments=2,
                reel_saves=20,
                reel_shares=8,
                reel_total_interactions=60,
                reel_content_breakdown_fetched=True,
                reel_audience_breakdown_fetched=True,
                raw='{"follows":8,"unfollows":1,"reach":1800,"reel_reach":1700}',
            )
            self.assertTrue(
                reel_ledger.record_account_follow_flow(
                    conn,
                    **revised,
                    fetched_at="2026-07-29T00:10:00Z",
                )
            )

            all_rows = reel_ledger.account_follow_flows(conn, "aibrief_jp")
            self.assertEqual(len(all_rows), 2)
            self.assertEqual(
                all_rows[0]["observed_since"],
                "2026-07-27T00:00:00+00:00",
            )
            self.assertEqual(
                all_rows[0]["observed_until"],
                "2026-07-28T00:00:00+00:00",
            )
            self.assertIsNone(all_rows[0]["reach"])
            self.assertEqual(all_rows[1]["reach"], 1800)

            latest = reel_ledger.latest_account_follow_flows(
                conn,
                "aibrief_jp",
            )
            self.assertEqual(len(latest), 1)
            self.assertEqual(latest[0]["follows"], 8)
            self.assertEqual(latest[0]["unfollows"], 1)
            self.assertEqual(latest[0]["reach"], 1800)
            self.assertEqual(latest[0]["reel_reach"], 1700)
            self.assertEqual(latest[0]["reel_non_follower_reach"], 1600)
            self.assertEqual(latest[0]["reel_follower_reach"], 120)
            self.assertEqual(latest[0]["reel_views"], 2200)
            self.assertEqual(latest[0]["reel_likes"], 30)
            self.assertEqual(latest[0]["reel_comments"], 2)
            self.assertEqual(latest[0]["reel_saves"], 20)
            self.assertEqual(latest[0]["reel_shares"], 8)
            self.assertEqual(latest[0]["reel_total_interactions"], 60)
            self.assertEqual(latest[0]["reel_content_breakdown_fetched"], 1)
            self.assertEqual(latest[0]["reel_audience_breakdown_fetched"], 1)

    def test_account_follow_flow_days_supports_inclusive_range_and_reach_coverage(self) -> None:
        with reel_ledger.connect(self.db) as conn:
            reel_ledger.record_account_follow_flow(
                conn,
                channel_id="aibrief_jp",
                ig_user_id="17841411137200252",
                day="2026-07-24",
                follows=None,
                unfollows=None,
                unknown=None,
                reach=800,
                fetched_at="2026-07-24T23:59:00Z",
            )
            for day, reach in (
                ("2026-07-25", 900),
                ("2026-07-26", None),
                ("2026-07-27", 1100),
                ("2026-07-28", 1200),
            ):
                reel_ledger.record_account_follow_flow(
                    conn,
                    channel_id="aibrief_jp",
                    ig_user_id="17841411137200252",
                    day=day,
                    follows=2,
                    unfollows=0,
                    reach=reach,
                    fetched_at=f"{day}T23:59:00Z",
                )
            reel_ledger.record_account_follow_flow(
                conn,
                channel_id="other",
                ig_user_id="999",
                day="2026-07-26",
                follows=99,
                unfollows=0,
                reach=9999,
                fetched_at="2026-07-26T23:59:00Z",
            )

            self.assertEqual(
                reel_ledger.account_follow_flow_days(
                    conn,
                    "aibrief_jp",
                    "2026-07-25",
                    "2026-07-27",
                ),
                {"2026-07-25", "2026-07-26", "2026-07-27"},
            )
            self.assertEqual(
                reel_ledger.account_follow_flow_days(
                    conn,
                    "aibrief_jp",
                    "2026-07-25",
                    "2026-07-27",
                    require_reach=True,
                ),
                {"2026-07-25", "2026-07-27"},
            )
            self.assertEqual(
                reel_ledger.account_follow_flow_days(
                    conn,
                    "aibrief_jp",
                    "2026-07-24",
                    "2026-07-27",
                    require_reach=True,
                    require_flow=True,
                ),
                {"2026-07-25", "2026-07-27"},
            )

    def test_account_follow_flow_rejects_ambiguous_or_invalid_interval(self) -> None:
        with reel_ledger.connect(self.db) as conn:
            with self.assertRaises(ValueError):
                reel_ledger.record_account_follow_flow(
                    conn,
                    channel_id="aibrief_jp",
                    ig_user_id="17841411137200252",
                    day="2026-07-27",
                    observed_since="2026-07-27T00:00:00Z",
                    observed_until="2026-07-28T00:00:00Z",
                    follows=1,
                    unfollows=0,
                )
            with self.assertRaises(ValueError):
                reel_ledger.record_account_follow_flow(
                    conn,
                    channel_id="aibrief_jp",
                    ig_user_id="17841411137200252",
                    observed_since="2026-07-28T00:00:00Z",
                    observed_until="2026-07-27T00:00:00Z",
                    follows=1,
                    unfollows=0,
                )

    def test_hash_file_streams_correct_sha256(self) -> None:
        blob = self.db.parent / "x.bin"
        blob.write_bytes(b"hello reels")
        self.assertEqual(reel_ledger.hash_file(blob), hashlib.sha256(b"hello reels").hexdigest())


if __name__ == "__main__":
    unittest.main()
