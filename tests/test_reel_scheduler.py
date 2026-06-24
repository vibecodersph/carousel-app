import argparse
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import reel_ledger
import reel_scheduler
from channel import load_channel


class ReelSchedulePlanTests(unittest.TestCase):
    def test_builds_ordered_aibrief_manifests_and_captions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            clips = root / "clips"
            (root / "metadata.json").write_text(
                json.dumps(
                    {
                        "title": "Inside Claude Code",
                        "uploader": "Example Channel",
                        "webpage_url": "https://www.youtube.com/watch?v=example",
                    }
                ),
                encoding="utf-8",
            )
            for index in (2, 1):
                clip = clips / f"{index:03d}-clip-{index}"
                clip.mkdir(parents=True)
                (clip / "reel.mp4").write_bytes(b"video")
                (clip / "notes.json").write_text(
                    json.dumps(
                        {
                            "index": index,
                            "one_liner": f"Claude Code clip {index}",
                            "one_liner_translated": f"Claude Codeの話 {index}",
                            "reason": "A developer used an AI agent in the terminal.",
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )

            schedule_path, schedule = reel_scheduler.create_schedule(
                clips_dir=clips,
                channel=load_channel("aibrief_jp"),
                start_at=datetime(2026, 6, 23, 9, 0, tzinfo=ZoneInfo("Asia/Tokyo")),
                interval_hours=24,
                timezone_name="Asia/Tokyo",
                media_filename="reel.mp4",
                out_dir=root / "schedule",
                created_at="2026-06-22T00:00:00+00:00",
            )

            self.assertEqual(schedule_path, (root / "schedule" / "schedule.json").resolve())
            self.assertEqual([job["id"] for job in schedule["jobs"]], ["001-clip-1", "002-clip-2"])
            self.assertEqual(schedule["jobs"][1]["scheduled_at"], "2026-06-24T09:00:00+09:00")

            manifest = json.loads(Path(schedule["jobs"][0]["manifest_path"]).read_text(encoding="utf-8"))
            self.assertEqual(manifest["channel_id"], "aibrief_jp")
            self.assertEqual(manifest["slides"][0]["type"], "video")
            self.assertTrue(manifest["slides"][0]["path"].endswith("001-clip-1/reel.mp4"))
            self.assertIn("Claude Codeの話 1", manifest["instagram_caption"])
            self.assertIn("#AIブリーフ", manifest["instagram_caption"])
            self.assertIn("#ClaudeCode", manifest["hashtags"])
            self.assertEqual(manifest["source_url"], "https://www.youtube.com/watch?v=example")
            self.assertIn("Source: https://www.youtube.com/watch?v=example", manifest["instagram_caption"])

    def test_reports_missing_channel_media_filename(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            clips = Path(temporary) / "clips"
            clips.mkdir()
            with self.assertRaisesRegex(SystemExit, "No 'reel.mp4' files"):
                reel_scheduler.discover_clips(clips, "reel.mp4")


class ReelScheduleRunTests(unittest.TestCase):
    def test_dry_run_processes_only_due_jobs_and_keeps_preview_publishable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            schedule_path = root / "schedule.json"
            jobs = []
            for name, scheduled_at in (
                ("due", "2026-06-23T09:00:00+09:00"),
                ("future", "2026-06-24T09:00:00+09:00"),
            ):
                job_dir = root / "jobs" / name
                job_dir.mkdir(parents=True)
                manifest_path = job_dir / "manifest.json"
                manifest_path.write_text("{}\n", encoding="utf-8")
                jobs.append(
                    {
                        "id": name,
                        "status": "scheduled",
                        "scheduled_at": scheduled_at,
                        "manifest_path": str(manifest_path),
                        "publish_report_path": str(job_dir / "instagram_publish.json"),
                    }
                )
            reel_scheduler.write_json(
                schedule_path,
                {
                    "version": 1,
                    "channel_id": "aibrief_jp",
                    "timezone": "Asia/Tokyo",
                    "jobs": jobs,
                },
            )

            with patch.object(reel_scheduler.subprocess, "run") as run:
                run.return_value.returncode = 0
                rc, updated = reel_scheduler.run_due_jobs(
                    schedule_path,
                    now=datetime(2026, 6, 23, 10, 0, tzinfo=ZoneInfo("Asia/Tokyo")),
                    dry_run=True,
                    include_future=False,
                    retry_failed=False,
                    limit=None,
                    upload_r2=False,
                    media_base_url="",
                    r2_bucket="",
                    r2_public_base_url="",
                )

            self.assertEqual(rc, 0)
            run.assert_called_once()
            command = run.call_args.args[0]
            self.assertIn("--single-video-media-type", command)
            self.assertIn("REELS", command)
            self.assertIn("--dry-run", command)
            self.assertEqual(updated["jobs"][0]["status"], "publish_previewed")
            self.assertEqual(updated["jobs"][1]["status"], "scheduled")

    def test_legacy_run_due_updates_ledger_with_publish_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = root / "clip" / "reel.ja.aibrief_jp.mp4"
            media.parent.mkdir(parents=True)
            media.write_bytes(b"video")
            job_dir = root / "jobs" / "due"
            job_dir.mkdir(parents=True)
            manifest_path = job_dir / "manifest.json"
            report_path = job_dir / "instagram_publish.json"
            manifest_path.write_text(json.dumps({"topic": "Ledger publish"}), encoding="utf-8")
            report_path.write_text(
                json.dumps(
                    {
                        "result": {
                            "published": {"id": "178900000"},
                            "permalink": {"permalink": "https://www.instagram.com/reel/abc/"},
                        }
                    }
                ),
                encoding="utf-8",
            )
            schedule_path = root / "schedule.json"
            reel_scheduler.write_json(
                schedule_path,
                {
                    "version": 1,
                    "channel_id": "aibrief_jp",
                    "timezone": "Asia/Tokyo",
                    "clips_dir": str(root / "clips"),
                    "jobs": [
                        {
                            "id": "due",
                            "status": "scheduled",
                            "scheduled_at": "2026-06-23T09:00:00+09:00",
                            "clip_dir": str(media.parent),
                            "media_path": str(media),
                            "manifest_path": str(manifest_path),
                            "publish_report_path": str(report_path),
                        }
                    ],
                },
            )
            db = root / "reels.db"

            with patch.object(reel_scheduler.subprocess, "run") as run:
                run.return_value.returncode = 0
                rc, updated = reel_scheduler.run_due_jobs(
                    schedule_path,
                    now=datetime(2026, 6, 23, 10, 0, tzinfo=ZoneInfo("Asia/Tokyo")),
                    dry_run=False,
                    include_future=False,
                    retry_failed=False,
                    limit=None,
                    upload_r2=False,
                    media_base_url="",
                    r2_bucket="",
                    r2_public_base_url="",
                    db_path=db,
                )

            self.assertEqual(rc, 0)
            self.assertEqual(updated["jobs"][0]["status"], "published")
            with reel_ledger.connect(db) as conn:
                row = conn.execute("SELECT * FROM reels WHERE channel_id='aibrief_jp'").fetchone()
                self.assertEqual(row["status"], reel_ledger.STATUS_PUBLISHED)
                self.assertEqual(row["media_id"], "178900000")
                self.assertEqual(row["permalink"], "https://www.instagram.com/reel/abc/")

    def test_run_due_without_schedule_publishes_due_ledger_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_path = root / "job" / "manifest.json"
            manifest_path.parent.mkdir(parents=True)
            media_path = root / "clip" / "reel.ja.aibrief_jp.mp4"
            media_path.parent.mkdir(parents=True)
            media_path.write_bytes(b"video")
            manifest_path.write_text(json.dumps({"slides": []}), encoding="utf-8")
            (manifest_path.parent / "instagram_publish.json").write_text(
                json.dumps(
                    {
                        "result": {
                            "published": {"id": "178900001"},
                            "permalink": {"permalink": "https://www.instagram.com/reel/def/"},
                        }
                    }
                ),
                encoding="utf-8",
            )
            db = root / "reels.db"
            with reel_ledger.connect(db) as conn:
                reel_ledger.upsert_imported(
                    conn,
                    content_hash="h-ledger",
                    channel_id="aibrief_jp",
                    lang="ja",
                    clip_dir=str(media_path.parent),
                    media_path=str(media_path),
                    status=reel_ledger.STATUS_SCHEDULED,
                    scheduled_at="2026-06-23T09:00:00+09:00",
                    manifest_path=str(manifest_path),
                )

            with patch.object(reel_scheduler.subprocess, "run") as run:
                run.return_value.returncode = 0
                rc = reel_scheduler.run_due_ledger(
                    db_path=db,
                    now=datetime(2026, 6, 23, 10, 0, tzinfo=ZoneInfo("Asia/Tokyo")),
                    channel_id=None,
                    scheduled_date=None,
                    dry_run=False,
                    include_future=False,
                    retry_failed=False,
                    limit=None,
                    upload_r2=False,
                    media_base_url="",
                    r2_bucket="",
                    r2_public_base_url="",
                )

            self.assertEqual(rc, 0)
            run.assert_called_once()
            with reel_ledger.connect(db) as conn:
                row = reel_ledger.get_reel(conn, "h-ledger", "aibrief_jp")
                self.assertEqual(row["status"], reel_ledger.STATUS_PUBLISHED)
                self.assertEqual(row["media_id"], "178900001")

    def test_run_due_ledger_date_filter_respects_due_time_without_all(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = root / "reels.db"
            for content_hash, day, hour in (
                ("morning", "2026-06-24", "09:00:00"),
                ("evening", "2026-06-24", "19:00:00"),
                ("tomorrow", "2026-06-25", "09:00:00"),
            ):
                job_dir = root / content_hash
                job_dir.mkdir()
                media = job_dir / "reel.ja.aibrief_jp.mp4"
                media.write_bytes(b"video")
                manifest_path = job_dir / "manifest.json"
                manifest_path.write_text(
                    json.dumps({"slides": [{"index": 1, "type": "video", "path": str(media)}]}),
                    encoding="utf-8",
                )
                (job_dir / "instagram_publish.json").write_text(
                    json.dumps({"result": {"published": {"id": content_hash}}}),
                    encoding="utf-8",
                )
                with reel_ledger.connect(db) as conn:
                    reel_ledger.upsert_imported(
                        conn,
                        content_hash=content_hash,
                        channel_id="aibrief_jp",
                        lang="ja",
                        clip_dir=str(job_dir),
                        media_path=str(media),
                        status=reel_ledger.STATUS_SCHEDULED,
                        scheduled_at=f"{day}T{hour}+09:00",
                        manifest_path=str(manifest_path),
                    )

            with patch.object(reel_scheduler.subprocess, "run") as run:
                run.return_value.returncode = 0
                rc = reel_scheduler.run_due_ledger(
                    db_path=db,
                    now=datetime(2026, 6, 24, 12, 0, tzinfo=ZoneInfo("Asia/Tokyo")),
                    channel_id=None,
                    scheduled_date=reel_scheduler.parse_date("2026-06-24"),
                    dry_run=True,
                    include_future=False,
                    retry_failed=False,
                    limit=None,
                    upload_r2=False,
                    media_base_url="",
                    r2_bucket="",
                    r2_public_base_url="",
                )

            self.assertEqual(rc, 0)
            run.assert_called_once()
            with reel_ledger.connect(db) as conn:
                self.assertEqual(
                    reel_ledger.get_reel(conn, "morning", "aibrief_jp")["status"],
                    reel_ledger.STATUS_PREVIEWED,
                )
                self.assertEqual(
                    reel_ledger.get_reel(conn, "evening", "aibrief_jp")["status"],
                    reel_ledger.STATUS_SCHEDULED,
                )
                self.assertEqual(
                    reel_ledger.get_reel(conn, "tomorrow", "aibrief_jp")["status"],
                    reel_ledger.STATUS_SCHEDULED,
                )

    def test_run_due_ledger_date_filter_with_all_processes_future_slots_that_day(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = root / "reels.db"
            for content_hash, day in (("today", "2026-06-24"), ("tomorrow", "2026-06-25")):
                job_dir = root / content_hash
                job_dir.mkdir()
                media = job_dir / "reel.ja.aibrief_jp.mp4"
                media.write_bytes(b"video")
                manifest_path = job_dir / "manifest.json"
                manifest_path.write_text(
                    json.dumps({"slides": [{"index": 1, "type": "video", "path": str(media)}]}),
                    encoding="utf-8",
                )
                with reel_ledger.connect(db) as conn:
                    reel_ledger.upsert_imported(
                        conn,
                        content_hash=content_hash,
                        channel_id="aibrief_jp",
                        lang="ja",
                        clip_dir=str(job_dir),
                        media_path=str(media),
                        status=reel_ledger.STATUS_SCHEDULED,
                        scheduled_at=f"{day}T19:00:00+09:00",
                        manifest_path=str(manifest_path),
                    )

            with patch.object(reel_scheduler.subprocess, "run") as run:
                run.return_value.returncode = 0
                rc = reel_scheduler.run_due_ledger(
                    db_path=db,
                    now=datetime(2026, 6, 24, 12, 0, tzinfo=ZoneInfo("Asia/Tokyo")),
                    channel_id=None,
                    scheduled_date=reel_scheduler.parse_date("2026-06-24"),
                    dry_run=True,
                    include_future=True,
                    retry_failed=False,
                    limit=None,
                    upload_r2=False,
                    media_base_url="",
                    r2_bucket="",
                    r2_public_base_url="",
                )

            self.assertEqual(rc, 0)
            run.assert_called_once()
            with reel_ledger.connect(db) as conn:
                self.assertEqual(
                    reel_ledger.get_reel(conn, "today", "aibrief_jp")["status"],
                    reel_ledger.STATUS_PREVIEWED,
                )
                self.assertEqual(
                    reel_ledger.get_reel(conn, "tomorrow", "aibrief_jp")["status"],
                    reel_ledger.STATUS_SCHEDULED,
                )

    def test_run_due_ledger_marks_missing_media_failed_without_publisher(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = root / "reels.db"
            manifest_path = root / "manifest.json"
            manifest_path.write_text("{}", encoding="utf-8")
            with reel_ledger.connect(db) as conn:
                reel_ledger.upsert_imported(
                    conn,
                    content_hash="missing",
                    channel_id="aibrief_jp",
                    lang="ja",
                    clip_dir=str(root),
                    media_path=str(root / "missing.mp4"),
                    status=reel_ledger.STATUS_SCHEDULED,
                    scheduled_at="2026-06-24T09:00:00+09:00",
                    manifest_path=str(manifest_path),
                )

            with patch.object(reel_scheduler.subprocess, "run") as run:
                rc = reel_scheduler.run_due_ledger(
                    db_path=db,
                    now=datetime(2026, 6, 24, 10, 0, tzinfo=ZoneInfo("Asia/Tokyo")),
                    channel_id=None,
                    scheduled_date=None,
                    dry_run=True,
                    include_future=False,
                    retry_failed=False,
                    limit=None,
                    upload_r2=False,
                    media_base_url="",
                    r2_bucket="",
                    r2_public_base_url="",
                )

            self.assertEqual(rc, 1)
            run.assert_not_called()
            with reel_ledger.connect(db) as conn:
                row = reel_ledger.get_reel(conn, "missing", "aibrief_jp")
                self.assertEqual(row["status"], reel_ledger.STATUS_FAILED)
                self.assertIn("missing media", row["last_error"])


class ReelLedgerPlanningTests(unittest.TestCase):
    def test_parse_date_only_uses_requested_timezone_and_clock(self) -> None:
        parsed = reel_scheduler.parse_datetime(
            "2026-06-24",
            "Asia/Tokyo",
            date_clock="09:00",
        )

        self.assertEqual(parsed.isoformat(), "2026-06-24T09:00:00+09:00")

    def test_scheduler_date_arg_rejects_duplicate_date_inputs(self) -> None:
        args = argparse.Namespace(date="2026-06-24", start_at="2026-06-25")

        with self.assertRaisesRegex(SystemExit, "Use either DATE or --start-at"):
            reel_scheduler.scheduler_date_arg(args)

    def test_plan_ledger_assigns_channel_slots_and_uses_localized_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clips = root / "VID123" / "clips"
            clip = clips / "001-some-clip"
            clip.mkdir(parents=True)
            (root / "VID123" / "metadata.json").write_text(
                json.dumps({"webpage_url": "https://example.com/source"}), encoding="utf-8"
            )
            (clip / "reel.ja.aibrief_jp.mp4").write_bytes(b"ja-bytes")
            (clip / "reel.en.vibecodersph.mp4").write_bytes(b"en-bytes")
            (clip / "notes.json").write_text(
                json.dumps(
                    {
                        "index": 1,
                        "one_liner": "English hook",
                        "reason": "OpenAI developer tooling",
                    }
                ),
                encoding="utf-8",
            )
            (clip / "one_liners.json").write_text(
                json.dumps({"ja": "日本語フック"}, ensure_ascii=False), encoding="utf-8"
            )
            db = root / "reels.db"
            planned = reel_scheduler.plan_ledger_rows(
                db_path=db,
                clips_dir=clips,
                out_dir=root / "manifests",
                channel_filter=None,
                start_at_text="2026-06-24",
                limit_per_channel=None,
                jitter_minutes=0,
                scan_first=True,
            )

            self.assertEqual(planned["aibrief_jp"], 1)
            self.assertEqual(planned["vibecodersph"], 1)
            with reel_ledger.connect(db) as conn:
                jp = conn.execute(
                    "SELECT * FROM reels WHERE channel_id='aibrief_jp'"
                ).fetchone()
                ph = conn.execute(
                    "SELECT * FROM reels WHERE channel_id='vibecodersph'"
                ).fetchone()
                self.assertEqual(jp["scheduled_at"], "2026-06-24T09:00:00+09:00")
                self.assertEqual(ph["scheduled_at"], "2026-06-24T12:00:00+08:00")
                manifest = json.loads(Path(jp["manifest_path"]).read_text(encoding="utf-8"))
                self.assertEqual(manifest["topic"], "日本語フック")
                self.assertIn("日本語フック", manifest["instagram_caption"])
                self.assertIn("Source: https://example.com/source", manifest["instagram_caption"])

    def test_plan_ledger_round_robins_new_rows_by_source_video_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "reels.db"
            with reel_ledger.connect(db) as conn:
                for source in ("AAA111", "BBB222"):
                    for index in (1, 2):
                        clip = root / source / "clips" / f"{index:03d}-{source.lower()}"
                        clip.mkdir(parents=True)
                        media = clip / "reel.ja.aibrief_jp.mp4"
                        media.write_bytes(f"{source}-{index}".encode("utf-8"))
                        result = reel_ledger.upsert_discovered(
                            conn,
                            content_hash=f"{source}-{index}",
                            channel_id="aibrief_jp",
                            lang="ja",
                            clip_dir=clip,
                            media_path=media,
                            source_video=source,
                            title=f"{source} title {index}",
                        )
                        self.assertEqual(result, "inserted")
                scheduled_clip = root / "AAA111" / "clips" / "000-existing"
                scheduled_clip.mkdir(parents=True)
                scheduled_media = scheduled_clip / "reel.ja.aibrief_jp.mp4"
                scheduled_media.write_bytes(b"already")
                reel_ledger.upsert_imported(
                    conn,
                    content_hash="already-scheduled",
                    channel_id="aibrief_jp",
                    lang="ja",
                    clip_dir=scheduled_clip,
                    media_path=scheduled_media,
                    source_video="AAA111",
                    title="already scheduled",
                    status=reel_ledger.STATUS_SCHEDULED,
                    scheduled_at="2026-06-23T09:00:00+09:00",
                )

            planned = reel_scheduler.plan_ledger_rows(
                db_path=db,
                clips_dir=root / "unused",
                out_dir=root / "manifests",
                channel_filter="aibrief_jp",
                start_at_text="2026-06-24",
                limit_per_channel=None,
                jitter_minutes=0,
                scan_first=False,
            )

            self.assertEqual(planned["aibrief_jp"], 4)
            with reel_ledger.connect(db) as conn:
                rows = conn.execute(
                    "SELECT source_video, title FROM reels "
                    "WHERE channel_id='aibrief_jp' AND content_hash != 'already-scheduled' "
                    "ORDER BY scheduled_at"
                ).fetchall()
                self.assertEqual(
                    [(row["source_video"], row["title"]) for row in rows],
                    [
                        ("AAA111", "AAA111 title 1"),
                        ("BBB222", "BBB222 title 1"),
                        ("AAA111", "AAA111 title 2"),
                        ("BBB222", "BBB222 title 2"),
                    ],
                )
                existing = reel_ledger.get_reel(conn, "already-scheduled", "aibrief_jp")
                self.assertEqual(existing["scheduled_at"], "2026-06-23T09:00:00+09:00")

    def test_reflow_queue_updates_only_queued_rows_to_current_slots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "reels.db"
            with reel_ledger.connect(db) as conn:
                reel_ledger.upsert_imported(
                    conn,
                    content_hash="published",
                    channel_id="aibrief_jp",
                    lang="ja",
                    clip_dir=str(root / "published"),
                    media_path=str(root / "published" / "reel.ja.aibrief_jp.mp4"),
                    source_video="AAA111",
                    title="published",
                    status=reel_ledger.STATUS_PUBLISHED,
                    scheduled_at="2026-06-24T13:00:00+09:00",
                    published_at="2026-06-24T04:00:00+00:00",
                )
                for index in range(1, 5):
                    clip = root / ("AAA111" if index % 2 else "BBB222") / "clips" / f"{index:03d}"
                    clip.mkdir(parents=True)
                    media = clip / "reel.ja.aibrief_jp.mp4"
                    media.write_bytes(f"queued-{index}".encode("utf-8"))
                    manifest = root / "manifests" / f"queued-{index}" / "manifest.json"
                    reel_scheduler.write_json(manifest, {"scheduled_at": "old"})
                    reel_ledger.upsert_imported(
                        conn,
                        content_hash=f"queued-{index}",
                        channel_id="aibrief_jp",
                        lang="ja",
                        clip_dir=clip,
                        media_path=media,
                        source_video="AAA111" if index % 2 else "BBB222",
                        title=f"queued {index}",
                        status=reel_ledger.STATUS_PREVIEWED,
                        scheduled_at=f"2026-07-{index + 1:02d}T13:00:00+09:00",
                        manifest_path=str(manifest),
                    )

            reel_scheduler.reflow_queue_rows(
                db_path=db,
                channel_filter="aibrief_jp",
                start_at_text="2026-06-25",
                jitter_minutes=0,
                settings_key="instagram_reels",
                apply=False,
            )
            with reel_ledger.connect(db) as conn:
                self.assertEqual(
                    reel_ledger.get_reel(conn, "queued-1", "aibrief_jp")["scheduled_at"],
                    "2026-07-02T13:00:00+09:00",
                )

            reel_scheduler.reflow_queue_rows(
                db_path=db,
                channel_filter="aibrief_jp",
                start_at_text="2026-06-25",
                jitter_minutes=0,
                settings_key="instagram_reels",
                apply=True,
            )
            with reel_ledger.connect(db) as conn:
                published = reel_ledger.get_reel(conn, "published", "aibrief_jp")
                self.assertEqual(published["scheduled_at"], "2026-06-24T13:00:00+09:00")
                rows = conn.execute(
                    "SELECT content_hash, scheduled_at FROM reels "
                    "WHERE channel_id='aibrief_jp' AND status=? "
                    "ORDER BY scheduled_at",
                    (reel_ledger.STATUS_PREVIEWED,),
                ).fetchall()
                self.assertEqual(
                    [(row["content_hash"], row["scheduled_at"]) for row in rows],
                    [
                        ("queued-1", "2026-06-25T09:00:00+09:00"),
                        ("queued-2", "2026-06-25T13:00:00+09:00"),
                        ("queued-3", "2026-06-25T19:00:00+09:00"),
                        ("queued-4", "2026-06-26T09:00:00+09:00"),
                    ],
                )
                manifest = reel_scheduler.read_json(
                    root / "manifests" / "queued-1" / "manifest.json"
                )
                self.assertEqual(manifest["scheduled_at"], "2026-06-25T09:00:00+09:00")

    def test_reflow_queue_can_start_today_after_posts_were_published_early(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "reels.db"
            with reel_ledger.connect(db) as conn:
                reel_ledger.upsert_imported(
                    conn,
                    content_hash="published-early",
                    channel_id="aibrief_jp",
                    lang="ja",
                    clip_dir=str(root / "published"),
                    media_path=str(root / "published" / "reel.ja.aibrief_jp.mp4"),
                    title="published early",
                    status=reel_ledger.STATUS_PUBLISHED,
                    scheduled_at="2026-06-24T09:00:00+09:00",
                    published_at="2026-06-23T08:00:00+00:00",
                )
                for index in range(1, 4):
                    clip = root / "clips" / f"{index:03d}"
                    clip.mkdir(parents=True)
                    media = clip / "reel.ja.aibrief_jp.mp4"
                    media.write_bytes(f"queued-{index}".encode("utf-8"))
                    manifest = root / "manifests" / f"queued-{index}" / "manifest.json"
                    reel_scheduler.write_json(manifest, {"scheduled_at": "old"})
                    reel_ledger.upsert_imported(
                        conn,
                        content_hash=f"queued-today-{index}",
                        channel_id="aibrief_jp",
                        lang="ja",
                        clip_dir=clip,
                        media_path=media,
                        source_video="AAA111",
                        title=f"queued today {index}",
                        status=reel_ledger.STATUS_PREVIEWED,
                        scheduled_at=f"2026-06-25T0{index}:00:00+09:00",
                        manifest_path=str(manifest),
                    )

            reel_scheduler.reflow_queue_rows(
                db_path=db,
                channel_filter="aibrief_jp",
                start_at_text="2026-06-24T09:45:00+09:00",
                jitter_minutes=0,
                settings_key="instagram_reels",
                apply=True,
            )
            with reel_ledger.connect(db) as conn:
                published = reel_ledger.get_reel(conn, "published-early", "aibrief_jp")
                self.assertEqual(published["scheduled_at"], "2026-06-24T09:00:00+09:00")
                rows = conn.execute(
                    "SELECT content_hash, scheduled_at FROM reels "
                    "WHERE channel_id='aibrief_jp' AND status=? "
                    "ORDER BY scheduled_at",
                    (reel_ledger.STATUS_PREVIEWED,),
                ).fetchall()
                self.assertEqual(
                    [(row["content_hash"], row["scheduled_at"]) for row in rows],
                    [
                        ("queued-today-1", "2026-06-24T09:45:00+09:00"),
                        ("queued-today-2", "2026-06-24T13:00:00+09:00"),
                        ("queued-today-3", "2026-06-24T19:00:00+09:00"),
                    ],
                )

    def test_alternate_sources_reorders_queued_rows_after_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "reels.db"
            rows = [
                ("queued-1", "We7BZVKbCVw", "2026-06-24T13:00:00+09:00"),
                ("queued-2", "We7BZVKbCVw", "2026-06-24T19:00:00+09:00"),
                ("queued-3", "PQU9o_5rHC4", "2026-06-25T09:00:00+09:00"),
                ("queued-4", "PQU9o_5rHC4", "2026-06-25T13:00:00+09:00"),
            ]
            with reel_ledger.connect(db) as conn:
                reel_ledger.upsert_imported(
                    conn,
                    content_hash="published-at-boundary",
                    channel_id="aibrief_jp",
                    lang="ja",
                    clip_dir=str(root / "published"),
                    media_path=str(root / "published" / "reel.ja.aibrief_jp.mp4"),
                    source_video="PQU9o_5rHC4",
                    title="published",
                    status=reel_ledger.STATUS_PUBLISHED,
                    scheduled_at="2026-06-24T09:45:00+09:00",
                    published_at="2026-06-24T00:45:00+00:00",
                )
                for content_hash, source, scheduled_at in rows:
                    clip = root / source / "clips" / content_hash
                    clip.mkdir(parents=True)
                    media = clip / "reel.ja.aibrief_jp.mp4"
                    media.write_bytes(content_hash.encode("utf-8"))
                    manifest = root / "manifests" / content_hash / "manifest.json"
                    reel_scheduler.write_json(manifest, {"scheduled_at": scheduled_at})
                    reel_ledger.upsert_imported(
                        conn,
                        content_hash=content_hash,
                        channel_id="aibrief_jp",
                        lang="ja",
                        clip_dir=clip,
                        media_path=media,
                        source_video=source,
                        title=content_hash,
                        status=reel_ledger.STATUS_PREVIEWED,
                        scheduled_at=scheduled_at,
                        manifest_path=str(manifest),
                    )

            reel_scheduler.alternate_source_queue_rows(
                db_path=db,
                after_text="2026-06-24T09:45:00+09:00",
                channel_filter=None,
                apply=True,
            )
            with reel_ledger.connect(db) as conn:
                scheduled = conn.execute(
                    "SELECT content_hash, source_video, scheduled_at FROM reels "
                    "WHERE status=? ORDER BY scheduled_at",
                    (reel_ledger.STATUS_PREVIEWED,),
                ).fetchall()
                self.assertEqual(
                    [(row["content_hash"], row["source_video"], row["scheduled_at"]) for row in scheduled],
                    [
                        ("queued-1", "We7BZVKbCVw", "2026-06-24T13:00:00+09:00"),
                        ("queued-3", "PQU9o_5rHC4", "2026-06-24T19:00:00+09:00"),
                        ("queued-2", "We7BZVKbCVw", "2026-06-25T09:00:00+09:00"),
                        ("queued-4", "PQU9o_5rHC4", "2026-06-25T13:00:00+09:00"),
                    ],
                )
                moved_manifest = reel_scheduler.read_json(
                    root / "manifests" / "queued-3" / "manifest.json"
                )
                self.assertEqual(moved_manifest["scheduled_at"], "2026-06-24T19:00:00+09:00")

    def test_sync_insights_records_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "reels.db"
            with reel_ledger.connect(db) as conn:
                reel_ledger.upsert_imported(
                    conn,
                    content_hash="h-insight",
                    channel_id="aibrief_jp",
                    lang="ja",
                    clip_dir="/clip",
                    media_path="/clip/reel.mp4",
                    status=reel_ledger.STATUS_PUBLISHED,
                    published_at="2026-06-23T00:00:00+00:00",
                    media_id="178900002",
                )
            args = argparse.Namespace(
                channel=None,
                db=db,
                limit=None,
                dry_run=False,
                metrics="views,saved,total_interactions",
                access_token="",
                graph_api_version="v23.0",
                graph_api_root="https://graph.instagram.com",
            )

            with patch("instagram_publish.load_env_file"), patch(
                "instagram_publish.resolve_instagram_access_token",
                return_value=("token", "test"),
            ), patch.object(
                reel_scheduler,
                "fetch_insights",
                return_value={
                    "data": [
                        {"name": "views", "values": [{"value": 1200}]},
                        {"name": "saved", "values": [{"value": 44}]},
                        {"name": "total_interactions", "values": [{"value": 88}]},
                    ]
                },
            ):
                rc = reel_scheduler.sync_insights_command(args)

            self.assertEqual(rc, 0)
            with reel_ledger.connect(db) as conn:
                row = conn.execute("SELECT * FROM insights WHERE media_id='178900002'").fetchone()
                self.assertEqual(row["views"], 1200)
                self.assertEqual(row["saved"], 44)

    def test_cleanup_missing_deletes_failed_missing_media_only_when_applied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "reels.db"
            media = root / "exists.mp4"
            media.write_bytes(b"video")
            with reel_ledger.connect(db) as conn:
                reel_ledger.upsert_imported(
                    conn,
                    content_hash="missing",
                    channel_id="aibrief_jp",
                    lang="ja",
                    clip_dir=str(root),
                    media_path=str(root / "missing.mp4"),
                    status=reel_ledger.STATUS_FAILED,
                )
                reel_ledger.upsert_imported(
                    conn,
                    content_hash="exists",
                    channel_id="aibrief_jp",
                    lang="ja",
                    clip_dir=str(root),
                    media_path=str(media),
                    status=reel_ledger.STATUS_FAILED,
                )
            dry_args = argparse.Namespace(
                channel=None,
                db=db,
                all_statuses=False,
                apply=False,
            )
            apply_args = argparse.Namespace(
                channel=None,
                db=db,
                all_statuses=False,
                apply=True,
            )

            self.assertEqual(reel_scheduler.cleanup_missing_command(dry_args), 0)
            with reel_ledger.connect(db) as conn:
                self.assertIsNotNone(reel_ledger.get_reel(conn, "missing", "aibrief_jp"))
            self.assertEqual(reel_scheduler.cleanup_missing_command(apply_args), 0)
            with reel_ledger.connect(db) as conn:
                self.assertIsNone(reel_ledger.get_reel(conn, "missing", "aibrief_jp"))
                self.assertIsNotNone(reel_ledger.get_reel(conn, "exists", "aibrief_jp"))


class ReelChannelRoutingTests(unittest.TestCase):
    def test_parse_channel_media(self) -> None:
        self.assertEqual(
            reel_scheduler.parse_channel_media("reel.ja.aibrief_jp.mp4"), ("ja", "aibrief_jp")
        )
        self.assertEqual(
            reel_scheduler.parse_channel_media("reel.en.vibecodersph.mp4"), ("en", "vibecodersph")
        )
        self.assertIsNone(reel_scheduler.parse_channel_media("reel.mp4"))
        self.assertIsNone(reel_scheduler.parse_channel_media("poster.png"))

    def test_routed_title_prefers_localized_then_falls_back(self) -> None:
        notes = {"one_liner": "English hook"}
        self.assertEqual(reel_scheduler.routed_title("ja", notes, {"ja": "日本語フック"}), "日本語フック")
        self.assertEqual(reel_scheduler.routed_title("en", notes, {"ja": "日本語フック"}), "English hook")
        self.assertEqual(
            reel_scheduler.routed_title("ja", {"one_liner": "EN", "one_liner_translated": "翻訳"}, {}),
            "翻訳",
        )
        self.assertEqual(reel_scheduler.routed_title("ja", {"one_liner": "EN"}, {}), "EN")

    def test_scan_fans_out_channels_into_ledger_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clips = root / "VID123" / "clips"
            clip = clips / "001-some-clip"
            clip.mkdir(parents=True)
            (clip / "reel.en.vibecodersph.mp4").write_bytes(b"en-bytes")
            (clip / "reel.ja.aibrief_jp.mp4").write_bytes(b"ja-bytes")
            (clip / "notes.json").write_text(
                json.dumps({"index": 1, "one_liner": "English hook"}), encoding="utf-8"
            )
            (clip / "one_liners.json").write_text(
                json.dumps({"ja": "日本語フック"}, ensure_ascii=False), encoding="utf-8"
            )

            self.assertEqual(len(reel_scheduler.discover_channel_clips(clips)), 2)

            db = root / "reels.db"
            args = argparse.Namespace(clips_dir=clips, db=db)
            reel_scheduler.scan_command(args)

            with reel_ledger.connect(db) as conn:
                counts = reel_ledger.status_counts(conn)
                self.assertEqual(counts["vibecodersph"][reel_ledger.STATUS_NEW], 1)
                self.assertEqual(counts["aibrief_jp"][reel_ledger.STATUS_NEW], 1)
                jp = conn.execute(
                    "SELECT title, source_video, lang FROM reels WHERE channel_id='aibrief_jp'"
                ).fetchone()
                en = conn.execute(
                    "SELECT title FROM reels WHERE channel_id='vibecodersph'"
                ).fetchone()
                self.assertEqual(jp["title"], "日本語フック")
                self.assertEqual(jp["source_video"], "VID123")
                self.assertEqual(jp["lang"], "ja")
                self.assertEqual(en["title"], "English hook")

            # Re-scanning the same folder must not create duplicate rows.
            reel_scheduler.scan_command(args)
            with reel_ledger.connect(db) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM reels").fetchone()["n"], 2)


if __name__ == "__main__":
    unittest.main()
