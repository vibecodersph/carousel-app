from __future__ import annotations

import importlib.util
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import reel_ledger

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "sync_aibrief_facebook_queue",
    ROOT / "scripts" / "sync_aibrief_facebook_queue.py",
)
assert SPEC is not None and SPEC.loader is not None
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)


class AIBriefFacebookQueueSyncTests(unittest.TestCase):
    def seed(
        self,
        db_path: Path,
        *,
        content_hash: str,
        scheduled_at: str,
        status: str = reel_ledger.STATUS_SCHEDULED,
        trial_reel: bool = False,
        trial_graduation_strategy: str | None = None,
    ) -> None:
        with reel_ledger.connect(db_path) as conn:
            reel_ledger.upsert_imported(
                conn,
                content_hash=content_hash,
                channel_id="aibrief_jp",
                lang="ja",
                clip_dir=f"/clips/{content_hash}",
                media_path=f"/clips/{content_hash}/reel.ja.aibrief_jp.mp4",
                source_video="source",
                title=f"title {content_hash}",
                status=status,
                scheduled_at=scheduled_at,
                manifest_path=f"/manifests/{content_hash}/manifest.json",
            )
            if trial_reel:
                conn.execute(
                    "UPDATE reels SET trial_reel=1, trial_graduation_strategy=? "
                    "WHERE content_hash=? AND channel_id=?",
                    (trial_graduation_strategy or "MANUAL", content_hash, "aibrief_jp"),
                )

    def test_mirrors_only_rows_at_or_after_activation_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_db = root / "reels.db"
            facebook_db = root / "facebook.db"
            self.seed(source_db, content_hash="old", scheduled_at="2026-07-24T18:00:00+09:00")
            self.seed(source_db, content_hash="next", scheduled_at="2026-07-24T20:58:00+09:00")

            counts = sync.sync_queue(
                source_db=source_db,
                facebook_db=facebook_db,
                channel_id="aibrief_jp",
                start_at=datetime.fromisoformat("2026-07-24T20:58:00+09:00"),
            )

            self.assertEqual(counts["inserted"], 1)
            with reel_ledger.connect(facebook_db) as conn:
                rows = conn.execute("SELECT * FROM reels").fetchall()
            self.assertEqual([row["content_hash"] for row in rows], ["next"])
            self.assertEqual(rows[0]["status"], reel_ledger.STATUS_SCHEDULED)

    def test_does_not_downgrade_published_facebook_row(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_db = root / "reels.db"
            facebook_db = root / "facebook.db"
            when = "2026-07-25T09:00:00+09:00"
            self.seed(source_db, content_hash="same", scheduled_at=when)
            self.seed(
                facebook_db,
                content_hash="same",
                scheduled_at=when,
                status=reel_ledger.STATUS_PUBLISHED,
            )

            counts = sync.sync_queue(
                source_db=source_db,
                facebook_db=facebook_db,
                channel_id="aibrief_jp",
                start_at=datetime.fromisoformat("2026-07-24T20:58:00+09:00"),
            )

            self.assertEqual(counts["kept"], 1)
            with reel_ledger.connect(facebook_db) as conn:
                row = conn.execute("SELECT * FROM reels").fetchone()
            self.assertEqual(row["status"], reel_ledger.STATUS_PUBLISHED)

    def test_does_not_mirror_already_published_instagram_row(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_db = root / "reels.db"
            facebook_db = root / "facebook.db"
            self.seed(
                source_db,
                content_hash="already-published",
                scheduled_at="2026-07-24T20:58:00+09:00",
                status=reel_ledger.STATUS_PUBLISHED,
            )

            counts = sync.sync_queue(
                source_db=source_db,
                facebook_db=facebook_db,
                channel_id="aibrief_jp",
                start_at=datetime.fromisoformat("2026-07-24T20:58:00+09:00"),
            )

            self.assertEqual(counts["inserted"], 0)
            with reel_ledger.connect(facebook_db) as conn:
                count = conn.execute("SELECT COUNT(*) FROM reels").fetchone()[0]
            self.assertEqual(count, 0)

    def test_does_not_mirror_active_instagram_trial_reel(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_db = root / "reels.db"
            facebook_db = root / "facebook.db"
            self.seed(
                source_db,
                content_hash="trial",
                scheduled_at="2026-07-25T09:00:00+09:00",
                trial_reel=True,
            )

            counts = sync.sync_queue(
                source_db=source_db,
                facebook_db=facebook_db,
                channel_id="aibrief_jp",
                start_at=datetime.fromisoformat("2026-07-24T20:58:00+09:00"),
            )

            self.assertEqual(counts["inserted"], 0)
            with reel_ledger.connect(facebook_db) as conn:
                count = conn.execute("SELECT COUNT(*) FROM reels").fetchone()[0]
            self.assertEqual(count, 0)

    def test_cancels_mutable_facebook_row_when_source_becomes_trial(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_db = root / "reels.db"
            facebook_db = root / "facebook.db"
            when = "2026-07-25T13:00:00+09:00"
            self.seed(source_db, content_hash="converted", scheduled_at=when)
            start_at = datetime.fromisoformat("2026-07-24T20:58:00+09:00")

            first_counts = sync.sync_queue(
                source_db=source_db,
                facebook_db=facebook_db,
                channel_id="aibrief_jp",
                start_at=start_at,
            )
            self.assertEqual(first_counts["inserted"], 1)

            with reel_ledger.connect(source_db) as conn:
                conn.execute(
                    "UPDATE reels SET trial_reel=1, trial_graduation_strategy='MANUAL' "
                    "WHERE content_hash='converted' AND channel_id='aibrief_jp'"
                )

            second_counts = sync.sync_queue(
                source_db=source_db,
                facebook_db=facebook_db,
                channel_id="aibrief_jp",
                start_at=start_at,
            )

            self.assertEqual(second_counts["cancelled"], 1)
            with reel_ledger.connect(facebook_db) as conn:
                row = conn.execute("SELECT * FROM reels").fetchone()
            self.assertEqual(row["status"], reel_ledger.STATUS_SKIPPED)
            self.assertEqual(
                row["last_error"],
                "Instagram Trial Reel excluded from Facebook mirroring",
            )

    def test_does_not_cancel_immutable_facebook_row_for_trial_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_db = root / "reels.db"
            facebook_db = root / "facebook.db"
            when = "2026-07-25T18:00:00+09:00"
            self.seed(
                source_db,
                content_hash="published-trial",
                scheduled_at=when,
                status=reel_ledger.STATUS_PUBLISHED,
                trial_reel=True,
            )
            self.seed(
                facebook_db,
                content_hash="published-trial",
                scheduled_at=when,
                status=reel_ledger.STATUS_PUBLISHED,
            )

            counts = sync.sync_queue(
                source_db=source_db,
                facebook_db=facebook_db,
                channel_id="aibrief_jp",
                start_at=datetime.fromisoformat("2026-07-24T20:58:00+09:00"),
            )

            self.assertEqual(counts["cancelled"], 0)
            with reel_ledger.connect(facebook_db) as conn:
                row = conn.execute("SELECT * FROM reels").fetchone()
            self.assertEqual(row["status"], reel_ledger.STATUS_PUBLISHED)

    def test_cancels_future_facebook_row_removed_from_source_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_db = root / "reels.db"
            facebook_db = root / "facebook.db"
            when = "2026-07-25T13:00:00+09:00"
            self.seed(
                source_db,
                content_hash="removed",
                scheduled_at=when,
                status=reel_ledger.STATUS_SKIPPED,
            )
            self.seed(facebook_db, content_hash="removed", scheduled_at=when)

            counts = sync.sync_queue(
                source_db=source_db,
                facebook_db=facebook_db,
                channel_id="aibrief_jp",
                start_at=datetime.fromisoformat("2026-07-24T20:58:00+09:00"),
            )

            self.assertEqual(counts["cancelled"], 1)
            with reel_ledger.connect(facebook_db) as conn:
                row = conn.execute("SELECT * FROM reels").fetchone()
            self.assertEqual(row["status"], reel_ledger.STATUS_SKIPPED)


if __name__ == "__main__":
    unittest.main()
