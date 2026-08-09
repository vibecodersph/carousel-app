from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from scripts import merge_llm_candidate_evaluations as merge_script


class MergeLlmCandidateEvaluationsTests(unittest.TestCase):
    def test_in_place_merge_preserves_content_addressed_base(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir) / "report.json"
            raw = '{"summary":{"candidates":1}}\n'
            base.write_text(raw, encoding="utf-8")

            preserved = merge_script._preserve_in_place_base(base, base)

            digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
            self.assertEqual(
                preserved,
                base.with_name(f"report.premerge.{digest}.json"),
            )
            self.assertEqual(preserved.read_text(encoding="utf-8"), raw)
            self.assertEqual(base.read_text(encoding="utf-8"), raw)

    def test_distinct_output_uses_original_base_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir) / "base.json"
            output = Path(temp_dir) / "merged.json"
            base.write_text("{}\n", encoding="utf-8")

            self.assertEqual(
                merge_script._preserve_in_place_base(base, output),
                base,
            )
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
