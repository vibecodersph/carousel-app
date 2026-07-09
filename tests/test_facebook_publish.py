import argparse
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import facebook_publish
import reel_ledger
import reel_scheduler


class FacebookPublishCredentialTests(unittest.TestCase):
    def test_channel_page_id_beats_global(self) -> None:
        manifest = {"channel_id": "vibecodersph"}
        env = {
            "FACEBOOK_PAGE_ID": "global-page",
            "FACEBOOK_PAGE_ID_VIBECODERSPH": "channel-page",
        }
        with patch.dict(facebook_publish.os.environ, env, clear=True):
            page_id, source = facebook_publish.resolve_facebook_page_id(
                "", manifest, facebook_publish.ROOT / "out" / "manifest.json"
            )
        self.assertEqual(page_id, "channel-page")
        self.assertEqual(source, "channel:vibecodersph")

    def test_channel_token_beats_global(self) -> None:
        manifest = {"channel_id": "vibecodersph"}
        env = {
            "FACEBOOK_PAGE_ACCESS_TOKEN": "global-token",
            "FACEBOOK_PAGE_ACCESS_TOKEN_VIBECODERSPH": "channel-token",
        }
        with patch.dict(facebook_publish.os.environ, env, clear=True):
            token, source = facebook_publish.resolve_facebook_access_token(
                "", manifest, facebook_publish.ROOT / "out" / "manifest.json"
            )
        self.assertEqual(token, "channel-token")
        self.assertEqual(source, "channel:vibecodersph")

    def test_reuses_channel_meta_system_user_token_when_facebook_token_missing(self) -> None:
        manifest = {"channel_id": "vibecodersph"}
        env = {"META_SYSTEM_USER_ACCESS_TOKEN_VIBECODERSPH": "same-token-as-instagram"}
        with patch.dict(facebook_publish.os.environ, env, clear=True):
            token, source = facebook_publish.resolve_facebook_access_token(
                "", manifest, facebook_publish.ROOT / "out" / "manifest.json"
            )
        self.assertEqual(token, "same-token-as-instagram")
        self.assertEqual(source, "channel:vibecodersph")


