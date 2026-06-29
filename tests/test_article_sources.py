import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import build_article_carousel
import build_weekly_carousel
import build_x_article_carousel
import story_scout
from channel import load_channel


EXPECTED_SOURCE_NAMES = {
    "Anthropic",
    "Ars Technica Technology Lab",
    "Back End News",
    "BitPinas",
    "BusinessMirror",
    "BusinessWorld",
    "Cloudflare AI",
    "DOST-ASTI",
    "Fintech News Philippines",
    "GMA SciTech",
    "GMA Tech",
    "GitHub AI & ML",
    "GitHub Changelog",
    "Google DeepMind Blog",
    "Hugging Face Blog",
    "IEEE Spectrum AI",
    "Inquirer Technology",
    "LangChain Blog",
    "MIT Technology Review",
    "Mistral AI",
    "NVIDIA Blog",
    "NVIDIA Developer AI",
    "Newsbytes.ph",
    "OpenAI News",
    "PhilSA",
    "Philstar Business",
    "Qwen Blog",
    "Qwen Hugging Face Models",
    "Qwen Research",
    "Rappler Business",
    "Rappler Tech",
    "Replicate Blog",
    "Simon Willison",
    "Supabase Blog",
    "TechCrunch AI",
    "The Decoder",
    "The Verge AI",
    "Together AI Blog",
    "VentureBeat AI",
    "Workforce AI Jobs Stories",
    "X Trending AI",
    "YugaTech",
}


RSS_FIXTURE = """<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <item>
      <title>AI jobs platform launches 32K token agent benchmark</title>
      <link>https://example.com/news/ai-jobs-agent-benchmark</link>
      <description><![CDATA[The launch gives developers a model benchmark and hiring workflow for AI teams.]]></description>
      <pubDate>Tue, 16 Jun 2026 00:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""


SITEMAP_FIXTURE = """<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://lab.example/news/claude-agent-release</loc>
    <lastmod>2026-06-15</lastmod>
  </url>
  <url>
    <loc>https://lab.example/company/about</loc>
    <lastmod>2026-06-15</lastmod>
  </url>
