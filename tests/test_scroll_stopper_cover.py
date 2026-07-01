import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import scroll_stopper_cover as cover


def normalized_request(**overrides):
    request = {
        "topic": "How to make Instagram carousels get more saves",
        "audience": "solo creators and marketers",
        "carouselPromise": "Teach 5 first-slide fixes that increase saves and swipes",
    }
    request.update(overrides)
    return cover.normalize_request(request)


def basic_strategy(**overrides):
    strategy = {
        "mainHook": "Stop Killing Your Saves",
        "subHook": "Your first slide is doing this",
        "contentPromise": "5 cover fixes that make people stop and swipe",
        "audienceRelevance": "Creators who publish carousels but get low saves",
        "visualCategory": "mistake",
        "signal": {"type": "huge_text", "description": "Huge warning type"},
        "stakes": {"type": "loss", "description": "The viewer may be losing saves."},
        "curiosityGap": "What exactly on the first slide is killing saves?",
        "focalPoint": {"description": "One dominant headline", "preferredPosition": "center"},
        "eyePath": ["headline", "accent", "subhook"],
        "patternInterrupt": "One marked object breaks the calm layout.",
        "humanCue": {"use": False},
        "motionPlan": None,
    }
    strategy.update(overrides)
    return strategy


class ScrollStopperTemplateTests(unittest.TestCase):
    def test_transformation_strategy_returns_before_after_split(self):
        request = normalized_request()
        strategy = basic_strategy(visualCategory="transformation")

        self.assertEqual(cover.choose_template(strategy, request), "before_after_split")

    def test_proof_strategy_returns_proof_receipt(self):
        request = normalized_request()
        strategy = basic_strategy(visualCategory="proof")

        self.assertEqual(cover.choose_template(strategy, request), "proof_receipt")

    def test_human_cue_allowed_returns_face_template(self):
        request = normalized_request(creativeDirection={"allowHumanFace": True})
        strategy = basic_strategy(humanCue={"use": True, "emotion": "concern"})

        self.assertEqual(cover.choose_template(strategy, request), "face_reaction_object")

    def test_human_cue_disabled_does_not_return_face_template(self):
        request = normalized_request(creativeDirection={"allowHumanFace": False})
        strategy = basic_strategy(humanCue={"use": True, "emotion": "concern"})

        self.assertNotEqual(cover.choose_template(strategy, request), "face_reaction_object")

    def test_kinetic_preference_can_return_kinetic_reveal(self):
        request = normalized_request(creativeDirection={"motionIntensity": "kinetic"})
        strategy = basic_strategy(visualCategory="identity", signal={"type": "huge_text", "description": "Big type"})

        self.assertEqual(cover.choose_template(strategy, request), "kinetic_reveal")


class ScrollStopperScoringTests(unittest.TestCase):
    def test_rejects_unreadable_long_headline(self):
        request = normalized_request()
        strategy = basic_strategy(
            mainHook="This is a very long headline that asks the viewer to read a paragraph before understanding"
        )

        self.assertLess(cover.score_mobile_readability(strategy, request), 10)

    def test_penalizes_low_contrast(self):
        request = normalized_request()
        css = ".ssc-cover { --ssc-fg: #FFFFFF; --ssc-bg: #FFFFFF; }"

        self.assertLess(cover.score_value_contrast(css, request), 8)

    def test_rewards_strong_curiosity_gap(self):
        strategy = basic_strategy(curiosityGap="What exactly changed between the two covers?")

        self.assertGreaterEqual(cover.score_curiosity(strategy), 10)

    def test_penalizes_multiple_competing_focal_points(self):
        strategy = basic_strategy(
            focalPoint={"description": "Many focal points compete for attention", "preferredPosition": "center"},
            eyePath=["face", "headline", "object", "sticker", "chart"],
        )

        self.assertLess(cover.score_strategy_focal(strategy), 10)

    def test_no_human_strategy_gets_neutral_human_score(self):
        self.assertEqual(cover.score_human(basic_strategy(humanCue={"use": False})), 7)

    def test_penalizes_decorative_only_motion(self):
        strategy = basic_strategy(
            motionPlan={
                "intensity": "kinetic",
                "firstFrameHook": "Visible",
                "timeline": [{"target": "accent", "action": "float", "startMs": 0, "durationMs": 900}],
                "loop": [{"target": "accent", "action": "pulse", "startMs": 900, "durationMs": 2000}],
            }
        )

        self.assertLess(cover.score_motion(strategy), 5)


