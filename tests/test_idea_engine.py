from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from idea_engine.curation import (
    HOOK_MAX_EN_WORDS,
    HOOK_MAX_JA_CHARS,
    ITEM_MAX_LINES,
    estimated_item_lines,
    hook_length,
    run_idea_engine,
    validate_carousel,
)


class IdeaEngineCarouselJsonTests(unittest.TestCase):
    def test_generated_candidate_becomes_one_carousel_with_item_pages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "ph_carousels.json"
            result = run_idea_engine(
                lens="ph_builder",
                count=1,
                provider="local",
                out_path=out_path,
                set_category="oss",
                axis="cheapest",
                twist="own_money_tested",
                candidate_pool=1,
            )
            self.assertEqual(result["carousel_count"], 1)
            self.assertEqual(result["validation_errors"], {})
            payload = json.loads(out_path.read_text(encoding="utf-8"))
            carousel = payload["carousels"][0]
            self.assertEqual(carousel["page_order"][0], "cover_page")
            self.assertEqual(carousel["page_order"][-1], "cta")
            self.assertRegex(carousel["source_candidate"]["title"], r"\d")
            self.assertLessEqual(
                hook_length(carousel["source_candidate"]["title"], "ph_builder"),
                HOOK_MAX_EN_WORDS,
            )
            self.assertLessEqual(
                hook_length(carousel["cover_page"]["headline"], "ph_builder"),
                HOOK_MAX_EN_WORDS,
            )
            self.assertEqual(carousel["source_candidate"]["hook"]["max"], HOOK_MAX_EN_WORDS)
            self.assertEqual(carousel["research_method"]["hook_topic"], carousel["source_candidate"]["title"])
            self.assertIn("source_candidate.items fixed", " ".join(carousel["research_method"]["steps"]))
            self.assertTrue(carousel["cover_page"]["alt_text"])
            self.assertTrue(carousel["cta"]["alt_text"])
            self.assertIn("Research notes:", carousel["instagram_caption"])
            self.assertIn("item_1", carousel)
            self.assertIn("item_2", carousel)
            self.assertEqual(carousel["item_1"]["type"], "item")
            self.assertTrue(carousel["item_1"]["alt_text"])
            self.assertTrue(carousel["item_1"]["proof_points"])
            self.assertTrue(carousel["item_1"]["best_for"])
            self.assertTrue(carousel["item_1"]["watch_out"])
            first_source_url = carousel["item_1"]["sources"][0]["url"]
            if first_source_url:
                self.assertIn(first_source_url, carousel["instagram_caption"])
            item_name = carousel["item_1"]["item_name"]
            for field in ("headline", "body", "takeaway", "best_for", "watch_out"):
                self.assertLessEqual(
                    estimated_item_lines(carousel["item_1"][field], "ph_builder", item_name, field),
                    ITEM_MAX_LINES,
                )
            self.assertEqual(validate_carousel(carousel), [])

    def test_japanese_candidate_hook_stays_under_character_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "jp_carousels.json"
            result = run_idea_engine(
                lens="jp_business",
                count=1,
                provider="local",
                out_path=out_path,
                set_category="frameworks",
                axis="enterprise_safe",
                twist="overseas_arbitrage",
                candidate_pool=1,
            )
            self.assertEqual(result["carousel_count"], 1)
            self.assertEqual(result["validation_errors"], {})
            carousel = json.loads(out_path.read_text(encoding="utf-8"))["carousels"][0]
            self.assertRegex(carousel["source_candidate"]["title"], r"\d")
            self.assertLessEqual(
                hook_length(carousel["source_candidate"]["title"], "jp_business"),
                HOOK_MAX_JA_CHARS,
            )
            self.assertLessEqual(
                hook_length(carousel["cover_page"]["headline"], "jp_business"),
                HOOK_MAX_JA_CHARS,
            )
            self.assertEqual(carousel["source_candidate"]["hook"]["unit"], "characters")
            self.assertTrue(carousel["cover_page"]["alt_text"])
            self.assertIn("調査メモ", carousel["instagram_caption"])
            self.assertEqual(validate_carousel(carousel), [])

    def test_local_batches_prefer_unique_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "ph_carousels.json"
            result = run_idea_engine(
                lens="ph_builder",
                count=3,
                provider="local",
                out_path=out_path,
            )
            self.assertEqual(result["carousel_count"], 3)
            self.assertEqual(result["validation_errors"], {})
            carousels = json.loads(out_path.read_text(encoding="utf-8"))["carousels"]
            hooks = [carousel["source_candidate"]["title"] for carousel in carousels]
            self.assertEqual(len(hooks), len(set(hooks)))
            self.assertTrue(all(any(char.isdigit() for char in hook) for hook in hooks))
            self.assertFalse(any(hook.startswith("1 ") for hook in hooks))

    def test_legacy_stories_are_converted_one_story_to_one_carousel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_path = Path(tmp) / "stories.json"
            out_path = Path(tmp) / "carousels.json"
            source_path.write_text(
                json.dumps(
                    {
                        "stories": [
                            {
                                "headline": "Walang Sponcon: Pinakamurang OSS LLM Tech Stack na Sinubukan Ko Gamit ang Sariling Pera",
                                "body": "Hindi ito galing sa marketing pitch. Mismo kaming gumastos para i-test kung paano makatipid sa compute gamit ang vLLM at LiteLLM nang hindi sumasabog ang server bill.",
                                "source_text": "\n".join(
                                    [
                                        "Title: Walang Sponcon",
                                        "Angle: vLLM and LiteLLM tested with own money.",
                                        "Items: vLLM, LiteLLM",
                                        "Formula: oss ranked by cheapest for ph_builder with own_money_tested",
                                    ]
                                ),
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            result = run_idea_engine(
                lens="ph_builder",
                count=10,
                provider="local",
                from_stories=source_path,
                out_path=out_path,
            )
            self.assertEqual(result["carousel_count"], 1)
            carousel = json.loads(out_path.read_text(encoding="utf-8"))["carousels"][0]
            self.assertEqual(carousel["cover_page"]["headline"], carousel["source_candidate"]["title"])
            self.assertRegex(carousel["source_candidate"]["title"], r"\d")
            self.assertLessEqual(
                hook_length(carousel["source_candidate"]["title"], "ph_builder"),
                HOOK_MAX_EN_WORDS,
            )
            self.assertEqual(carousel["page_order"], ["cover_page", "item_1", "item_2", "cta"])
            self.assertEqual(carousel["item_1"]["item_name"], "vLLM")
            self.assertEqual(carousel["item_2"]["item_name"], "LiteLLM")
            self.assertEqual(validate_carousel(carousel), [])

    def test_validation_reports_overlong_hooks(self) -> None:
        carousel = {
            "lens": "ph_builder",
            "page_order": ["cover_page", "item_1", "cta"],
            "source_candidate": {
                "title": (
                    "one two three four five six seven eight nine ten eleven "
                    "twelve thirteen fourteen fifteen"
                ),
                "items": ["vLLM"],
            },
            "cover_page": {
                "headline": (
                    "one two three four five six seven eight nine ten eleven "
                    "twelve thirteen fourteen fifteen"
                )
            },
            "item_1": {"item_name": "vLLM", "headline": "vLLM", "body": "A useful test body."},
            "cta": {"headline": "Save"},
        }
        errors = validate_carousel(carousel)
        self.assertIn("source_candidate.title exceeds 14 words (15)", errors)
        self.assertIn("cover_page.headline exceeds 14 words (15)", errors)

    def test_validation_reports_item_copy_over_two_lines(self) -> None:
        carousel = {
            "lens": "ph_builder",
            "page_order": ["cover_page", "item_1", "cta"],
            "source_candidate": {"title": "3 checks before vLLM", "items": ["vLLM"]},
            "cover_page": {"headline": "3 checks before vLLM"},
            "item_1": {
                "item_name": "vLLM",
                "headline": "Quick verdict",
                "body": (
                    "This body is intentionally too long for a compact carousel item page "
                    "because it keeps adding caveats, context, setup detail, migration advice, "
                    "pricing notes, security reminders, and extra decision criteria."
                ),
                "takeaway": "Test one workflow first.",
                "proof_points": ["vLLM documents its high-throughput serving engine."],
                "best_for": "Best for self-hosted inference.",
                "watch_out": "Check setup time.",
                "sources": [{"title": "vLLM", "url": "https://github.com/vllm-project/vllm"}],
            },
            "cta": {"headline": "Save"},
        }
        self.assertIn("item_1.body exceeds 2 estimated lines", validate_carousel(carousel))


if __name__ == "__main__":
    unittest.main()
