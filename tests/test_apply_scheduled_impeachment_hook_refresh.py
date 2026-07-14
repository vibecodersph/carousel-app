import sqlite3
import shutil
import tempfile
import unittest
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from channel import load_channel
from scripts import apply_scheduled_impeachment_hook_refresh as migration


class SchedulerLockTests(unittest.TestCase):
    def test_lock_is_exclusive_and_cleaned_up(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            lock = Path(temporary) / "scheduler.lock"
            with migration.scheduler_lock(lock, {"run_id": "test"}):
                self.assertTrue((lock / "owner.json").is_file())
                with self.assertRaises(migration.MigrationError):
                    with migration.scheduler_lock(lock, {"run_id": "other"}):
                        pass
            self.assertFalse(lock.exists())

    def test_retained_lock_can_only_be_adopted_by_its_restore_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            lock = Path(temporary) / "scheduler.lock"
            owner = {"run_id": "run-a"}
            with migration.scheduler_lock(lock, owner):
                owner["retain_lock"] = True
                owner["retained_reason"] = "restore failed"
            self.assertTrue(lock.is_dir())
            with self.assertRaises(migration.MigrationError):
                with migration.scheduler_lock(
                    lock,
                    {"run_id": "run-b"},
                    adopt_recovery_run_id="run-b",
                ):
                    pass
            with migration.scheduler_lock(
                lock,
                {"run_id": "run-a"},
                adopt_recovery_run_id="run-a",
            ):
                self.assertTrue(lock.is_dir())
            self.assertFalse(lock.exists())


class PublishResidueTests(unittest.TestCase):
    def test_accepts_null_publish_fields_and_blank_last_error(self) -> None:
        row = {
            "status": "scheduled",
            "published_at": None,
            "media_id": None,
            "permalink": None,
            "last_error": "  ",
        }
        migration.validate_scheduled_unpublished_row(row, "old-hash")

    def test_rejects_every_publish_identity_field_and_nonblank_error(self) -> None:
        base = {
            "status": "scheduled",
            "published_at": None,
            "media_id": None,
            "permalink": None,
            "last_error": None,
        }
        for field, value in (
            ("published_at", "2026-07-13T00:00:00+00:00"),
            ("media_id", "123"),
            ("permalink", "https://example.test/reel"),
            ("last_error", "upload failed"),
        ):
            with self.subTest(field=field):
                row = {**base, field: value}
                with self.assertRaises(migration.MigrationError):
                    migration.validate_scheduled_unpublished_row(row, "old-hash")


class QueueScheduleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            """
            CREATE TABLE reels (
              content_hash TEXT,
              channel_id TEXT,
              status TEXT,
              scheduled_at TEXT,
              published_at TEXT,
              trial_reel INTEGER,
              trial_graduation_strategy TEXT
            )
            """
        )
        self.channel = load_channel("vibecodersph")

    def tearDown(self) -> None:
        self.connection.close()

    @staticmethod
    def item(old_hash: str, new_hash: str, scheduled_at: datetime) -> SimpleNamespace:
        return SimpleNamespace(
            old_hash=old_hash,
            new_hash=new_hash,
            row={
                "scheduled_at": scheduled_at.isoformat(),
                "trial_reel": 0,
                "trial_graduation_strategy": None,
            },
            schedule=None,
        )

    def insert_target(self, item: SimpleNamespace) -> None:
        self.connection.execute(
            "INSERT INTO reels VALUES (?, ?, 'scheduled', ?, NULL, 0, NULL)",
            (item.old_hash, self.channel.id, item.row["scheduled_at"]),
        )

    def test_reflows_the_entire_queue_when_earliest_is_within_eight_minutes(self) -> None:
        now = datetime(2026, 7, 13, 14, 0, tzinfo=timezone.utc)
        items = [
            self.item("old-a", "new-a", now + timedelta(minutes=5)),
            self.item("old-b", "new-b", now + timedelta(hours=1)),
        ]
        for item in items:
            self.insert_target(item)

        report = migration.schedule_items(
            connection=self.connection,
            items=items,
            channel=self.channel,
            now=now,
            threshold_minutes=8,
        )

        boundary = now + timedelta(minutes=8)
        self.assertTrue(report["reflowed_all_remaining"])
        self.assertTrue(all(item.schedule.scheduled_at > boundary for item in items))
        self.assertLess(items[0].schedule.scheduled_at, items[1].schedule.scheduled_at)
        self.assertNotEqual(
            items[1].schedule.scheduled_at,
            datetime.fromisoformat(items[1].row["scheduled_at"]),
        )

    def test_preserves_all_schedule_fields_when_earliest_is_safe(self) -> None:
        now = datetime(2026, 7, 13, 0, 0, tzinfo=timezone.utc)
        original = now + timedelta(hours=12)
        item = self.item("old-a", "new-a", original)
        item.row["trial_reel"] = 1
        item.row["trial_graduation_strategy"] = "MANUAL"
        self.insert_target(item)

        report = migration.schedule_items(
            connection=self.connection,
            items=[item],
            channel=self.channel,
            now=now,
            threshold_minutes=8,
        )

        self.assertFalse(report["reflowed_all_remaining"])
        self.assertEqual(item.schedule.scheduled_at, original)
        self.assertTrue(item.schedule.trial_reel)
        self.assertEqual(item.schedule.trial_graduation_strategy, "MANUAL")


