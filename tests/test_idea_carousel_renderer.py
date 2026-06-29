from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

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

    def test_load_reusable_assets_maps_cover_and_items(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            cover = root / "cover.png"
            vllm = root / "vllm.png"
            cover.write_bytes(b"cover")
            vllm.write_bytes(b"vllm")
            manifest = root / "manifest.json"
            manifest.write_text(
                """
{
  "slides": [
    {"type": "title", "image_path": "%s"},
    {"type": "item", "item_name": "vLLM", "image_path": "%s"}
  ]
}
"""
                % (cover, vllm),
                encoding="utf-8",
            )
            assets = build_idea_carousel.load_reusable_assets(manifest)
            self.assertEqual(assets["cover"], cover)
            self.assertEqual(assets["items"]["vllm"], vllm)

    def test_render_carousel_writes_animated_cover_manifest(self) -> None:
        carousel = {
            "id": "agent-stack",
            "page_order": ["cover_page", "item_1", "cta"],
            "cover_page": {
                "headline": "Build agents without the [setup spiral]",
                "subheadline": "A compact stack for tiny teams.",
            },
            "item_1": {
                "item_name": "LiteLLM",
                "body": "Route model calls without rewriting the app.",
                "sources": [{"url": "https://example.com/litellm"}],
            },
            "cta": {"headline": "Save the stack", "body": "Follow for more tools."},
        }

        with TemporaryDirectory() as tmp, patch.object(
            build_idea_carousel, "render_animated_title_slide"
        ) as render_cover, patch.object(
            build_idea_carousel, "render_item_slide"
        ), patch.object(
            build_idea_carousel, "render_cta_slide"
        ):
            out_dir = Path(tmp)
            manifest_path = build_idea_carousel.render_carousel(
                carousel,
                out_dir=out_dir,
                generate_images=False,
                channel_id=None,
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        cover = manifest["slides"][0]
        self.assertEqual(cover["type"], "title")
        self.assertTrue(cover["path"].endswith("slide_01.mp4"))
        self.assertTrue(cover["poster"].endswith("slide_01_poster.png"))
        render_cover.assert_called_once()


if __name__ == "__main__":
    unittest.main()