class ScrollStopperRenderTests(unittest.TestCase):
    def test_rendered_html_escapes_user_text_and_has_no_scripts(self):
        request = normalized_request()
        strategy = basic_strategy(
            mainHook="<script>alert(1)</script> Saves",
            subHook="A & B <b>should be text</b>",
        )

        html_text, css = cover.render_cover_html(
            strategy=strategy,
            template_id="mistake_warning",
            assets=[],
            request=request,
        )

        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html_text)
        self.assertNotIn("<script", html_text.lower())
        self.assertIn("aria-label=", html_text)
        self.assertIn(".ssc-cover", css)
        self.assertIn("prefers-reduced-motion", css)
        class_values = re.findall(r'class="([^"]+)"', html_text)
        self.assertTrue(class_values)
        self.assertTrue(
            all(part.startswith("ssc-") for value in class_values for part in value.split())
        )

    def test_rendered_html_includes_image_alt_text_when_asset_has_url(self):
        request = normalized_request()
        strategy = basic_strategy(humanCue={"use": True, "emotion": "concern"})
        assets = [
            {
                "id": "asset",
                "kind": "uploaded_image",
                "url": "https://example.com/face.png",
                "alt": "Concerned creator looking at the warning.",
                "source": "user",
                "placement": {"role": "face", "x": 0, "y": 0, "width": 1, "height": 1, "zIndex": 1},
            }
        ]

        html_text, _css = cover.render_cover_html(
            strategy=strategy,
            template_id="face_reaction_object",
            assets=assets,
            request=request,
        )

        self.assertIn('alt="Concerned creator looking at the warning."', html_text)

    def test_kinetic_render_includes_motion_graphic_layers(self):
        request = normalized_request(creativeDirection={"motionIntensity": "kinetic"})
        strategy = basic_strategy(motionPlan=cover.motion_plan("kinetic", "kinetic"))

        html_text, css = cover.render_cover_html(
            strategy=strategy,
            template_id="pattern_break_grid",
            assets=[],
            request=request,
        )

        self.assertIn("ssc-motion-field", html_text)
        self.assertIn("ssc-word", html_text)
        self.assertIn("ssc-route-draw", css)
        self.assertIn("ssc-word-rubber", css)


class ScrollStopperImageAssetTests(unittest.TestCase):
    def test_image_prompt_for_face_is_safe_and_mentions_emotion_and_gaze(self):
        request = normalized_request(
            creativeDirection={"allowGeneratedImages": True, "allowHumanFace": True}
        )
        strategy = basic_strategy(
            humanCue={"use": True, "cueType": "face", "emotion": "concern", "gazeTarget": "headline"}
        )

        prompt = cover.build_image_asset_prompt(strategy, "face_reaction_object", request)

        self.assertIn("no text", prompt.lower())
        self.assertIn("no logos", prompt.lower())
        self.assertIn("no watermark", prompt.lower())
        self.assertIn("editable HTML", prompt)
        self.assertIn("concern", prompt)
        self.assertIn("headline", prompt)

    def test_asset_generation_is_skipped_when_generated_images_disallowed(self):
        request = normalized_request(creativeDirection={"allowGeneratedImages": False})
        strategy = basic_strategy(humanCue={"use": True, "emotion": "concern"})

        self.assertEqual(cover.plan_assets(strategy, "face_reaction_object", request), [])


class ScrollStopperPipelineTests(unittest.TestCase):
    def test_langgraph_budget_topic_gets_specific_token_hooks(self):
        request = normalized_request(
            topic="LangGraph token traps",
            carouselPromise="3 budget-killing LangGraph deal-breakers before deployment",
        )

        hooks = cover.generate_hook_candidates(request)

        self.assertIn("Stop Token Bleeding", hooks)
        self.assertIn("The LangGraph Cost Trap", hooks)

    def test_api_budget_topic_gets_specific_credit_hooks(self):
        request = normalized_request(
            topic="AI API budget picks",
            carouselPromise="3 AI APIs worth paying for, tested with actual pesos saved",
        )

        hooks = cover.generate_hook_candidates(request)

        self.assertIn("Stop Burning API Credits", hooks)
        self.assertIn("3 APIs Worth Paying For", hooks)

    def test_generate_returns_ranked_variants_and_artifacts(self):
        with TemporaryDirectory() as tmp:
            response = cover.generate_scroll_stopper_cover(
                {
                    "topic": "How to make Instagram carousels get more saves",
                    "audience": "solo creators and marketers",
                    "carouselPromise": "Teach 5 first-slide fixes that increase saves and swipes",
                    "creativeDirection": {"motionIntensity": "kinetic", "allowHumanFace": True},
                    "constraints": {"numberOfVariants": 4},
                },
                out_dir=Path(tmp),
                debug=True,
            )
            manifest_path = cover.write_response_artifacts(response, Path(tmp))
            manifest_exists = manifest_path.exists()

        variants = response["variants"]
        scores = [variant["score"]["total"] for variant in variants]
        self.assertGreaterEqual(len(variants), 3)
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual(response["recommendedVariantId"], variants[0]["id"])
        self.assertGreaterEqual(variants[0]["score"]["total"], 80)
        self.assertEqual(variants[0]["exportHints"]["width"], 1080)
        self.assertEqual(variants[0]["exportHints"]["height"], 1350)
        self.assertIn("telemetry", response)
        self.assertTrue(manifest_exists)


if __name__ == "__main__":
    unittest.main()