</urlset>
"""


JSON_API_FIXTURE = json.dumps(
    {
        "props": {
            "pageProps": {
                "articles": [
                    {
                        "title": "Qwen agent research improves benchmark scores",
                        "slug": "qwen-agent-benchmark",
                        "description": "A research release compares agent scores and model behavior.",
                        "publishTime": "2026-06-15T12:00:00Z",
                    }
                ]
            }
        }
    }
)


HF_MODELS_FIXTURE = json.dumps(
    [
        {
            "modelId": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
            "pipeline_tag": "text-generation",
            "downloads": 12345,
            "likes": 678,
            "lastModified": "2026-06-15T08:00:00Z",
            "tags": ["qwen", "agent", "code", "open-source"],
        }
    ]
)


class ArticleSourceConfigTests(unittest.TestCase):
    def test_daily_drop_source_roster_is_configured(self) -> None:
        config = story_scout.load_config(Path("story_sources.example.json"))
        sources = config["article_sources"]
        names = {source["name"] for source in sources}

        self.assertEqual(EXPECTED_SOURCE_NAMES - names, set())
        self.assertEqual(len(sources), len(EXPECTED_SOURCE_NAMES))

        source_types = {source["name"]: source["source_type"] for source in sources}
        self.assertEqual(source_types["Anthropic"], "sitemap")
        self.assertEqual(source_types["Mistral AI"], "sitemap")
        self.assertEqual(source_types["Qwen Research"], "json_api")
        self.assertEqual(source_types["Qwen Hugging Face Models"], "huggingface_models")
        self.assertEqual(source_types["X Trending AI"], "x_search")
        self.assertEqual(source_types["Workforce AI Jobs Stories"], "workforce")

    def test_config_urls_match_daily_drop_list(self) -> None:
        config = story_scout.load_config(Path("story_sources.example.json"))
        by_name = {source["name"]: source for source in config["article_sources"]}

        self.assertEqual(
            by_name["TechCrunch AI"]["feed_url"],
            "https://techcrunch.com/category/artificial-intelligence/feed/",
        )
        self.assertEqual(
            by_name["OpenAI News"]["feed_url"],
            "https://openai.com/news/rss.xml",
        )
        self.assertEqual(
            by_name["Anthropic"]["sitemap_url"],
            "https://www.anthropic.com/sitemap.xml",
        )
        self.assertEqual(
            by_name["Qwen Research"]["json_url"],
            "https://qwen.ai/api/page_config?code=research.research-list",
        )
        self.assertEqual(by_name["Qwen Hugging Face Models"]["huggingface_org"], "Qwen")


class ArticleSourceParserTests(unittest.TestCase):
    def test_rss_sitemap_json_and_huggingface_sources_parse_to_article_items(self) -> None:
        rss_items = story_scout.parse_feed_entries(
            RSS_FIXTURE,
            {"name": "RSS Source", "feed_url": "https://example.com/feed.xml"},
        )
        self.assertEqual(rss_items[0]["url"], "https://example.com/news/ai-jobs-agent-benchmark")
        self.assertIn("developers", rss_items[0]["summary"])

        sitemap_items = story_scout.parse_sitemap_entries(
            SITEMAP_FIXTURE,
            {
                "name": "Lab Sitemap",
                "sitemap_url": "https://lab.example/sitemap.xml",
                "include_paths": ["/news/"],
            },
        )
        self.assertEqual(len(sitemap_items), 1)
        self.assertEqual(sitemap_items[0]["title"], "Claude Agent Release")

        json_items = story_scout.parse_json_api_entries(
            JSON_API_FIXTURE,
            {
                "name": "Qwen Research",
                "json_url": "https://qwen.ai/api/page_config?code=research.research-list",
                "site_url": "https://qwen.ai",
                "item_url_template": "https://qwen.ai/research/{slug}",
            },
        )
        self.assertEqual(json_items[0]["url"], "https://qwen.ai/research/qwen-agent-benchmark")
        self.assertIn("research", json_items[0]["summary"].lower())

        hf_items = story_scout.parse_huggingface_model_entries(
            HF_MODELS_FIXTURE,
            {"name": "Qwen Hugging Face Models", "huggingface_org": "Qwen"},
        )
        self.assertEqual(hf_items[0]["url"], "https://huggingface.co/Qwen/Qwen3-Coder-30B-A3B-Instruct")
        self.assertIn("12,345 downloads", hf_items[0]["summary"])

    def test_fetch_article_items_covers_new_source_types_and_workforce_slot(self) -> None:
        config = {
            "article_lookback_hours": 100000,
            "max_articles_per_source": 3,
            "include_keywords": ["agent", "ai", "benchmark", "jobs", "model", "qwen"],
            "exclude_keywords": ["hiring"],
            "article_sources": story_scout.normalize_article_sources(
                [
                    {
                        "name": "RSS Source",
                        "feed_url": "https://example.com/feed.xml",
                        "base_score": 20,
                    },
                    {
                        "name": "Lab Sitemap",
                        "source_type": "sitemap",
                        "sitemap_url": "https://lab.example/sitemap.xml",
                        "include_paths": ["/news/"],
                        "base_score": 30,
                    },
                    {
                        "name": "Qwen Research",
                        "source_type": "json_api",
                        "json_url": "https://qwen.ai/api/page_config?code=research.research-list",
                        "site_url": "https://qwen.ai",
                        "item_url_template": "https://qwen.ai/research/{slug}",
                        "base_score": 30,
                    },
                    {
                        "name": "Qwen Hugging Face Models",
                        "source_type": "huggingface_models",
                        "huggingface_org": "Qwen",
                        "base_score": 30,
                    },
                    {
                        "name": "Workforce",
                        "source_type": "workforce",
                        "base_score": 30,
                        "ignore_global_exclude": True,
                        "workforce_keywords": ["hiring", "jobs", "workforce"],
                        "include_keywords": ["ai", "jobs", "workforce"],
                    },
                ]
            ),
        }

        def fake_fetch(url: str, **_: object) -> str:
            if url == "https://example.com/feed.xml":
                return RSS_FIXTURE
            if url == "https://lab.example/sitemap.xml":
                return SITEMAP_FIXTURE
            if url == "https://qwen.ai/api/page_config?code=research.research-list":
                return JSON_API_FIXTURE
            if url.startswith("https://huggingface.co/api/models"):
                return HF_MODELS_FIXTURE
            return ""

        with patch("story_scout.fetch_url_text", side_effect=fake_fetch):
            items = story_scout.fetch_article_items(config, limit=20)

        source_names = [item["source_name"] for item in items]
        self.assertIn("RSS Source", source_names)
        self.assertIn("Lab Sitemap", source_names)
        self.assertIn("Qwen Research", source_names)
        self.assertIn("Qwen Hugging Face Models", source_names)
        self.assertIn("Workforce", source_names)

        queue = {"version": story_scout.QUEUE_VERSION, "candidates": []}
        discovered, queued = story_scout.merge_article_candidates(
            queue,
            [dict(item) for item in items],
            config,
            min_score=1,
        )
        self.assertTrue(discovered)
        self.assertTrue(all(candidate["source_type"] == "article" for candidate in queued))
        self.assertTrue(any(candidate["article"]["source_name"] == "Workforce" for candidate in queued))

    def test_article_global_limit_ranks_sources_before_truncating(self) -> None:
        config = {
            "article_lookback_hours": 100000,
            "max_articles_per_source": 2,
            "include_keywords": ["agent", "ai", "benchmark", "philippines"],
            "exclude_keywords": [],
            "article_sources": story_scout.normalize_article_sources(
                [
                    {
                        "name": "Generic One",
                        "feed_url": "https://example.com/one.xml",
                        "base_score": 20,
                    },
                    {
                        "name": "Generic Two",
                        "feed_url": "https://example.com/two.xml",
                        "base_score": 20,
                    },
                    {
                        "name": "PH Source",
                        "category": "Philippines",
                        "feed_url": "https://example.com/ph.xml",
                        "base_score": 20,
                    },
                ]
            ),
        }

        feeds = {
            "https://example.com/one.xml": """<rss><channel><item>
                <title>General app update arrives today</title>
                <link>https://example.com/general-one</link>
                <description>Small product maintenance update.</description>
                <pubDate>Tue, 16 Jun 2026 00:00:00 GMT</pubDate>
            </item></channel></rss>""",
            "https://example.com/two.xml": """<rss><channel><item>
                <title>Company shares quarterly roadmap</title>
                <link>https://example.com/general-two</link>
                <description>Regular business roadmap summary.</description>
                <pubDate>Tue, 16 Jun 2026 00:00:00 GMT</pubDate>
            </item></channel></rss>""",
            "https://example.com/ph.xml": """<rss><channel><item>
                <title>Philippines AI benchmark launches 20x agent evaluation</title>
                <link>https://example.com/ph-ai-benchmark</link>
                <description>Local researchers publish an agent benchmark for AI systems.</description>
                <pubDate>Tue, 16 Jun 2026 00:00:00 GMT</pubDate>
            </item></channel></rss>""",
        }

        with patch("story_scout.fetch_url_text", side_effect=lambda url, **_: feeds[url]):
            items = story_scout.fetch_article_items(config, limit=1)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["source_name"], "PH Source")
        self.assertEqual(items[0]["url"], "https://example.com/ph-ai-benchmark")

    def test_article_scoring_matches_whole_terms_not_substrings(self) -> None:
        config = {
            "include_keywords": ["ai", "app"],
            "exclude_keywords": ["ad"],
        }
        source = {"base_score": 20, "include_keywords": ["launch"]}

        score, reasons = story_scout.score_article_item(
            {
                "title": "vivo specs revealed ahead of launch",
                "summary": "The phone arrives today.",
                "published_at": "",
            },
            source,
            config,
        )

        self.assertLess(score, 30)
        self.assertIn("keywords: launch", reasons)
        self.assertNotIn("ai", " ".join(reasons))
        self.assertNotIn("app", " ".join(reasons))


class ScoutApiUsageCostTests(unittest.TestCase):
    def test_scan_cost_summary_uses_xai_billed_ticks(self) -> None:
        usage_log: list[dict[str, object]] = []
        story_scout.record_api_usage(
            usage_log,
            provider="xAI",
            endpoint="scout_posts",
            tool_type="x_search",
            usage={
                "model": "grok-4.3",
                "input_tokens": 1000,
                "output_tokens": 200,
                "total_tokens": 1200,
                "num_server_side_tools_used": 2,
                "cost_in_usd_ticks": 1_500_000,
            },
        )

        [line] = story_scout.format_api_usage_summary(usage_log)

        self.assertIn("[scan-cost] xAI/grok-4.3", line)
        self.assertIn("input=1,000", line)
        self.assertIn("output=200", line)
        self.assertIn("total=1,200", line)
        self.assertIn("x_search_calls=2", line)
        self.assertIn("cost=$0.000150", line)
        self.assertNotIn("est_cost", line)

    def test_scan_cost_summary_estimates_when_ticks_are_missing(self) -> None:
        usage_log: list[dict[str, object]] = []
        story_scout.record_api_usage(
            usage_log,
            provider="xAI",
            endpoint="article_x_search",
            tool_type="x_search",
            usage={
                "model": "grok-4.3",
                "input_tokens": 1_000_000,
                "output_tokens": 1_000_000,
                "total_tokens": 2_000_000,
                "num_server_side_tools_used": 1,
            },
        )

        [line] = story_scout.format_api_usage_summary(usage_log)

        self.assertIn("est_cost=$3.755000", line)
        self.assertNotIn(" cost=$", line)

    def test_scan_cost_summary_handles_no_billable_api_usage(self) -> None:
        self.assertEqual(
            story_scout.format_api_usage_summary([]),
            ["[scan-cost] no billable API usage reported"],
        )


class TelegramArticleWorkflowTests(unittest.TestCase):
    def test_article_notification_uses_article_callback_and_review_copy(self) -> None:
        url = "https://blogs.nvidia.com/blog/nvidia-blackwell-agentperf-artificial-analysis/"
        cid = story_scout.article_candidate_id(url)
        candidate = {
            "id": cid,
            "source_type": "article",
            "status": "candidate",
            "score": 64,
            "score_reasons": ["article source match", "keywords: benchmark, ai"],
            "source_account": "NVIDIA Blog",
            "article": {
                "url": url,
                "title": "NVIDIA Blackwell leads agentic AI benchmark",
                "summary": "AgentPerf results show Blackwell runs more AI agents per megawatt.",
                "source_name": "NVIDIA Blog",
                "published_at": "2026-06-12T21:00:08+00:00",
            },
        }
        calls: list[tuple[str, dict[str, object]]] = []

        def fake_telegram(method: str, payload: dict[str, object], *, timeout: int = 30):
            del timeout
            calls.append((method, payload))
            return {"ok": True, "result": {"chat": {"id": 123}, "message_id": 456}}

        with patch.dict(os.environ, {"TELEGRAM_CHAT_ID": "123"}, clear=False), patch(
            "story_scout.telegram_api",
            side_effect=fake_telegram,
        ):
            self.assertTrue(story_scout.notify_telegram(candidate))

        self.assertEqual(calls[0][0], "sendMessage")
        payload = calls[0][1]
        self.assertIn("64 score - ARTICLE NVIDIA Blog", str(payload["text"]))
        self.assertIn("NVIDIA Blackwell leads agentic AI benchmark", str(payload["text"]))
        self.assertIn("AgentPerf results show", str(payload["text"]))
        keyboard = payload["reply_markup"]["inline_keyboard"]
        self.assertEqual(keyboard[0][0]["callback_data"], f"approve_build:{cid}")
        self.assertEqual(candidate["telegram_message"]["message_id"], 456)

    def test_recovers_article_candidate_from_telegram_callback(self) -> None:
        url = "https://blogs.nvidia.com/blog/nvidia-blackwell-agentperf-artificial-analysis/"
        cid = story_scout.article_candidate_id(url)
        queue = {"version": story_scout.QUEUE_VERSION, "candidates": []}
        callback = {
            "message": {
                "text": "\n".join(
                    [
                        "Carousel candidate",
                        "64 score - ARTICLE NVIDIA Blog",
                        "NVIDIA Blackwell leads agentic AI benchmark",
                        "AgentPerf results show Blackwell runs more AI agents per megawatt.",
                        url,
                        "Why: article source match; keywords: benchmark, ai",
                    ]
                ),
                "chat": {"id": 123},
                "message_id": 456,
            }
        }

        candidate = story_scout.recover_candidate_from_callback(queue, cid, callback)

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate["source_type"], "article")
        self.assertEqual(candidate["article"]["url"], url)
        self.assertEqual(candidate["article"]["source_name"], "NVIDIA Blog")
        self.assertEqual(candidate["article"]["title"], "NVIDIA Blackwell leads agentic AI benchmark")
        self.assertIn("AgentPerf results", candidate["article"]["summary"])
        self.assertEqual(candidate["score"], 64)
        self.assertTrue(candidate["recovered_from_telegram"])
        self.assertEqual(queue["candidates"][0]["id"], cid)

    def test_recovers_existing_article_candidate_by_url(self) -> None:
        url = "https://example.com/ai-agent-benchmark"
        existing = {
            "id": story_scout.article_candidate_id(url),
            "source_type": "article",
            "status": "candidate",
            "article": {"url": url, "title": "AI agent benchmark", "source_name": "Example"},
        }
        queue = {"version": story_scout.QUEUE_VERSION, "candidates": [existing]}
        callback = {
            "message": {
                "text": "\n".join(
                    [
                        "Carousel candidate",
                        "51 score - ARTICLE Example",
                        "AI agent benchmark",
                        url,
                        "Why: keywords: agent",
                    ]
                )
            }
        }

        recovered = story_scout.recover_candidate_from_callback(
            queue,
            "article_missing_from_callback_state",
            callback,
        )

        self.assertIs(recovered, existing)
        self.assertEqual(len(queue["candidates"]), 1)


class ArticleCarouselQualityTests(unittest.TestCase):
    def test_h1_only_articles_use_body_signal_for_headlines(self) -> None:
        html = """<!doctype html>
