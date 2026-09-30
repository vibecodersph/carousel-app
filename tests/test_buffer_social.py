import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
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

    def test_cover_offset_only_on_tiktok(self):
        w = bs.parse_when("now")
        p = bs.build_input(CHANNELS[0], "hi", "https://pub.example/v.mp4", w, cover_ms=4600)
        self.assertEqual(p["assets"][0]["video"]["metadata"], {"thumbnailOffset": 4600})
        x = bs.build_input(CHANNELS[2], "hi", "https://pub.example/v.mp4", w, cover_ms=4600)
        self.assertNotIn("metadata", x["assets"][0]["video"])

    def test_x_thread_becomes_replies_and_only_on_x(self):
        w = bs.parse_when("now")
        p = bs.build_input(CHANNELS[2], "hi", "https://pub.example/v.mp4", w, thread=["one", "two"])
        video = [{"video": {"url": "https://pub.example/v.mp4"}}]
        # Buffer publishes what `thread` holds: the root post (same text, with the video) first, then the replies
        self.assertEqual(p["metadata"]["twitter"]["thread"], [{"text": "hi", "assets": video}, {"text": "one", "assets": []}, {"text": "two", "assets": []}])
        self.assertEqual(p["text"], p["metadata"]["twitter"]["thread"][0]["text"])
        self.assertEqual(p["assets"], video)
        self.assertTrue(p["metadata"]["twitter"]["isAiGenerated"])
        t = bs.build_input(CHANNELS[0], "hi", "https://pub.example/v.mp4", w, thread=["one"])
        self.assertEqual(t["metadata"], {"tiktok": {"isAiGenerated": True}})
        plain = bs.build_input(CHANNELS[2], "hi", "https://pub.example/v.mp4", w)
        self.assertNotIn("thread", plain["metadata"]["twitter"])

    def test_thread_for_validates(self):
        self.assertEqual(bs.thread_for("twitter", {"x_thread": [" a ", "b"]}), ["a", "b"])
        self.assertEqual(bs.thread_for("tiktok", {"x_thread": ["a"]}), [])
        self.assertEqual(bs.thread_for("twitter", {}), [])
        for bad in ("just a string", [""], ["ok", "x" * 281], ["dash — here"]):
            with self.assertRaises(SystemExit):
                bs.thread_for("twitter", {"x_thread": bad})

    def test_post_problem_catches_a_dropped_video_or_thread(self):
        w = bs.parse_when("draft")
        plain = bs.build_input(CHANNELS[0], "hi", "https://pub.example/v.mp4", w)
        self.assertIsNone(bs.post_problem(plain, {"assets": [{"id": None, "source": "u"}]}))
        self.assertIn("0 of 1 assets", bs.post_problem(plain, {"assets": []}))
        x = bs.build_input(CHANNELS[2], "hi", "https://pub.example/v.mp4", w, thread=["one"])
        ok = {"assets": [{"source": "u"}], "metadata": {"thread": [{"text": "hi", "assets": [{"source": "u"}]}, {"text": "one", "assets": []}]}}
        self.assertIsNone(bs.post_problem(x, ok))
        no_video_on_root = {"assets": [{"source": "u"}], "metadata": {"thread": [{"text": "hi", "assets": []}, {"text": "one", "assets": []}]}}
        self.assertIn("thread post 1", bs.post_problem(x, no_video_on_root))
        short_thread = {"assets": [{"source": "u"}], "metadata": {"thread": [{"text": "hi", "assets": [{"source": "u"}]}]}}
        self.assertIn("1 of 2 thread posts", bs.post_problem(x, short_thread))

    def test_draft_never_shares_now(self):
        p = bs.build_input(CHANNELS[0], "hi", "https://pub.example/v.mp4", bs.parse_when("draft"))
        self.assertTrue(p["saveToDraft"])
        self.assertEqual(p["mode"], "addToQueue")


