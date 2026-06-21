import unittest
from unittest.mock import patch

import build_reel
from channel import Channel


def make_channel(**brand) -> Channel:
    return Channel(
        id="test",
        account_name="Test",
        brand_name="Test Brand",
        handle="@test",
        language_name="Japanese",
        audience="a test audience",
        brand=brand,
    )


class MediaRectTests(unittest.TestCase):
    def test_landscape_fits_width_and_top_aligns(self) -> None:
        rx, ry, rw, rh = build_reel.MEDIA_REGION
        mx, my, mw, mh = build_reel.media_rect(854, 480)
        self.assertEqual(mw, rw)  # width-bound
        self.assertLess(mh, rh)
        self.assertEqual(my, ry)  # top-aligned: fixed gap below the headline
        # aspect preserved (no distortion) within rounding
        self.assertAlmostEqual(mw / mh, 854 / 480, places=1)

    def test_all_orientations_share_the_same_video_top(self) -> None:
        _, ry, _, _ = build_reel.MEDIA_REGION
        for w, h in [(854, 480), (1080, 1920), (720, 720)]:
            self.assertEqual(build_reel.media_rect(w, h)[1], ry)

    def test_portrait_fits_height_and_centres_horizontally(self) -> None:
        rx, ry, rw, rh = build_reel.MEDIA_REGION
        mx, my, mw, mh = build_reel.media_rect(1080, 1920)
        self.assertEqual(mh, rh)  # height-bound
        self.assertLess(mw, rw)
        self.assertAlmostEqual(mx, rx + (rw - mw) // 2)

    def test_dimensions_are_even_for_yuv420p(self) -> None:
        for w, h in [(854, 480), (1080, 1920), (640, 641), (333, 777)]:
            _, _, mw, mh = build_reel.media_rect(w, h)
            self.assertEqual(mw % 2, 0)
            self.assertEqual(mh % 2, 0)


class CleanHeadlineTests(unittest.TestCase):
    def test_takes_first_line_and_strips_quotes(self) -> None:
        self.assertEqual(build_reel.clean_headline('"AIが世界を変える"\nsecond'), "AIが世界を変える")

    def test_strips_label_prefix_and_trailing_punctuation(self) -> None:
        self.assertEqual(build_reel.clean_headline("Headline: The big reveal."), "The big reveal")

    def test_collapses_whitespace(self) -> None:
        self.assertEqual(build_reel.clean_headline("  too    many   spaces  "), "too many spaces")


class HeadlineSizeTests(unittest.TestCase):
    def test_shrinks_as_text_grows(self) -> None:
        self.assertGreater(build_reel.headline_size("short"), build_reel.headline_size("x" * 120))


class HandleSlugTests(unittest.TestCase):
    def test_strips_at_and_lowercases(self) -> None:
        self.assertEqual(build_reel.handle_slug("@HighSignal_AI"), "highsignal_ai")

    def test_falls_back_when_empty(self) -> None:
        self.assertEqual(build_reel.handle_slug("@@@"), "source")


class ReelThemeTests(unittest.TestCase):
    def test_defaults_when_channel_has_no_reel_block(self) -> None:
        self.assertEqual(build_reel.reel_theme(make_channel()), build_reel.DEFAULT_THEME)

    def test_channel_overrides_merge_over_defaults(self) -> None:
        theme = build_reel.reel_theme(make_channel(reel={"background": "#FFFFFF"}))
        self.assertEqual(theme["background"], "#FFFFFF")
        self.assertEqual(theme["text"], build_reel.DEFAULT_THEME["text"])


class GenerateHeadlineTests(unittest.TestCase):
    def test_override_is_used_verbatim_after_cleaning(self) -> None:
        out = build_reel.generate_headline(make_channel(), "ignored post text", '"Hand hook"')
        self.assertEqual(out, "Hand hook")

    def test_falls_back_to_clamped_post_text_without_credentials(self) -> None:
        with patch.object(build_reel, "resolve_xai_token", return_value=""):
            out = build_reel.generate_headline(make_channel(), "word " * 60, None)
        self.assertTrue(out.endswith("…"))
        self.assertLessEqual(len(out), 95)

    def test_does_not_call_model_when_override_present(self) -> None:
        with patch.object(build_reel, "xai_responses_text") as mocked:
            build_reel.generate_headline(make_channel(), "post", "my hook")
            mocked.assert_not_called()


if __name__ == "__main__":
    unittest.main()
