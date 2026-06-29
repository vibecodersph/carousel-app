import json
import unittest
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
                                                "headline": "Akala mo agent, intern pala na may [browser].",
                                                "accent_word": "browser",
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


if __name__ == "__main__":
    unittest.main()
