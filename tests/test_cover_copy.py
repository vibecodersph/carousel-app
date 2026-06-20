import unittest
from unittest.mock import patch
from pathlib import Path

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


    @patch("build_x_carousel.gemini_generate_content")
    @patch("build_x_carousel.gemini_text_model", return_value="gemini-2.5-flash")
    @patch("pathlib.Path.exists", return_value=True)
    @patch("pathlib.Path.read_bytes", return_value=b"fake_image_bytes")
    def test_gemini_describe_image_constructs_multimodal_payload(self, mock_read, mock_exists, mock_model, mock_generate):
        mock_generate.return_value = {
            "candidates": [
                {
                    "content": {
                        "parts": [{"text": "A beautiful remixed landscape"}]
                    }
                }
            ]
        }
        
        desc = build_x_carousel.gemini_describe_image(Path("some_image.png"), "fake_api_key")
        
        self.assertEqual(desc, "A beautiful remixed landscape")
        mock_generate.assert_called_once()
        payload = mock_generate.call_args[0][2]
        self.assertIn("inlineData", payload["contents"][0]["parts"][1])
        inline_data = payload["contents"][0]["parts"][1]["inlineData"]
        self.assertEqual(inline_data["mimeType"], "image/png")
        self.assertIn("data", inline_data)

    @patch("build_x_carousel.openai_api_key", return_value="fake_openai_key")
    @patch("build_x_carousel.gemini_describe_image", return_value="a glowing orange circle on paper")
    @patch("build_x_carousel.generate_openai")
    @patch("build_x_carousel.generated_openai_image_path", return_value=Path("out/cover.png"))
    def test_generate_openai_topic_image_with_remix(self, mock_path, mock_generate_openai, mock_describe, mock_openai_key):
        def exists_side_effect(self_obj):
            if "original_og.png" in str(self_obj):
                return True
            return False

        with patch("pathlib.Path.exists", new=exists_side_effect):
            path, prompt = build_x_carousel.generate_openai_topic_image(
                topic="Test Remix",
                companies=[],
                ceos=[],
                source_people=[],
                analysis=None,
                out_dir=Path("out"),
                article_image_path=Path("original_og.png")
            )
            
            mock_describe.assert_called_once_with(Path("original_og.png"), build_x_carousel.gemini_api_key())
            self.assertIn("Visual concept based on: a glowing orange circle on paper", prompt)
            mock_generate_openai.assert_called_once_with(prompt, Path("out/cover.png"), model="gpt-image-2", size="2048x1152")


if __name__ == "__main__":
    unittest.main()