class FacebookPublishApiTests(unittest.TestCase):
    def test_resolve_page_access_token_for_publish_uses_page_lookup(self) -> None:
        with patch.object(
            facebook_publish.ig,
            "graph_request",
            return_value={"access_token": "page-token"},
        ) as request:
            token, source = facebook_publish.resolve_page_access_token_for_publish(
                page_id="page1",
                access_token="meta-token",
                access_token_source="channel:vibecodersph",
                graph_version="v25.0",
                graph_api_root="https://graph.facebook.com",
            )

        self.assertEqual(token, "page-token")
        self.assertEqual(source, "channel:vibecodersph->page:page1")
        self.assertEqual(request.call_args.args[0], "page1")
        self.assertEqual(request.call_args.kwargs["params"], {"fields": "access_token"})
        self.assertEqual(request.call_args.kwargs["method"], "GET")

    def test_video_item_requires_one_video_slide(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "reel.mp4"
            video.write_bytes(b"video")
            manifest_path = root / "manifest.json"
            manifest = {"slides": [{"index": 1, "type": "video", "path": str(video)}]}

            item = facebook_publish.video_item_from_manifest(manifest, manifest_path)

        self.assertEqual(item.local_path, str(video.resolve()))
        self.assertEqual(item.video_size, 5)

    def test_start_reel_upload_uses_video_reels_start_phase(self) -> None:
        with patch.object(
            facebook_publish.ig,
            "graph_request",
            return_value={"video_id": "v1", "upload_url": "https://upload.example/reel"},
        ) as request:
            result = facebook_publish.start_reel_upload(
                page_id="page1",
                access_token="token",
                graph_version="v25.0",
                graph_api_root="https://graph.facebook.com",
            )

        self.assertEqual(result["video_id"], "v1")
        kwargs = request.call_args.kwargs
        self.assertEqual(request.call_args.args[0], "page1/video_reels")
        self.assertEqual(kwargs["params"], {"upload_phase": "start"})
        self.assertEqual(kwargs["api_name"], "Facebook")

    def test_finish_reel_publish_sets_published_state_and_description(self) -> None:
        with patch.object(facebook_publish.ig, "graph_request", return_value={"success": True}) as request:
            result = facebook_publish.finish_reel_publish(
                page_id="page1",
                video_id="v1",
                caption="caption",
                access_token="token",
                graph_version="v25.0",
                graph_api_root="https://graph.facebook.com",
            )

        self.assertTrue(result["success"])
        params = request.call_args.kwargs["params"]
        self.assertEqual(params["upload_phase"], "finish")
        self.assertEqual(params["video_id"], "v1")
        self.assertEqual(params["video_state"], "PUBLISHED")
        self.assertEqual(params["description"], "caption")


class SchedulerFacebookPlatformTests(unittest.TestCase):
    def _row(self, **over):
        base = {"manifest_path": "/tmp/job/manifest.json", "content_hash": "h", "channel_id": "vibecodersph"}
        base.update(over)
        return base

    def test_scheduler_has_separate_facebook_platform_defaults(self) -> None:
        row = self._row()
        self.assertEqual(reel_scheduler.ledger_report_path(row, "facebook").name, "facebook_publish.json")
        self.assertEqual(
            reel_scheduler.resolve_db(argparse.Namespace(db=None, platform="facebook")).name,
            "facebook.db",
        )
        self.assertEqual(reel_scheduler.settings_key_for("facebook"), "facebook_reels")

    def test_vibecodersph_facebook_slots_are_ph_daytime_news_cadence(self) -> None:
        channel = reel_scheduler.load_channel("vibecodersph")
        settings = reel_scheduler.reel_settings(channel, "facebook_reels")
        self.assertEqual(settings["timezone"], "Asia/Manila")
        self.assertEqual(
            settings["slots"],
            ["07:00", "09:00", "12:00", "15:00", "17:00", "20:00", "22:00"],
        )

    def test_publisher_command_facebook_shape(self) -> None:
        job = {"manifest_path": "/tmp/m.json", "publish_report_path": "/tmp/facebook_publish.json", "id": "j1"}
        cmd = reel_scheduler.publisher_command(
            job,
            channel_id="vibecodersph",
            schedule_id="ledger",
            dry_run=True,
            upload_r2=True,
            media_base_url="https://cdn.example.com",
            r2_bucket="bucket",
            r2_public_base_url="https://cdn.example.com",
            platform="facebook",
        )
        self.assertIn("facebook_publish.py", cmd[1])
        self.assertIn("--out", cmd)
        self.assertIn("--dry-run", cmd)
        self.assertNotIn("--single-video-media-type", cmd)
        self.assertNotIn("--upload-r2", cmd)
        self.assertNotIn("--media-base-url", cmd)


class FacebookDryRunPipelineTests(unittest.TestCase):
    def test_plan_and_preview_facebook_impeachment_reel(self) -> None:
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
                json.dumps({"index": 1, "one_liner": "Senate procedure gets a key update"}),
                encoding="utf-8",
            )

            db_path = root / "facebook_impeachment.db"
            out_dir = root / "out"
            planned = reel_scheduler.plan_ledger_rows(
                db_path=db_path,
                clips_dir=root / "clips",
                out_dir=out_dir,
                channel_filter="vibecodersph",
                start_at_text="2026-07-10",
                limit_per_channel=None,
                jitter_minutes=0,
                scan_first=True,
                settings_key="facebook_reels",
            )
            self.assertEqual(planned.get("vibecodersph"), 1)

            rc = reel_scheduler.run_due_ledger(
                db_path=db_path,
                now=datetime(2026, 7, 10, 12, 0, tzinfo=ZoneInfo("Asia/Manila")),
                channel_id="vibecodersph",
                scheduled_date=None,
                dry_run=True,
                include_future=True,
                retry_failed=False,
                limit=None,
                upload_r2=False,
                media_base_url="",
                r2_bucket="",
                r2_public_base_url="",
                platform="facebook",
            )
            self.assertEqual(rc, 0)

            reports = list(out_dir.rglob("facebook_publish.json"))
            self.assertEqual(len(reports), 1)
            report = json.loads(reports[0].read_text())
            self.assertEqual(report["platform"], "facebook")
            self.assertTrue(report["dry_run"])
            self.assertEqual(report["result"], {})
            self.assertIn("#Philippines", report["caption"])

            with reel_ledger.connect(db_path) as conn:
                counts = reel_ledger.status_counts(conn, "vibecodersph")
            self.assertEqual(counts["vibecodersph"].get(reel_ledger.STATUS_PREVIEWED), 1)


if __name__ == "__main__":
    unittest.main()
