import json
import tempfile
import unittest
from pathlib import Path

import reel_candidate_evaluator as evaluator


def winner(
    media_id: str,
    *,
    script_asset_id: str,
    source_video_id: str,
    uploader: str,
    duration: float,
    tier: str = "ATTENTION_REPLAY_SPECIALIST",
):
    return {
        "identity": {
            "media_id": media_id,
            "permalink": f"https://www.instagram.com/reel/{media_id}/",
        },
        "content": {
            "published_hook": {
                "value": f"公開フック {media_id}",
                "source": "published_caption_first_line",
                "confidence": "high",
            },
            "script_asset_id": script_asset_id,
            "source_selection_hook": "Ask Claude to write the failing test first",
            "source_hook_variants": [
                "Claude makes test driven development practical",
                "Write the failing test before the code",
            ],
            "source_transcript": {
                "status": "AVAILABLE",
                "text": (
                    "Claude writes a failing test first, verifies the failure, "
                    "then writes code until the test passes."
                ),
            },
            "opening_japanese_script": ["失敗するテストを先に書かせます。"],
            "duration_seconds": duration,
        },
        "source": {
            "video_id": source_video_id,
            "title": "A practical Claude coding workflow",
            "uploader": uploader,
            "chapter": "Test driven development and verification",
        },
        "winner_evidence": {
            "actual_age_hours": 24.2,
            "tier": tier,
            "signal_families": ["attention_replay"],
            "ranking_memberships": [
                {
                    "label": "Watch depth",
                    "rank": 1,
                    "value": 0.8,
                }
            ],
            "aggregate": {
                "rank": 2 if tier == "BALANCED_REFERENCE" else None,
            },
            "all_metrics_at_window": {
                "total_interactions_per_reach": {
                    "value": 0.02,
                    "supporting_metrics": {
                        "interactions": 4,
                        "reach": 200,
                        "denominator_type": "reach",
                    },
                },
                "watch_depth": {
                    "value": 0.8,
                    "supporting_metrics": {
                        "average_watch_time_seconds": 24,
                        "duration_seconds": 30,
                    },
                },
                "three_second_skip_rate": {
                    "value": 35,
                    "supporting_metrics": {"reels_skip_rate": 35},
                },
                "saves_per_1000_reach": {
                    "value": 10,
                    "supporting_metrics": {
                        "saves": 2,
                        "reach": 200,
                        "denominator_type": "reach",
                    },
                },
                "views_per_reached_account": {
                    "value": 1.2,
                    "supporting_metrics": {
                        "views": 240,
                        "reach": 200,
                        "denominator_type": "reach",
                    },
                },
            },
        },
        "evidence_flags": [],
    }


def candidate_payload(*, transcript: str):
    return {
        "caption_source": "auto:en-orig",
        "selection_mode": "ai",
        "selection_profile": "ai-tech",
        "selection_profile_version": 5,
        "candidate_reconciliation_version": 1,
        "prompt_versions": {"generator": 4, "final_discriminator": 5},
        "prompt_lineage_sha256": "fixture-lineage",
        "clips": [
            {
                "index": 1,
                "slug": "001-ask-claude-to-write-the-failing-test-first",
                "start": 10.25,
                "end": 40.25,
                "duration": 30.0,
                "score": 9,
                "hook_score": 9,
                "value_score": 9,
                "one_liner": "Ask Claude to write the failing test first.",
                "hook_variants": [
                    "Ask Claude to write the failing test first.",
                    "Make test driven development practical with Claude.",
                ],
                "reason": (
                    "A concrete test driven development workflow with a clear "
                    "verification payoff."
                ),
                "source_chapter": "Test driven development and verification",
                "transcript": transcript,
                "opening_assessment": {
                    "score": 9,
                    "reason": "Immediate practical instruction.",
                    "source": "final_discriminator",
                },
            }
        ],
    }


class ReelCandidateEvaluatorTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.config = evaluator.load_config()
        self.winner_library = {
            "library_metadata": {
                "schema_version": 1,
                "generated_at": "2026-07-30T00:00:00+00:00",
                "account": "aibrief_jp",
                "platform": "instagram",
                "maturity_window": "24h",
            },
            "winners": [
                winner(
                    "winner-1",
                    script_asset_id="script-1",
                    source_video_id="source-1",
                    uploader="Uploader One",
                    duration=29,
                    tier="BALANCED_REFERENCE",
                ),
                winner(
                    "winner-1-repost",
                    script_asset_id="script-1",
                    source_video_id="source-1",
                    uploader="Uploader One",
                    duration=29,
                ),
                winner(
                    "winner-2",
                    script_asset_id="script-2",
                    source_video_id="source-2",
                    uploader="Uploader Two",
                    duration=31,
                ),
                winner(
                    "winner-3",
                    script_asset_id="script-3",
                    source_video_id="source-3",
                    uploader="Uploader Three",
                    duration=35,
                ),
            ],
        }

    def tearDown(self):
        self._tmp.cleanup()

    def write_source(
        self,
        video_id: str,
        payload: dict,
        *,
        uploader: str = "New Uploader",
    ) -> Path:
        source = self.root / video_id
        source.mkdir()
        candidates_path = source / "candidates.json"
        candidates_path.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
        (source / "metadata.json").write_text(
            json.dumps(
                {
                    "id": video_id,
                    "title": "A new Claude coding workflow",
                    "uploader": uploader,
                    "webpage_url": f"https://www.youtube.com/watch?v={video_id}",
                }
            ),
            encoding="utf-8",
        )
        return candidates_path

    def test_builds_deterministic_linked_evaluation_and_dedupes_scripts(self):
        path = self.write_source(
            "new-video",
            candidate_payload(
                transcript=(
                    "Ask Claude to write the failing test first, confirm that "
                    "it fails, write the code, and verify that the test passes."
                )
            ),
        )
        original = path.read_bytes()
        report = evaluator.build_candidate_evaluation(
            [path],
            self.winner_library,
            self.config,
        )

        self.assertEqual(report["report_metadata"]["winner_post_count"], 4)
        self.assertEqual(report["report_metadata"]["winner_script_asset_count"], 3)
        self.assertEqual(report["summary"]["candidates"], 1)
        self.assertEqual(
            report["summary"]["review_queue"][0]["candidate_id"],
            "new-video:10250-40250",
        )
        evaluation = report["sources"][0]["evaluations"][0]
        self.assertEqual(
            evaluation["candidate"]["candidate_id"],
            "new-video:10250-40250",
        )
        self.assertEqual(len(evaluation["nearest_measured_analogues"]), 3)
        self.assertEqual(
            len(
                {
                    item["source_video_id"]
                    for item in evaluation["nearest_measured_analogues"]
                }
            ),
            3,
        )
        self.assertTrue(
            all(
                item["permalink"].startswith("https://www.instagram.com/reel/")
                for item in evaluation["nearest_measured_analogues"]
            )
        )
        self.assertEqual(
            evaluation["decision"]["recommendation"],
            evaluator.DECISION_REPLICATION,
        )
        self.assertEqual(
            evaluation["decision"]["confidence_scope"],
            "comparison quality only",
        )
        self.assertIsNone(evaluation["decision"]["outcome_prediction"])
        self.assertEqual(path.read_bytes(), original)

        first_json = evaluator.render_candidate_evaluation_json(report)
        second_json = evaluator.render_candidate_evaluation_json(report)
        first_markdown = evaluator.render_candidate_evaluation_markdown(report)
        second_markdown = evaluator.render_candidate_evaluation_markdown(report)
        self.assertEqual(first_json, second_json)
        self.assertEqual(first_markdown, second_markdown)
        self.assertNotIn("NaN", first_json)
        self.assertNotIn("Infinity", first_json)
        self.assertIn("https://www.instagram.com/reel/winner-1/", first_markdown)
        self.assertIn("Similarity retrieves analogues", first_markdown)

    def test_preserves_empty_reconciled_source(self):
        path = self.write_source(
            "empty-video",
            {
                "selection_mode": "ai",
                "selection_profile": "ai-tech",
                "selection_profile_version": 5,
                "candidate_reconciliation_version": 1,
                "clips": [],
            },
        )
        work = path.parent / "work"
        work.mkdir()
        discriminator_path = work / "ai_candidate_discriminator.json"
        discriminator_path.write_text(
            json.dumps(
                {
                    "stage": "final_discriminator",
                    "selection_profile": "ai-tech",
                    "selection_profile_version": 5,
                    "kept_count": 0,
                    "judgments": [
                        {
                            "index": 1,
                            "start": 10.25,
                            "end": 40.25,
                            "duration": 30,
                            "keep": False,
                            "raw_keep": False,
                            "one_liner": (
                                "Ask Claude to write the failing test first."
                            ),
                            "hook_variants": [],
                            "reason": "Originally rejected despite a concrete workflow.",
                            "source_chapter": (
                                "Test driven development and verification"
                            ),
                            "transcript": (
                                "Claude writes the failing test first, verifies "
                                "the failure, and then writes code until it passes."
                            ),
                            "score": 6,
                            "hook_score": 8,
                            "value_score": 6,
                            "opening_assessment": {"score": 6},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        original_discriminator = discriminator_path.read_bytes()
        report = evaluator.build_candidate_evaluation(
            [path],
            self.winner_library,
            self.config,
        )
        source = report["sources"][0]
        self.assertEqual(source["status"], "NO_RECONCILED_CANDIDATES")
        self.assertEqual(source["candidate_count"], 0)
        self.assertEqual(source["evaluations"], [])
        self.assertEqual(report["summary"]["empty_sources"], 1)
        audit = source["false_negative_audit"]
        self.assertEqual(audit["status"], "AUDITED")
        self.assertEqual(audit["rejected_judgment_count"], 1)
        self.assertEqual(audit["suspect_count"], 1)
        self.assertEqual(audit["automatic_promotions"], 0)
        self.assertEqual(
            audit["review_queue"][0]["verdict"],
            "POSSIBLE_FALSE_NEGATIVE",
        )
        self.assertEqual(discriminator_path.read_bytes(), original_discriminator)
        markdown = evaluator.render_candidate_evaluation_markdown(report)
        self.assertIn("Empty-batch false-negative audit", markdown)
        self.assertIn("No rejected candidate was restored automatically", markdown)

    def test_missing_transcript_is_insufficient_evidence(self):
        path = self.write_source(
            "missing-transcript",
            candidate_payload(transcript=""),
        )
        report = evaluator.build_candidate_evaluation(
            [path],
            self.winner_library,
            self.config,
        )
        decision = report["sources"][0]["evaluations"][0]["decision"]
        self.assertEqual(
            decision["recommendation"],
            evaluator.DECISION_INSUFFICIENT,
        )

    def test_missing_opening_score_is_unavailable_not_a_failure(self):
        payload = candidate_payload(
            transcript=(
                "Claude writes the failing test first and then writes code "
                "until the test passes."
            )
        )
        payload["clips"][0]["opening_assessment"] = {}
        path = self.write_source("missing-opening-score", payload)
        report = evaluator.build_candidate_evaluation(
            [path],
            self.winner_library,
            self.config,
        )
        decision = report["sources"][0]["evaluations"][0]["decision"]
        self.assertEqual(
            decision["recommendation"],
            evaluator.DECISION_INSUFFICIENT,
        )
        self.assertIn("unavailable evidence", decision["reason"])
        self.assertNotEqual(decision["recommendation"], evaluator.DECISION_REVISE)

    def test_known_score_failure_still_revises_when_another_score_is_missing(self):
        payload = candidate_payload(
            transcript=(
                "Claude writes the failing test first and then writes code "
                "until the test passes."
            )
        )
        payload["clips"][0]["value_score"] = 7.5
        payload["clips"][0]["opening_assessment"] = {}
        path = self.write_source("known-failure-and-missing", payload)
        report = evaluator.build_candidate_evaluation(
            [path],
            self.winner_library,
            self.config,
        )
        decision = report["sources"][0]["evaluations"][0]["decision"]
        self.assertEqual(
            decision["recommendation"],
            evaluator.DECISION_REVISE,
        )
        self.assertIn("value score 7.5 < 8", decision["reason"])

    def test_exact_entity_anchor_matches_before_sentence_punctuation(self):
        candidate = {
            "hook": "How Microsoft manages 20 million AI agents.",
            "transcript": (
                "There may be 20 million agents at Microsoft. They need to be "
                "fully inspectable and auditable."
            ),
            "source_chapter": "",
            "source": {"title": ""},
        }
        support = evaluator.hook_support(candidate)
        self.assertIn(
            "entity:microsoft",
            support["supported_anchors"],
        )
        self.assertNotIn(
            "entity:microsoft",
            support["hard_mismatches"],
        )

    def test_only_allowed_decision_vocabulary_is_emitted(self):
        path = self.write_source(
            "allowed-decisions",
            candidate_payload(
                transcript=(
                    "Claude writes the failing test first and then writes code "
                    "until the test passes."
                )
            ),
        )
        report = evaluator.build_candidate_evaluation(
            [path],
            self.winner_library,
            self.config,
        )
        allowed = {
            evaluator.DECISION_REPLICATION,
            evaluator.DECISION_NOVEL,
            evaluator.DECISION_REVISE,
            evaluator.DECISION_HOLD,
            evaluator.DECISION_INSUFFICIENT,
        }
        emitted = {
            evaluation["decision"]["recommendation"]
            for source in report["sources"]
            for evaluation in source["evaluations"]
        }
        self.assertTrue(emitted.issubset(allowed))


if __name__ == "__main__":
    unittest.main()
