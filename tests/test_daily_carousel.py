import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import build_daily_carousel
from channel import load_channel


class DailyCarouselTemplateTests(unittest.TestCase):
    def test_vibecodersph_channel_has_local_voice_guide(self) -> None:
        channel = load_channel("vibecodersph")

        self.assertEqual(channel.brand_name, "VibeCoders PH")
        self.assertTrue(channel.voice_doc and channel.voice_doc.exists())
        self.assertIn("text-free cover photo", channel.voice_prompt)

    def test_cover_template_owns_branding_and_story_list(self) -> None:
        channel = load_channel("vibecodersph")
        headline_html, headline_text = build_daily_carousel._headline_with_accent(
            "OpenAI may bagong [control]"
        )
        stories = [
            {"title": "OpenAI adds enterprise spend controls"},
            {"title": "Grok arrives inside Microsoft Word"},
            {"title": "Codex can replay recorded workflows"},
            {"title": "FEU Tech partners with OpenAI"},
            {"title": "Claude 4 Cyber research lands"},
        ]

        html = build_daily_carousel._cover_slide_html(
            channel,
            headline_html,
            headline_text,
            "May practical AI updates today",
            "swipe mo",
            stories,
        )

        self.assertIn("@vibecodersph", html)
        self.assertIn("OpenAI", html)
        self.assertIn("Also in this drop", html)
        self.assertIn("Grok arrives inside Microsoft Word", html)
        self.assertIn("01 / 07", html)
        self.assertIn("color: var(--primary)", html)
        self.assertNotIn("#E040FB", html)
        self.assertNotIn("magenta", html.lower())

    def test_cover_template_uses_voice_rewritten_story_titles(self) -> None:
        channel = load_channel("vibecodersph")
        headline_html, headline_text = build_daily_carousel._headline_with_accent(
            "OpenAI may bagong [control]"
        )
        stories = [
            {"title": "OpenAI adds enterprise spend controls", "desc": "desc"},
            {"title": "Grok arrives inside Microsoft Word", "desc": "desc"},
            {"title": "Codex can replay recorded workflows", "desc": "desc"},
            {"title": "FEU Tech partners with OpenAI", "desc": "desc"},
            {"title": "Claude 4 Cyber research lands", "desc": "desc"},
        ]
        voice = {
            "stories": [
                {"n": 1, "headline": "OpenAI may bagong gastos control", "body": "Body"},
                {"n": 2, "headline": "Grok nasa Microsoft Word na", "body": "Body"},
                {"n": 3, "headline": "Codex may replay para sa workflows", "body": "Body"},
                {"n": 4, "headline": "FEU Tech may OpenAI campus push", "body": "Body"},
                {"n": 5, "headline": "Claude 4 Cyber may bagong research", "body": "Body"},
            ]
        }

        display_stories = build_daily_carousel._display_stories_for_render(stories, voice)
        html = build_daily_carousel._cover_slide_html(
            channel,
            headline_html,
            headline_text,
            "May practical AI updates today",
            "swipe mo",
            display_stories,
        )

        self.assertIn("Grok nasa Microsoft Word na", html)
        self.assertIn("Codex may replay para sa workflows", html)
        self.assertNotIn("Grok arrives inside Microsoft Word", html)

    def test_taglish_voice_guard_rejects_straight_english_headlines(self) -> None:
        channel = load_channel("vibecodersph")
        data = {
            "cover_headline": "OpenAI may bagong [control]",
            "cover_subtitle": "May practical AI updates today",
            "cover_swipe_line": "swipe mo",
            "instagram_caption": "May updates para sa builders.",
            "stories": [
                {"n": 1, "headline": "OpenAI launches new spend controls", "body": "Body"},
                {"n": 2, "headline": "Grok arrives inside Microsoft Word", "body": "Body"},
            ],
        }

        issues = build_daily_carousel._daily_voice_issues(data, 2, channel)

        self.assertIn("story 1 headline is not Taglish enough", issues)
        self.assertIn("story 2 headline is not Taglish enough", issues)

    def test_daily_drop_caption_title_and_hashtag_cap(self) -> None:
        title = build_daily_carousel._daily_drop_caption_title("2026-07-28")
        raw = (
            "Hook muna: may agent news na dapat bantayan.\n\n"
            "Source links are in the comments. Save this.\n\n"
            "#AI #Tech #News #Philippines #VibeCodersPH #Extra"
        )

        caption = build_daily_carousel._cap_hashtags(
            build_daily_carousel._ensure_caption_title(raw, title),
            max_tags=5,
        )

        self.assertTrue(caption.startswith("🌀 The Daily Drop, July 28, 2026\n\n"))
        self.assertIn("\n\nSource links are in the comments", caption)
        self.assertEqual(caption.count("#"), 5)
        self.assertNotIn("#Extra", caption)

    def test_vcph_image_build_requires_generated_cover_photo(self) -> None:
        channel = load_channel("vibecodersph")
        stories = [
            {
                "source": "OpenAI News",
                "title": "OpenAI adds enterprise spend controls",
                "desc": "Enterprises can track usage and budgets.",
                "link": "",
            }
        ]

        with TemporaryDirectory() as tmp:
            with patch("build_daily_carousel._generate_cover_image", return_value=None):
                with self.assertRaisesRegex(RuntimeError, "GPT Image 2.0 cover photo"):
                    build_daily_carousel.build_daily_carousel(
                        stories,
                        channel,
                        voice=None,
                        out_dir=Path(tmp),
                        use_images=True,
                    )

    def test_daily_builder_uses_daily_cta_template(self) -> None:
        channel = load_channel("vibecodersph")
        stories = [
            {
                "source": "OpenAI News",
                "title": "OpenAI adds enterprise spend controls",
                "desc": "Enterprises can track usage and budgets.",
                "link": "",
            }
        ]

        with TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            with patch("build_daily_carousel.render_html_slide", side_effect=lambda html_path, out_path: Path(out_path).write_bytes(b"png")):
                manifest_path = build_daily_carousel.build_daily_carousel(
                    stories,
                    channel,
                    voice=None,
                    out_dir=out_dir,
                    use_images=False,
                )

            cta_html = (out_dir / "slide_03.html").read_text(encoding="utf-8")
            self.assertTrue(manifest_path.exists())
            self.assertIn("cta-progress", cta_html)
            self.assertIn("@vibecodersph", cta_html)
            self.assertIn("Na-save", cta_html)

    def test_daily_drop_cover_uses_gpt_image_2_for_cover_photo(self) -> None:
        import daily_drop_cover

        class FakeImages:
            def __init__(self):
                self.kwargs = None

            def generate(self, **kwargs):
                self.kwargs = kwargs

                class Data:
                    b64_json = "ZmFrZQ=="

                class Response:
                    data = [Data()]

                return Response()

        class FakeClient:
            instance = None

            def __init__(self, api_key):
                self.api_key = api_key
                self.images = FakeImages()
                FakeClient.instance = self

        stories = [
            {"headline": f"Story {i} headline", "blurb": "A useful AI update for builders."}
            for i in range(1, 6)
        ]

        with TemporaryDirectory() as tmp, patch.dict("os.environ", {"OPENAI_API_KEY": "test", "OPENAI_IMAGE_MODEL": "not-gpt-image-2"}), patch("openai.OpenAI", FakeClient), patch("daily_drop_cover._commit_issue_number"):
            out_path = Path(tmp) / "cover.jpg"
            result = daily_drop_cover.generate_full_cover(
                stories,
                hero_cover_line="OpenAI spend controls",
                cover_subject="A symbolic cover photo with burnt-orange light.",
                output_path=out_path,
                skip_logo_overlay=True,
                cover_size="1024x1280",
            )

        self.assertEqual(result, out_path)
        self.assertEqual(FakeClient.instance.images.kwargs["model"], "gpt-image-2")
        self.assertIn("burnt-orange", FakeClient.instance.images.kwargs["prompt"])
        self.assertIn("no purple", FakeClient.instance.images.kwargs["prompt"])


if __name__ == "__main__":
    unittest.main()
