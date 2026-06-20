import unittest
import argparse
from unittest.mock import patch, MagicMock
from pathlib import Path

# Add parent directory to path to resolve local imports cleanly.
import sys
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import build_rss_carousel

class RssCarouselTests(unittest.TestCase):
    @patch("argparse.ArgumentParser.parse_args")
    @patch("build_rss_carousel.load_config")
    @patch("build_rss_carousel.fetch_article_items")
    def test_no_articles(self, mock_fetch, mock_load_config, mock_parse_args):
        mock_parse_args.return_value = argparse.Namespace(
            config=Path("story_sources.json"),
            queue=Path("out/automation/candidates.json"),
            channel=None,
            min_score=None,
            max_pages=6,
            curation_backend="auto",
            out_dir=Path("out/rss_carousel"),
            publish=False,
            dry_run=False,
            upload_r2=False,
            no_title_enrichment=False,
            limit=30,
        )
        mock_load_config.return_value = {
            "article_sources": [{"name": "Test RSS", "feed_url": "http://test.com/feed.xml"}]
        }
        mock_fetch.return_value = []
        
        with patch("builtins.print") as mock_print:
            rc = build_rss_carousel.main()
            self.assertEqual(rc, 0)
            mock_print.assert_any_call("[rss] No recent articles found in RSS feeds.")

    @patch("argparse.ArgumentParser.parse_args")
    @patch("build_rss_carousel.load_config")
    @patch("build_rss_carousel.fetch_article_items")
    @patch("build_rss_carousel.load_queue")
    @patch("build_rss_carousel.save_queue")
    @patch("build_rss_carousel.score_article_item")
    @patch("subprocess.run")
    def test_builds_top_article(
        self, mock_run, mock_score, mock_save_queue, mock_load_queue,
        mock_fetch, mock_load_config, mock_parse_args
    ):
        mock_parse_args.return_value = argparse.Namespace(
            config=Path("story_sources.json"),
            queue=Path("out/automation/candidates.json"),
            channel="vibecodersph",
            min_score=50,
            max_pages=6,
            curation_backend="auto",
            out_dir=Path("out/rss_carousel"),
            publish=True,
            dry_run=True,
            upload_r2=True,
            no_title_enrichment=True,
            limit=30,
        )
        mock_load_config.return_value = {
            "article_sources": [{"name": "Test RSS", "feed_url": "http://test.com/feed.xml"}]
        }
        # Two mock articles
        mock_fetch.return_value = [
            {
                "url": "http://test.com/news/1",
                "title": "Article One",
                "summary": "This is a great story about agents.",
                "source_name": "Test RSS",
                "_source_config": {"name": "Test RSS"}
            },
            {
                "url": "http://test.com/news/2",
                "title": "Article Two",
                "summary": "This is a lower scoring story.",
                "source_name": "Test RSS",
                "_source_config": {"name": "Test RSS"}
            }
        ]
        # Score mapping: first item has 60 (above threshold), second has 40 (below threshold)
        mock_score.side_effect = [
            (60, ["keywords", "numbers"]),
            (40, ["low-signal"])
        ]
        
        mock_load_queue.return_value = {"candidates": []}
        
        # Mock subprocess runs
        mock_run_build = MagicMock()
        mock_run_build.returncode = 0
        mock_run_pub = MagicMock()
        mock_run_pub.returncode = 0
        mock_run.side_effect = [mock_run_build, mock_run_pub]
        
        rc = build_rss_carousel.main()
        self.assertEqual(rc, 0)
        
        # Verify both subprocess run calls (build then publish)
        self.assertEqual(mock_run.call_count, 2)
        
        # First call args (build_article_carousel.py)
        build_args = mock_run.call_args_list[0][0][0]
        self.assertIn("build_article_carousel.py", build_args[1])
        self.assertIn("http://test.com/news/1", build_args)
        self.assertIn("--channel", build_args)
        self.assertIn("vibecodersph", build_args)
        self.assertIn("--no-title-enrichment", build_args)
        
        # Second call args (instagram_publish.py)
        pub_args = mock_run.call_args_list[1][0][0]
        self.assertIn("instagram_publish.py", pub_args[1])
        self.assertIn("--dry-run", pub_args)
        self.assertIn("--upload-r2", pub_args)
        
        # Verify queue was saved with "built" status
        mock_save_queue.assert_called()
        saved_queue = mock_save_queue.call_args[0][1]
        candidates = saved_queue["candidates"]
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["status"], "publish_previewed") # Since dry-run publishing succeeded

    @patch("argparse.ArgumentParser.parse_args")
    @patch("build_rss_carousel.load_config")
    @patch("build_rss_carousel.fetch_article_items")
    @patch("build_rss_carousel.load_queue")
    @patch("build_rss_carousel.score_article_item")
    @patch("build_rss_carousel.article_candidate_id")
    def test_duplicate_guarding(
        self, mock_cid, mock_score, mock_load_queue,
        mock_fetch, mock_load_config, mock_parse_args
    ):
        mock_parse_args.return_value = argparse.Namespace(
            config=Path("story_sources.json"),
            queue=Path("out/automation/candidates.json"),
            channel=None,
            min_score=50,
            max_pages=6,
            curation_backend="auto",
            out_dir=Path("out/rss_carousel"),
            publish=False,
            dry_run=False,
            upload_r2=False,
            no_title_enrichment=False,
            limit=30,
        )
        mock_load_config.return_value = {
            "article_sources": [{"name": "Test RSS", "feed_url": "http://test.com/feed.xml"}]
        }
        mock_fetch.return_value = [
            {
                "url": "http://test.com/news/1",
                "title": "Article One",
                "summary": "This is a great story about agents.",
                "source_name": "Test RSS",
                "_source_config": {"name": "Test RSS"}
            }
        ]
        mock_score.return_value = (60, ["keywords"])
        mock_cid.return_value = "article_mocked_id_1"
        
        # Existing built candidate in candidates list
        mock_load_queue.return_value = {
            "candidates": [
                {
                    "id": "article_mocked_id_1",
                    "status": "built",
                    "article": {"url": "http://test.com/news/1"}
                }
            ]
        }
        
        with patch("builtins.print") as mock_print:
            rc = build_rss_carousel.main()
            self.assertEqual(rc, 0)
            mock_print.assert_any_call("[rss] No new RSS articles passed the scoring filter and min_score threshold.")

if __name__ == "__main__":
    unittest.main()
