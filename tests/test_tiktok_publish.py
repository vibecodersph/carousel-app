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
import tiktok_publish


def make_args(**overrides):
    base = dict(
        mode="inbox",
        source="file",
        privacy_level="SELF_ONLY",
        disable_comment=False,
        disable_duet=False,
        disable_stitch=False,
        caption=None,
        caption_file=None,
        access_token="",
        upload_r2=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


class TokenResolutionTests(unittest.TestCase):
    def test_channel_token_beats_global(self) -> None:
        manifest = {"channel_id": "vibecodersph"}
        env = {
            "TIKTOK_ACCESS_TOKEN": "global",
            "TIKTOK_ACCESS_TOKEN_VIBECODERSPH": "channel",
        }
        with patch.dict(tiktok_publish.os.environ, env, clear=True):
            token, source = tiktok_publish.resolve_tiktok_access_token(
                "", manifest, tiktok_publish.ROOT / "out" / "manifest.json"
            )
        self.assertEqual(token, "channel")
        self.assertEqual(source, "channel:vibecodersph")

    def test_cli_token_beats_channel(self) -> None:
        manifest = {"channel_id": "vibecodersph"}
        with patch.dict(tiktok_publish.os.environ, {"TIKTOK_ACCESS_TOKEN_VIBECODERSPH": "channel"}, clear=True):
            token, source = tiktok_publish.resolve_tiktok_access_token(
                "cli", manifest, tiktok_publish.ROOT / "out" / "manifest.json"
            )
        self.assertEqual(token, "cli")
        self.assertEqual(source, "cli")


class CaptionAndBodyTests(unittest.TestCase):
    def test_caption_falls_back_to_instagram_caption(self) -> None:
        manifest = {"instagram_caption": "hello world", "topic": "ignored"}
        self.assertEqual(tiktok_publish.read_caption(make_args(), manifest), "hello world")

    def test_caption_truncates_to_rune_cap(self) -> None:
        manifest = {"instagram_caption": "x" * 5000}
        self.assertEqual(len(tiktok_publish.read_caption(make_args(), manifest)), tiktok_publish.TITLE_MAX_RUNES)

    def test_file_source_info_is_single_chunk(self) -> None:
        item = tiktok_publish.VideoItem(local_path="/tmp/x.mp4", video_size=2048)
        info = tiktok_publish.source_info(item, tiktok_publish.SOURCE_FILE)
        self.assertEqual(info["source"], "FILE_UPLOAD")
        self.assertEqual(info["video_size"], 2048)
        self.assertEqual(info["chunk_size"], 2048)
        self.assertEqual(info["total_chunk_count"], 1)

    def test_pull_source_info_uses_public_url(self) -> None:
        item = tiktok_publish.VideoItem(local_path="/tmp/x.mp4", video_size=1, public_url="https://cdn/x.mp4")
        info = tiktok_publish.source_info(item, tiktok_publish.SOURCE_PULL)
        self.assertEqual(info, {"source": "PULL_FROM_URL", "video_url": "https://cdn/x.mp4"})

    def test_direct_init_has_post_info_inbox_does_not(self) -> None:
        item = tiktok_publish.VideoItem(local_path="/tmp/x.mp4", video_size=10)
        direct = tiktok_publish.init_body(
            mode="direct", item=item, source="file", title="hi", privacy_level="SELF_ONLY", args=make_args()
        )
        inbox = tiktok_publish.init_body(
            mode="inbox", item=item, source="file", title="hi", privacy_level="SELF_ONLY", args=make_args()
        )
        self.assertEqual(direct["post_info"]["privacy_level"], "SELF_ONLY")
        self.assertEqual(direct["post_info"]["title"], "hi")
        self.assertNotIn("post_info", inbox)


class InsightsTests(unittest.TestCase):
    def test_parse_video_metrics_maps_counts(self) -> None:
        payload = {"data": {"videos": [{"id": "v1", "view_count": 10, "like_count": 3, "comment_count": 1, "share_count": 2}]}}
        parsed = tiktok_publish.parse_video_metrics(payload, "v1")
        self.assertEqual(parsed, {"views": 10, "likes": 3, "comments": 1, "shares": 2})

    def test_parse_video_metrics_missing_id_returns_empty(self) -> None:
        payload = {"data": {"videos": [{"id": "other"}]}}
        self.assertEqual(tiktok_publish.parse_video_metrics(payload, "v1"), {})

    def test_post_permalink_builds_url(self) -> None:
        status = {"publicaly_available_post_id": ["7234"]}
        url = tiktok_publish.post_permalink(status, {"creator_username": "vibecodersph"})
        self.assertEqual(url, "https://www.tiktok.com/@vibecodersph/video/7234")


class SchedulerPlatformTests(unittest.TestCase):
    def _row(self, **over):
        base = {"manifest_path": "/tmp/job/manifest.json", "content_hash": "h", "channel_id": "vibecodersph"}
        base.update(over)
        return base

    def test_ledger_report_path_per_platform(self) -> None:
        row = self._row()
        self.assertEqual(reel_scheduler.ledger_report_path(row, "instagram").name, "instagram_publish.json")
        self.assertEqual(reel_scheduler.ledger_report_path(row, "tiktok").name, "tiktok_publish.json")

    def test_publisher_command_tiktok_shape(self) -> None:
        job = {"manifest_path": "/tmp/m.json", "publish_report_path": "/tmp/tiktok_publish.json", "id": "j1"}
        cmd = reel_scheduler.publisher_command(
            job,
            channel_id="vibecodersph",
            schedule_id="ledger",
            dry_run=True,
            upload_r2=False,
            media_base_url="",
            r2_bucket="",
            r2_public_base_url="",
            platform="tiktok",
            tiktok_mode="inbox",
            tiktok_source="file",
            tiktok_privacy="SELF_ONLY",
        )
        self.assertIn("tiktok_publish.py", cmd[1])
        self.assertIn("--mode", cmd)
        self.assertIn("inbox", cmd)
        self.assertIn("--dry-run", cmd)
        self.assertNotIn("--single-video-media-type", cmd)

    def test_resolve_db_defaults_per_platform(self) -> None:
        ig = reel_scheduler.resolve_db(argparse.Namespace(db=None, platform="instagram"))
        tt = reel_scheduler.resolve_db(argparse.Namespace(db=None, platform="tiktok"))
        self.assertEqual(ig, reel_ledger.DEFAULT_DB_PATH)
        self.assertEqual(tt.name, "tiktok.db")

    def test_settings_key_for(self) -> None:
        self.assertEqual(reel_scheduler.settings_key_for("instagram"), "instagram_reels")
        self.assertEqual(reel_scheduler.settings_key_for("tiktok"), "tiktok")


class TikTokDryRunPipelineTests(unittest.TestCase):
    """Scan -> plan-ledger -> run-due, end to end in dry-run, on a tiktok.db."""

    def test_plan_and_preview_tiktok_reel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "metadata.json").write_text(
                json.dumps({"title": "T", "uploader": "U", "webpage_url": "https://x/v"}),
                encoding="utf-8",
            )
            clip = root / "clips" / "001-foo"
            clip.mkdir(parents=True)
            (clip / "reel.en.vibecodersph.mp4").write_bytes(b"a fake mp4 payload")
            (clip / "notes.json").write_text(
                json.dumps({"index": 1, "one_liner": "Claude Code in the terminal"}, ensure_ascii=False),
                encoding="utf-8",
            )

            db_path = root / "tiktok.db"
            out_dir = root / "out"
            planned = reel_scheduler.plan_ledger_rows(
                db_path=db_path,
                clips_dir=root / "clips",
                out_dir=out_dir,
                channel_filter=None,
                start_at_text="2026-06-24",
                limit_per_channel=None,
                jitter_minutes=0,
                scan_first=True,
                settings_key="tiktok",
            )
            self.assertEqual(planned.get("vibecodersph"), 1)

            rc = reel_scheduler.run_due_ledger(
                db_path=db_path,
                now=datetime(2026, 6, 30, 12, 0, tzinfo=ZoneInfo("Asia/Manila")),
                channel_id=None,
                scheduled_date=None,
                dry_run=True,
                include_future=True,
                retry_failed=False,
                limit=None,
                upload_r2=False,
                media_base_url="",
                r2_bucket="",
                r2_public_base_url="",
                platform="tiktok",
            )
            self.assertEqual(rc, 0)

            reports = list(out_dir.rglob("tiktok_publish.json"))
            self.assertEqual(len(reports), 1)
            report = json.loads(reports[0].read_text())
            self.assertEqual(report["platform"], "tiktok")
            self.assertTrue(report["dry_run"])
            self.assertEqual(report["mode"], "inbox")
            self.assertEqual(report["source"], "file")
            self.assertEqual(report["result"], {})
            # caption routed from the tiktok block's hashtags, not instagram's
            self.assertIn("#fyp", report["title"])

            with reel_ledger.connect(db_path) as conn:
                counts = reel_ledger.status_counts(conn, "vibecodersph")
            self.assertEqual(counts["vibecodersph"].get(reel_ledger.STATUS_PREVIEWED), 1)


if __name__ == "__main__":
    unittest.main()
