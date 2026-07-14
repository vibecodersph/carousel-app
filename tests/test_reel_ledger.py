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
            reel_ledger.record_insight(
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
                    "clips_replays_count": 321,
                    "facebook_views": 700,
                    "crossposted_views": 1900,
                    "follows": 9,
                },
            )
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
            self.assertEqual(row["clips_replays_count"], 321)
            self.assertEqual(row["facebook_views"], 700)
            self.assertEqual(row["crossposted_views"], 1900)
            self.assertEqual(row["follows"], 9)

            latest = reel_ledger.latest_insight_rows(conn, "aibrief_jp", limit=None)[0]
            self.assertEqual(latest["views"], 1200)
            self.assertEqual(latest["total_views"], 2200)
            self.assertEqual(latest["ig_reels_avg_watch_time"], 4321.5)
            self.assertEqual(latest["reels_skip_rate"], 0.375)
            self.assertEqual(latest["facebook_views"], 700)
            self.assertEqual(latest["crossposted_views"], 1900)
            self.assertEqual(latest["follows"], 9)

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
            self.assertEqual(columns["clips_replays_count"], "INTEGER")
            self.assertEqual(columns["facebook_views"], "INTEGER")
            self.assertEqual(columns["crossposted_views"], "INTEGER")
            self.assertEqual(columns["follows"], "INTEGER")

            legacy = conn.execute(
                "SELECT * FROM insights WHERE media_id='legacy-media'"
            ).fetchone()
            self.assertEqual(legacy["views"], 456)
            self.assertEqual(legacy["reach"], 321)
            self.assertEqual(legacy["saved"], 12)
            self.assertIsNone(legacy["total_views"])
            self.assertEqual(legacy["raw"], '{"legacy": true}')
            schema_version = conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()[0]
            self.assertEqual(schema_version, str(reel_ledger.SCHEMA_VERSION))

    def test_hash_file_streams_correct_sha256(self) -> None:
        blob = self.db.parent / "x.bin"
        blob.write_bytes(b"hello reels")
        self.assertEqual(reel_ledger.hash_file(blob), hashlib.sha256(b"hello reels").hexdigest())


if __name__ == "__main__":
    unittest.main()
