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

    def test_research_carousel_brief_normalizes_to_render_schema(self) -> None:
        brief = {
            "id": "brief-local-agents",
            "workingTitle": "The Rise of Local-First AI Agent Frameworks",
            "hook": "4 local agent capabilities developers are building right now",
            "hookStyle": "list",
            "confidence": "high",
            "score": 0.68,
            "slides": [
                {
                    "slideNumber": 1,
                    "type": "cover",
                    "headline": "4 local agent capabilities developers are building right now",
                    "lines": [],
                    "altText": "Cover alt text",
                    "image": {
                        "kind": "source_image",
                        "sourceImageUrl": "https://opengraph.githubassets.com/1/example/cover",
                        "sourceImageUrls": ["https://opengraph.githubassets.com/1/example/cover"],
                        "promptBase": "Cover image prompt",
                        "altText": "Cover image alt",
                    },
                },
                {
                    "slideNumber": 2,
                    "type": "list_item",
                    "headline": "1. Local-First Agent Runtimes For Offline Execution",
                    "lines": [
                        "Run agents against private docs without cloud-only dependencies.",
                        "Keep retrieval close to the data.",
                    ],
                    "altText": "Local-first runtime alt text",
                    "image": {
                        "kind": "generated_prompt",
                        "sourceImageUrls": ["https://opengraph.githubassets.com/1/example/item"],
                        "promptBase": "Supporting image prompt",
                        "altText": "Supporting image alt",
                    },
                },
            ],
            "instagramDescription": (
                "4 local agent capabilities developers are building right now\n\n"
                "Trending repositories show builders prioritizing local-first runtimes.\n\n"
                "This matters because teams want private workflows and lower latency.\n\n"
                "Evidence base: 2 sources, including GitHub: Mintplex-Labs/anything-llm.\n\n"
                "Content angle: Practical local-first agent setup.\n\n"
                "Publish note: Avoid broad adoption claims."
            ),
            "evidenceUrls": [
                "https://github.com/Mintplex-Labs/anything-llm",
                "https://github.com/gptme/gptme",
            ],
        }

        carousel = build_idea_carousel.normalize_carousel_for_render(
            brief,
            source_payload={"generatedAt": "2026-07-01T00:00:00.000Z"},
        )

        self.assertEqual(carousel["render_source"], "research_idea_generator")
        self.assertEqual(carousel["id"], "research-brief-local-agents")
        self.assertEqual(carousel["page_order"], ["cover_page", "item_1"])
        self.assertTrue(carousel["suppress_cta"])
        self.assertEqual(carousel["cover_page"]["headline"], brief["hook"])
        self.assertEqual(carousel["cover_page"]["subheadline"], "")
        self.assertEqual(carousel["cover_page"]["kinetic_subline"], "")
        self.assertTrue(carousel["cover_page"]["hook_only_cover"])
        self.assertEqual(carousel["cover_page"]["source_image_url"], "https://opengraph.githubassets.com/1/example/cover")
        self.assertEqual(carousel["cover_page"]["image_prompt"], "Cover image prompt")
        self.assertEqual(carousel["cover_page"]["alt_text"], "Cover alt text")
        self.assertTrue(carousel["cover_page"]["kinetic_fly_lines"])
        self.assertEqual(carousel["item_1"]["item_name"], "")
        self.assertTrue(carousel["item_1"]["literal_slide"])
        self.assertFalse(carousel["item_1"]["show_source"])
        self.assertIn("private docs", carousel["item_1"]["body"])
        self.assertEqual(carousel["item_1"]["best_for"], "")
        self.assertEqual(carousel["item_1"]["takeaway"], "")
        self.assertEqual(carousel["item_1"]["sources"], [])
        self.assertEqual(carousel["item_1"]["source_image_url"], "https://opengraph.githubassets.com/1/example/item")
        self.assertEqual(carousel["item_1"]["image_prompt"], "Supporting image prompt")
        self.assertNotIn("cta", carousel)

    def test_render_carousel_accepts_research_carousel_brief_standard(self) -> None:
        brief = {
            "id": "brief-router",
            "workingTitle": "Model routing becomes AI cost control",
            "hook": "Stop choosing a single AI model for your entire coding workflow",
            "hookStyle": "contrarian",
            "confidence": "medium",
            "score": 0.56,
            "slides": [
                {
                    "slideNumber": 1,
                    "type": "cover",
                    "headline": "Stop choosing a single AI model for your entire coding workflow",
                    "lines": [],
                    "altText": "Cover alt text",
                },
                {
                    "slideNumber": 2,
                    "type": "hook_detail",
                    "headline": "Smart routing sends simple tasks to cheaper models.",
                    "lines": [],
                    "altText": "Detail alt text",
                },
            ],
            "instagramDescription": (
                "Stop choosing a single AI model for your entire coding workflow\n\n"
                "A pattern is emerging around cost trackers and model routers.\n\n"
                "This helps builders reserve expensive models for complex logic.\n\n"
                "Evidence base: 1 source, including GitHub: getagentseal/codeburn.\n\n"
                "Content angle: Practical routing for AI coding agents.\n\n"
                "Publish note: Verify the source before posting."
            ),
            "evidenceUrls": ["https://github.com/getagentseal/codeburn"],
        }

        with TemporaryDirectory() as tmp, patch.object(
            build_idea_carousel, "render_animated_title_slide"
        ), patch.object(
            build_idea_carousel, "render_item_slide"
        ) as render_item, patch.object(
            build_idea_carousel, "render_cta_slide"
        ) as render_cta:
            manifest_path = build_idea_carousel.render_carousel(
                brief,
                out_dir=Path(tmp),
                generate_images=False,
                channel_id="vibecodersph",
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(manifest["source"], "research_idea_generator")
        self.assertEqual(manifest["source_brief_id"], "brief-router")
        self.assertEqual(manifest["source_brief_hook_style"], "contrarian")
        self.assertEqual(manifest["slide_count"], 2)
        self.assertTrue(manifest["suppress_cta"])
        self.assertEqual(manifest["slides"][1]["source_url"], "")
        self.assertFalse(any(slide["type"] == "cta" for slide in manifest["slides"]))
        page = render_item.call_args.args[0]
        self.assertEqual(page["headline"], "Smart routing sends simple tasks to cheaper models.")
        self.assertEqual(page["body"], "")
        render_cta.assert_not_called()

    def test_hook_only_research_cover_omits_content_labels(self) -> None:
        channel = build_idea_carousel.load_channel("vibecodersph")
        carousel = build_idea_carousel.normalize_carousel_for_render(
            {
                "id": "brief-hook-only",
                "workingTitle": "A deeper working title that should not show on cover",
                "hook": "Only this hook belongs on the cover",
                "slides": [
                    {
                        "slideNumber": 1,
                        "type": "cover",
                        "headline": "Only this hook belongs on the cover",
                        "lines": [],
                        "altText": "Cover",
                        "image": {
                            "kind": "source_image",
                            "sourceImageUrl": "https://opengraph.githubassets.com/1/example/repo",
                        },
                    },
                    {
                        "slideNumber": 2,
                        "type": "hook_detail",
                        "headline": "This is slide two",
                        "lines": ["Only this line may appear."],
                        "altText": "Slide",
                    },
                ],
                "evidenceUrls": ["https://github.com/example/repo"],
            }
        )

        html_text = build_idea_carousel.kinetic_fly_cover_html(carousel, count=2, channel=channel)

        self.assertIn("Only this hook belongs on the cover", html_text)
        self.assertIn("source-art", html_text)
        self.assertIn("https://opengraph.githubassets.com/1/example/repo", html_text)
        self.assertNotIn("A deeper working title", html_text)
        self.assertNotIn("This is slide two", html_text)
        self.assertNotIn('<header class="brand-bar">', html_text)
        self.assertNotIn("vibecodersph", html_text.lower())
        self.assertNotIn('<div class="option-row"', html_text)
        self.assertNotIn('<p class="subline"', html_text)
        self.assertNotIn("Swipe for the comparison", html_text)

    def test_literal_research_slide_omits_non_json_chrome(self) -> None:
        with TemporaryDirectory() as tmp, patch.object(build_idea_carousel, "render_html_slide"):
            out_path = Path(tmp) / "slide_02.png"
            build_idea_carousel.render_item_slide(
                {
                    "literal_slide": True,
                    "headline": "1. Local-First Agent Runtimes For Offline Execution",
                    "body": "Search private docs without rebuilding every workflow.",
                    "item_name": "",
                    "sources": [],
                    "show_source": False,
                    "source_image_url": "https://opengraph.githubassets.com/1/example/item",
                },
                out_path,
                active=2,
                count=5,
                image_path=None,
            )
            html_text = out_path.with_suffix(".html").read_text(encoding="utf-8")

        self.assertIn("1. Local-First Agent Runtimes For Offline Execution", html_text)
        self.assertIn("Search private docs without rebuilding every workflow.", html_text)
        self.assertIn("https://opengraph.githubassets.com/1/example/item", html_text)
        self.assertIn("slide is-literal", html_text)
        self.assertNotIn("item-rule", html_text.split("<body>", 1)[1])
        self.assertNotIn("Source:", html_text)
        self.assertNotIn("swipe for more", html_text)
        self.assertNotIn("@vibecodersph", html_text.lower())
        self.assertNotIn("02 / 05", html_text)

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

    def test_item_slide_css_clamps_visible_copy_to_two_lines(self) -> None:
        css = build_idea_carousel.item_slide_css()
        self.assertEqual(css.count("-webkit-line-clamp: 2"), 3)

    def test_cover_prompt_is_landscape_and_item_prompt_stays_horizontal(self) -> None:
        cover_prompt = build_idea_carousel.cover_image_prompt("A glowing server", "OSS stack")
        item_prompt = build_idea_carousel.image_prompt("A model router", "LiteLLM")

        self.assertIn("16:9 horizontal landscape editorial artwork", cover_prompt)
        self.assertIn("animated title text below the focal art", cover_prompt)
        self.assertIn("lower portion quiet, warm, and uncluttered", cover_prompt)
        self.assertNotIn("spot illustration", cover_prompt)
        self.assertNotIn("4:5 vertical portrait", cover_prompt)
        self.assertNotIn("Do not create a fade, vanishing gradient", cover_prompt)
        self.assertIn("16:9 horizontal editorial artwork", item_prompt)
        self.assertNotIn("4:5 vertical portrait", item_prompt)

    def test_reusable_cover_asset_sets_top_art_composition(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            cover = root / "cover.png"
            cover.write_bytes(b"cover")
            context, _copy, cover_image = build_idea_carousel.title_context(
                {"cover_page": {"headline": "Lead gather"}},
                root,
                generate_images=False,
                reusable_image=cover,
            )

        self.assertEqual(cover_image, cover)
        self.assertEqual(context["topic_image_path"], cover)
        self.assertEqual(context["image_composition"], "top_art")

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
            self.assertEqual(assets["cover_composition"], "top_art")
            self.assertEqual(assets["items"]["vllm"], vllm)

    def test_load_reusable_assets_normalizes_old_spot_composition(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            cover = root / "cover.png"
            cover.write_bytes(b"cover")
            manifest = root / "manifest.json"
            manifest.write_text(
                """
{
  "slides": [
    {"type": "title", "image_path": "%s", "image_composition": "spot_illustration"}
  ]
}
"""
                % cover,
                encoding="utf-8",
            )
            assets = build_idea_carousel.load_reusable_assets(manifest)

        self.assertEqual(assets["cover"], cover)
        self.assertEqual(assets["cover_composition"], "top_art")

    def test_render_carousel_writes_animated_cover_manifest(self) -> None:
        carousel = {
            "id": "agent-stack",
            "page_order": ["cover_page", "item_1", "cta"],
            "cover_page": {
                "headline": "Build agents without the [setup spiral]",
                "subheadline": "A compact stack for tiny teams.",
                "alt_text": "Cover alt text",
            },
            "item_1": {
                "item_name": "LiteLLM",
                "body": "Route model calls without rewriting the app.",
                "alt_text": "Item alt text",
                "sources": [{"url": "https://example.com/litellm"}],
            },
            "cta": {"headline": "Save the stack", "body": "Follow for more tools.", "alt_text": "CTA alt text"},
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
        self.assertEqual(cover["alt_text"], "Cover alt text")
        self.assertEqual(manifest["slides"][1]["alt_text"], "Item alt text")
        self.assertEqual(manifest["slides"][2]["alt_text"], "CTA alt text")
        self.assertTrue(cover["path"].endswith("slide_01.mp4"))
        self.assertTrue(cover["poster"].endswith("slide_01_poster.png"))
        render_cover.assert_called_once()
        title_context = render_cover.call_args.args[4]
        self.assertEqual(title_context["cover_animation"], "text-motion-lines")
        self.assertEqual(title_context["image_composition"], "")

    def test_kinetic_fly_cover_html_uses_circular_aibrief_logo_and_plain_handle(self) -> None:
        channel = build_idea_carousel.load_channel("aibrief_jp")
        carousel = {
            "id": "build-techniques",
            "page_order": ["cover_page", "item_1", "item_2", "cta"],
            "cover_page": {
                "headline": "デフォルト前に試す2つの作り方",
                "subheadline": "評価とルーティングを先に比べる。",
            },
            "item_1": {"item_name": "評価から始める"},
            "item_2": {"item_name": "モデルルーティング"},
        }

        html_text = build_idea_carousel.kinetic_fly_cover_html(
            carousel,
            count=4,
            channel=channel,
        )

        self.assertIn('data-cover-style="kinetic-fly"', html_text)
        self.assertIn("channels/aibrief_jp/logo.png", html_text)
        self.assertIn("border-radius: 50%", html_text)
        self.assertIn("text-transform: none", html_text)
        self.assertIn(">aibrief.jp<", html_text)
        self.assertNotIn(">@aibrief.jp<", html_text)

    def test_render_carousel_can_use_kinetic_fly_cover_style(self) -> None:
        carousel = {
            "id": "build-techniques",
            "page_order": ["cover_page", "item_1", "cta"],
            "cover_page": {
                "headline": "2 build techniques before defaulting",
                "subheadline": "A compact stack for small teams.",
                "alt_text": "Cover alt text",
            },
            "item_1": {
                "item_name": "model routing",
                "body": "Route model calls before committing.",
                "alt_text": "Item alt text",
                "sources": [{"url": "https://example.com/routing"}],
            },
            "cta": {"headline": "Save this", "body": "Follow for more.", "alt_text": "CTA alt text"},
        }

        with TemporaryDirectory() as tmp, patch.object(
            build_idea_carousel, "render_kinetic_fly_cover"
        ) as render_fly, patch.object(
            build_idea_carousel, "render_animated_title_slide"
        ) as render_default, patch.object(
            build_idea_carousel, "render_item_slide"
        ), patch.object(
            build_idea_carousel, "render_cta_slide"
        ):
            out_dir = Path(tmp)
            manifest_path = build_idea_carousel.render_carousel(
                carousel,
                out_dir=out_dir,
                generate_images=False,
                channel_id="aibrief_jp",
                cover_style="kinetic-fly",
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        cover = manifest["slides"][0]
        self.assertEqual(manifest["cover_style"], "kinetic-fly")
        self.assertEqual(cover["cover_style"], "kinetic-fly")
        self.assertEqual(cover["type"], "title")
        self.assertTrue(cover["path"].endswith("slide_01.mp4"))
        self.assertTrue(cover["poster"].endswith("slide_01_poster.png"))
        self.assertEqual(cover["image_path"], "")
        self.assertEqual(manifest["cover_image_provider"], "")
        render_fly.assert_called_once()
        render_default.assert_not_called()


if __name__ == "__main__":
    unittest.main()
