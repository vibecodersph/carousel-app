import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

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


if __name__ == "__main__":
    unittest.main()
