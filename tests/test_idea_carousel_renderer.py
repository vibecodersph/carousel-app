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
                    "sourceUrls": ["https://github.com/Mintplex-Labs/anything-llm"],
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
        self.assertEqual(carousel["page_order"], ["cover_page", "item_1", "cta"])
        self.assertFalse(carousel["suppress_cta"])
        self.assertEqual(carousel["cta"], build_idea_carousel.fixed_research_cta_copy())
        self.assertEqual(carousel["cover_page"]["headline"], brief["hook"])
        self.assertEqual(carousel["cover_page"]["subheadline"], "")
        self.assertEqual(carousel["cover_page"]["kinetic_subline"], "")
        self.assertTrue(carousel["cover_page"]["hook_only_cover"])
        self.assertEqual(carousel["cover_page"]["source_image_url"], "")
        self.assertEqual(carousel["cover_page"]["source_image_urls"], ["https://opengraph.githubassets.com/1/example/cover"])
        self.assertEqual(carousel["cover_page"]["image_prompt"], "Cover image prompt")
        self.assertEqual(carousel["cover_page"]["alt_text"], "Cover alt text")
        self.assertTrue(carousel["cover_page"]["kinetic_fly_lines"])
        self.assertEqual(carousel["item_1"]["item_name"], "")
        self.assertTrue(carousel["item_1"]["literal_slide"])
        self.assertFalse(carousel["item_1"]["show_source"])
        self.assertIn("private docs", carousel["item_1"]["body"])
        self.assertEqual(carousel["item_1"]["best_for"], "")
        self.assertEqual(carousel["item_1"]["takeaway"], "")
        self.assertEqual(carousel["item_1"]["sources"], [{
            "title": "Mintplex-Labs/anything-llm",
            "url": "https://github.com/Mintplex-Labs/anything-llm",
        }])
        self.assertEqual(carousel["item_1"]["source_image_url"], "https://opengraph.githubassets.com/1/example/item")
        self.assertEqual(carousel["item_1"]["source_image_urls"], ["https://opengraph.githubassets.com/1/example/item"])
        self.assertEqual(carousel["item_1"]["image_prompt"], "Supporting image prompt")
        self.assertEqual(carousel["cta"]["headline"], "気になる論点をコメントで教えて")

    def test_single_source_image_is_used_once_starting_on_slide_two(self) -> None:
        source_url = "https://example.com/source.webp"
        brief = {
            "id": "brief-single-source",
            "workingTitle": "Open model economics",
            "hook": "The hidden cost pattern driving open models",
            "slides": [
                {
                    "slideNumber": 1,
                    "type": "cover",
                    "headline": "The hidden cost pattern driving open models",
                    "image": {
                        "kind": "source_image",
                        "sourceImageUrl": source_url,
                        "sourceImageUrls": [source_url],
                    },
                },
                {
                    "slideNumber": 2,
                    "type": "hook_detail",
                    "headline": "Long-running loops consume tokens quickly.",
                    "image": {
                        "kind": "generated_prompt",
                        "sourceImageUrls": [source_url],
                        "promptBase": "Token loop visual",
                    },
                },
                {
                    "slideNumber": 3,
                    "type": "hook_detail",
                    "headline": "Open models change the unit economics.",
                    "image": {
                        "kind": "generated_prompt",
                        "sourceImageUrls": [source_url],
                        "promptBase": "Economics visual",
                    },
                },
            ],
        }

        carousel = build_idea_carousel.normalize_carousel_for_render(brief)

        self.assertEqual(carousel["cover_page"]["source_image_url"], "")
        self.assertEqual(carousel["cover_page"]["source_image_urls"], [source_url])
        self.assertEqual(carousel["item_1"]["source_image_url"], source_url)
        self.assertEqual(carousel["item_1"]["source_image_urls"], [source_url])
        self.assertEqual(carousel["item_2"]["source_image_url"], "")
        self.assertEqual(carousel["item_2"]["source_image_urls"], [source_url])

    def test_localize_research_brief_copy_preserves_assets_and_qas_japanese(self) -> None:
        brief = {
            "id": "brief-ja",
            "workingTitle": "Agentic Loops",
            "hook": "Why builders are shifting to loop engineering",
            "slides": [
                {
                    "slideNumber": 1,
                    "type": "cover",
                    "headline": "Why builders are shifting to loop engineering",
                    "lines": [],
                    "image": {
                        "kind": "source_image",
                        "sourceImageUrl": "https://example.com/source.jpg",
                    },
                },
                {
                    "slideNumber": 2,
                    "type": "hook_detail",
                    "headline": "Agentic coding starts with a product spec.",
                    "lines": ["The loop keeps improving the code."],
                    "image": {
                        "kind": "generated_prompt",
                        "promptBase": "English image prompt should stay untouched",
                    },
                },
            ],
            "instagramDescription": "A short caption with Source: https://example.com",
        }
        gemini_payload = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": json.dumps(
                                    {
                                        "workingTitle": "エージェント開発のループ",
                                        "hook": "AI開発はQAよりループ設計に寄っています",
                                        "instagramDescription": "AI開発の現場では、QAだけでなくループ設計が重要になっています。\n\n出典: https://example.com",
                                        "slides": [
                                            {
                                                "slideNumber": 1,
                                                "headline": "AI開発はQAよりループ設計に寄っています",
                                                "lines": [],
                                                "altText": "カバー",
                                            },
                                            {
                                                "slideNumber": 2,
                                                "headline": "エージェント開発は仕様から始まります",
                                                "lines": ["コードはループの中で少しずつ良くなります。"],
                                                "altText": "本文",
                                            },
                                        ],
                                    },
                                    ensure_ascii=False,
                                )
                            }
                        ]
                    }
                }
            ]
        }

        with patch.object(build_idea_carousel, "gemini_api_key", return_value="key"), patch.object(
            build_idea_carousel, "gemini_text_model", return_value="gemini-test"
        ), patch.object(
            build_idea_carousel, "gemini_generate_content", return_value=gemini_payload
        ):
            localized, qa = build_idea_carousel.localize_research_brief_copy(
                brief,
                channel=build_idea_carousel.load_channel("aibrief_jp"),
                source_payload={"generatedAt": "2026-07-02T00:00:00.000Z"},
            )

        self.assertTrue(qa["passed"])
        self.assertEqual(localized["hook"], "AI開発はQAよりループ設計に寄っています")
        self.assertEqual(localized["slides"][0]["image"]["sourceImageUrl"], "https://example.com/source.jpg")
        self.assertEqual(localized["slides"][1]["image"]["promptBase"], "English image prompt should stay untouched")
        self.assertEqual(localized["slides"][1]["lines"], ["コードはループの中で少しずつ良くなります。"])

    def test_japanese_localization_qa_rejects_english_headline(self) -> None:
        qa = build_idea_carousel.qa_localized_research_brief(
            {
                "workingTitle": "English Title",
                "hook": "English hook only",
                "slides": [
                    {"headline": "English slide headline", "lines": []},
                ],
            },
            channel_language="Japanese",
        )

        self.assertFalse(qa["passed"])
        self.assertTrue(any("does not contain Japanese text" in error for error in qa["errors"]))

    def test_japanese_localization_qa_caps_cover_hook_at_25_chars(self) -> None:
        qa = build_idea_carousel.qa_localized_research_brief(
            {
                "workingTitle": "ループ設計",
                "hook": "エージェント開発はQAからループ設計へ大きく移っています",
                "slides": [
                    {"headline": "ループ設計の話", "lines": []},
                ],
            },
            channel_language="Japanese",
        )

        self.assertFalse(qa["passed"])
        self.assertIn("hook is too long for the Japanese cover template", qa["errors"])

    def test_japanese_kinetic_cover_chunks_particles_naturally(self) -> None:
        lines = build_idea_carousel.kinetic_fly_lines(
            {"headline": "AI開発はQAからループ設計へ"},
            "Japanese",
        )
        tokens = [word["text"] for line in lines for word in line]
        markup = build_idea_carousel.kinetic_hook_title_markup(
            "AI開発はQAからループ設計へ",
            japanese=True,
        )

        self.assertIn("AI開発は", tokens)
        self.assertIn("QAから", tokens)
        self.assertNotIn("QAか", tokens)
        self.assertNotIn("らループ", tokens)
        self.assertIn('<span class="hook-line">QAから</span>', markup)
        self.assertIn('<span class="hook-line">ループ設計へ</span>', markup)

    def test_japanese_item_title_markup_keeps_terms_and_particles_together(self) -> None:
        markup = build_idea_carousel.item_title_markup("1. エージェントの自律ループ")
        chunks = build_idea_carousel.japanese_phrase_chunks(
            "1. エージェントの自律ループ",
            max_chars=10,
        )

        self.assertEqual(chunks, ["1. エージェントの", "自律ループ"])
        self.assertIn('<span class="jp-phrase">1. エージェントの</span>', markup)
        self.assertIn('<span class="jp-phrase">自律ループ</span>', markup)
        self.assertNotIn("エージェン", chunks)
        self.assertNotIn("の自律", chunks)

    def test_japanese_phrase_chunks_keep_toshite_together(self) -> None:
        chunks = build_idea_carousel.japanese_phrase_chunks(
            "分子を言語として読み解くAI",
            max_chars=9,
        )

        self.assertIn("分子を言語として", chunks)
        self.assertIn("読み解くAI", chunks)
        self.assertNotIn("分子を言語と", chunks)
        self.assertNotIn("して読み解くAI", chunks)

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
                    "sourceUrls": ["https://github.com/getagentseal/codeburn"],
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
                enable_music=False,
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(manifest["source"], "research_idea_generator")
        self.assertEqual(manifest["source_brief_id"], "brief-router")
        self.assertEqual(manifest["source_brief_hook_style"], "contrarian")
        self.assertEqual(manifest["slide_count"], 3)
        self.assertEqual(manifest["instagram_caption"], brief["instagramDescription"])
        self.assertFalse(manifest["suppress_cta"])
        self.assertEqual(manifest["slides"][1]["source_url"], "https://github.com/getagentseal/codeburn")
        self.assertEqual(manifest["slides"][2]["type"], "cta")
        self.assertEqual(manifest["slides"][2]["alt_text"], build_idea_carousel.FIXED_RESEARCH_CTA_COPY["alt_text"])
        page = render_item.call_args.args[0]
        self.assertEqual(page["headline"], "Smart routing sends simple tasks to cheaper models.")
        self.assertEqual(page["body"], "")
        render_cta.assert_called_once()
        self.assertEqual(render_cta.call_args.args[3], build_idea_carousel.fixed_research_cta_copy())

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
        body_html = html_text.split("<body>", 1)[1]

        self.assertIn("Only this hook belongs on the cover", html_text)
        self.assertNotIn('<div class="source-art"', body_html)
        self.assertNotIn("https://opengraph.githubassets.com/1/example/repo", body_html)
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

        body_html = html_text.split("<body>", 1)[1]
        self.assertIn("1. Local-First Agent Runtimes For Offline Execution", html_text)
        self.assertIn("Search private docs without rebuilding every workflow.", html_text)
        self.assertIn("https://opengraph.githubassets.com/1/example/item", html_text)
        self.assertIn("slide is-literal", html_text)
        self.assertIn("has-source-image", body_html)
        self.assertNotIn("has-generated-image", body_html)
        self.assertNotIn("item-rule", body_html)
        self.assertNotIn("Source:", html_text)
        self.assertNotIn("swipe for more", html_text)
        self.assertNotIn("@vibecodersph", html_text.lower())
        self.assertNotIn("02 / 05", html_text)

    def test_literal_research_slide_marks_generated_images(self) -> None:
        with TemporaryDirectory() as tmp, patch.object(build_idea_carousel, "render_html_slide"):
            out_path = Path(tmp) / "slide_02.png"
            generated = Path(tmp) / "generated.png"
            build_idea_carousel.render_item_slide(
                {
                    "literal_slide": True,
                    "headline": "Generated art should lead this slide",
                    "body": "",
                    "item_name": "",
                    "sources": [],
                    "show_source": False,
                },
                out_path,
                active=2,
                count=3,
                image_path=generated,
            )
            html_text = out_path.with_suffix(".html").read_text(encoding="utf-8")

        body_html = html_text.split("<body>", 1)[1]
        self.assertIn("has-generated-image", body_html)
        self.assertNotIn("has-source-image", body_html)
        self.assertIn(str(generated), html_text)

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
        ) as render_cta:
            out_dir = Path(tmp)
            manifest_path = build_idea_carousel.render_carousel(
                carousel,
                out_dir=out_dir,
                generate_images=False,
                channel_id=None,
                enable_music=False,
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
        self.assertNotIn("duration_seconds", render_cover.call_args.kwargs)
        cta_copy = render_cta.call_args.args[3]
        self.assertEqual(cta_copy["headline"], "Save the stack")
        self.assertEqual(cta_copy["body"], "Follow for more tools.")
        self.assertEqual(cta_copy["action"], "Follow + Save")
        self.assertNotEqual(cta_copy["headline"], build_idea_carousel.FIXED_RESEARCH_CTA_COPY["headline"])

    def test_render_carousel_mixes_short_music_into_cover_video(self) -> None:
        carousel = {
            "id": "music-enabled",
            "page_order": ["cover_page", "item_1"],
            "suppress_cta": True,
            "cover_page": {
                "headline": "Short clips belong on carousel covers",
                "alt_text": "Cover alt text",
            },
            "item_1": {
                "headline": "The music is baked into the MP4.",
                "body": "",
                "sources": [],
                "show_source": False,
                "literal_slide": True,
            },
        }

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio = root / "signal.mp3"
            audio.write_bytes(b"audio")
            library = root / "library.json"
            library.write_text(
                json.dumps(
                    {
                        "tracks": [
                            {
                                "id": "signal-glow",
                                "title": "Signal Glow",
                                "path": str(audio),
                                "duration_seconds": 26,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            with patch.object(
                build_idea_carousel, "render_animated_title_slide"
            ) as render_cover, patch.object(
                build_idea_carousel, "render_item_slide"
            ), patch.object(
                build_idea_carousel, "add_music_to_video"
            ) as add_music:
                manifest_path = build_idea_carousel.render_carousel(
                    carousel,
                    out_dir=root,
                    generate_images=False,
                    channel_id="vibecodersph",
                    music_library=library,
                    music_clip_id="signal-glow",
                    music_duration_seconds=18,
                )
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        cover = manifest["slides"][0]
        self.assertTrue(cover["path"].endswith("slide_01_music.mp4"))
        self.assertTrue(cover["source_video_path"].endswith("slide_01.mp4"))
        self.assertEqual(cover["audio"]["clip_id"], "signal-glow")
        self.assertEqual(cover["audio"]["duration_seconds"], 18)
        self.assertEqual(manifest["carousel_music"]["clip_id"], "signal-glow")
        self.assertEqual(render_cover.call_args.kwargs["duration_seconds"], 18)
        add_music.assert_called_once()
        self.assertEqual(add_music.call_args.args[1].clip_id, "signal-glow")
        self.assertEqual(add_music.call_args.kwargs["duration_seconds"], 18)

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

    def test_cover_template_catalog_lists_motion_concepts(self) -> None:
        catalog = build_idea_carousel.cover_template_catalog()
        ids = [template["id"] for template in catalog]

        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn("stop-signal", ids)
        self.assertIn("pattern-break", ids)
        self.assertIn("metric-snap", ids)
        self.assertIn("split-switch", ids)
        self.assertIn("loom-reveal", ids)
        for template in catalog:
            self.assertTrue(template["motion"])
            self.assertTrue(template["best_use"])

    def test_auto_cover_template_selection_matches_hook_shape(self) -> None:
        cases = [
            (
                {
                    "source_brief_hook_style": "list",
                    "source_brief_title": "Local agent capabilities",
                    "cover_page": {"headline": "4 local agent capabilities developers are building right now"},
                },
                "pattern-break",
            ),
            (
                {
                    "source_brief_hook_style": "contrarian",
                    "source_brief_title": "Local inference alternatives",
                    "cover_page": {"headline": "Stop assuming NVIDIA is the only option for local LLM inference"},
                },
                "stop-signal",
            ),
            (
                {
                    "source_brief_hook_style": "proof",
                    "source_brief_title": "Stacking 16 free LLM tiers for 1.7B tokens",
                    "cover_page": {"headline": "Stack 16 free LLM tiers for 1.7B tokens"},
                },
                "metric-snap",
            ),
            (
                {
                    "source_brief_hook_style": "shift",
                    "source_brief_title": "Local AI moves beyond cloud-only setups",
                    "cover_page": {"headline": "Local LLMs are moving from cloud-only setups to desktop NPUs"},
                },
                "split-switch",
            ),
            (
                {
                    "source_brief_hook_style": "spotlight",
                    "source_brief_title": "A new SDK for agent tools",
                    "cover_page": {"headline": "A new SDK makes agent tools easier to ship"},
                },
                "loom-reveal",
            ),
        ]

        for carousel, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(build_idea_carousel.select_kinetic_cover_template(carousel), expected)

    def test_cover_template_html_marks_auto_choice_without_extra_cover_copy(self) -> None:
        channel = build_idea_carousel.load_channel("vibecodersph")
        carousel = {
            "id": "local-agents",
            "source_brief_hook_style": "list",
            "page_order": ["cover_page", "item_1"],
            "cover_page": {
                "headline": "4 local agent capabilities developers are building right now",
                "hook_only_cover": True,
            },
            "item_1": {"headline": "This should stay off the cover"},
        }

        html_text = build_idea_carousel.kinetic_fly_cover_html(
            carousel,
            count=2,
            channel=channel,
            cover_template="auto",
        )
        body_html = html_text.split("<body>", 1)[1]

        self.assertIn('data-cover-template="pattern-break"', html_text)
        self.assertIn("pattern-grid", body_html)
        self.assertIn("4 local agent capabilities developers are building right now", html_text)
        self.assertNotIn("This should stay off the cover", body_html)
        self.assertNotIn("Swipe for the comparison", body_html)

    def test_each_cover_template_renders_hook_only_html(self) -> None:
        channel = build_idea_carousel.load_channel("vibecodersph")
        carousel = {
            "id": "template-check",
            "page_order": ["cover_page", "item_1"],
            "cover_page": {
                "headline": "Stop defaulting before the real signal appears",
                "hook_only_cover": True,
            },
            "item_1": {"headline": "Hidden slide headline"},
        }

        for template in build_idea_carousel.cover_template_catalog():
            template_id = template["id"]
            with self.subTest(template=template_id):
                html_text = build_idea_carousel.kinetic_fly_cover_html(
                    carousel,
                    count=2,
                    channel=channel,
                    cover_template=template_id,
                )
                body_html = html_text.split("<body>", 1)[1]

                self.assertIn(f'data-cover-template="{template_id}"', html_text)
                self.assertIn("Stop defaulting before the real signal appears", html_text)
                self.assertNotIn("Hidden slide headline", body_html)

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
                enable_music=False,
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

    def test_render_carousel_records_auto_cover_template(self) -> None:
        carousel = {
            "id": "token-stack",
            "source_brief_hook_style": "proof",
            "page_order": ["cover_page", "item_1"],
            "suppress_cta": True,
            "cover_page": {
                "headline": "Stack 16 free LLM tiers for 1.7B tokens",
                "hook_only_cover": True,
                "alt_text": "Cover alt text",
            },
            "item_1": {
                "headline": "Route cheap prompts first.",
                "body": "",
                "sources": [],
                "show_source": False,
                "literal_slide": True,
            },
        }

        with TemporaryDirectory() as tmp, patch.object(
            build_idea_carousel, "render_kinetic_fly_cover"
        ) as render_fly, patch.object(
            build_idea_carousel, "render_item_slide"
        ):
            manifest_path = build_idea_carousel.render_carousel(
                carousel,
                out_dir=Path(tmp),
                generate_images=False,
                channel_id="vibecodersph",
                cover_style="kinetic-fly",
                cover_template="auto",
                enable_music=False,
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(manifest["cover_template"], "metric-snap")
        self.assertEqual(manifest["slides"][0]["cover_template"], "metric-snap")
        self.assertEqual(render_fly.call_args.kwargs["cover_template"], "metric-snap")

    def test_render_carousel_uses_assigned_source_image_before_generating(self) -> None:
        carousel = {
            "id": "source-then-generated",
            "page_order": ["cover_page", "item_1", "item_2"],
            "suppress_cta": True,
            "cover_page": {
                "headline": "The hidden cost pattern driving open models",
                "hook_only_cover": True,
            },
            "item_1": {
                "headline": "Long-running loops consume tokens quickly.",
                "body": "",
                "sources": [],
                "show_source": False,
                "literal_slide": True,
                "source_image_url": "https://example.com/source.webp",
            },
            "item_2": {
                "headline": "Open models change the unit economics.",
                "body": "",
                "sources": [],
                "show_source": False,
                "literal_slide": True,
                "image_prompt": "Generated economics visual",
            },
        }

        with TemporaryDirectory() as tmp, patch.object(
            build_idea_carousel, "render_kinetic_fly_cover"
        ), patch.object(
            build_idea_carousel, "render_item_slide"
        ) as render_item, patch.object(
            build_idea_carousel, "maybe_cache_source_image"
        ) as cache_source, patch.object(
            build_idea_carousel, "maybe_generate_image"
        ) as maybe_generate:
            source_path = Path(tmp) / "source.webp"
            generated = Path(tmp) / "generated.png"
            cache_source.return_value = source_path
            maybe_generate.return_value = generated
            manifest_path = build_idea_carousel.render_carousel(
                carousel,
                out_dir=Path(tmp),
                generate_images=True,
                channel_id="vibecodersph",
                cover_style="kinetic-fly",
                cover_template="auto",
                enable_music=False,
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(render_item.call_args_list[0].kwargs["image_path"], source_path)
        self.assertEqual(render_item.call_args_list[1].kwargs["image_path"], generated)
        cache_source.assert_called_once_with(Path(tmp), "https://example.com/source.webp")
        maybe_generate.assert_called_once()
        self.assertEqual(manifest["slides"][1]["source_image_url"], "https://example.com/source.webp")
        self.assertEqual(manifest["slides"][1]["image_path"], str(source_path))
        self.assertEqual(manifest["slides"][2]["image_path"], str(generated))


if __name__ == "__main__":
    unittest.main()
