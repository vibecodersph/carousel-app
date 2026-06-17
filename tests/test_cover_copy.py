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


if __name__ == "__main__":
    unittest.main()
