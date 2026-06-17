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


if __name__ == "__main__":
    unittest.main()
