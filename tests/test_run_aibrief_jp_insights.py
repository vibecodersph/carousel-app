from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import run_aibrief_jp_insights


class RunAibriefJpInsightsTests(unittest.TestCase):
    def create_project(self, root: Path, *, with_snapshot: bool) -> tuple[Path, Path]:
        (root / "scripts").mkdir(parents=True)
        (root / "state").mkdir()
        (root / "out").mkdir()
        (root / "reel_scheduler.py").write_text("# test scheduler\n", encoding="utf-8")
        (root / "scripts" / "aibrief_jp_reach_analysis.py").write_text(
            "# test analyzer\n", encoding="utf-8"
        )

        db_path = root / "state" / "reels.db"
        with sqlite3.connect(db_path) as connection:
            connection.execute(
                """
                CREATE TABLE reels (
                    content_hash TEXT,
                    channel_id TEXT,
                    status TEXT,
                    media_id TEXT,
                    published_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE insights (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content_hash TEXT,
                    channel_id TEXT,
                    media_id TEXT,
                    captured_at TEXT
                )
                """
            )
            connection.execute(
                "INSERT INTO reels VALUES (?, ?, ?, ?, ?)",
                (
                    "hash-1",
                    run_aibrief_jp_insights.CHANNEL,
                    "published",
                    "media-1",
                    "2026-07-12T09:00:00+00:00",
                ),
            )
            if with_snapshot:
                connection.execute(
                    "INSERT INTO insights (content_hash, channel_id, media_id, captured_at) VALUES (?, ?, ?, ?)",
                    (
                        "hash-1",
                        run_aibrief_jp_insights.CHANNEL,
                        "media-1",
                        "2026-07-12T10:00:00+00:00",
                    ),
                )

        return db_path, root / "out" / "aibrief_jp_reel_report.html"

    def write_staged_report(
        self,
        command: list[str],
        *,
        html: str = "new html",
        media_ids: list[str] | None = None,
    ) -> None:
        report_media_ids = media_ids or ["media-1"]
        out_path = Path(command[command.index("--out") + 1])
        out_path.write_text(html, encoding="utf-8")
        out_path.with_suffix(".insights.json").write_text(
            json.dumps(
                {
                    "platform": "instagram",
                    "channel_filter": run_aibrief_jp_insights.CHANNEL,
                    "generated_at": "2026-07-13T10:15:00+00:00",
                    "items": [
                        {
                            "channel_id": run_aibrief_jp_insights.CHANNEL,
                            "media_id": media_id,
                            "content_hash": (
                                f"hash-{media_id.rsplit('-', 1)[-1]}"
                                if media_id.rsplit('-', 1)[-1].isdigit()
                                else "hash-1"
                            ),
                        }
                        for media_id in report_media_ids
                    ],
                }
            ),
            encoding="utf-8",
        )
        out_path.with_suffix(".insights.md").write_text(
            "new insights markdown\n", encoding="utf-8"
        )

    def test_unhealthy_limited_sync_preserves_existing_report_and_reach_brief(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path, report_out = self.create_project(root, with_snapshot=True)
            media_ids = [f"media-{index}" for index in range(1, 11)]
            with sqlite3.connect(db_path) as connection:
                for index in range(2, 11):
                    connection.execute(
                        "INSERT INTO reels VALUES (?, ?, ?, ?, ?)",
                        (
                            f"hash-{index}",
                            run_aibrief_jp_insights.CHANNEL,
                            "published",
                            f"media-{index}",
                            f"2026-07-12T{8 + index:02d}:00:00+00:00",
                        ),
                    )
                    if index < 10:
                        connection.execute(
                            "INSERT INTO insights (content_hash, channel_id, media_id, captured_at) VALUES (?, ?, ?, ?)",
                            (
                                f"hash-{index}",
                                run_aibrief_jp_insights.CHANNEL,
                                f"media-{index}",
                                "2026-07-12T20:00:00+00:00",
                            ),
                        )

            existing = {
                report_out: "old html",
                report_out.with_suffix(".insights.json"): "old insights json",
                report_out.with_suffix(".insights.md"): "old insights markdown",
                root / "out" / "aibrief_jp_reach_brief.json": "old reach json",
                root / "out" / "aibrief_jp_reach_brief.md": "old reach markdown",
            }
            for path, content in existing.items():
                path.write_text(content, encoding="utf-8")

            commands: list[list[str]] = []

            def fake_run(command: list[str], cwd: Path) -> int:
                self.assertEqual(cwd, root.resolve())
                commands.append(command)
                if "sync-insights" in command:
                    self.assertEqual(command[command.index("--limit") + 1], "10")
                    with sqlite3.connect(db_path) as connection:
                        for index in range(1, 10):
                            connection.execute(
                                "INSERT INTO insights (content_hash, channel_id, media_id, captured_at) VALUES (?, ?, ?, ?)",
                                (
                                    f"hash-{index}",
                                    run_aibrief_jp_insights.CHANNEL,
                                    f"media-{index}",
                                    "2026-07-13T10:15:00+00:00",
                                ),
                            )
                    return 0
                if "report" in command:
                    self.write_staged_report(command, media_ids=media_ids)
                    return 0
                self.fail(f"analyzer should not run after an unhealthy sync: {command}")

            argv = [
                "run_aibrief_jp_insights.py",
                "--root",
                str(root),
                "--db",
                str(db_path),
                "--out",
                str(report_out),
                "--sync-limit",
                "10",
            ]
            with (
                patch.object(sys, "argv", argv),
                patch.object(run_aibrief_jp_insights, "utc_now", return_value="2026-07-13T10:15:00+00:00"),
                patch.object(run_aibrief_jp_insights, "run", side_effect=fake_run),
            ):
                result = run_aibrief_jp_insights.main()

            self.assertEqual(result, 1)
            self.assertEqual(len(commands), 2)
            for path, content in existing.items():
                self.assertEqual(path.read_text(encoding="utf-8"), content)
            self.assertFalse((root / "state" / "reel_scheduler.lock").exists())
            self.assertEqual(list((root / "out").glob(".reel-report-stage.*")), [])

    def test_nonzero_sync_is_not_accepted_when_every_target_is_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path, report_out = self.create_project(root, with_snapshot=False)
            commands: list[list[str]] = []

            def fake_run(command: list[str], cwd: Path) -> int:
                self.assertEqual(cwd, root.resolve())
                commands.append(command)
                if "sync-insights" in command:
                    with sqlite3.connect(db_path) as connection:
                        connection.execute(
                            "INSERT INTO insights (content_hash, channel_id, media_id, captured_at) VALUES (?, ?, ?, ?)",
                            (
                                "hash-1",
                                run_aibrief_jp_insights.CHANNEL,
                                "media-1",
                                "2026-07-13T10:15:00+00:00",
                            ),
                        )
                    return 1
                if "report" in command:
                    self.write_staged_report(command)
                    return 0
                self.fail(f"analyzer should not run after a nonzero sync: {command}")

            argv = [
                "run_aibrief_jp_insights.py",
                "--root",
                str(root),
                "--db",
                str(db_path),
                "--out",
                str(report_out),
                "--sync-limit",
                "1",
            ]
            with (
                patch.object(sys, "argv", argv),
                patch.object(
                    run_aibrief_jp_insights,
                    "utc_now",
                    return_value="2026-07-13T10:15:00+00:00",
                ),
                patch.object(run_aibrief_jp_insights, "run", side_effect=fake_run),
            ):
                result = run_aibrief_jp_insights.main()

            self.assertEqual(result, 1)
            self.assertEqual(len(commands), 2)
            self.assertFalse(report_out.exists())
            self.assertFalse((root / "state" / "reel_scheduler.lock").exists())

    def test_same_count_published_identity_swap_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path, report_out = self.create_project(root, with_snapshot=False)

            def fake_run(command: list[str], cwd: Path) -> int:
                if "sync-insights" in command:
                    with sqlite3.connect(db_path) as connection:
                        connection.execute(
                            "INSERT INTO insights (content_hash, channel_id, media_id, captured_at) VALUES (?, ?, ?, ?)",
                            (
                                "hash-1",
                                run_aibrief_jp_insights.CHANNEL,
                                "media-1",
                                "2026-07-13T10:15:00+00:00",
                            ),
                        )
                        connection.execute(
                            "UPDATE reels SET media_id=? WHERE content_hash=?",
                            ("media-replacement", "hash-1"),
                        )
                    return 0
                if "report" in command:
                    self.write_staged_report(command, media_ids=["media-replacement"])
                    return 0
                self.fail(f"analyzer should not run after an identity swap: {command}")

            argv = [
                "run_aibrief_jp_insights.py",
                "--root",
                str(root),
                "--db",
                str(db_path),
                "--out",
                str(report_out),
                "--sync-limit",
                "1",
            ]
            with (
                patch.object(sys, "argv", argv),
                patch.object(
                    run_aibrief_jp_insights,
                    "utc_now",
                    return_value="2026-07-13T10:15:00+00:00",
                ),
                patch.object(run_aibrief_jp_insights, "run", side_effect=fake_run),
            ):
                result = run_aibrief_jp_insights.main()

            self.assertEqual(result, 1)
            self.assertFalse(report_out.exists())

    def test_validation_only_promotes_report_and_analysis_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path, report_out = self.create_project(root, with_snapshot=False)
            commands: list[list[str]] = []

            def fake_run(command: list[str], cwd: Path) -> int:
                self.assertEqual(cwd, root.resolve())
                commands.append(command)
                if "report" in command:
                    self.write_staged_report(command)
                    return 0
                if Path(command[1]).name == "aibrief_jp_reach_analysis.py":
                    self.assertEqual(
                        Path(
                            command[command.index("--source-report-label") + 1]
                        ).resolve(),
                        report_out.with_suffix(".insights.json").resolve(),
                    )
                    Path(command[command.index("--json-out") + 1]).write_text(
                        '{"analysis": "new"}\n', encoding="utf-8"
                    )
                    Path(command[command.index("--markdown-out") + 1]).write_text(
                        "new reach markdown\n", encoding="utf-8"
                    )
                    return 0
                self.fail(f"unexpected command: {command}")

            argv = [
                "run_aibrief_jp_insights.py",
                "--root",
                str(root),
                "--db",
                str(db_path),
                "--out",
                str(report_out),
                "--no-sync",
            ]
            with (
                patch.object(sys, "argv", argv),
                patch.object(run_aibrief_jp_insights, "run", side_effect=fake_run),
            ):
                result = run_aibrief_jp_insights.main()

            self.assertEqual(result, 0)
            self.assertFalse(any("sync-insights" in command for command in commands))
            self.assertEqual(report_out.read_text(encoding="utf-8"), "new html")
            self.assertEqual(
                json.loads(report_out.with_suffix(".insights.json").read_text(encoding="utf-8"))[
                    "items"
                ][0]["media_id"],
                "media-1",
            )
            self.assertEqual(
                report_out.with_suffix(".insights.md").read_text(encoding="utf-8"),
                "new insights markdown\n",
            )
            self.assertEqual(
                json.loads(
                    (root / "out" / "aibrief_jp_reach_brief.json").read_text(encoding="utf-8")
                ),
                {"analysis": "new"},
            )
            self.assertEqual(
                (root / "out" / "aibrief_jp_reach_brief.md").read_text(encoding="utf-8"),
                "new reach markdown\n",
            )
            self.assertFalse((root / "state" / "reel_scheduler.lock").exists())

    def test_negative_sync_limit_is_rejected_before_running_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path, report_out = self.create_project(root, with_snapshot=False)
            argv = [
                "run_aibrief_jp_insights.py",
                "--root",
                str(root),
                "--db",
                str(db_path),
                "--out",
                str(report_out),
                "--sync-limit",
                "-1",
            ]
            with (
                patch.object(sys, "argv", argv),
                patch.object(run_aibrief_jp_insights, "run") as run,
                self.assertRaisesRegex(SystemExit, "--sync-limit must be zero or greater"),
            ):
                run_aibrief_jp_insights.main()

            run.assert_not_called()
            self.assertFalse((root / "state" / "reel_scheduler.lock").exists())

    def test_coverage_requires_snapshot_for_current_media_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path, _ = self.create_project(root, with_snapshot=True)
            with sqlite3.connect(db_path) as connection:
                connection.execute(
                    "UPDATE reels SET media_id=? WHERE content_hash=?",
                    ("media-replacement", "hash-1"),
                )

            self.assertEqual(run_aibrief_jp_insights.snapshot_media_count(db_path), 0)

            with sqlite3.connect(db_path) as connection:
                connection.execute(
                    "INSERT INTO insights (content_hash, channel_id, media_id, captured_at) VALUES (?, ?, ?, ?)",
                    (
                        "hash-1",
                        run_aibrief_jp_insights.CHANNEL,
                        "media-replacement",
                        "2026-07-13T10:15:00+00:00",
                    ),
                )

            self.assertEqual(run_aibrief_jp_insights.snapshot_media_count(db_path), 1)

    def test_daily_full_refresh_due_uses_oldest_current_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path, _ = self.create_project(root, with_snapshot=True)

            self.assertTrue(
                run_aibrief_jp_insights.daily_full_refresh_due(
                    db_path, as_of="2026-07-13T10:15:00+00:00"
                )
            )
            with sqlite3.connect(db_path) as connection:
                connection.execute(
                    "UPDATE insights SET captured_at=?",
                    ("2026-07-13T09:15:00+00:00",),
                )
            self.assertFalse(
                run_aibrief_jp_insights.daily_full_refresh_due(
                    db_path, as_of="2026-07-13T10:15:00+00:00"
                )
            )

    def test_daily_full_refresh_accepts_only_reviewed_data_holds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path, report_out = self.create_project(root, with_snapshot=True)
            media_ids = [f"media-{index}" for index in range(1, 11)]
            with sqlite3.connect(db_path) as connection:
                for index in range(2, 11):
                    connection.execute(
                        "INSERT INTO reels VALUES (?, ?, ?, ?, ?)",
                        (
                            f"hash-{index}",
                            run_aibrief_jp_insights.CHANNEL,
                            "published",
                            f"media-{index}",
                            "2026-07-01T00:00:00+00:00",
                        ),
                    )
                    if index < 10:
                        connection.execute(
                            "INSERT INTO insights (content_hash, channel_id, media_id, captured_at) VALUES (?, ?, ?, ?)",
                            (
                                f"hash-{index}",
                                run_aibrief_jp_insights.CHANNEL,
                                f"media-{index}",
                                "2026-07-12T10:00:00+00:00",
                            ),
                        )

            holds_path = root / "ops" / "aibrief-jp-insights-data-holds.json"
            holds_path.parent.mkdir()
            holds_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "holds": [
                            {"content_hash": "hash-10", "media_id": "media-10"}
                        ],
                    }
                ),
                encoding="utf-8",
            )

            commands: list[list[str]] = []

            def fake_run(command: list[str], cwd: Path) -> int:
                commands.append(command)
                if "sync-insights" in command:
                    self.assertNotIn("--limit", command)
                    with sqlite3.connect(db_path) as connection:
                        for index in range(1, 10):
                            connection.execute(
                                "INSERT INTO insights (content_hash, channel_id, media_id, captured_at) VALUES (?, ?, ?, ?)",
                                (
                                    f"hash-{index}",
                                    run_aibrief_jp_insights.CHANNEL,
                                    f"media-{index}",
                                    "2026-07-13T10:15:00+00:00",
                                ),
                            )
                    return 1
                if "report" in command:
                    self.write_staged_report(command, media_ids=media_ids)
                    return 0
                if Path(command[1]).name == "aibrief_jp_reach_analysis.py":
                    Path(command[command.index("--json-out") + 1]).write_text(
                        '{"analysis": "new"}\n', encoding="utf-8"
                    )
                    Path(command[command.index("--markdown-out") + 1]).write_text(
                        "new reach markdown\n", encoding="utf-8"
                    )
                    return 0
                self.fail(f"unexpected command: {command}")

            argv = [
                "run_aibrief_jp_insights.py",
                "--root",
                str(root),
                "--db",
                str(db_path),
                "--out",
                str(report_out),
                "--daily-full-refresh",
            ]
            with (
                patch.object(sys, "argv", argv),
                patch.object(
                    run_aibrief_jp_insights,
                    "utc_now",
                    return_value="2026-07-13T10:15:00+00:00",
                ),
                patch.object(run_aibrief_jp_insights, "run", side_effect=fake_run),
            ):
                result = run_aibrief_jp_insights.main()

            self.assertEqual(result, 0)
            self.assertEqual(len(commands), 3)
            self.assertTrue(report_out.is_file())
            self.assertEqual(run_aibrief_jp_insights.snapshot_media_count(db_path), 9)

    def test_unlisted_unsynced_media_is_not_an_accepted_data_hold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path, _ = self.create_project(root, with_snapshot=False)
            manifest = root / "holds.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "holds": [
                            {"content_hash": "different", "media_id": "media-1"}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            holds = run_aibrief_jp_insights.approved_data_hold_identities(
                db_path,
                [("hash-1", "media-1")],
                manifest_path=manifest,
            )
            self.assertEqual(holds, set())

    def test_scheduler_lock_is_cleaned_up_after_exception(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_dir = Path(tmp) / "state" / "reel_scheduler.lock"

            with self.assertRaisesRegex(RuntimeError, "boom"):
                with run_aibrief_jp_insights.scheduler_lock(lock_dir, wait_seconds=0):
                    self.assertTrue(lock_dir.is_dir())
                    raise RuntimeError("boom")

            self.assertFalse(lock_dir.exists())


if __name__ == "__main__":
    unittest.main()
