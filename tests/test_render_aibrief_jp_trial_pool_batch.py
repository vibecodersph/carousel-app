from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from scripts import render_aibrief_jp_trial_pool_batch as renderer


class TrialBatchHookConstraintTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.pool_path = self.root / "rotation.json"
        self.plan_path = self.root / "batch.json"
        self.pool_entry = {
            "content_hash": "protected-parent",
            "enabled": True,
            "required_hook_phrases": ["中国製AI"],
        }
        self.batch_entry = {
            "parent_content_hash": "protected-parent",
            "experiment_id": "test-hook-variant",
            "scheduled_at": "2026-09-08T19:00:00+09:00",
            "hook": "中国製AIは回答、米国AIは翻訳も拒絶？",
        }

    def write_inputs(self, extra_pool_entries: list[dict] | None = None) -> None:
        self.pool_path.write_text(
            json.dumps(
                {
                    "pool_id": "test-pool",
                    "entries": [self.pool_entry, *(extra_pool_entries or [])],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.plan_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "pool_id": "test-pool",
                    "pool_path": str(self.pool_path),
                    "entries": [self.batch_entry],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def test_accepts_rewritten_hook_retaining_required_phrase(self) -> None:
        self.write_inputs()

        plan, _pool = renderer.load_inputs(self.plan_path)

        self.assertEqual(plan["entries"][0]["hook"], self.batch_entry["hook"])

    def test_rejects_missing_or_rewritten_required_phrase(self) -> None:
        for hook in ("米国AIは翻訳すら拒絶する皮肉", "中国のAIは回答する"):
            with self.subTest(hook=hook):
                self.batch_entry["hook"] = hook
                self.write_inputs()

                with self.assertRaisesRegex(SystemExit, "中国製AI"):
                    renderer.load_inputs(self.plan_path)

    def test_batch_entry_cannot_override_pool_constraint(self) -> None:
        self.batch_entry["hook"] = "米国AIは翻訳すら拒絶する皮肉"
        self.batch_entry["required_hook_phrases"] = []
        self.write_inputs()

        with self.assertRaisesRegex(SystemExit, "中国製AI"):
            renderer.load_inputs(self.plan_path)

    def test_uses_constraint_only_from_matching_enabled_parent(self) -> None:
        self.batch_entry["parent_content_hash"] = "ordinary-parent"
        self.batch_entry["hook"] = "Another hook"
        self.write_inputs(
            [{"content_hash": "ordinary-parent", "enabled": True}]
        )

        plan, _pool = renderer.load_inputs(self.plan_path)

        self.assertEqual(plan["entries"][0]["hook"], "Another hook")

    def test_disabled_parent_cannot_be_rendered(self) -> None:
        self.pool_entry["enabled"] = False
        self.write_inputs()

        with self.assertRaisesRegex(RuntimeError, "not enabled in pool"):
            renderer.load_inputs(self.plan_path)


if __name__ == "__main__":
    unittest.main()
