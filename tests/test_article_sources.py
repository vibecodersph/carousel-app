import json
import unittest
from pathlib import Path
from unittest.mock import patch

import build_article_carousel
import story_scout


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

    def test_brand_headline_renders_single_two_tone_accent(self) -> None:
        import build_x_carousel

        markup, plain, has_accent = build_x_carousel.headline_markup_from_brackets(
            "Kahit AI, marunong na ring mag-[budol] sa digital economy natin."
        )
        self.assertTrue(has_accent)
        self.assertEqual(markup.count('class="accent"'), 1)
        self.assertIn("budol", plain)
        self.assertNotIn("[", plain)

    def test_cover_copy_repairs_missing_or_phrase_accent(self) -> None:
        import build_x_carousel

        repaired = build_x_carousel.normalize_cover_copy(
            {
                "cover": {
                    "headline": "Kahit AI, marunong na ring mag-budol sa atin.",
                    "accent_word": "budol",
                }
            }
        )
        self.assertEqual(
            repaired["headline"],
            "Kahit AI, marunong na ring mag-[budol] sa atin.",
        )

        phrase = build_x_carousel.normalize_cover_copy(
            {"cover": {"headline": "May [AI slop] na naman sa feed mo."}}
        )
        markup, plain, has_accent = build_x_carousel.headline_markup_from_brackets(
            phrase["headline"]
        )
        self.assertTrue(has_accent)
        self.assertEqual(markup.count('class="accent"'), 1)
        self.assertIn('<span class="accent">slop</span>', markup)
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
