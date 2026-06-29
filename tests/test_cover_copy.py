import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import build_x_carousel


class CoverCopyTests(unittest.TestCase):
    def test_preserves_explicit_japanese_accent(self) -> None:
        headline = "本番エージェント、[足場]作りで止まっていませんか"

        self.assertEqual(build_x_carousel.bracket_single_accent_word(headline), headline)

    def test_can_insert_japanese_accent_word(self) -> None:
        headline = "本番エージェント、足場作りで止まっていませんか"

        self.assertEqual(
            build_x_carousel.bracket_single_accent_word(headline, "足場"),
            "本番エージェント、[足場]作りで止まっていませんか",
        )

    def test_can_accent_ascii_token_embedded_in_japanese_text(self) -> None:
        headline = "1.5TBの怪物、MITライセンスで誰でも使えます"

        self.assertEqual(
            build_x_carousel.bracket_single_accent_word(headline),
            "1.5TBの怪物、[MIT]ライセンスで誰でも使えます",
        )

    def test_headline_markup_supports_japanese_accent(self) -> None:
        markup, plain, has_accent = build_x_carousel.headline_markup_from_brackets(
            "本番エージェント、[足場]作りで止まっていませんか"
        )

        self.assertTrue(has_accent)
        self.assertEqual(plain, "本番エージェント、足場作りで止まっていませんか")
        self.assertIn('<span class="accent">足場</span>', markup)
        self.assertIn('<span class="term">本番エージェント</span>', markup)

    def test_headline_markup_supports_multiple_highlights(self) -> None:
        markup, plain, has_accent = build_x_carousel.headline_markup_from_brackets(
            "エンジニアや[PM]の肩書が[溶ける]と、何が残る？"
        )

        self.assertTrue(has_accent)
        self.assertEqual(plain, "エンジニアやPMの肩書が溶けると、何が残る？")
        self.assertEqual(markup.count('class="accent"'), 2)
        self.assertIn('<span class="accent">PM</span>', markup)
        self.assertIn('<span class="accent">溶ける</span>', markup)

    def test_cover_highlights_are_clamped_to_half_the_headline(self) -> None:
        headline = build_x_carousel.bracket_single_accent_word(
            "[Ganito mag-survive kapag tunaw] na ang role mo."
        )
        highlighted = sum(
            build_x_carousel.visible_highlight_len(match)
            for match in build_x_carousel.BRACKETED_HIGHLIGHT_RE.findall(headline)
        )
        total = build_x_carousel.visible_highlight_len(headline)

        self.assertLessEqual(highlighted, total // 2)
        self.assertNotEqual(headline, "[Ganito mag-survive kapag tunaw] na ang role mo.")

    def test_japanese_channel_uses_japanese_swipe_cue(self) -> None:
        with patch.dict("os.environ", {"CAROUSEL_CHANNEL": "aibrief_jp"}):
            self.assertEqual(build_x_carousel.dot_markup(2, 5), "<span>スワイプで続きへ</span>")

    def test_non_org_author_can_trigger_profile_image_fetch(self) -> None:
        post = {"author": "Elvis", "handle": "elvissun", "profile_image_url": ""}

        self.assertTrue(build_x_carousel.source_profile_candidate_post(post))

    def test_unknown_person_source_requires_real_profile_image(self) -> None:
        post = {
            "author": "Elvis",
            "handle": "elvissun",
            "profile_image_url": "https://pbs.twimg.com/profile_images/1234/avatar_normal.jpg",
        }

        self.assertTrue(build_x_carousel.is_person_source_post(post))
        self.assertIsNotNone(build_x_carousel.source_person_from_post(post))

    def test_org_source_does_not_trigger_profile_fallback(self) -> None:
        post = {"author": "OpenAI", "handle": "openai", "profile_image_url": ""}

        self.assertFalse(build_x_carousel.source_profile_candidate_post(post))
        self.assertFalse(build_x_carousel.is_person_source_post(post))

    def test_title_analysis_prompt_uses_mrbeast_quality_bar(self) -> None:
        prompts: list[str] = []

        def fake_generate(model, api_key, payload, *, api_version, timeout):
            del model, api_key, api_version, timeout
            prompts.append(payload["contents"][0]["parts"][0]["text"])
            return {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": json.dumps(
                                        {
                                            "topic": "AI browser agent",
                                            "cover": {
                                                "kicker": "THE CATCH",
                                                "headline": "Akala mo [agent], intern pala na may [browser].",
                                                "accent_words": ["agent", "browser"],
                                                "swipe_line": "slide 2 yung sablay",
                                            },
                                            "instagram_caption": "Hook\n\nSource: https://x.com/a/status/1",
                                            "post_summaries": [],
                                            "companies": [],
                                        }
                                    )
                                }
                            ]
                        }
                    }
                ]
            }

        with patch.object(
            build_x_carousel, "gemini_generate_content", side_effect=fake_generate
        ), patch.object(
            build_x_carousel, "gemini_text_model", return_value="test-model"
        ), patch.object(
            build_x_carousel, "load_ig_voice_prompt", return_value="voice"
        ):
            build_x_carousel.gemini_title_analysis(
                [
                    {
                        "author": "Builder",
                        "handle": "builder",
                        "text": "An AI browser agent completed 3 of 10 tasks and failed on checkout.",
                        "url": "https://x.com/a/status/1",
                    }
                ],
                "AI browser agent",
                "test-key",
            )

        prompt = prompts[0]
        self.assertIn("MrBeast-grade thumbnail-title pair", prompt)
        self.assertIn("internally draft at least 6 options", prompt)
        self.assertIn("Would a stranger understand the", prompt)
        self.assertIn("Make slide 2 feel necessary", prompt)
        self.assertIn("If the hook could work unchanged for 20 other AI posts", prompt)
        self.assertIn("at most 50% of the visible", prompt)
        self.assertIn('"image_brief"', prompt)
        self.assertIn("one concrete visual metaphor", prompt)

    def test_title_image_prompt_uses_image_brief(self) -> None:
        prompt = build_x_carousel.title_image_prompt(
            "Future of Tech Roles",
            [],
            [],
            [],
            {
                "image_brief": {
                    "core_tension": "Traditional product roles are dissolving into fluid archetypes.",
                    "visual_metaphor": "A single office nameplate melting into five small architectural blocks.",
                    "visual_extreme": "One vast title marker set against tiny new role blocks.",
                    "unresolved_moment": "The marker is mid-dissolve while the blocks cross a threshold.",
                    "avoid": ["screens", "org charts", "robots"],
                }
            },
        )

        self.assertIn("Image brief for the symbolic metaphor", prompt)
        self.assertIn("Traditional product roles are dissolving", prompt)
        self.assertIn("office nameplate melting", prompt)
        self.assertIn("screens, org charts, robots", prompt)
        self.assertIn("the image brief is the richer context", prompt)

    def test_title_slide_art_fades_into_copy_area(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            build_x_carousel, "render_html_slide"
        ):
            image_path = Path(tmp) / "cover.png"
            image_path.write_bytes(b"fake image bytes")
            out_path = Path(tmp) / "slide_01.png"
            build_x_carousel.render_title_slide(
                {"text": "OpenAI splits a model into three tiers.", "url": "https://x.com/a/status/1"},
                out_path,
                1,
                None,
                {
                    "topic_image_path": str(image_path),
                    "cover_copy": {"headline": "Bakit [hinati sa tatlo] ang GPT?"},
                },
                "vibecodersph",
            )
            html_text = out_path.with_suffix(".html").read_text()

        self.assertIn('class="slide title-slide"', html_text)
        self.assertIn("background-image: url(file://", html_text)
        self.assertIn(f"height: {build_x_carousel.TITLE_VISUAL_HEIGHT}px", html_text)
        self.assertIn("mask-image: linear-gradient", html_text)
        self.assertIn("rgba(var(--bg-rgb), 0) 58%", html_text)
        self.assertIn("rgba(var(--bg-rgb), 0.36) 100%", html_text)
        self.assertNotIn("var(--bg) 88%", html_text)
        self.assertIn("text-shadow:", html_text)
        self.assertIn(f"font-weight: {build_x_carousel.TITLE_HEADLINE_WEIGHT}", html_text)
        self.assertIn(f"transform: scaleY({build_x_carousel.TITLE_HEADLINE_SCALE_Y})", html_text)


if __name__ == "__main__":
    unittest.main()