class GuardedLedgerUpdateTests(unittest.TestCase):
    def test_update_keeps_scheduled_state_and_clears_blank_last_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            live_media = root / "reel.mp4"
            live_notes = root / "notes.json"
            live_manifest = root / "manifest.json"
            live_caption = root / "caption.txt"
            live_media.write_bytes(b"old-media")
            live_notes.write_text("old-notes", encoding="utf-8")
            live_manifest.write_text("old-manifest", encoding="utf-8")
            live_caption.write_text("old-caption", encoding="utf-8")
            temp_media = root / ".reel.tmp"
            temp_notes = root / ".notes.tmp"
            temp_manifest = root / ".manifest.tmp"
            temp_caption = root / ".caption.tmp"
            temp_media.write_bytes(b"new-media")
            temp_notes.write_text("new-notes", encoding="utf-8")
            temp_manifest.write_text("new-manifest", encoding="utf-8")
            temp_caption.write_text("new-caption", encoding="utf-8")

            connection = sqlite3.connect(":memory:", isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute(
                """
                CREATE TABLE reels (
                  content_hash TEXT,
                  channel_id TEXT,
                  title TEXT,
                  caption TEXT,
                  scheduled_at TEXT,
                  trial_reel INTEGER,
                  trial_graduation_strategy TEXT,
                  updated_at TEXT,
                  status TEXT,
                  published_at TEXT,
                  media_path TEXT,
                  manifest_path TEXT,
                  media_id TEXT,
                  permalink TEXT,
                  last_error TEXT
                )
                """
            )
            old_hash = migration.sha256_file(live_media)
            new_hash = migration.sha256_file(temp_media)
            old_schedule = "2026-07-13T22:07:00+08:00"
            connection.execute(
                "INSERT INTO reels VALUES (?, 'vibecodersph', 'old', 'old-caption', ?, "
                "0, NULL, 'old-updated', 'scheduled', NULL, ?, ?, NULL, NULL, '   ')",
                (old_hash, old_schedule, str(live_media), str(live_manifest)),
            )
            row = dict(connection.execute("SELECT * FROM reels").fetchone())
            item = SimpleNamespace(
                old_hash=old_hash,
                new_hash=new_hash,
                row=row,
                live_media=live_media,
                live_notes=live_notes,
                live_manifest=live_manifest,
                live_caption=live_caption,
                temp_media=temp_media,
                temp_notes=temp_notes,
                temp_manifest=temp_manifest,
                temp_caption=temp_caption,
                schedule=migration.ScheduleValue(
                    scheduled_at=datetime.fromisoformat("2026-07-14T07:00:00+08:00"),
                    trial_reel=False,
                    trial_graduation_strategy="",
                ),
                review={"new_hook": "New hook"},
                caption="New caption",
            )

            connection.execute("BEGIN IMMEDIATE")
            migration.swap_and_update(
                connection=connection,
                items=[item],
                updated_at="2026-07-13T15:00:00+00:00",
            )
            connection.commit()
            updated = dict(connection.execute("SELECT * FROM reels").fetchone())
            connection.close()

            self.assertEqual(updated["content_hash"], new_hash)
            self.assertEqual(updated["status"], "scheduled")
            self.assertIsNone(updated["published_at"])
            self.assertIsNone(updated["media_id"])
            self.assertIsNone(updated["permalink"])
            self.assertIsNone(updated["last_error"])
            self.assertEqual(live_media.read_bytes(), b"new-media")


class InterruptRecoveryTests(unittest.TestCase):
    def test_keyboard_interrupt_after_file_mutation_restores_db_and_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db_path = root / "live.db"
            connection = sqlite3.connect(db_path)
            connection.execute("CREATE TABLE marker (value TEXT)")
            connection.execute("INSERT INTO marker VALUES ('old')")
            connection.commit()
            connection.close()
            backup_db = root / "backup.db"
            shutil.copy2(db_path, backup_db)

            live_file = root / "live.mp4"
            backup_file = root / "backup.mp4"
            live_file.write_bytes(b"original-live-bytes")
            shutil.copy2(live_file, backup_file)
            backup_manifest = {
                "database_path": str(db_path),
                "database_backup_path": str(backup_db),
                "database_backup_sha256": migration.sha256_file(backup_db),
                "artifacts": [
                    {
                        "live_path": str(live_file),
                        "backup_path": str(backup_file),
                        "sha256": migration.sha256_file(backup_file),
                    }
                ],
            }
            review = root / "review.json"
            review.write_text("{}\n", encoding="utf-8")
            review_sha = migration.sha256_file(review)
            stage_plan = root / "stage_plan.json"
            stage_plan.write_text("{}\n", encoding="utf-8")
            runs_root = root / "runs"
            lock = root / "scheduler.lock"
            item = SimpleNamespace(
                temp_media=None,
                temp_notes=None,
                temp_manifest=None,
                temp_caption=None,
            )
            args = Namespace(
                review=review,
                stage_plan=stage_plan,
                db=db_path,
                channel="vibecodersph",
                expected_count=1,
                expected_published_count=0,
                lock=lock,
                runs_root=runs_root,
                now="2026-07-13T15:00:00+00:00",
                reflow_threshold_minutes=8,
            )

            def interrupted_swap(*, connection, items, updated_at):
                del items, updated_at
                connection.execute("UPDATE marker SET value='mutated'")
                live_file.write_bytes(b"partially-swapped-bytes")
                raise KeyboardInterrupt("simulated mid-swap")

            preflight = (
                {},
                review_sha,
                {},
                [item],
                {},
                {"reflowed_all_remaining": True},
            )
            with patch.object(migration, "common_preflight", return_value=preflight), patch.object(
                migration, "create_backups", return_value=backup_manifest
            ), patch.object(migration, "prepare_temporary_files"), patch.object(
                migration, "swap_and_update", side_effect=interrupted_swap
            ):
                with self.assertRaises(migration.MigrationError):
                    migration.apply_command(args)

            self.assertEqual(live_file.read_bytes(), b"original-live-bytes")
            connection = sqlite3.connect(db_path)
            value = connection.execute("SELECT value FROM marker").fetchone()[0]
            connection.close()
            self.assertEqual(value, "old")
            self.assertFalse(lock.exists())
            run_dirs = list(runs_root.iterdir())
            self.assertEqual(len(run_dirs), 1)
            report = migration.read_json_object(run_dirs[0] / "report.json")
            self.assertEqual(report["status"], "failed_restored")
            self.assertEqual(report["automatic_restore_attempts"], 1)
            self.assertIn("KeyboardInterrupt", report["error"])

    def test_failed_automatic_restore_retries_and_retains_scheduler_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db_path = root / "live.db"
            connection = sqlite3.connect(db_path)
            connection.execute("CREATE TABLE marker (value TEXT)")
            connection.execute("INSERT INTO marker VALUES ('old')")
            connection.commit()
            connection.close()
            review = root / "review.json"
            review.write_text("{}\n", encoding="utf-8")
            stage_plan = root / "stage_plan.json"
            stage_plan.write_text("{}\n", encoding="utf-8")
            runs_root = root / "runs"
            lock = root / "scheduler.lock"
            args = Namespace(
                review=review,
                stage_plan=stage_plan,
                db=db_path,
                channel="vibecodersph",
                expected_count=1,
                expected_published_count=0,
                lock=lock,
                runs_root=runs_root,
                now="2026-07-13T15:00:00+00:00",
                reflow_threshold_minutes=8,
            )
            item = SimpleNamespace(
                temp_media=None,
                temp_notes=None,
                temp_manifest=None,
                temp_caption=None,
            )
            preflight = (
                {},
                migration.sha256_file(review),
                {},
                [item],
                {},
                {"reflowed_all_remaining": True},
            )
            backup_manifest = {
                "artifacts": [],
                "database_backup_path": str(root / "backup.db"),
            }
            with patch.object(migration, "common_preflight", return_value=preflight), patch.object(
                migration, "create_backups", return_value=backup_manifest
            ), patch.object(migration, "prepare_temporary_files"), patch.object(
                migration, "swap_and_update", side_effect=KeyboardInterrupt("mid-swap")
            ), patch.object(
                migration, "restore_from_backup", side_effect=OSError("restore unavailable")
            ) as restore:
                with self.assertRaises(migration.MigrationError):
                    migration.apply_command(args)

            self.assertEqual(restore.call_count, 2)
            self.assertTrue(lock.is_dir())
            owner = migration.read_json_object(lock / "owner.json")
            self.assertTrue(owner["retain_lock"])
            self.assertIn("restore unavailable", owner["retained_reason"])
            run_dir = next(runs_root.iterdir())
            report = migration.read_json_object(run_dir / "report.json")
            self.assertEqual(report["status"], "failed_unrecovered")
            self.assertEqual(report["automatic_restore_attempts"], 2)
            self.assertTrue(report["scheduler_lock_retained"])


if __name__ == "__main__":
    unittest.main()