class YoutubeTextTests(unittest.TestCase):
    CAPTION = "Who started the Delano grape strike? Most people say Cesar Chavez.\n\nSecond paragraph.\n\n#Tag"
    COMMENT = "Sources\n- one https://a.example\n- two https://b.example"

    def test_title_is_the_hook_question(self):
        self.assertEqual(bs.youtube_title(self.CAPTION), "Who started the Delano grape strike?")

    def test_title_falls_back_to_the_first_line_without_a_question(self):
        self.assertEqual(bs.youtube_title("No question here. Still first line.\n\nMore? yes"), "No question here. Still first line.")

    def test_title_is_cut_at_a_word_boundary_within_100(self):
        t = bs.youtube_title(("word " * 40) + "end?")
        self.assertLessEqual(len(t), 100)
        self.assertTrue(t.endswith("word"))
        self.assertEqual(len(bs.youtube_title("x" * 150 + "?")), 100)

    def test_empty_title_and_description_refuse(self):
        for bad in ("", "  \n "):
            with self.assertRaises(SystemExit):
                bs.youtube_title(bad)
        with self.assertRaises(SystemExit):
            bs.validate_youtube("", "d")
        with self.assertRaises(SystemExit):
            bs.validate_youtube("t" * 101, "d")
        with self.assertRaises(SystemExit):
            bs.validate_youtube("t", "d" * 5001)
        with self.assertRaises(SystemExit):
            bs.validate_youtube("t", "")
        bs.validate_youtube("t" * 100, "d" * 5000)

    def test_description_is_caption_blank_line_sources(self):
        self.assertEqual(bs.youtube_description(self.CAPTION, self.COMMENT), self.CAPTION + "\n\n" + self.COMMENT)

    def test_wording_points_at_the_description_not_a_first_comment(self):
        self.assertEqual(bs.youtube_wording("Sources in the first comment. Made with AI."), "Sources below. Made with AI.")
        self.assertEqual(bs.youtube_wording("Every claim is sourced in the first comment."), "Every claim is sourced below.")
        self.assertEqual(bs.youtube_wording("Nasa comments ang sources. Ingat!"), "Nasa baba ang sources. Ingat!")
        self.assertEqual(bs.youtube_wording("Nothing to change here."), "Nothing to change here.")
        d = bs.youtube_description("Q? Sources in the first comment. #a", "Sources\n- x")
        self.assertEqual(d, "Q? Sources below. #a\n\nSources\n- x")

    def test_long_sources_are_trimmed_at_a_line_boundary_and_the_caption_stays_whole(self):
        comment = "Sources\n" + "\n".join(f"- line {i} " + "y" * 100 for i in range(80))
        d = bs.youtube_description(self.CAPTION, comment)
        self.assertLessEqual(len(d), 5000)
        self.assertTrue(d.startswith(self.CAPTION + "\n\n"))
        self.assertTrue(d.endswith("y" * 100))
        self.assertTrue(comment.startswith(d[len(self.CAPTION) + 2:]))

    def test_texts_read_the_two_approved_files(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "caption.txt").write_text(self.CAPTION + "\n", encoding="utf-8")
            (Path(d) / "first-comment.txt").write_text(self.COMMENT + "\n", encoding="utf-8")
            title, desc = bs.youtube_texts(Path(d))
            self.assertEqual(title, "Who started the Delano grape strike?")
            self.assertEqual(desc, self.CAPTION + "\n\n" + self.COMMENT)
            (Path(d) / "first-comment.txt").unlink()
            with self.assertRaises(SystemExit):
                bs.youtube_texts(Path(d))


