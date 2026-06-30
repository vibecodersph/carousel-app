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
        self.assertIn("16:9 horizontal", prompt)
        self.assertIn("lower 38% should be quiet warm background", prompt)
        self.assertIn("quiet vanishing gradient", prompt)
        self.assertNotIn("Do not create a fade, vanishing gradient", prompt)
        self.assertNotIn("directly over the artwork", prompt)
        self.assertIn("Traditional product roles are dissolving", prompt)
        self.assertIn("office nameplate melting", prompt)
        self.assertIn("screens, org charts, robots", prompt)
        self.assertIn("the image brief is the richer context", prompt)

    def test_title_slide_art_uses_top_aligned_image_with_lower_fade(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            build_x_carousel, "render_html_slide"
        ):
            image_path = Path(tmp) / "cover.png"
            image_path.write_bytes(
                b"\x89PNG\r\n\x1a\n"
                + b"\x00\x00\x00\rIHDR"
                + (2048).to_bytes(4, "big")
                + (1152).to_bytes(4, "big")
                + b"\x08\x02\x00\x00\x00"
            )
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
        self.assertIn("inset: 0;", html_text)
        self.assertIn("height: 100%", html_text)
        self.assertIn('class="visual-card is-wide-art"', html_text)
        self.assertIn("background-position: center top", html_text)
        self.assertIn("background-size: 100% auto", html_text)
        self.assertIn("background-size: auto 63%", html_text)
        self.assertIn("mask-image: linear-gradient", html_text)
        self.assertIn("z-index: 3;\n  inset: 0;\n  background:", html_text)
        self.assertIn("z-index: 4;", html_text)
        self.assertIn("rgba(var(--bg-rgb), 0) 42%", html_text)
        self.assertIn("rgba(var(--bg-rgb), 0.82) 78%", html_text)
        self.assertIn("rgba(var(--bg-rgb), 0.78) 62%", html_text)
        self.assertIn("rgba(var(--bg-rgb), 0.98) 100%", html_text)
        self.assertNotIn("var(--bg) 88%", html_text)
        self.assertIn("text-shadow:", html_text)
        self.assertIn(f"font-weight: {build_x_carousel.TITLE_HEADLINE_WEIGHT}", html_text)
        self.assertIn(f"transform: scaleY({build_x_carousel.TITLE_HEADLINE_SCALE_Y})", html_text)

    def test_animated_title_slide_html_has_premium_motion_hooks(self) -> None:
        html_text = build_x_carousel.title_slide_html(
            {"text": "OpenAI ships a faster agent.", "url": "https://x.com/a/status/1"},
            4,
            None,
            {"cover_copy": {"headline": "Agent mo, [nagmadali] bigla.", "swipe_line": "See the receipts"}},
            "vibecodersph",
            animated=True,
        )

        self.assertIn('class="slide title-slide animated-cover"', html_text)
        self.assertIn('data-cover-animation="premium-still"', html_text)
        self.assertIn("cover-light", html_text)
        self.assertIn("cover-shadow", html_text)
        self.assertIn("cover-grain", html_text)
        self.assertIn("kinetic-title", html_text)
        self.assertIn("kinetic-token", html_text)
        self.assertIn("data-token-index", html_text)
        self.assertIn("--cover-progress", html_text)
        self.assertIn("__setCoverAnimationProgress", html_text)
        self.assertIn("__pauseCoverAnimation", html_text)
        self.assertIn("__fitCoverHeadline", html_text)
        self.assertIn("will-change: transform, opacity, filter", html_text)
        self.assertIn("easeInOutSine", html_text)
        self.assertIn("scaleX(", html_text)
        self.assertIn("typeResolve", html_text)
        self.assertIn("See the receipts", html_text)

    def test_long_text_motion_headlines_can_use_four_rows(self) -> None:
        lines = build_x_carousel.text_motion_headline_lines(
            "Bakit biglang hinati sa tatlo ang bagong GPT-5.6?"
        )

        self.assertEqual(len(lines), 4)
        self.assertIn("hinati sa tatlo", lines)
        self.assertIn("ang bagong", lines)
        self.assertIn("GPT-5.6?", lines[-1])

    def test_very_long_text_motion_headlines_use_balanced_five_rows(self) -> None:
        lines = build_x_carousel.text_motion_headline_lines(
            "Walang sponcon: Pinakamurang LLM stack na sinubukan ko gamit ang sariling pera"
        )

        self.assertEqual(len(lines), 5)
        self.assertIn("Pinakamurang", lines)
        self.assertIn("LLM stack na", lines)
        self.assertEqual(lines[-1], "ang sariling pera")

    def test_long_japanese_text_motion_headlines_use_balanced_rows(self) -> None:
        headline = "「セキュリティ審査、通る？」に[震えずに即答する]。グローバル基準を低コストで揃える最新スタック"
        plain, spans = build_x_carousel.bracket_highlight_spans(headline)
        lines = build_x_carousel.text_motion_headline_lines(plain)
        payload = build_x_carousel.text_motion_line_payload(plain, lines, spans)
        highlighted = "".join(
            str(line["text"])[start:end]
            for line in payload
            for start, end in line["highlights"]
        )

        self.assertEqual(len(lines), 5)
        self.assertLessEqual(max(len(line) for line in lines), 12)
        self.assertNotIn("」に震えずに即答する。グローバル基準を低コストで揃える最新スタック", lines)
        self.assertEqual(highlighted, "震えずに即答する")

    def test_text_motion_cover_keeps_brand_rule_and_animates_headline_lines(self) -> None:
        html_text = build_x_carousel.title_slide_html(
            {"text": "OpenAI ships a faster agent.", "url": "https://x.com/a/status/1"},
            4,
            None,
            {
                "cover_animation": "text-motion-lines",
                "cover_copy": {
                    "headline": "Agent mo, [nagmadali] bigla.",
                    "swipe_line": "See the receipts",
                },
            },
            "vibecodersph",
            animated=True,
        )

        self.assertIn('data-cover-animation="text-motion-lines"', html_text)
        self.assertIn('<div class="account-rule"><span>vibecodersph</span></div>', html_text)
        self.assertIn("cover-motion-lines", html_text)
        self.assertIn('aria-label="Agent mo, nagmadali bigla."', html_text)
        self.assertIn('data-line-index="0"', html_text)
        self.assertIn("&quot;highlights&quot;", html_text)
        self.assertIn("tm-viewport--embedded", html_text)
        self.assertIn("tm-letter--reverse", html_text)
        self.assertIn("reverse: isHighlightedWord", html_text)
        self.assertIn("wordsForLine", html_text)
        self.assertIn("window.TextMotion.createBoard", html_text)
        self.assertIn("__coverTextMotionLines", html_text)
        self.assertIn("__mountCoverTextMotionLines", html_text)
        self.assertIn("const TEXT_MOTION_FIT_SCALE = 1.4", html_text)
        self.assertIn("const TEXT_MOTION_ROW_SCALE = 0.86", html_text)
        self.assertIn("const TEXT_MOTION_DEFAULT_PULL = 50", html_text)
        self.assertIn("function motionLineText(line)", html_text)
        self.assertIn("function motionDefaultPull()", html_text)
        self.assertIn("fontSize * TEXT_MOTION_FIT_SCALE", html_text)
        self.assertIn("TEXT_MOTION_FIT_SCALE * TEXT_MOTION_ROW_SCALE", html_text)
        self.assertIn(f'cluster.style.top = "{build_x_carousel.TITLE_TEXT_TOP}px"', html_text)
        self.assertIn(f"const desiredTop = {build_x_carousel.TITLE_TEXT_DYNAMIC_BOTTOM} - blockHeight", html_text)
        self.assertIn(f"{build_x_carousel.TITLE_TEXT_MIN_TOP},", html_text)
        self.assertIn("cluster.style.top = `${Math.round(nextTop)}px`", html_text)
        self.assertIn("window.TextMotion.metricFor", html_text)
        self.assertIn("defaultPull: DEFAULT_PULL", html_text)
        self.assertIn("const pullSpace = motionDefaultPull()", html_text)
        self.assertIn("let low = isTextMotionCover ? 42 : 52", html_text)
        self.assertNotIn('<div class="cover-light"', html_text)
        self.assertNotIn('<div class="cover-shadow"', html_text)
        self.assertNotIn('<div class="cover-grain"', html_text)
        self.assertIn('"anchored-lead"', html_text)
        self.assertIn('const motionStyle = "anchored-lead"', html_text)
        self.assertIn("const motionScale = 1.4", html_text)
        self.assertIn("const motionRowScale = 0.86", html_text)
        self.assertIn("const motionLineDelayMs = 95", html_text)
        self.assertIn("const rowHeight = Math.ceil(fontSize * motionRowScale)", html_text)
        self.assertNotIn("activeMs: motionActiveMs", html_text)
        self.assertNotIn("outActiveMs: motionOutActiveMs", html_text)
        self.assertNotIn("returnActiveMs: motionReturnActiveMs", html_text)
        self.assertNotIn("pauseMs: motionPauseMs", html_text)
        self.assertNotIn("pull: motionPull", html_text)
        self.assertIn("board.setProgress(0, index * motionLineDelayMs)", html_text)
        self.assertIn("board.setProgress(p, index * motionLineDelayMs)", html_text)
        self.assertIn("const loopArc = Math.sin(p * Math.PI)", html_text)
        self.assertIn("const grainDrift = fixedFurniture ? loopEase : p", html_text)
        self.assertIn("const typeResolve = fixedFurniture ? loopEase", html_text)
        self.assertIn("if (bg && fixedFurniture)", html_text)
        self.assertIn('bg.style.transform = "none"', html_text)
        self.assertIn("if (fallback && fixedFurniture)", html_text)
        self.assertIn("if (avatar && fixedFurniture)", html_text)
        self.assertIn('motionLines.style.transform = "none"', html_text)
        self.assertIn('? "scaleY(" + headlineScaleY + ")"', html_text)
        self.assertIn('dots.style.transform = fixedFurniture ? "none"', html_text)
        self.assertIn(".text-motion-cover .account-rule", html_text)
        self.assertIn("margin-bottom: 46px", html_text)
        self.assertIn(".text-motion-cover .cover-light", html_text)
        self.assertIn(".text-motion-cover .cover-shadow", html_text)
        self.assertIn(".text-motion-cover .cover-grain", html_text)
        self.assertIn("display: none;", html_text)
        self.assertIn(f"top: {build_x_carousel.TITLE_TEXT_TOP}px", html_text)
        self.assertIn(".text-motion-cover .visual-card::after", html_text)
        self.assertIn(".text-motion-cover .visual-bg", html_text)
        self.assertIn(".text-motion-cover .visual-card.is-top-art .visual-bg", html_text)
        self.assertIn("background-size: auto 70%", html_text)
        self.assertIn("justify-content: flex-start", html_text)
        self.assertIn("mask-image: linear-gradient", html_text)
        self.assertIn("#000 46%, rgba(0, 0, 0, 0.72) 52%", html_text)
        self.assertIn("rgba(0, 0, 0, 0.24) 60%, transparent 68%", html_text)
        self.assertIn("rgba(var(--bg-rgb), 0) 42%, rgba(var(--bg-rgb), 0.28) 49%", html_text)
        self.assertIn("rgba(var(--bg-rgb), 0.95) 64%, var(--bg) 78%", html_text)
        self.assertNotIn("rgba(var(--bg-rgb), 0.94) 0%", html_text)
        self.assertIn("--tm-style-two: var(--bg)", html_text)
        self.assertIn("--tm-style-two-bg: var(--primary)", html_text)
        self.assertIn("--tm-style-two-bg-inset-y: 0.018em", html_text)
        self.assertIn("--tm-style-two-bg-inset-x: 0.014em", html_text)
        self.assertIn("inset: var(--tm-style-two-bg-inset-y, 0.018em) var(--tm-style-two-bg-inset-x, 0.014em)", html_text)
        self.assertNotIn("inset: -0.04em -0.035em 0.02em", html_text)
        self.assertNotIn("const motionStyles =", html_text)
        self.assertNotIn("motionStyles[index % motionStyles.length]", html_text)
        self.assertNotIn("display: none;\n  position: absolute;\n  inset: 0;", html_text)
        self.assertIn("Agent mo", html_text)
        self.assertIn("nagmadali bigla.", html_text)
        self.assertNotIn('class="headline-static"', html_text)
        self.assertNotIn("visibility: hidden", html_text)

    def test_top_art_cover_fits_background_image_above_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "cover.png"
            image_path.write_bytes(b"cover")
            html_text = build_x_carousel.title_slide_html(
                {"text": "OpenAI ships a faster agent.", "url": "https://x.com/a/status/1"},
                4,
                None,
                {
                    "cover_animation": "text-motion-lines",
                    "topic_image_path": image_path,
                    "image_composition": "top_art",
                    "cover_copy": {
                        "headline": "Agent mo, [nagmadali] bigla.",
                        "swipe_line": "See the receipts",
                    },
                },
                "vibecodersph",
                animated=True,
            )

        self.assertIn('class="visual-card is-top-art"', html_text)
        self.assertIn('<div class="visual-bg" style="background-image:', html_text)
        self.assertIn(".visual-card.is-top-art .visual-bg", html_text)
        self.assertIn("background: linear-gradient(180deg, var(--bg-top) 0%, var(--bg) 100%)", html_text)
        self.assertIn("inset: 0;", html_text)
        self.assertIn("background-position: center top", html_text)
        self.assertIn("background-size: 100% auto", html_text)
        self.assertIn("mask-image: linear-gradient", html_text)
        self.assertIn(".visual-card.is-top-art .visual-fallback", html_text)
        self.assertNotIn(".visual-card.is-top-art::after {{\n  display: none;", html_text)
        self.assertIn("display: none;", html_text)
        self.assertNotIn("visual-spot-art", html_text)
        self.assertNotIn("is-spot-art", html_text)
        self.assertNotIn("cover-motion-brand", html_text)
        self.assertNotIn('<div class="kinetic-title"', html_text)

    def test_cover_poster_path_matches_mp4_cover_name(self) -> None:
        poster = build_x_carousel.cover_poster_path(Path("/tmp/slide_01.mp4"))

        self.assertEqual(poster, Path("/tmp/slide_01_poster.png"))


if __name__ == "__main__":
    unittest.main()