<html><body><article>
  <h1>TP boosts TP.ai Data Services amid growing AI demand in Asia</h1>
  <p>Reported outcomes included up to 31% improvement in customer experience scores and up to 15% workforce efficiency gains from AI data services.</p>
  <p>TP.ai Data Services supports model evaluation, annotation, labeling, 32K token benchmarks, tool calls, and human-in-the-loop governance for enterprise AI teams.</p>
</article></body></html>
"""
        article = build_article_carousel.parse_article(
            "https://example.com/ph-ai",
            html,
            "https://example.com/ph-ai",
        )
        sections = build_article_carousel.build_candidate_sections(article)
        pages = build_article_carousel.local_curate_pages(sections, max_pages=2, min_score=4)

        self.assertGreaterEqual(len(pages), 2)
        self.assertNotIn("TP.ai Data Services amid growing AI", pages[0].headline)
        self.assertIn("31%", pages[0].body)

    def test_local_article_curation_outputs_slide_sized_pages(self) -> None:
        html = """<!doctype html>
<html>
<head>
  <title>Qwen agent model beats benchmark</title>
  <meta property="og:site_name" content="Example AI Lab">
  <meta name="description" content="A model release with concrete benchmark and open-source details.">
</head>
<body>
  <article>
    <h1>Qwen agent model beats benchmark</h1>
    <p>Subscribe to our newsletter for weekly updates and events.</p>
    <h2>Benchmark results</h2>
    <p>The released Qwen agent model scored 72% on SWE-Bench and handled 32K token coding tasks with tool calls, beating the previous open-source baseline.</p>
    <h2>Open source release</h2>
    <p>The team published model weights, an Apache 2.0 license, and a GitHub evaluation harness so developers can reproduce the benchmark and test agent workflows.</p>
    <h2>Workforce impact</h2>
    <p>Enterprise teams are using the agent to automate repetitive developer jobs, changing hiring plans and raising demand for AI upskilling programs.</p>
    <h2>SHARE</h2>
    <p>Share on Facebook (Opens in new window) Facebook Share on X (Opens in new window) X Share on LinkedIn (Opens in new window) LinkedIn.</p>
  </article>