class YoutubePayloadTests(unittest.TestCase):
    YT = {"id": "c9", "service": "youtube", "name": "VibeCodersPH", "displayName": "VCPH", "type": "profile", "timezone": "Asia/Manila", "isDisconnected": False, "isLocked": False, "organizationId": "o1"}

    def test_youtube_payload_carries_title_metadata_and_description_as_text(self):
        p = bs.build_input(self.YT, "the description", "https://pub.example/v.mp4", bs.parse_when("now"), cover_ms=3000, title="The title?")
        self.assertEqual(p["text"], "the description")
        self.assertEqual(p["metadata"], {"youtube": {"title": "The title?", "categoryId": "27", "privacy": "public", "madeForKids": False,
                                                     "notifySubscribers": False, "embeddable": True, "isAiGenerated": True}})
        self.assertEqual(p["assets"], [{"video": {"url": "https://pub.example/v.mp4"}}])
        self.assertEqual(p["mode"], "shareNow")

    def test_not_ai_and_missing_title(self):
        p = bs.build_input(self.YT, "d", "https://pub.example/v.mp4", bs.parse_when("draft"), ai_generated=False, title="T?")
        self.assertFalse(p["metadata"]["youtube"]["isAiGenerated"])
        with self.assertRaises(SystemExit):
            bs.build_input(self.YT, "d", "https://pub.example/v.mp4", bs.parse_when("now"))

    def test_made_for_kids_and_title_override(self):
        p = bs.build_input(self.YT, "d", "https://pub.example/v.mp4", bs.parse_when("now"), title="T?", made_for_kids=True)
        self.assertTrue(p["metadata"]["youtube"]["madeForKids"])
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "caption.txt").write_text("Derived question? More.\n", encoding="utf-8")
            (Path(d) / "first-comment.txt").write_text("Sources\n", encoding="utf-8")
            self.assertEqual(bs.youtube_texts(Path(d))[0], "Derived question?")
            self.assertEqual(bs.youtube_texts(Path(d), "Approved by hand")[0], "Approved by hand")
            with self.assertRaises(SystemExit):
                bs.youtube_texts(Path(d), "t" * 101)

    def test_dry_run_post_reads_the_manifest_keys_the_autopublisher_writes(self):
        with tempfile.TemporaryDirectory() as d:
            pub = Path(d) / "publish"
            (pub / "youtube").mkdir(parents=True)
            video = pub / "v.mp4"
            video.write_bytes(b"x")
            manifest = pub / "youtube" / "manifest.json"
            manifest.write_text(json.dumps({"slides": [{"index": 1, "type": "video", "path": str(video)}], "youtube_title": "Saan galing ang bagyo?",
                                            "youtube_description": "the description", "youtube_made_for_kids": True}))

            class FakeBuf:
                def __init__(self, *_):
                    pass

                def channels(self):
                    return CHANNELS + [YoutubePayloadTests.YT]

            args = bs.build_parser().parse_args(["post", str(manifest), "--channel", "c9", "--when", "now", "--gap-hours", "0", "--dry-run"])
            out = io.StringIO()
            with patch.object(bs, "Buffer", FakeBuf), patch.object(bs, "create_post", side_effect=AssertionError("dry run")), redirect_stdout(out):
                self.assertEqual(bs.cmd_post(args), 0)
            self.assertIn('"title": "Saan galing ang bagyo?"', out.getvalue())
            self.assertIn('"madeForKids": true', out.getvalue())
            self.assertIn('"text": "the description"', out.getvalue())
            self.assertFalse((manifest.parent / bs.REPORT_NAME).exists())          # the report sits beside the manifest, and a dry run writes nothing

    def test_a_youtube_post_passes_the_readback_check_when_the_video_is_kept(self):
        p = bs.build_input(self.YT, "d", "https://pub.example/v.mp4", bs.parse_when("now"), title="T?")
        self.assertIsNone(bs.post_problem(p, {"assets": [{"id": None, "source": "u"}]}))
        self.assertIn("0 of 1 assets", bs.post_problem(p, {"assets": []}))

    def test_dry_run_post_uses_the_episode_files_and_never_creates(self):
        with tempfile.TemporaryDirectory() as d:
            pub = Path(d) / "publish"
            (pub / "social").mkdir(parents=True)
            video = pub / "v.mp4"
            video.write_bytes(b"x")
            (pub / "caption.txt").write_text("Why is it so?\n\nBecause.\n", encoding="utf-8")
            (pub / "first-comment.txt").write_text("Sources\n- s\n", encoding="utf-8")
            manifest = pub / "social" / "manifest.json"
            manifest.write_text(json.dumps({"slides": [{"index": 1, "type": "video", "path": str(video)}], "tiktok_caption": "tt #a"}))

            class FakeBuf:
                def __init__(self, *_):
                    pass

                def channels(self):
                    return CHANNELS + [YoutubePayloadTests.YT]

            args = bs.build_parser().parse_args(["post", str(manifest), "--channel", "youtube:vibecoders", "--when", "now", "--gap-hours", "0", "--dry-run"])
            out = io.StringIO()
            with patch.object(bs, "Buffer", FakeBuf), patch.object(bs, "create_post", side_effect=AssertionError("dry run")), redirect_stdout(out):
                self.assertEqual(bs.cmd_post(args), 0)
            self.assertIn('"title": "Why is it so?"', out.getvalue())
            self.assertIn("Because.\\n\\nSources", out.getvalue())
            self.assertIn('"privacy": "public"', out.getvalue())


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

    def test_drafts_and_cancelled_posts_do_not_block_a_real_post(self):
        with tempfile.TemporaryDirectory() as d:
            manifest = Path(d) / "manifest.json"
            video = Path(d) / "v.mp4"
            video.write_bytes(b"x")
            manifest.write_text(json.dumps({"slides": [{"index": 1, "type": "video", "path": str(video)}], "tiktok_caption": "hi #a"}))
            (Path(d) / bs.REPORT_NAME).write_text(json.dumps({"posts": [
                {"channel": {"id": "c1", "service": "tiktok", "name": "x"}, "post_id": "p1", "when": "draft"},
                {"channel": {"id": "c1", "service": "tiktok", "name": "x"}, "post_id": "p2", "when": "now", "cancelled": "deleted"}]}))

            class FakeBuf:
                def __init__(self, *_):
                    pass

                def channels(self):
                    return CHANNELS

            args = bs.build_parser().parse_args(["post", str(manifest), "--channel", "tiktok:vibecoders", "--when", "draft", "--dry-run"])
            with patch.object(bs, "Buffer", FakeBuf), patch.object(bs, "create_post", side_effect=AssertionError("dry run")):
                self.assertEqual(bs.cmd_post(args), 0)  # not skipped: reaches the payload print without creating

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
