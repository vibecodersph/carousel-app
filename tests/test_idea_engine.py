from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from idea_engine.curation import run_idea_engine, validate_carousel


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
            self.assertIn("item_1", carousel)
            self.assertIn("item_2", carousel)
            self.assertEqual(carousel["item_1"]["type"], "item")
            self.assertEqual(validate_carousel(carousel), [])

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
            self.assertEqual(carousel["page_order"], ["cover_page", "item_1", "item_2", "cta"])
            self.assertEqual(carousel["item_1"]["item_name"], "vLLM")
            self.assertEqual(carousel["item_2"]["item_name"], "LiteLLM")
            self.assertEqual(validate_carousel(carousel), [])


if __name__ == "__main__":
    unittest.main()