</body>
</html>
"""
        article = build_article_carousel.parse_article(
            "https://example.com/qwen-agent-benchmark",
            html,
            "https://example.com/qwen-agent-benchmark",
        )
        block_text = " ".join(block.text.lower() for block in article.blocks)
        self.assertNotIn("subscribe to our newsletter", block_text)
        self.assertNotIn("share on facebook", block_text)

        sections = build_article_carousel.build_candidate_sections(article)
        pages = build_article_carousel.local_curate_pages(sections, max_pages=4, min_score=4)

        self.assertGreaterEqual(len(pages), 2)
        for page in pages:
            self.assertLessEqual(build_article_carousel.count_words(page.headline), 9)
            self.assertLessEqual(build_article_carousel.count_words(page.body), 42)
            self.assertTrue(page.kicker)
            self.assertTrue(page.source_indices)
            self.assertNotIn("subscribe", page.body.lower())


class LocalFallbackBodyQualityTests(unittest.TestCase):
    def test_subject_predicate_split_separates_long_subject_sentences(self) -> None:
        subject, predicate = build_article_carousel.subject_predicate_split(
            "The increasing use of artificial intelligence in online propaganda "
            "campaigns has underscored the need for stronger digital literacy."
        )
        self.assertGreaterEqual(build_article_carousel.count_words(subject), 3)
        self.assertTrue(predicate.startswith("has"))

    def test_short_subject_sentences_are_left_intact(self) -> None:
        # A punchy short-subject sentence keeps its lead rather than being split.
        self.assertEqual(
            build_article_carousel.subject_predicate_split(
                "GPT-5 launched with a new agent benchmark today."
            ),
            ("", ""),
        )


class XArticleParsingTests(unittest.TestCase):
    def test_looks_like_heading_accepts_title_style_lines(self) -> None:
        looks_like_heading = build_x_article_carousel.looks_like_heading
        self.assertTrue(looks_like_heading("The Benchmark That Broke The Room Is A Capacity Test"))
        self.assertTrue(looks_like_heading("Pick The Model The Bandwidth Likes"))
        self.assertTrue(looks_like_heading("You're About To Buy The Wrong Box"))

    def test_looks_like_heading_rejects_prose_and_bullets(self) -> None:
        looks_like_heading = build_x_article_carousel.looks_like_heading
        # Sentence-cased prose ending in a period is a paragraph, not a heading.
        self.assertFalse(
            looks_like_heading(
                "The system carves a slice of the shared pool for the GPU on boot."
            )
        )
        # Lowercase running text is body even without terminal punctuation.
        self.assertFalse(looks_like_heading("same chip, brand-name tax"))
        # Bullets / numbered leads are body.
        self.assertFalse(looks_like_heading("1. First buy the right SKU"))
        self.assertFalse(looks_like_heading(""))

    def test_blocks_from_full_text_tags_headings_and_paragraphs(self) -> None:
        full_text = (
            "The viral thread oversold a tiny PC and you almost bought the wrong box "
            "for two grand based on a benchmark that measures capacity not speed.\n"
            "Pick The Model The Bandwidth Likes\n"
            "At 256 GB/s the box favours mixture-of-experts models that stream only "
            "the active experts, while dense 70B models stall in the single digits."
        )
        blocks = build_x_article_carousel.blocks_from_full_text(full_text)
        roles = [block.role for block in blocks]
        self.assertIn("h2", roles)
        self.assertIn("p", roles)
        heading = next(block.text for block in blocks if block.role == "h2")
        self.assertEqual(heading, "Pick The Model The Bandwidth Likes")
        # Block indices stay contiguous so source_indices map back correctly.
        self.assertEqual([block.index for block in blocks], list(range(len(blocks))))

    def test_blocks_from_full_text_splits_overlong_paragraphs(self) -> None:
        long_paragraph = " ".join(
            f"Sentence number {n} carries a concrete benchmark detail." for n in range(60)
        )
        blocks = build_x_article_carousel.blocks_from_full_text(long_paragraph)
        self.assertGreater(len(blocks), 1)
        for block in blocks:
            self.assertLessEqual(build_article_carousel.count_words(block.text), 220)

    def test_x_article_to_article_feeds_the_shared_pipeline(self) -> None:
        data = {
            "id": "2066297019912610286",
            "is_long_form": True,
            "title": "The $1,499 Box Cannot Run That Model",
            "subtitle": "What the viral AMD thread left out.",
            "full_text": (
                "You saw the thread and almost bought the cheap box, but the demo ran "
                "a 235B model the $1,499 unit physically cannot load.\n"
                "The Cost You Actually Pay\n"
                "The 128GB EVO-X2 lands around $2,200, not $1,499, because a 235B "
                "mixture-of-experts model needs the larger memory pool to load at all."
            ),
            "author_name": "plutos",
            "handle": "@plutos_eth",
            "date": "Sun Jun 14, 2026",
        }
        article = build_x_article_carousel.x_article_to_article(
            data, url="https://x.com/plutos_eth/status/2066297019912610286"
        )
        self.assertEqual(article.title, "The $1,499 Box Cannot Run That Model")
        self.assertEqual(article.site_name, "plutos on X")
        self.assertEqual(article.author, "plutos")
        self.assertTrue(article.blocks)

        sections = build_article_carousel.build_candidate_sections(article)
        pages = build_article_carousel.local_curate_pages(sections, max_pages=2, min_score=2)
        self.assertGreaterEqual(len(pages), 1)
        for page in pages:
            self.assertTrue(page.source_indices)

    def test_derive_title_falls_back_to_first_heading(self) -> None:
        data = {"title": "", "full_text": ""}
        blocks = [
            build_article_carousel.TextBlock("p", "A short lede paragraph that opens the piece.", 0),
            build_article_carousel.TextBlock("h2", "The Real Cost Of The Box", 1),
        ]
        self.assertEqual(
            build_x_article_carousel.derive_title(data, blocks),
            "The Real Cost Of The Box",
        )


WEEKLY_STORIES = [
    {"author": "Anthropic", "handle": "@AnthropicAI", "url": "https://x.com/AnthropicAI/status/1",
     "text": "Anthropic launches a new policy initiative on AI regulation.", "score": 94,
     "reasons": ["keywords: ai, policy, launch"]},
    {"author": "Anthropic", "handle": "@AnthropicAI", "url": "https://x.com/AnthropicAI/status/2",
     "text": "Anthropic launches Claude Corps, a national fellowship program.", "score": 90,
     "reasons": ["keywords: ai, launch"]},
    {"author": "Anthropic", "handle": "@AnthropicAI", "url": "https://x.com/AnthropicAI/status/3",
     "text": "Anthropic shares a third update for the week.", "score": 88, "reasons": []},
    {"author": "OpenAI", "handle": "@OpenAI", "url": "https://x.com/OpenAI/status/4",
     "text": "OpenAI rolls out the ability to save Codex rate limit resets.", "score": 92,
     "reasons": ["keywords: launch"]},
    {"author": "Google DeepMind", "handle": "@GoogleDeepMind", "url": "https://x.com/GoogleDeepMind/status/5",
     "text": "Google DeepMind announces a benchmark for multi-agent research.", "score": 89,
     "reasons": ["keywords: agent, research, benchmark"]},
]


class WeeklyCarouselTests(unittest.TestCase):
    def _gather(self, *, max_stories=7, per_source=2):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"stories": WEEKLY_STORIES}, fh)
            path = Path(fh.name)
        try:
            return build_weekly_carousel.gather_top_stories(
                queue_path=Path("/nonexistent.json"),
                input_path=path,
                days=7,
                max_stories=max_stories,
                per_source=per_source,
            )
        finally:
            path.unlink(missing_ok=True)

    def test_ranking_orders_by_score_and_assigns_rank(self) -> None:
        stories = self._gather(per_source=0)
        self.assertEqual([s.score for s in stories], sorted((s.score for s in stories), reverse=True))
        self.assertEqual([s.rank for s in stories], list(range(1, len(stories) + 1)))

    def test_per_source_cap_limits_one_account(self) -> None:
        # max_stories=3 with 5 inputs: the cap bites before top-up can refill.
        stories = self._gather(max_stories=3, per_source=2)
        self.assertEqual(len(stories), 3)
        anthropic = [s for s in stories if s.handle == "@AnthropicAI"]
        self.assertEqual(len(anthropic), 2)  # 3rd Anthropic post is capped out

    def test_per_source_top_up_when_sources_are_scarce(self) -> None:
        # Asking for more stories than distinct sources falls back past the cap.
        stories = self._gather(max_stories=5, per_source=2)
        self.assertEqual(len(stories), 5)

    def test_max_stories_clamped_to_instagram_budget(self) -> None:
        # cover + outro leave at most MAX_STORIES slides for stories.
        clamp = lambda n: max(
            build_weekly_carousel.MIN_STORIES,
            min(n, build_weekly_carousel.MAX_STORIES),
        )
        self.assertEqual(clamp(99), build_weekly_carousel.MAX_STORIES)
        self.assertEqual(clamp(1), build_weekly_carousel.MIN_STORIES)
        self.assertEqual(
            build_weekly_carousel.MAX_STORIES + 2, build_weekly_carousel.MAX_TOTAL_SLIDES
        )

    def test_local_copy_fallback_produces_slide_sized_text(self) -> None:
        story = build_weekly_carousel.WeeklyStory(
            rank=1, author="OpenAI", handle="@OpenAI",
            url="https://x.com/OpenAI/status/4",
            text="OpenAI rolls out the ability to save Codex rate limit resets for later use.",
            date="", score=92, source_type="x", reasons=["keywords: launch"],
        )
        kicker, headline, summary = build_weekly_carousel.local_story_copy(story)
        self.assertEqual(kicker, "LAUNCH")
        self.assertLessEqual(build_article_carousel.count_words(headline), 10)
        self.assertTrue(summary)

    def test_curated_weekly_copy_is_preserved(self) -> None:
        story = build_weekly_carousel.WeeklyStory(
            rank=1,
            author="OpenAI",
            handle="@OpenAI",
            url="https://x.com/OpenAI/status/4",
            text="OpenAI source text",
            date="",
            score=92,
            source_type="primary_x",
            category="LAUNCH",
            kicker="LAUNCH",
            headline="OpenAIがCodexのレート制限リセットを繰り越し可能に",
            summary="有料プランの開発者がCodexのレート制限リセットを保存可能に。長時間作業前に温存でき、30日で失効。",
            copy_locked=True,
        )

        _, backend = build_weekly_carousel.curate_copy(load_channel("aibrief_jp"), [story])

        self.assertEqual(backend, "curated")
        self.assertIn("Codex", story.headline)
        self.assertIn("30日", story.summary)

    def test_accent_markup_wraps_one_word(self) -> None:
        markup, plain = build_weekly_carousel.accent_markup("Ang [pinakamainit] na balita")
        self.assertIn('<span class="accent">pinakamainit</span>', markup)
        self.assertEqual(plain, "Ang pinakamainit na balita")
        # No brackets: text passes through, html-escaped.
        markup2, plain2 = build_weekly_carousel.accent_markup("a & b")
        self.assertEqual(plain2, "a & b")
        self.assertIn("&amp;", markup2)

    def test_channels_share_light_branding(self) -> None:
        # aibrief_jp intentionally shares vibecodersph's cream/ink palette;
        # channels differ in language and voice, not visual theme.
        for cid in ("vibecodersph", "aibrief_jp"):
            css = build_weekly_carousel.channel_css(load_channel(cid))
            self.assertIn("#F4F2EC", css)      # shared cream background
            self.assertIn("#C0552E", css)      # shared rust primary
            self.assertNotIn("#14161A", css)   # not the old charcoal dark theme

    def test_aibrief_shares_vibecodersph_palette_in_shared_css(self) -> None:
        # Regression guard: the regular builders read brand_colors() too, so the
        # JP channel must resolve to the light palette there as well (no dark leak).
        import build_x_carousel

        prev = os.environ.get("CAROUSEL_CHANNEL")
        try:
            os.environ["CAROUSEL_CHANNEL"] = "aibrief_jp"
            colors = build_x_carousel.brand_colors()
            self.assertEqual(colors["bg"], "#F4F2EC")
            self.assertEqual(colors["primary"], "#C0552E")
            self.assertFalse(build_x_carousel._is_dark_color(colors["bg"]))
        finally:
            if prev is None:
                os.environ.pop("CAROUSEL_CHANNEL", None)
            else:
                os.environ["CAROUSEL_CHANNEL"] = prev

    def test_japanese_channel_localizes_structural_labels(self) -> None:
        from datetime import datetime, timezone
        labels = build_weekly_carousel.localized_labels(
            load_channel("aibrief_jp"),
            start=datetime(2026, 6, 11, tzinfo=timezone.utc),
            end=datetime(2026, 6, 18, tzinfo=timezone.utc),
            count=7,
        )
        self.assertEqual(labels["section_label"], "今週のAIニュース")
        self.assertIn("6月11日", labels["week_range"])

    def test_single_sentence_section_headline_is_not_a_body_prefix(self) -> None:
        html = """<!doctype html><html><body><article>
  <p>The increasing use of artificial intelligence in online propaganda campaigns
  has underscored the need for stronger digital literacy and cybersecurity measures
  in the Philippines as the country accelerates its digital transformation.</p>
