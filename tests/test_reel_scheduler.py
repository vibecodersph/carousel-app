import argparse
import http.client
import io
import json
import threading
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
            self.assertIn("気になったら保存して、あとで見返してください", manifest["instagram_caption"])
            self.assertNotIn("毎日のAI開発ニュースはフォローでチェック", manifest["instagram_caption"])
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


class ReelCaptionRefreshTests(unittest.TestCase):
    def test_refresh_queued_captions_is_dry_run_until_apply(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = root / "reels.db"
            clip = root / "SRC123" / "clips" / "001"
            clip.mkdir(parents=True)
            media = clip / "reel.ja.aibrief_jp.mp4"
            media.write_bytes(b"video")
            (clip / "notes.json").write_text(
                json.dumps(
                    {
                        "one_liner": "Claude Code changed how builders work",
                        "one_liner_translated": "Claude Codeで開発の働き方が変わった",
                        "reason": "A developer talks about AI agents in software work.",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (root / "SRC123" / "metadata.json").write_text(
                json.dumps({"webpage_url": "https://www.youtube.com/watch?v=SRC123"}),
                encoding="utf-8",
            )
            manifest_path = root / "manifests" / "queued" / "manifest.json"
            old_caption = "古いキャプション\n\n気になったら保存して、あとで見返してください。"
            reel_scheduler.write_json(
                manifest_path,
                {
                    "instagram_caption": old_caption,
                    "hashtags": ["#old"],
                    "topic": "old",
                    "description": "old",
                    "source_url": "https://www.youtube.com/watch?v=SRC123",
                    "slides": [{"source_url": "https://www.youtube.com/watch?v=SRC123"}],
                },
            )
            caption_path = manifest_path.parent / "caption.txt"
            caption_path.write_text(old_caption + "\n", encoding="utf-8")
            with reel_ledger.connect(db) as conn:
                reel_ledger.upsert_imported(
                    conn,
                    content_hash="queued-caption",
                    channel_id="aibrief_jp",
                    lang="ja",
                    clip_dir=clip,
                    media_path=media,
                    source_video="SRC123",
                    title="Claude Codeで開発の働き方が変わった",
                    status=reel_ledger.STATUS_SCHEDULED,
                    scheduled_at="2026-07-04T09:00:00+09:00",
                    manifest_path=str(manifest_path),
                )
                conn.execute(
                    "UPDATE reels SET caption=? WHERE content_hash=? AND channel_id=?",
                    (old_caption, "queued-caption", "aibrief_jp"),
                )

            preview_path = root / "caption_refresh_preview.md"
            with patch.object(reel_scheduler.subprocess, "run") as run:
                changed = reel_scheduler.refresh_queued_captions(
                    db_path=db,
                    channel_filter="aibrief_jp",
                    settings_key="instagram_reels",
                    apply=False,
                    preview_out=preview_path,
                )
                run.assert_not_called()
            self.assertEqual(changed, 1)
            preview = preview_path.read_text(encoding="utf-8")
            self.assertIn("# Queued Caption Refresh Preview", preview)
            self.assertIn("Claude Codeで開発の働き方が変わった", preview)
            self.assertIn("気になったら保存して、あとで見返してください", preview)
            self.assertIn("Publisher subprocess invoked: False", preview)
            self.assertIn("Fingerprint before:", preview)
            with reel_ledger.connect(db) as conn:
                row = reel_ledger.get_reel(conn, "queued-caption", "aibrief_jp")
                self.assertEqual(row["caption"], old_caption)
            self.assertEqual(reel_scheduler.read_json(manifest_path)["instagram_caption"], old_caption)
            self.assertEqual(caption_path.read_text(encoding="utf-8"), old_caption + "\n")

            with patch.object(reel_scheduler.subprocess, "run") as run:
                changed = reel_scheduler.refresh_queued_captions(
                    db_path=db,
                    channel_filter="aibrief_jp",
                    settings_key="instagram_reels",
                    apply=True,
                )
                run.assert_not_called()
            self.assertEqual(changed, 1)
            with reel_ledger.connect(db) as conn:
                row = reel_ledger.get_reel(conn, "queued-caption", "aibrief_jp")
                self.assertIn("気になったら保存して、あとで見返してください", row["caption"])
                self.assertNotIn("毎日のAI開発ニュースはフォローでチェック", row["caption"])
            manifest = reel_scheduler.read_json(manifest_path)
            self.assertIn("気になったら保存して、あとで見返してください", manifest["instagram_caption"])
            self.assertIn("#AIブリーフ", manifest["hashtags"])
            self.assertEqual(caption_path.read_text(encoding="utf-8"), manifest["instagram_caption"] + "\n")


class ReelQueueGrowthAuditTests(unittest.TestCase):
    def test_queue_growth_audit_reports_source_and_cta_risks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = root / "reels.db"
            with reel_ledger.connect(db) as conn:
                for index in range(1, 5):
                    source = "SRC_A" if index <= 3 else "SRC_B"
                    reel_ledger.upsert_imported(
                        conn,
                        content_hash=f"queued-audit-{index}",
                        channel_id="aibrief_jp",
                        lang="ja",
                        clip_dir=root / source / "clips" / f"{index:03d}",
                        media_path=root / source / "clips" / f"{index:03d}" / "reel.ja.aibrief_jp.mp4",
                        source_video=source,
                        title=f"Claude audit title {index}",
                        status=reel_ledger.STATUS_SCHEDULED,
                        scheduled_at=f"2026-07-0{index}T09:00:00+09:00",
                    )
                    conn.execute(
                        "UPDATE reels SET caption=? WHERE content_hash=? AND channel_id=?",
                        (
                            "AI開発の現場で何が起きているのか、短いクリップで紹介します。\n\n"
                            "気になったら保存して、あとで見返してください。",
                            f"queued-audit-{index}",
                            "aibrief_jp",
                        ),
                    )

            audit = reel_scheduler.build_queue_growth_audit(
                db_path=db,
                channel_filter="aibrief_jp",
                platform="instagram",
                limit=None,
            )
            self.assertEqual(audit["queued_count"], 4)
            self.assertEqual(audit["cta"]["follow_cta_count"], 0)
            self.assertEqual(audit["cta"]["old_save_cta_count"], 4)
            self.assertEqual(audit["source_counts"][0]["source_video"], "SRC_A")
            self.assertEqual(audit["source_counts"][0]["count"], 3)
            self.assertTrue(any("Top source SRC_A" in warning for warning in audit["warnings"]))
            markdown = reel_scheduler.render_queue_growth_audit_markdown(audit)
            self.assertIn("# Queued Reel Growth Audit", markdown)
            self.assertNotIn("No queued captions contain a follow CTA", markdown)
            self.assertIn("| SRC_A | 3 | 75.0%", markdown)


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
    def test_discover_output_clip_dirs_finds_sorted_youtube_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "B-video" / "clips").mkdir(parents=True)
            (root / "A-video" / "clips").mkdir(parents=True)
            (root / "no-clips").mkdir()

            clips_dirs = reel_scheduler.discover_output_clip_dirs(root)

            self.assertEqual([path.parent.name for path in clips_dirs], ["A-video", "B-video"])

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
                self.assertEqual(jp["trial_reel"], 0)
                self.assertIsNone(jp["trial_graduation_strategy"])
                self.assertEqual(ph["scheduled_at"], "2026-06-24T12:00:00+08:00")
                manifest = json.loads(Path(jp["manifest_path"]).read_text(encoding="utf-8"))
                self.assertEqual(manifest["topic"], "日本語フック")
                self.assertIn("日本語フック", manifest["instagram_caption"])
                self.assertIn("Source: https://example.com/source", manifest["instagram_caption"])
                self.assertNotIn("instagram_trial_reel", manifest)

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
                    "SELECT source_video, title, trial_reel FROM reels "
                    "WHERE channel_id='aibrief_jp' AND content_hash != 'already-scheduled' "
                    "ORDER BY scheduled_at, trial_reel"
                ).fetchall()
                self.assertEqual(
                    [(row["source_video"], row["title"], row["trial_reel"]) for row in rows],
                    [
                        ("AAA111", "AAA111 title 1", 0),
                        ("BBB222", "BBB222 title 1", 0),
                        ("AAA111", "AAA111 title 2", 0),
                        ("BBB222", "BBB222 title 2", 0),
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
                    "SELECT content_hash, scheduled_at, trial_reel FROM reels "
                    "WHERE channel_id='aibrief_jp' AND status=? "
                    "ORDER BY scheduled_at, trial_reel, content_hash",
                    (reel_ledger.STATUS_PREVIEWED,),
                ).fetchall()
                self.assertEqual(
                    [(row["content_hash"], row["scheduled_at"], row["trial_reel"]) for row in rows],
                    [
                        ("queued-1", "2026-06-25T09:00:00+09:00", 0),
                        ("queued-2", "2026-06-25T13:00:00+09:00", 0),
                        ("queued-3", "2026-06-25T18:00:00+09:00", 0),
                        ("queued-4", "2026-06-25T21:00:00+09:00", 0),
                    ],
                )
                manifest = reel_scheduler.read_json(
                    root / "manifests" / "queued-1" / "manifest.json"
                )
                self.assertEqual(manifest["scheduled_at"], "2026-06-25T09:00:00+09:00")
                self.assertNotIn("instagram_trial_reel", manifest)
                evening_manifest = reel_scheduler.read_json(
                    root / "manifests" / "queued-3" / "manifest.json"
                )
                self.assertNotIn("instagram_trial_reel", evening_manifest)

    def test_reflow_queue_preserves_queued_rows_before_start_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "reels.db"
            with reel_ledger.connect(db) as conn:
                for content_hash, scheduled_at in [
                    ("before-boundary", "2026-06-24T19:00:00+09:00"),
                    ("after-boundary-1", "2026-06-25T01:00:00+09:00"),
                    ("after-boundary-2", "2026-06-25T02:00:00+09:00"),
                ]:
                    clip = root / "clips" / content_hash
                    clip.mkdir(parents=True)
                    media = clip / "reel.ja.aibrief_jp.mp4"
                    media.write_bytes(content_hash.encode("utf-8"))
                    reel_ledger.upsert_imported(
                        conn,
                        content_hash=content_hash,
                        channel_id="aibrief_jp",
                        lang="ja",
                        clip_dir=clip,
                        media_path=media,
                        source_video="AAA111",
                        title=content_hash,
                        status=reel_ledger.STATUS_PREVIEWED,
                        scheduled_at=scheduled_at,
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
                rows = conn.execute(
                    "SELECT content_hash, scheduled_at FROM reels "
                    "WHERE channel_id='aibrief_jp' AND status=? ORDER BY scheduled_at",
                    (reel_ledger.STATUS_PREVIEWED,),
                ).fetchall()
                self.assertEqual(
                    [(row["content_hash"], row["scheduled_at"]) for row in rows],
                    [
                        ("before-boundary", "2026-06-24T19:00:00+09:00"),
                        ("after-boundary-1", "2026-06-25T09:00:00+09:00"),
                        ("after-boundary-2", "2026-06-25T13:00:00+09:00"),
                    ],
                )

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
                    "SELECT content_hash, scheduled_at, trial_reel FROM reels "
                    "WHERE channel_id='aibrief_jp' AND status=? "
                    "ORDER BY scheduled_at, trial_reel, content_hash",
                    (reel_ledger.STATUS_PREVIEWED,),
                ).fetchall()
                self.assertEqual(
                    [(row["content_hash"], row["scheduled_at"], row["trial_reel"]) for row in rows],
                    [
                        ("queued-today-1", "2026-06-24T09:45:00+09:00", 0),
                        ("queued-today-2", "2026-06-24T13:00:00+09:00", 0),
                        ("queued-today-3", "2026-06-24T18:00:00+09:00", 0),
                    ],
                )

    def test_reflow_queue_can_skip_ad_hoc_slot_for_wrapper_reshuffle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "reels.db"
            with reel_ledger.connect(db) as conn:
                for index in range(1, 3):
                    clip = root / "clips" / f"{index:03d}"
                    clip.mkdir(parents=True)
                    media = clip / "reel.ja.aibrief_jp.mp4"
                    media.write_bytes(f"queued-{index}".encode("utf-8"))
                    reel_ledger.upsert_imported(
                        conn,
                        content_hash=f"queued-no-ad-hoc-{index}",
                        channel_id="aibrief_jp",
                        lang="ja",
                        clip_dir=clip,
                        media_path=media,
                        source_video="AAA111",
                        title=f"queued {index}",
                        status=reel_ledger.STATUS_PREVIEWED,
                        scheduled_at=f"2026-06-26T0{index}:00:00+09:00",
                    )

            reel_scheduler.reflow_queue_rows(
                db_path=db,
                channel_filter="aibrief_jp",
                start_at_text="2026-06-25T13:24:30+09:00",
                jitter_minutes=0,
                settings_key="instagram_reels",
                apply=True,
                include_start_at_slot=False,
            )
            with reel_ledger.connect(db) as conn:
                rows = conn.execute(
                    "SELECT content_hash, scheduled_at, trial_reel FROM reels "
                    "WHERE channel_id='aibrief_jp' AND status=? "
                    "ORDER BY scheduled_at, trial_reel, content_hash",
                    (reel_ledger.STATUS_PREVIEWED,),
                ).fetchall()
                self.assertEqual(
                    [(row["content_hash"], row["scheduled_at"], row["trial_reel"]) for row in rows],
                    [
                        ("queued-no-ad-hoc-1", "2026-06-25T18:00:00+09:00", 0),
                        ("queued-no-ad-hoc-2", "2026-06-25T21:00:00+09:00", 0),
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

            preview_path = root / "alternate_preview.md"
            changed = reel_scheduler.alternate_source_queue_rows(
                db_path=db,
                after_text="2026-06-24T09:45:00+09:00",
                channel_filter=None,
                apply=False,
                preview_out=preview_path,
            )
            self.assertEqual(changed, 4)
            preview = preview_path.read_text(encoding="utf-8")
            self.assertIn("# Alternate Source Preview", preview)
            self.assertIn("Rows that would move: 2", preview)
            with reel_ledger.connect(db) as conn:
                dry_run_scheduled = conn.execute(
                    "SELECT content_hash, source_video, scheduled_at FROM reels "
                    "WHERE status=? ORDER BY scheduled_at",
                    (reel_ledger.STATUS_PREVIEWED,),
                ).fetchall()
                self.assertEqual(
                    [(row["content_hash"], row["source_video"], row["scheduled_at"]) for row in dry_run_scheduled],
                    [
                        ("queued-1", "We7BZVKbCVw", "2026-06-24T13:00:00+09:00"),
                        ("queued-2", "We7BZVKbCVw", "2026-06-24T19:00:00+09:00"),
                        ("queued-3", "PQU9o_5rHC4", "2026-06-25T09:00:00+09:00"),
                        ("queued-4", "PQU9o_5rHC4", "2026-06-25T13:00:00+09:00"),
                    ],
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

    def test_unschedule_queued_reel_marks_skipped_and_refuses_published(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "reels.db"
            manifest = root / "manifest.json"
            reel_scheduler.write_json(manifest, {"scheduled_at": "2026-06-24T13:00:00+09:00"})
            with reel_ledger.connect(db) as conn:
                reel_ledger.upsert_imported(
                    conn,
                    content_hash="queued",
                    channel_id="aibrief_jp",
                    lang="ja",
                    clip_dir=str(root / "queued"),
                    media_path=str(root / "queued" / "reel.ja.aibrief_jp.mp4"),
                    status=reel_ledger.STATUS_PREVIEWED,
                    scheduled_at="2026-06-24T13:00:00+09:00",
                    manifest_path=str(manifest),
                )
                reel_ledger.upsert_imported(
                    conn,
                    content_hash="published",
                    channel_id="aibrief_jp",
                    lang="ja",
                    clip_dir=str(root / "published"),
                    media_path=str(root / "published" / "reel.ja.aibrief_jp.mp4"),
                    status=reel_ledger.STATUS_PUBLISHED,
                    scheduled_at="2026-06-24T09:45:00+09:00",
                    published_at="2026-06-24T00:45:00+00:00",
                )

            ok, message = reel_scheduler.unschedule_queued_reel(
                db_path=db,
                content_hash="queued",
                channel_id="aibrief_jp",
            )
            self.assertTrue(ok, message)
            with reel_ledger.connect(db) as conn:
                row = reel_ledger.get_reel(conn, "queued", "aibrief_jp")
                self.assertEqual(row["status"], reel_ledger.STATUS_SKIPPED)
                self.assertIsNone(row["scheduled_at"])
                self.assertEqual(reel_ledger.upcoming(conn, "aibrief_jp"), [])
            updated_manifest = reel_scheduler.read_json(manifest)
            self.assertNotIn("scheduled_at", updated_manifest)
            self.assertEqual(updated_manifest["schedule_status"], reel_ledger.STATUS_SKIPPED)

            ok, message = reel_scheduler.unschedule_queued_reel(
                db_path=db,
                content_hash="published",
                channel_id="aibrief_jp",
            )
            self.assertFalse(ok)
            self.assertIn("published", message)
            with reel_ledger.connect(db) as conn:
                row = reel_ledger.get_reel(conn, "published", "aibrief_jp")
                self.assertEqual(row["status"], reel_ledger.STATUS_PUBLISHED)
                self.assertEqual(row["scheduled_at"], "2026-06-24T09:45:00+09:00")

    def test_queue_ui_renders_reshuffle_action(self) -> None:
        html = reel_scheduler.render_queue_ui_html(
            rows=[],
            counts={},
            db_path=Path("/tmp/reels.db"),
        )

        self.assertIn('action="/reshuffle"', html)
        self.assertIn("Reshuffle Queue", html)

    def test_queue_append_start_ignores_old_published_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "reels.db"
            with reel_ledger.connect(db) as conn:
                reel_ledger.upsert_imported(
                    conn,
                    content_hash="old-published",
                    channel_id="aibrief_jp",
                    lang="ja",
                    clip_dir=str(root / "old"),
                    media_path=str(root / "old" / "reel.ja.aibrief_jp.mp4"),
                    status=reel_ledger.STATUS_PUBLISHED,
                    scheduled_at="2026-07-01T09:00:00+09:00",
                    published_at="2026-07-01T00:00:00+00:00",
                )

            start = reel_scheduler.queue_append_start_text(
                db_path=db,
                channel_filter="aibrief_jp",
                now=datetime(2026, 7, 5, 12, 0, tzinfo=ZoneInfo("Asia/Tokyo")),
            )

            self.assertEqual(start, "2026-07-05T12:00:00+09:00")

    def test_queue_append_start_uses_latest_future_queued_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "reels.db"
            with reel_ledger.connect(db) as conn:
                for content_hash, scheduled_at in (
                    ("old-published", "2026-07-01T09:00:00+09:00"),
                    ("future-queued", "2026-07-08T22:00:00+09:00"),
                ):
                    reel_ledger.upsert_imported(
                        conn,
                        content_hash=content_hash,
                        channel_id="aibrief_jp",
                        lang="ja",
                        clip_dir=str(root / content_hash),
                        media_path=str(root / content_hash / "reel.ja.aibrief_jp.mp4"),
                        status=(
                            reel_ledger.STATUS_PUBLISHED
                            if content_hash == "old-published"
                            else reel_ledger.STATUS_SCHEDULED
                        ),
                        scheduled_at=scheduled_at,
                        published_at=(
                            "2026-07-01T00:00:00+00:00"
                            if content_hash == "old-published"
                            else None
                        ),
                    )

            start = reel_scheduler.queue_append_start_text(
                db_path=db,
                channel_filter="aibrief_jp",
                now=datetime(2026, 7, 5, 12, 0, tzinfo=ZoneInfo("Asia/Tokyo")),
            )

            self.assertEqual(start, "2026-07-08T22:00:00+09:00")

    def test_scan_and_reshuffle_outputs_scans_outputs_and_plans_new_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outputs = root / "outputs"
            clip = outputs / "VID123" / "clips" / "001-clip"
            clip.mkdir(parents=True)
            (outputs / "VID123" / "metadata.json").write_text(
                json.dumps({"webpage_url": "https://example.com/source"}), encoding="utf-8"
            )
            (clip / "reel.ja.aibrief_jp.mp4").write_bytes(b"ja-video")
            (clip / "notes.json").write_text(
                json.dumps({"index": 1, "one_liner": "English hook"}), encoding="utf-8"
            )
            (clip / "one_liners.json").write_text(
                json.dumps({"ja": "日本語フック"}, ensure_ascii=False), encoding="utf-8"
            )
            db = root / "reels.db"

            result = reel_scheduler.scan_and_reshuffle_outputs(
                db_path=db,
                outputs_root=outputs,
                out_dir=root / "manifests",
                channel_filter="aibrief_jp",
                platform="instagram",
                settings_key="instagram_reels",
                limit_per_channel=None,
                jitter_minutes=0,
                start_at_text="2026-06-24T09:45:00+09:00",
            )

            self.assertEqual(result["clips_dirs"], 1)
            self.assertEqual(result["planned"], {"aibrief_jp": 1})
            self.assertEqual(result["reflowed"], {"aibrief_jp": 1})
            with reel_ledger.connect(db) as conn:
                row = conn.execute("SELECT * FROM reels WHERE channel_id='aibrief_jp'").fetchone()
                self.assertEqual(row["status"], reel_ledger.STATUS_SCHEDULED)
                self.assertEqual(row["source_video"], "VID123")
                self.assertEqual(row["scheduled_at"], "2026-06-24T13:00:00+09:00")
                self.assertTrue(Path(row["manifest_path"]).is_file())

    def test_scan_and_plan_outputs_is_noop_without_output_clip_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outputs = root / "outputs"
            outputs.mkdir()
            db = root / "reels.db"
            with reel_ledger.connect(db) as conn:
                clip = root / "already-discovered" / "clips" / "001"
                clip.mkdir(parents=True)
                media = clip / "reel.ja.aibrief_jp.mp4"
                media.write_bytes(b"video")
                reel_ledger.upsert_discovered(
                    conn,
                    content_hash="new-row",
                    channel_id="aibrief_jp",
                    lang="ja",
                    clip_dir=clip,
                    media_path=media,
                    source_video="already-discovered",
                    title="new row",
                )

            result = reel_scheduler.scan_and_plan_outputs(
                db_path=db,
                outputs_root=outputs,
                out_dir=root / "manifests",
                channel_filter="aibrief_jp",
                platform="instagram",
                settings_key="instagram_reels",
                limit_per_channel=None,
                jitter_minutes=0,
                start_at_text="2026-06-24",
            )

            self.assertEqual(result["clips_dirs"], 0)
            self.assertEqual(result["planned"], {})
            with reel_ledger.connect(db) as conn:
                row = reel_ledger.get_reel(conn, "new-row", "aibrief_jp")
                self.assertEqual(row["status"], reel_ledger.STATUS_NEW)

    def test_scan_and_reshuffle_outputs_does_not_append_after_old_published_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outputs = root / "outputs"
            clip = outputs / "VID123" / "clips" / "001-clip"
            clip.mkdir(parents=True)
            (clip / "reel.ja.aibrief_jp.mp4").write_bytes(b"ja-video")
            (clip / "notes.json").write_text(
                json.dumps({"index": 1, "one_liner": "English hook"}), encoding="utf-8"
            )
            db = root / "reels.db"
            with reel_ledger.connect(db) as conn:
                reel_ledger.upsert_imported(
                    conn,
                    content_hash="old-published",
                    channel_id="aibrief_jp",
                    lang="ja",
                    clip_dir=str(root / "old"),
                    media_path=str(root / "old" / "reel.ja.aibrief_jp.mp4"),
                    status=reel_ledger.STATUS_PUBLISHED,
                    scheduled_at="2026-07-01T09:00:00+09:00",
                    published_at="2026-07-01T00:00:00+00:00",
                )

            result = reel_scheduler.scan_and_reshuffle_outputs(
                db_path=db,
                outputs_root=outputs,
                out_dir=root / "manifests",
                channel_filter="aibrief_jp",
                platform="instagram",
                settings_key="instagram_reels",
                limit_per_channel=None,
                jitter_minutes=0,
                now=datetime(2026, 7, 5, 12, 0, tzinfo=ZoneInfo("Asia/Tokyo")),
            )

            self.assertEqual(result["append_start"], "2026-07-05T12:00:00+09:00")
            with reel_ledger.connect(db) as conn:
                row = conn.execute(
                    "SELECT * FROM reels WHERE channel_id='aibrief_jp' AND status=?",
                    (reel_ledger.STATUS_SCHEDULED,),
                ).fetchone()
                self.assertEqual(row["scheduled_at"], "2026-07-05T13:00:00+09:00")

    def test_scan_and_reshuffle_outputs_can_skip_reflow_when_no_new_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outputs = root / "outputs"
            clip = outputs / "VID123" / "clips" / "001-clip"
            clip.mkdir(parents=True)
            media = clip / "reel.ja.aibrief_jp.mp4"
            media.write_bytes(b"ja-video")
            (clip / "notes.json").write_text(
                json.dumps({"index": 1, "one_liner": "English hook"}), encoding="utf-8"
            )
            db = root / "reels.db"
            with reel_ledger.connect(db) as conn:
                content_hash = reel_ledger.hash_file(media)
                reel_ledger.upsert_discovered(
                    conn,
                    content_hash=content_hash,
                    channel_id="aibrief_jp",
                    lang="ja",
                    clip_dir=clip,
                    media_path=media,
                    source_video="VID123",
                    title="English hook",
                )
                reel_ledger.set_status(
                    conn,
                    content_hash,
                    "aibrief_jp",
                    reel_ledger.STATUS_SCHEDULED,
                    scheduled_at="2026-07-10T22:00:00+09:00",
                    manifest_path=str(root / "manifest.json"),
                )

            result = reel_scheduler.scan_and_reshuffle_outputs(
                db_path=db,
                outputs_root=outputs,
                out_dir=root / "manifests",
                channel_filter="aibrief_jp",
                platform="instagram",
                settings_key="instagram_reels",
                limit_per_channel=None,
                jitter_minutes=0,
                only_if_planned=True,
                now=datetime(2026, 7, 5, 12, 0, tzinfo=ZoneInfo("Asia/Tokyo")),
            )

            self.assertEqual(result["planned"], {})
            self.assertEqual(result["reflowed"], {})
            with reel_ledger.connect(db) as conn:
                row = conn.execute("SELECT * FROM reels WHERE channel_id='aibrief_jp'").fetchone()
                self.assertEqual(row["scheduled_at"], "2026-07-10T22:00:00+09:00")

    def test_queue_ui_reshuffle_endpoint_invokes_output_refill_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "reels.db"
            with reel_ledger.connect(db):
                pass
            handler = reel_scheduler.make_queue_ui_handler(
                db_path=db,
                channel_filter="aibrief_jp",
                limit=20,
                settings_key="instagram_reels",
                platform="instagram",
                report_out=root / "reel_report.html",
                outputs_root=root / "outputs",
                out_dir=root / "manifests",
            )
            server = reel_scheduler.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                fake_result = {
                    "clips_dirs": 2,
                    "planned": {"aibrief_jp": 3},
                    "reflowed": {"aibrief_jp": 5},
                    "alternated": 5,
                    "start_at": "2026-06-24T09:45:00+09:00",
                }
                with patch.object(
                    reel_scheduler,
                    "scan_and_reshuffle_outputs",
                    return_value=fake_result,
                ) as reshuffle, patch.object(reel_scheduler, "report_command", return_value=0):
                    conn = http.client.HTTPConnection(host, port, timeout=5)
                    conn.request("POST", "/reshuffle", body="")
                    response = conn.getresponse()
                    response.read()
                    conn.close()

                self.assertEqual(response.status, 303)
                location = response.getheader("Location") or ""
                self.assertIn("planned+3+new+rows", location)
                reshuffle.assert_called_once()
                kwargs = reshuffle.call_args.kwargs
                self.assertEqual(kwargs["channel_filter"], "aibrief_jp")
                self.assertEqual(kwargs["outputs_root"], root / "outputs")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_media_stream_treats_broken_pipe_as_client_disconnect(self) -> None:
        class BrokenWriter:
            def write(self, chunk: bytes) -> None:
                raise BrokenPipeError()

        ok = reel_scheduler.stream_http_body(BrokenWriter(), io.BytesIO(b"video"), 5)

        self.assertFalse(ok)

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
                metrics="views,total_views,likes,total_likes,comments,total_comments,saved,total_interactions",
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
                        {"name": "total_views", "values": [{"value": 2200}]},
                        {"name": "likes", "values": [{"value": 12}]},
                        {"name": "total_likes", "values": [{"value": 15}]},
                        {"name": "comments", "values": [{"value": 2}]},
                        {"name": "total_comments", "values": [{"value": 3}]},
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
                self.assertEqual(row["total_views"], 2200)
                self.assertEqual(row["likes"], 12)
                self.assertEqual(row["total_likes"], 15)
                self.assertEqual(row["comments"], 2)
                self.assertEqual(row["total_comments"], 3)
                self.assertEqual(row["saved"], 44)

    def test_fetch_insights_retries_transient_graph_dns_failure(self) -> None:
        with patch(
            "instagram_publish.graph_request",
            side_effect=[
                SystemExit(
                    "Instagram Graph API request failed: "
                    "<urlopen error [Errno 8] nodename nor servname provided, or not known>"
                ),
                {"data": [{"name": "views", "values": [{"value": 7}]}]},
            ],
        ) as request, patch.object(reel_scheduler.time_module, "sleep") as sleep:
            payload = reel_scheduler.fetch_insights(
                media_id="178900003",
                metrics=["views"],
                access_token="token",
                graph_version="v25.0",
                graph_api_root="https://graph.facebook.com",
            )

        self.assertEqual(payload["data"][0]["name"], "views")
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(1.0)

    def test_optional_metric_failure_preserves_core_snapshot(self) -> None:
        self.assertNotIn("follows", reel_scheduler.INSTAGRAM_INSIGHT_REQUEST_METRIC_KEYS)
        self.assertNotIn(
            "clips_replays_count", reel_scheduler.INSTAGRAM_INSIGHT_REQUEST_METRIC_KEYS
        )
        self.assertIn("follows", reel_scheduler.INSTAGRAM_OPTIONAL_INSIGHT_METRIC_KEYS)
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "reels.db"
            with reel_ledger.connect(db) as conn:
                reel_ledger.upsert_imported(
                    conn,
                    content_hash="h-optional",
                    channel_id="aibrief_jp",
                    lang="ja",
                    clip_dir="/clip",
                    media_path="/clip/reel.mp4",
                    status=reel_ledger.STATUS_PUBLISHED,
                    published_at="2026-06-23T00:00:00+00:00",
                    media_id="178900099",
                )
            args = argparse.Namespace(
                platform="instagram",
                channel="aibrief_jp",
                db=db,
                limit=None,
                dry_run=False,
                metrics="views,total_views,ig_reels_avg_watch_time,reels_skip_rate",
                access_token="",
                graph_api_version="v23.0",
                graph_api_root="https://graph.instagram.com",
            )
            graph_error = SystemExit("Instagram Graph API error 400: metric unavailable")
            with patch("instagram_publish.load_env_file"), patch(
                "instagram_publish.resolve_instagram_access_token",
                return_value=("token", "test"),
            ), patch.object(
                reel_scheduler,
                "fetch_insights",
                side_effect=[
                    graph_error,
                    {"data": [
                        {"name": "views", "values": [{"value": 1200}]},
                        {"name": "total_views", "values": [{"value": 2200}]},
                    ]},
                    graph_error,
                    {"data": [{
                        "name": "ig_reels_avg_watch_time",
                        "total_value": {"value": 4321.5},
                    }]},
                    graph_error,
                ],
            ) as fetch:
                rc = reel_scheduler.sync_insights_command(args)

            self.assertEqual(rc, 0)
            self.assertEqual(fetch.call_count, 5)
            with reel_ledger.connect(db) as conn:
                row = conn.execute("SELECT * FROM insights WHERE media_id='178900099'").fetchone()
                self.assertEqual(row["views"], 1200)
                self.assertEqual(row["total_views"], 2200)
                self.assertEqual(row["ig_reels_avg_watch_time"], 4321.5)
                self.assertIsNone(row["reels_skip_rate"])
                self.assertIn("reels_skip_rate", row["raw"])

    def test_sync_insights_skips_one_graph_error_and_continues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "reels.db"
            with reel_ledger.connect(db) as conn:
                reel_ledger.upsert_imported(
                    conn,
                    content_hash="h-bad",
                    channel_id="aibrief_jp",
                    lang="ja",
                    clip_dir="/clip-bad",
                    media_path="/clip-bad/reel.mp4",
                    status=reel_ledger.STATUS_PUBLISHED,
                    published_at="2026-06-24T00:00:00+00:00",
                    media_id="18234800344311426",
                )
                reel_ledger.upsert_imported(
                    conn,
                    content_hash="h-good",
                    channel_id="aibrief_jp",
                    lang="ja",
                    clip_dir="/clip-good",
                    media_path="/clip-good/reel.mp4",
                    status=reel_ledger.STATUS_PUBLISHED,
                    published_at="2026-06-23T00:00:00+00:00",
                    media_id="178900002",
                )
            args = argparse.Namespace(
                platform="instagram",
                channel=None,
                db=db,
                limit=None,
                dry_run=False,
                metrics="views,saved",
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
                side_effect=[
                    SystemExit("Instagram Graph API error 400: Unsupported get request."),
                    {"data": [{"name": "views", "values": [{"value": 321}]}]},
                ],
            ):
                rc = reel_scheduler.sync_insights_command(args)

            self.assertEqual(rc, 1)
            with reel_ledger.connect(db) as conn:
                self.assertIsNone(
                    conn.execute("SELECT * FROM insights WHERE media_id='18234800344311426'").fetchone()
                )
                row = conn.execute("SELECT * FROM insights WHERE media_id='178900002'").fetchone()
                self.assertEqual(row["views"], 321)

    def test_report_writes_update_button_and_llm_json_with_segment_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "VID123"
            clip = source / "clips" / "001-hook"
            clip.mkdir(parents=True)
            media_path = clip / "reel.en.vibecodersph.mp4"
            media_path.write_bytes(b"video")
            manifest_path = root / "manifest.json"
            manifest_path.write_text("{}", encoding="utf-8")
            (source / "metadata.json").write_text(
                json.dumps(
                    {
                        "title": "Source episode",
                        "uploader": "Example Channel",
                        "webpage_url": "https://example.com/watch",
                    }
                ),
                encoding="utf-8",
            )
            (source / "transcript.en.json").write_text(
                json.dumps(
                    [
                        {"start": 8.0, "end": 10.5, "text": "Lead-in"},
                        {"start": 10.5, "end": 14.0, "text": "This is the exact segment."},
                        {"start": 20.0, "end": 22.0, "text": "After the clip"},
                    ]
                ),
                encoding="utf-8",
            )
            (clip / "notes.json").write_text(
                json.dumps(
                    {
                        "start": 10.0,
                        "end": 15.0,
                        "duration": 5.0,
                        "score": 9.1,
                        "one_liner": "Exact hook",
                        "reason": "Strong proof point",
                        "source_chapter": "Demo section",
                        "transcript": "This is the exact segment.",
                    }
                ),
                encoding="utf-8",
            )
            (clip / "subtitles.en.ass").write_text(
                "\n".join(
                    [
                        "[Events]",
                        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
                        "Dialogue: 0,0:00:00.00,0:00:02.00,Default,,0,0,0,,Reel subtitle transcript.",
                    ]
                ),
                encoding="utf-8",
            )
            db = root / "reels.db"
            with reel_ledger.connect(db) as conn:
                reel_ledger.upsert_imported(
                    conn,
                    content_hash="h-report",
                    channel_id="vibecodersph",
                    lang="en",
                    clip_dir=clip,
                    media_path=media_path,
                    status=reel_ledger.STATUS_PUBLISHED,
                    source_video="VID123",
                    title="Exact hook",
                    scheduled_at="2026-06-23T00:00:00+00:00",
                    published_at="2026-06-24T00:00:00+00:00",
                    media_id="178900003",
                    permalink="https://www.instagram.com/reel/abc/",
                    manifest_path=str(manifest_path),
                )
                reel_ledger.set_status(
                    conn,
                    "h-report",
                    "vibecodersph",
                    reel_ledger.STATUS_PUBLISHED,
                    caption="Caption text",
                )
                reel_ledger.record_insight(
                    conn,
                    content_hash="h-report",
                    channel_id="vibecodersph",
                    media_id="178900003",
                    # Simulate a schema-v2 row whose visible views column was
                    # overwritten by the combined total; raw retains both scopes.
                    metrics={"views": 2200, "saved": 55, "total_interactions": 90},
                    raw=json.dumps({"data": [
                        {"name": "views", "values": [{"value": 1234}]},
                        {"name": "total_views", "values": [{"value": 2200}]},
                    ]}),
                    captured_at="2026-06-25T00:00:00+00:00",
                )
                reel_ledger.upsert_imported(
                    conn,
                    content_hash="h-report-jp",
                    channel_id="aibrief_jp",
                    lang="ja",
                    clip_dir=clip,
                    media_path=media_path,
                    status=reel_ledger.STATUS_SCHEDULED,
                    source_video="VID123",
                    title="JP queued",
                    scheduled_at="2026-06-24T09:00:00+09:00",
                )
            report_path = root / "reel_report.html"
            rc = reel_scheduler.report_command(
                argparse.Namespace(
                    platform="instagram",
                    channel=None,
                    db=db,
                    out=report_path,
                    limit=0,
                    insights_json_out=None,
                    sync_action_url="http://127.0.0.1:8765/sync-insights",
                )
            )

            self.assertEqual(rc, 0)
            html = report_path.read_text(encoding="utf-8")
            self.assertIn("Update Instagram Insights", html)
            self.assertIn("reel_report.insights.json", html)
            self.assertIn("reel_report.insights.md", html)
            self.assertIn('href="#channel-aibrief_jp"', html)
            self.assertIn('href="#channel-vibecodersph"', html)
            self.assertIn('id="channel-aibrief_jp"', html)
            self.assertIn('id="channel-vibecodersph"', html)
            self.assertIn("AI Brief JP (aibrief_jp)", html)
            self.assertIn("VibeCoders PH (vibecodersph)", html)
            self.assertIn("June 24, 9AM JST", html)
            self.assertIn("June 24, 8AM PHT", html)
            self.assertIn("June 25, 8AM PHT", html)
            self.assertIn("<th>Scheduled</th><th>Status</th><th>Title</th>", html)
            self.assertNotIn("<th>Type</th>", html)
            self.assertNotIn('class="post-type trial"', html)
            self.assertNotIn('class="post-type regular"', html)
            self.assertNotIn("<th>Published</th><th>Channel</th>", html)
            self.assertIn("<th>Instagram views</th><th>Meta all-surface views</th>", html)
            self.assertIn("These fields overlap", html)
            payload = json.loads(report_path.with_suffix(".insights.json").read_text(encoding="utf-8"))
            self.assertIn("never add them together", payload["metric_scopes"]["warning"])
            item = payload["items"][0]
            self.assertNotIn("trial_reel", item)
            self.assertNotIn("trial_graduation_strategy", item)
            self.assertEqual(item["insights"]["metrics"]["views"], 1234)
            self.assertEqual(item["insights"]["metrics"]["total_views"], 2200)
            self.assertEqual(item["segment"]["transcript"], "This is the exact segment.")
            self.assertEqual(item["segment"]["reel_transcript"], "Reel subtitle transcript.")
            self.assertTrue(item["segment"]["reel_transcript_path"].endswith("subtitles.en.ass"))
            self.assertEqual(item["segment"]["source_chapter"], "Demo section")
            self.assertEqual(item["source"]["url"], "https://example.com/watch")
            self.assertIn("This is the exact segment.", item["segment"]["source_transcript_segments"][1]["text"])
            markdown = report_path.with_suffix(".insights.md").read_text(encoding="utf-8")
            self.assertIn(
                "| # | Published | Channel | Reel | Instagram views | Meta all-surface views | Instagram reach |",
                markdown,
            )
            self.assertIn("| 1 | 2026-06-24T00:00:00+00:00 | vibecodersph | [reel]", markdown)
            self.assertIn("| 1,234 |", markdown)
            self.assertIn("Exact hook", markdown)
            self.assertIn("Reel subtitle transcript.", markdown)
            self.assertNotIn("## Reel Transcripts", markdown)

    def test_report_matches_stale_rows_to_reel_outputs_by_youtube_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outputs_root = root / "outputs"
            clip = outputs_root / "VID123" / "clips" / "001-hook"
            clip.mkdir(parents=True)
            media_path = clip / "reel.ja.aibrief_jp.mp4"
            media_path.write_bytes(b"ja-video")
            (outputs_root / "VID123" / "metadata.json").write_text(
                json.dumps({"webpage_url": "https://www.youtube.com/watch?v=VID123"}),
                encoding="utf-8",
            )
            (clip / "notes.json").write_text(
                json.dumps({"index": 1, "one_liner": "English hook"}, ensure_ascii=False),
                encoding="utf-8",
            )
            (clip / "one_liners.json").write_text(
                json.dumps({"ja": "日本語フック"}, ensure_ascii=False),
                encoding="utf-8",
            )
            (clip / "subtitles.ja.ass").write_text(
                "\n".join(
                    [
                        "[Events]",
                        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
                        "Dialogue: 0,0:00:00.00,0:00:02.00,Default,,0,0,0,,これはリール字幕です。",
                    ]
                ),
                encoding="utf-8",
            )
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "source_url": "https://www.youtube.com/watch?v=VID123",
                        "slides": [{"path": "/stale/reel.ja.aibrief_jp.mp4"}],
                    }
                ),
                encoding="utf-8",
            )
            db = root / "reels.db"
            with reel_ledger.connect(db) as conn:
                reel_ledger.upsert_imported(
                    conn,
                    content_hash=reel_ledger.hash_file(media_path),
                    channel_id="aibrief_jp",
                    lang="ja",
                    clip_dir="/stale/clip",
                    media_path="/stale/reel.ja.aibrief_jp.mp4",
                    status=reel_ledger.STATUS_PUBLISHED,
                    source_video="",
                    title="日本語フック",
                    published_at="2026-06-24T00:00:00+00:00",
                    media_id="178900004",
                    permalink="https://www.instagram.com/reel/def/",
                    manifest_path=str(manifest),
                )

            with patch.object(reel_scheduler, "DEFAULT_REEL_OUTPUTS", outputs_root):
                reel_scheduler.report_command(
                    argparse.Namespace(
                        platform="instagram",
                        channel=None,
                        db=db,
                        out=root / "reel_report.html",
                        limit=0,
                        insights_json_out=None,
                        insights_md_out=None,
                        max_transcript_chars=0,
                        sync_action_url="",
                    )
                )

            payload = json.loads((root / "reel_report.insights.json").read_text(encoding="utf-8"))
            item = payload["items"][0]
            self.assertEqual(Path(item["segment"]["clip_dir"]).resolve(), clip.resolve())
            self.assertEqual(item["segment"]["reel_transcript"], "これはリール字幕です。")
            markdown = (root / "reel_report.insights.md").read_text(encoding="utf-8")
            self.assertIn("これはリール字幕です。", markdown)
            self.assertNotIn("## Reel Transcripts", markdown)

    def test_insights_markdown_command_processes_existing_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            json_path = root / "insights.json"
            json_path.write_text(
                json.dumps(
                    {
                        "generated_at": "2026-07-01T00:00:00+00:00",
                        "platform": "instagram",
                        "items": [
                            {
                                "published_at": "2026-06-24T00:00:00+00:00",
                                "channel_id": "vibecodersph",
                                "permalink": "https://instagram.com/reel/abc/",
                                "title": "Hook with | pipe",
                                "source": {"title": "Source title", "url": "https://example.com"},
                                "insights": {
                                    "metrics": {
                                        "views": 1000,
                                        "reach": 800,
                                        "likes": 12,
                                        "comments": 3,
                                        "saved": 4,
                                        "shares": 5,
                                        "total_interactions": 24,
                                    }
                                },
                                "segment": {"reel_transcript": "A long transcript for the table."},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            out_path = root / "insights.md"

            rc = reel_scheduler.insights_markdown_command(
                argparse.Namespace(json_path=json_path, out=out_path, max_transcript_chars=0)
            )

            self.assertEqual(rc, 0)
            markdown = out_path.read_text(encoding="utf-8")
            self.assertIn("# Reel Insights", markdown)
            self.assertIn("Hook with \\| pipe", markdown)
            self.assertIn(
                "| 1,000 |  | 800 | 12 |  | 3 |  | 4 | 5 | 24 |",
                markdown,
            )
            self.assertIn("A long transcript for the table.", markdown)
            self.assertNotIn("## Reel Transcripts", markdown)

    def test_queue_ui_sync_endpoint_invokes_sync_and_refreshes_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "reels.db"
            report_out = root / "reel_report.html"
            with reel_ledger.connect(db):
                pass
            handler = reel_scheduler.make_queue_ui_handler(
                db_path=db,
                channel_filter=None,
                limit=20,
                settings_key="instagram_reels",
                platform="instagram",
                report_out=report_out,
            )
            server = reel_scheduler.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                with patch.object(reel_scheduler, "sync_insights_command", return_value=1) as sync:
                    conn = http.client.HTTPConnection(host, port, timeout=5)
                    conn.request("POST", "/sync-insights", body="")
                    response = conn.getresponse()
                    response.read()
                    conn.close()

                self.assertEqual(response.status, 303)
                location = response.getheader("Location") or ""
                self.assertIn("/report?message=", location)
                self.assertNotIn("error=", location)
                sync.assert_called_once()
                self.assertTrue(report_out.exists())
                html = report_out.read_text(encoding="utf-8")
                self.assertIn(
                    f'action="{reel_scheduler.DEFAULT_REPORT_SYNC_ACTION_URL}"',
                    html,
                )
                self.assertTrue(report_out.with_suffix(".insights.json").exists())
                self.assertTrue(report_out.with_suffix(".insights.md").exists())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

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

    def test_ph_impeachment_profile_adds_sara_caption_hashtags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "impeachments_news" / "VID123"
            clips = source / "clips"
            clip = clips / "001-some-clip"
            clip.mkdir(parents=True)
            (source / "candidates.json").write_text(
                json.dumps({"selection_profile": "ph-impeachment-news", "clips": []}), encoding="utf-8"
            )
            channel = reel_scheduler.load_channel("vibecodersph")
            caption, hashtags = reel_scheduler.build_caption(
                channel,
                clip,
                {
                    "index": 1,
                    "one_liner": "Court orders subpoena fight for Duterte bank records",
                    "reason": "Impeachment trial update.",
                },
            )

            self.assertIn("#VPSara", hashtags)
            self.assertIn("#SaraDuterte", hashtags)
            self.assertIn("#Impeachment", hashtags)
            self.assertNotIn("#AI", hashtags)
            self.assertIn("#VPSara #SaraDuterte", caption)

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
