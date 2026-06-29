from __future__ import annotations

import unittest

import build_idea_carousel


class IdeaCarouselRendererTests(unittest.TestCase):
    def test_item_keys_follow_page_order(self) -> None:
        carousel = {
            "page_order": ["cover_page", "item_1", "item_2", "cta"],
            "item_2": {"item_name": "LiteLLM"},
            "item_1": {"item_name": "vLLM"},
        }
        self.assertEqual(build_idea_carousel.item_keys(carousel), ["item_1", "item_2"])

    def test_concise_body_uses_first_sentence_without_ellipsis(self) -> None:
        page = {
            "body": (
                "Ito ang engine na gagamitin mo para mag-host ng sarili mong models "
                "nang hindi gumagapang ang speed. Extra details should stay out."
            )
        }
        body = build_idea_carousel.concise_body(page)
        self.assertIn("speed.", body)
        self.assertNotIn("Extra details", body)
        self.assertNotIn("...", body)

    def test_concise_takeaway_prefers_best_for(self) -> None:
        page = {
            "takeaway": "Gamitin ito kung may sarili kang GPU at gusto mo ng enterprise-grade speed nang libre.",
            "best_for": "Mga may access sa GPU na gustong mag-host ng Llama o Qwen.",
        }
        self.assertEqual(
            build_idea_carousel.concise_takeaway(page),
            "Mga may access sa GPU na gustong mag-host ng Llama o Qwen.",
        )


if __name__ == "__main__":
    unittest.main()