</article></body></html>"""
        article = build_article_carousel.parse_article(
            "https://example.com/ph", html, "https://example.com/ph"
        )
        sections = build_article_carousel.build_candidate_sections(article)
        pages = build_article_carousel.local_curate_pages(sections, max_pages=2, min_score=2)
        self.assertTrue(pages)
        headline_key = build_article_carousel.normalized_text_key(pages[0].headline)
        body_key = build_article_carousel.normalized_text_key(pages[0].body)
        self.assertTrue(headline_key)
        self.assertFalse(body_key.startswith(headline_key))


class ArticleScoringWholeWordTests(unittest.TestCase):
    def test_kicker_uses_whole_words_not_substrings(self) -> None:
        # "underscored" contains "score" and "answered" contains "swe" -- neither
        # is a benchmark story.
        self.assertEqual(
            build_article_carousel.kicker_for_text(
                "has underscored the need for stronger digital literacy"
            ),
            "THE SIGNAL",
        )
        self.assertEqual(
            build_article_carousel.kicker_for_text("the spokesperson answered questions"),
            "THE SIGNAL",
        )
        self.assertEqual(
            build_article_carousel.kicker_for_text("scored 72% on SWE-Bench leaderboard"),
            "BENCHMARK",
        )

    def test_section_score_ignores_substring_signal_hits(self) -> None:
        noise = "The training data and underscored governance reports were filed today."
        noise_score, noise_reasons = build_article_carousel.section_signal_score("", noise)
        self.assertNotIn("strong terms", noise_reasons)  # "score" in "underscored"

        real = "The model scored 72% on SWE-Bench, beating the open-source baseline."
        real_score, real_reasons = build_article_carousel.section_signal_score("", real)
        self.assertIn("strong terms", real_reasons)
        self.assertGreater(real_score, noise_score)

    def test_stat_chip_requires_a_quantity(self) -> None:
        self.assertEqual(build_article_carousel.normalize_stat_chip("Low impact"), "")
        self.assertEqual(build_article_carousel.normalize_stat_chip("Digital expansion"), "")
        self.assertEqual(build_article_carousel.normalize_stat_chip("72%"), "72%")
        self.assertEqual(build_article_carousel.normalize_stat_chip("2025 - 2026"), "2025 - 2026")

    def test_compact_headline_has_no_ellipsis(self) -> None:
        headline = build_article_carousel.compact_headline(
            "The increasing use of artificial intelligence in online propaganda campaigns",
            8,
        )
        self.assertNotIn("...", headline)
        self.assertLessEqual(build_article_carousel.count_words(headline), 8)


class ArticleNewsPickScoringTests(unittest.TestCase):
    BASE_CONFIG = {
        "include_keywords": ["ai", "agent", "model", "benchmark", "launch"],
        "exclude_keywords": ["ad"],
    }

    def _score(self, title: str, summary: str) -> int:
        score, _ = story_scout.score_article_item(
            {"title": title, "summary": summary, "published_at": ""},
            {"base_score": 20},
            self.BASE_CONFIG,
        )
        return score

    def test_roundup_listicle_is_penalized_below_real_news(self) -> None:
        roundup = self._score(
            "The 10 best AI tools for productivity this week",
            "A roundup of apps to try. Sponsored deals included.",
        )
        release = self._score(
            "Qwen3-Coder released, tops SWE-Bench with open-source weights",
            "The model scored 71% on SWE-Bench and ships an Apache 2.0 license on GitHub.",
        )
        self.assertLess(roundup, release)

    def test_named_model_release_and_title_signal_rank_high(self) -> None:
        score, reasons = story_scout.score_article_item(
            {
                "title": "OpenAI ships GPT-5 with new agent benchmark",
                "summary": "GPT-5 outperforms prior models on reasoning evals.",
                "published_at": "",
            },
            {"base_score": 20},
            self.BASE_CONFIG,
        )
        self.assertIn("named model release", reasons)
        self.assertIn("strong signal in title", reasons)
        self.assertGreater(score, 40)

    def test_scoring_still_caps_generic_gadget_launch(self) -> None:
        # Preserves the invariant from test_article_scoring_matches_whole_terms_not_substrings.
        score, reasons = story_scout.score_article_item(
            {"title": "vivo specs revealed ahead of launch", "summary": "The phone arrives today.", "published_at": ""},
            {"base_score": 20, "include_keywords": ["launch"]},
            {"include_keywords": ["ai", "app"], "exclude_keywords": ["ad"]},
        )
        self.assertLess(score, 30)
        self.assertNotIn("named model release", reasons)


class StoryScoutPostScoringTests(unittest.TestCase):
    BASE_CONFIG = {
        "accounts": ["OpenAI"],
        "include_keywords": ["ai", "agent", "benchmark", "codex", "launch", "model", "policy"],
        "exclude_keywords": ["hiring", "webinar"],
        "lookback_hours": 24,
        "require_story_signal": True,
    }

    def _date(self, *, hours_ago: int) -> str:
        return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()

    def _post(self, text: str, *, hours_ago: int = 2, **overrides) -> dict[str, object]:
        post: dict[str, object] = {
            "id": "",
            "text": text,
            "handle": "@OpenAI",
            "date": self._date(hours_ago=hours_ago),
            "likes": 2500,
            "retweets": 350,
            "replies": 90,
            "views": 600000,
            "has_video": False,
            "why": "",
            "url": "https://x.com/OpenAI/status/2065225362544726371",
        }
        post.update(overrides)
        return post

    def test_high_engagement_without_concrete_story_signal_is_blocked(self) -> None:
        post = self._post(
            "Really looking forward to working together. Big things coming soon.",
            likes=90000,
            retweets=12000,
            replies=5000,
            views=15000000,
        )

        score, reasons, components = story_scout.score_post_breakdown(post, self.BASE_CONFIG)

        self.assertEqual(score, 0)
        self.assertFalse(components["substance_gate"])
        self.assertGreater(components["pre_gate_score"], 0)
        self.assertIn("no concrete story signal", reasons)

    def test_concrete_story_has_separate_quality_components(self) -> None:
        post = self._post(
            "OpenAI launches GPT-5 with a new agent benchmark, scoring 72% on SWE-Bench."
        )

        score, reasons, components = story_scout.score_post_breakdown(post, self.BASE_CONFIG)

        self.assertGreaterEqual(score, 55)
        self.assertTrue(components["substance_gate"])
        self.assertGreater(components["story_substance"], 0)
        self.assertGreater(components["popularity"], 0)
        self.assertGreater(components["specificity"], 0)
        self.assertGreater(components["timeliness"], 0)
        self.assertIn("story signals", "; ".join(reasons))

    def test_x_post_recency_decay_reduces_old_story_score(self) -> None:
        text = "OpenAI launches GPT-5 with a new benchmark scoring 72% on SWE-Bench."
        fresh_score, _, fresh_components = story_scout.score_post_breakdown(
            self._post(text, hours_ago=2),
            self.BASE_CONFIG,
        )
        old_score, _, old_components = story_scout.score_post_breakdown(
            self._post(text, hours_ago=120),
            self.BASE_CONFIG,
        )

        self.assertLess(old_score, fresh_score)
        self.assertEqual(fresh_components["penalties"].get("recency_decay", 0), 0)
        self.assertGreater(old_components["penalties"].get("recency_decay", 0), 0)

    def test_prior_outcomes_feed_back_into_post_score(self) -> None:
        queue = {
            "version": story_scout.QUEUE_VERSION,
            "candidates": [
                {
                    "id": "x_good",
                    "status": "published",
                    "source_account": "@OpenAI",
                    "post": {
                        "handle": "@OpenAI",
                        "text": "OpenAI launches Codex benchmark for agent coding.",
                        "why": "Published carousel performed well",
                    },
                }
            ],
        }
        feedback = story_scout.build_outcome_feedback(queue, self.BASE_CONFIG)
        post = self._post("OpenAI launches a Codex agent benchmark for coding.")

        _, _, components = story_scout.score_post_breakdown(
            post,
            self.BASE_CONFIG,
            feedback=feedback,
        )

        self.assertGreater(components["outcome_feedback"], 0)

    def test_outcome_events_are_recorded(self) -> None:
        candidate = {"id": "x_test", "status": "approved"}

        story_scout.record_outcome_event(candidate, "approved")

        self.assertEqual(candidate["outcome_events"][0]["event"], "approved")
        self.assertEqual(candidate["outcome_events"][0]["status"], "approved")


class ArticleCurationLanguageTests(unittest.TestCase):
    def test_gemini_article_curation_prompt_uses_channel_language(self) -> None:
        article = build_article_carousel.Article(
            source="https://example.com/story",
            url="https://example.com/story",
            title="Open model release",
            description="A lab released a new model with strong benchmark results.",
            site_name="Example",
            author="Reporter",
            published_at="",
            image_url="",
        )
        candidates = [
            build_article_carousel.CandidateSection(
                index=0,
                title="Benchmark",
                body=(
                    "The model scored 71% on SWE-Bench Verified and ships with "
                    "open weights under an MIT license for developers."
                ),
                score=9,
                reasons=["benchmark"],
                block_indices=[0],
            )
        ]
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
                                            "pages": [
                                                {
                                                    "source_indices": [0],
                                                    "kicker": "実力の証拠",
                                                    "headline": "実務ベンチに迫るオープンモデル",
                                                    "body": "SWE-Bench Verifiedで71%を記録し、MITライセンスのオープンウェイトとして開発者が検証できます。",
                                                    "stat": "71%",
                                                    "tease": "",
                                                    "why": "具体的なベンチマーク結果",
                                                }
                                            ]
                                        },
                                        ensure_ascii=False,
                                    )
                                }
                            ]
                        }
                    }
                ]
            }

        with patch.dict(os.environ, {"CAROUSEL_CHANNEL": "aibrief_jp"}), patch.object(
            build_article_carousel,
            "gemini_api_key",
            return_value="test-key",
        ), patch.object(
            build_article_carousel,
            "gemini_generate_content",
            side_effect=fake_generate,
        ):
            pages = build_article_carousel.gemini_curate_pages(
                article,
                candidates,
                max_pages=1,
                min_score=1,
            )

        self.assertEqual(len(pages), 1)
        self.assertIn("Write every reader-facing field in Japanese", prompts[0])
        self.assertIn("実力の証拠", prompts[0])
        self.assertIn("short Japanese curiosity frame", prompts[0])
        self.assertNotIn("THE CLAIM", prompts[0])


class ArticleCoverVoiceTests(unittest.TestCase):
    def test_brand_voice_cover_copy_is_preferred_over_article_title(self) -> None:
        ctx = {"cover_copy": {"headline": "Kahit AI, marunong na ring mag-[budol] sa atin."}}
        # No --title override and a brand headline -> None, so render uses cover copy.
        self.assertIsNone(
            build_article_carousel.cover_title_override(None, "AI disinformation rises in PH", ctx)
        )

    def test_explicit_title_override_wins(self) -> None:
        ctx = {"cover_copy": {"headline": "Kahit AI, mag-[budol] na rin."}}
        self.assertEqual(
            build_article_carousel.cover_title_override("My Manual Title", "Article Title", ctx),
            "My Manual Title",
        )

    def test_falls_back_to_article_title_without_brand_headline(self) -> None:
        # Enrichment off / no cover copy -> keep the article title for the cover.
        self.assertEqual(
            build_article_carousel.cover_title_override(None, "Article Title", {}),
            "Article Title",
        )
        self.assertEqual(
            build_article_carousel.cover_title_override(None, "Article Title", {"cover_copy": {}}),
            "Article Title",
        )

    def test_brand_headline_renders_two_tone_accents(self) -> None:
        import build_x_carousel

        markup, plain, has_accent = build_x_carousel.headline_markup_from_brackets(
            "Kahit [AI], marunong na ring mag-[budol] sa digital economy natin."
        )
        self.assertTrue(has_accent)
        self.assertEqual(markup.count('class="accent"'), 2)
        self.assertIn("AI", plain)
        self.assertIn("budol", plain)
        self.assertNotIn("[", plain)

    def test_cover_copy_repairs_missing_or_phrase_accents(self) -> None:
        import build_x_carousel

        repaired = build_x_carousel.normalize_cover_copy(
            {
                "cover": {
                    "headline": "Kahit AI, marunong na ring mag-budol sa atin.",
                    "accent_words": ["AI", "budol"],
                }
            }
        )
        self.assertEqual(
            repaired["headline"],
            "Kahit [AI], marunong na ring mag-[budol] sa atin.",
        )

        phrase = build_x_carousel.normalize_cover_copy(
            {"cover": {"headline": "May [AI slop] na naman sa feed mo."}}
        )
        markup, plain, has_accent = build_x_carousel.headline_markup_from_brackets(
            phrase["headline"]
        )
        self.assertTrue(has_accent)
        self.assertEqual(markup.count('class="accent"'), 1)
        self.assertIn('<span class="accent">AI slop</span>', markup)
        self.assertNotIn("[", plain)

    def test_article_title_analysis_uses_article_prompt_context(self) -> None:
        import build_x_carousel

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
                                            "topic": "AI disinformation PH",
                                            "cover": {
                                                "kicker": "THE SIGNAL",
                                                "headline": "AI propaganda, naka-[upgrade] na rin.",
                                                "accent_word": "upgrade",
                                                "swipe_line": "paano? swipe",
                                            },
                                            "instagram_caption": "Hook\n\nSource: https://example.com/story",
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
            analysis = build_x_carousel.gemini_title_analysis(
                [{"author": "News", "handle": "", "text": "AI story", "url": "https://example.com/story"}],
                "AI story",
                "test-key",
                source_type="article",
            )

        self.assertEqual(analysis["topic"], "AI disinformation PH")
        self.assertIn("from an article source", prompts[0])
        self.assertIn("Source article JSON", prompts[0])


class ArticleCoverFallbackTests(unittest.TestCase):
    def test_og_image_fallback_cover_gets_brand_duotone_class(self) -> None:
        import build_x_carousel

        og_markup = build_x_carousel.title_visual_markup({"image_provider": "article_og_image"})
        self.assertIn("is-og-fallback", og_markup)

        generated_markup = build_x_carousel.title_visual_markup({"image_provider": "openai"})
        self.assertNotIn("is-og-fallback", generated_markup)


if __name__ == "__main__":
    unittest.main()
