import hashlib
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
                metrics={"views": 1200, "reach": 900, "saved": 40, "total_interactions": 75},
            )
            row = conn.execute(
                "SELECT views, reach, saved, total_interactions FROM insights WHERE media_id=?",
                ("178000",),
            ).fetchone()
            self.assertEqual(row["views"], 1200)
            self.assertEqual(row["saved"], 40)

    def test_hash_file_streams_correct_sha256(self) -> None:
        blob = self.db.parent / "x.bin"
        blob.write_bytes(b"hello reels")
        self.assertEqual(reel_ledger.hash_file(blob), hashlib.sha256(b"hello reels").hexdigest())


if __name__ == "__main__":
    unittest.main()
