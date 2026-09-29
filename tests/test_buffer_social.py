import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import buffer_social as bs

CHANNELS = [
    {"id": "c1", "service": "tiktok", "name": "vibecodersph_", "displayName": "Vibe Coders PH", "type": "profile", "timezone": "Asia/Manila", "isDisconnected": False, "isLocked": False, "organizationId": "o1"},
    {"id": "c2", "service": "tiktok", "name": "caisielim", "displayName": "Caisie Lim", "type": "profile", "timezone": "Asia/Manila", "isDisconnected": False, "isLocked": False, "organizationId": "o1"},
    {"id": "c3", "service": "twitter", "name": "vibecodersph", "displayName": "VCPH", "type": "profile", "timezone": "Asia/Manila", "isDisconnected": False, "isLocked": False, "organizationId": "o1"},
    {"id": "c4", "service": "instagram", "name": "old", "displayName": "old", "type": "business", "timezone": "Asia/Tokyo", "isDisconnected": True, "isLocked": False, "organizationId": "o1"},
    {"id": "c5", "service": "youtube", "name": "locked", "displayName": "locked", "type": "profile", "timezone": "UTC", "isDisconnected": False, "isLocked": True, "organizationId": "o1"},
]


class ResolveChannelTests(unittest.TestCase):
    def test_service_and_name_part(self):
        self.assertEqual(bs.resolve_channel(CHANNELS, "tiktok:caisie")["id"], "c2")
        self.assertEqual(bs.resolve_channel(CHANNELS, "tiktok:vibecoders")["id"], "c1")
        self.assertEqual(bs.resolve_channel(CHANNELS, "c3")["id"], "c3")

    def test_x_alias(self):
        self.assertEqual(bs.resolve_channel(CHANNELS, "x")["service"], "twitter")

    def test_ambiguous_missing_disconnected_locked_refuse(self):
        for spec in ("tiktok", "linkedin", "instagram", "youtube"):
            with self.assertRaises(SystemExit):
                bs.resolve_channel(CHANNELS, spec)


class WhenTests(unittest.TestCase):
    NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)

    def test_keywords(self):
        self.assertTrue(bs.parse_when("draft")["saveToDraft"])
        self.assertEqual(bs.parse_when("now")["mode"], "shareNow")
        self.assertEqual(bs.parse_when("queue")["mode"], "addToQueue")

    def test_scheduled_converts_to_utc(self):
        w = bs.parse_when("2026-10-01T19:00+08:00", self.NOW)
        self.assertEqual(w["mode"], "customScheduled")
        self.assertEqual(w["dueAt"], "2026-10-01T11:00:00.000Z")

    def test_past_naive_and_garbage_refuse(self):
        for v in ("2026-09-30T12:05+00:00", "2026-10-01T19:00", "tomorrow"):
            with self.assertRaises(SystemExit):
                bs.parse_when(v, self.NOW)


class CaptionTests(unittest.TestCase):
    def test_service_specific_key_wins_then_fallbacks(self):
        m = {"tiktok_caption": "tt", "x_text": "xx", "caption": "generic"}
        self.assertEqual(bs.caption_for("tiktok", m), "tt")
        self.assertEqual(bs.caption_for("twitter", m), "xx")
        self.assertEqual(bs.caption_for("threads", m), "generic")
        self.assertEqual(bs.caption_for("tiktok", m, "override"), "override")
        with self.assertRaises(SystemExit):
            bs.caption_for("tiktok", {})

    def test_limits(self):
        bs.validate_caption("tiktok", "ok #a #b #c #d #e")
        with self.assertRaises(SystemExit):
            bs.validate_caption("tiktok", "too many #a #b #c #d #e #f")
        with self.assertRaises(SystemExit):
            bs.validate_caption("tiktok", "x" * 2201)
        with self.assertRaises(SystemExit):
            bs.validate_caption("twitter", "x" * 281)
        with self.assertRaises(SystemExit):
            bs.validate_caption("tiktok", "a dash — here")


class PayloadTests(unittest.TestCase):
    def test_tiktok_scheduled_payload(self):
        when = bs.parse_when("2026-10-01T19:00+08:00", datetime(2026, 9, 30, tzinfo=timezone.utc))
        p = bs.build_input(CHANNELS[0], "hello #a", "https://pub.example/v.mp4", when)
        self.assertEqual(p["channelId"], "c1")
        self.assertEqual(p["mode"], "customScheduled")
        self.assertEqual(p["dueAt"], "2026-10-01T11:00:00.000Z")
        self.assertFalse(p["saveToDraft"])
        self.assertEqual(p["schedulingType"], "automatic")
        self.assertEqual(p["metadata"], {"tiktok": {"isAiGenerated": True}})
        self.assertEqual(p["assets"], [{"video": {"url": "https://pub.example/v.mp4"}}])

    def test_x_payload_and_not_ai(self):
        p = bs.build_input(CHANNELS[2], "hi", "https://pub.example/v.mp4", bs.parse_when("now"), ai_generated=False)
        self.assertEqual(p["metadata"], {"twitter": {"isAiGenerated": False}})
        self.assertNotIn("dueAt", p)

    def test_draft_never_shares_now(self):
        p = bs.build_input(CHANNELS[0], "hi", "https://pub.example/v.mp4", bs.parse_when("draft"))
        self.assertTrue(p["saveToDraft"])
        self.assertEqual(p["mode"], "addToQueue")


class GapTests(unittest.TestCase):
    T = datetime(2026, 10, 1, 11, 0, tzinfo=timezone.utc)

    def test_inside_and_outside_window(self):
        self.assertEqual(bs.gap_conflict(["2026-10-01T10:00:00.000Z"], self.T, 2), "2026-10-01T10:00:00.000Z")
        self.assertIsNone(bs.gap_conflict(["2026-10-01T08:59:00.000Z"], self.T, 2))
        self.assertIsNone(bs.gap_conflict(["2026-10-01T10:59:00.000Z"], self.T, 0))


class PostFlowTests(unittest.TestCase):
    def test_report_blocks_repeat_and_dry_run_never_calls_create(self):
        with tempfile.TemporaryDirectory() as d:
            manifest = Path(d) / "manifest.json"
            video = Path(d) / "v.mp4"
            video.write_bytes(b"x")
            manifest.write_text(json.dumps({"slides": [{"index": 1, "type": "video", "path": str(video)}], "tiktok_caption": "hi #a"}))
            (Path(d) / bs.REPORT_NAME).write_text(json.dumps({"posts": [{"channel": {"id": "c1", "service": "tiktok", "name": "x"}, "post_id": "p1"}]}))

            class FakeBuf:
                def __init__(self, *_):
                    pass

                def channels(self):
                    return CHANNELS

            args = bs.build_parser().parse_args(["post", str(manifest), "--channel", "tiktok:vibecoders", "--when", "draft", "--dry-run"])
            with patch.object(bs, "Buffer", FakeBuf), patch.object(bs, "create_post", side_effect=AssertionError("must not create")):
                self.assertEqual(bs.cmd_post(args), 0)  # already in report: skipped, nothing created

    def test_publish_flag_required_for_scheduled(self):
        with tempfile.TemporaryDirectory() as d:
            manifest = Path(d) / "manifest.json"
            manifest.write_text("{}")
            when = (datetime.now(timezone.utc) + timedelta(days=1)).astimezone(timezone(timedelta(hours=8))).isoformat(timespec="minutes")
            args = bs.build_parser().parse_args(["post", str(manifest), "--channel", "x", "--when", when])
            with self.assertRaises(SystemExit) as cm:
                bs.cmd_post(args)
            self.assertIn("--publish", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
