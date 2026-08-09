from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import llm_reel_candidate_evaluator as llm_eval


WINNER_REF = llm_eval._opaque_ref("winner", "media-1")
CANDIDATE_REF = llm_eval._opaque_ref(
    "candidate",
    "source-new:1000-41000",
)


def winner_library() -> dict[str, Any]:
    winners = []
    metric_categories = llm_eval.CATEGORIES[:-1]
    for rank in range(1, 11):
        memberships = [
            {
                "leaderboard": category,
                "rank": rank,
                "value": 0.1 + rank / 100,
                "direction": (
                    "lower"
                    if category == "three_second_skip_rate"
                    else "higher"
                ),
                "directional_percentile": 100 - rank,
                "cohort_size": 70,
                "actual_age_hours": 24.25,
                "supporting_metrics": {"reach": 1000 + rank},
            }
            for category in metric_categories
        ]
        winners.append(
            {
                "identity": {
                    "media_id": f"media-{rank}",
                    "permalink": f"https://instagram.test/reel/{rank}",
                },
                "source": {
                    "video_id": f"source-{rank}",
                    "title": f"Winner source {rank}",
                    "uploader": f"Uploader {rank}",
                    "url": f"https://youtube.test/{rank}",
                },
                "content": {
                    "duration_seconds": 30 + rank,
                    "published_hook": {
                        "value": f"Published winner hook {rank}"
                    },
                    "source_selection_hook": f"Winner hook {rank}",
                    "source_hook_variants": [f"Variant {rank}"],
                    "source_transcript": {
                        "text": f"winner transcript {rank} with a concrete payoff"
                    },
                    "japanese_script": {
                        "text": f"Japanese winner script {rank}"
                    },
                    "script_asset_id": f"asset-{rank}",
                },
                "evidence_flags": [],
                "winner_evidence": {
                    "ranking_memberships": memberships,
                    "aggregate": {
                        "rank": rank,
                        "average_directional_percentile": 90 - rank,
                        "cohort_size": 70,
                        "actual_age_hours": 24.25,
                        "components": {},
                    },
                },
            }
        )
    return {
        "library_metadata": {
            "schema_version": 1,
            "maturity_window": "24h",
            "account": "aibrief_jp",
            "platform": "instagram",
        },
        "winners": winners,
    }


def candidate() -> dict[str, Any]:
    return {
        "candidate_id": "source-new:1000-41000",
        "candidate_origin": "reconciled_candidate",
        "index": 1,
        "slug": "001-candidate",
        "hook": "A concrete candidate hook.",
        "hook_variants": ["Candidate variant"],
        "transcript": "candidate transcript with a concrete payoff",
        "source_chapter": "Candidate chapter",
        "start_seconds": 1.0,
        "end_seconds": 41.0,
        "duration_seconds": 40.0,
        "source_timestamp_url": "https://youtube.test/new?t=1",
        "selection_scores": {
            "overall": 9.0,
            "hook": 9.0,
            "value": 9.0,
            "opening": 9.0,
        },
        "source": {
            "video_id": "source-new",
            "title": "New source",
            "uploader": "New uploader",
            "url": "https://youtube.test/new",
        },
    }


class FakeAdapter:
    provider = "gemini"
    endpoint = llm_eval.GEMINI_OPENAI_COMPAT_BASE_URL
    model = "fake-semantic-model"
    reasoning_effort = "medium"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def parse(
        self,
        *,
        schema: type[llm_eval.StrictModel],
        instructions: str,
        input_payload: list[dict[str, Any]],
        max_output_tokens: int,
        prompt_cache_key: str,
    ) -> tuple[llm_eval.StrictModel, dict[str, Any]]:
        self.calls.append(
            {
                "schema": schema.__name__,
                "instructions": instructions,
                "input": input_payload,
            }
        )
        if schema is llm_eval.BlindSemanticReview:
            comparisons = []
            for category in llm_eval.CATEGORIES:
                comparisons.append(
                    {
                        "category": category,
                        "fit_hypothesis": "PLAUSIBLE",
                        "analogues": [
                            {
                                "media_id": WINNER_REF,
                                "relation": "CLOSE_MECHANISM",
                                "candidate_evidence_excerpt": (
                                    "candidate transcript with a concrete payoff"
                                ),
                                "winner_evidence_excerpt": (
                                    "winner transcript 1 with a concrete payoff"
                                ),
                                "shared_mechanisms": ["concrete payoff"],
                                "material_differences": ["different source"],
                                "duration_caveat": "Candidate is nine seconds longer.",
                                "why_relevant": "Both reveal a specific mechanism.",
                            }
                        ],
                        "candidate_case": "The payoff is concrete.",
                        "counterevidence": "The source differs.",
                        "uncertainty": "Performance cannot be inferred.",
                    }
                )
            parsed = schema.model_validate(
                {
                    "candidate_id": CANDIDATE_REF,
                    "semantic_profile": {
                        "target_viewer": "AI builders",
                        "core_topic": "AI workflow",
                        "audience_promise": "A concrete workflow",
                        "hook_mechanisms": ["specific reveal"],
                        "curiosity_driver": "unexpected mechanism",
                        "payoff_type": "actionable explanation",
                        "proof_form": "speaker account",
                        "delivery_structure": "claim then explanation",
                        "attention_hypothesis": "specific opening",
                        "action_hypothesis": "reference value",
                        "content_risks": ["duration"],
                    },
                    "claim_support": {
                        "overall_status": "SUPPORTED",
                        "claims": [
                            {
                                "claim": "There is a concrete payoff.",
                                "status": "SUPPORTED",
                                "source_excerpt": (
                                    "candidate transcript with a concrete payoff"
                                ),
                                "start_seconds": 1.0,
                                "end_seconds": 41.0,
                                "explanation": "The transcript states it.",
                                "required_revision": None,
                            }
                        ],
                    },
                    "category_comparisons": comparisons,
                    "source_and_topic_saturation": "New source.",
                    "blind_review_summary": "A supported candidate.",
                }
            )
        elif schema is llm_eval.VerifiedCandidateReview:
            parsed = schema.model_validate(
                {
                    "candidate_id": CANDIDATE_REF,
                    "verifier_audit": {
                        "blind_analogue_quality": "CONFIRMED",
                        "claim_support_check": "CONFIRMED",
                        "surface_match_risk": "Low",
                        "citation_issues": [],
                        "duration_confounding": "Visible and material",
                        "causal_or_predictive_language_removed": "None used",
                    },
                    "category_interpretations": [
                        {
                            "category": category,
                            "fit_after_metrics": "PLAUSIBLE",
                            "supporting_analogue_ids": [WINNER_REF],
                            "evidence_interpretation": (
                                "Observed evidence supports a test hypothesis."
                            ),
                            "important_difference": "Different source.",
                            "caveat": "No prediction.",
                            "conclusion": "Use as a bounded trial.",
                        }
                        for category in llm_eval.CATEGORIES
                    ],
                    "cross_category_synthesis": {
                        "credible_categories": list(llm_eval.CATEGORIES),
                        "independent_signal_families_supported": [
                            "ATTENTION_REPLAY",
                            "INTENT_ACTION",
                        ],
                        "strongest_mechanisms": ["specific payoff"],
                        "important_differences_from_winners": [
                            "different duration"
                        ],
                        "evidence_status": "DIRECTIONAL_ANALOGUE_EVIDENCE",
                    },
                    "decision": {
                        "label": "ADVANCE_AS_TRIAL",
                        "confidence": "MEDIUM",
                        "confidence_scope": "editorial interpretation only",
                        "reason": "Promising but not predictive.",
                        "strong_points": ["supported payoff"],
                        "blocking_risks": [],
                        "must_fix_before_use": [],
                        "revision_plan": None,
                        "test_hypothesis": "Test the concrete reveal.",
                        "primary_measurement": "3-second skip rate",
                        "non_prediction_statement": (
                            "Analogue evidence is observational and does not "
                            "predict candidate performance."
                        ),
                    },
                }
            )
        else:
            raise AssertionError(schema)
        return parsed, {
            "response_id": f"fake-{len(self.calls)}",
            "model": self.model,
            "status": "completed",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }


class FailingAdapter(FakeAdapter):
    def parse(self, **kwargs: Any) -> tuple[llm_eval.StrictModel, dict[str, Any]]:
        raise RuntimeError("network unavailable")


class NonVerbatimFakeAdapter(FakeAdapter):
    def parse(
        self,
        **kwargs: Any,
    ) -> tuple[llm_eval.StrictModel, dict[str, Any]]:
        parsed, trace = super().parse(**kwargs)
        if isinstance(parsed, llm_eval.BlindSemanticReview):
            for comparison in parsed.category_comparisons:
                for analogue in comparison.analogues:
                    analogue.winner_evidence_excerpt = "not present in source"
        return parsed, trace


class LLMReelCandidateEvaluatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.blind, self.assets, self.evidence = llm_eval.build_winner_context(
            winner_library()
        )

    def runner(
        self,
        adapter: FakeAdapter,
    ) -> llm_eval.LLMRunner:
        return llm_eval.LLMRunner(
            adapter,  # type: ignore[arg-type]
            cache=llm_eval.RequestCache(None, enabled=False),
            prompt_cache_key="test",
        )

    def test_blind_pack_hides_metric_values_and_rank(self) -> None:
        text = llm_eval._json(self.blind)
        self.assertNotIn('"directional_percentile"', text)
        self.assertNotIn('"supporting_metrics"', text)
        self.assertNotIn('"rank":', text)
        self.assertNotIn("instagram.test", text)
        self.assertNotIn("youtube.test", text)
        self.assertNotIn("Uploader", text)
        self.assertNotIn("Japanese winner script", text)
        self.assertNotIn('"media_id"', text)
        self.assertNotIn("source_hook_variants", text)
        self.assertNotIn("Variant 1", text)
        self.assertIn("Published winner hook 1", text)
        self.assertIn("Winner hook 1", text)
        for category in llm_eval.CATEGORIES:
            self.assertEqual(
                len(self.blind["leaderboard_memberships_without_rank"][category]),
                10,
            )

    def test_two_pass_review_joins_exact_metrics_after_selection(self) -> None:
        adapter = FakeAdapter()
        result = llm_eval.evaluate_candidate_with_llm(
            candidate(),
            origin="RECONCILED_CANDIDATE",
            runner=self.runner(adapter),
            blind_pack=self.blind,
            assets=self.assets,
            evidence_index=self.evidence,
        )
        self.assertEqual(result["decision"]["label"], "ADVANCE_AS_TRIAL")
        self.assertEqual(len(result["category_comparisons"]), 6)
        first = result["category_comparisons"][0]["analogues"][0]
        self.assertEqual(first["rank"], 1)
        self.assertEqual(first["winner_ref"], WINNER_REF)
        self.assertEqual(first["media_id"], "media-1")
        self.assertEqual(
            first["permalink"],
            "https://instagram.test/reel/1",
        )
        self.assertTrue(first["candidate_citation_verified"])
        self.assertTrue(first["winner_citation_verified"])
        self.assertNotIn("hook_variants", result["candidate"])
        self.assertEqual([call["schema"] for call in adapter.calls], [
            "BlindSemanticReview",
            "VerifiedCandidateReview",
        ])
        blind_candidate_input = adapter.calls[0]["input"][1]["content"]
        self.assertIn("A concrete candidate hook.", blind_candidate_input)
        self.assertNotIn("selection_scores", blind_candidate_input)
        self.assertNotIn("opening_assessment", blind_candidate_input)
        self.assertNotIn("source-new", blind_candidate_input)
        self.assertNotIn("duration_seconds", blind_candidate_input)
        self.assertNotIn("source_chapter", blind_candidate_input)
        self.assertNotIn("hook_variants", blind_candidate_input)
        self.assertNotIn("Candidate variant", blind_candidate_input)
        all_model_inputs = llm_eval._json(
            [call["input"] for call in adapter.calls]
        )
        self.assertNotIn("source_hook_variants", all_model_inputs)
        self.assertNotIn("hook_variants", all_model_inputs)
        self.assertNotIn("Candidate variant", all_model_inputs)
        self.assertNotIn("Variant 1", all_model_inputs)

    def test_hook_variants_are_not_valid_citation_evidence(self) -> None:
        projected = llm_eval.candidate_projection(
            candidate(),
            origin="RECONCILED_CANDIDATE",
        )
        self.assertNotIn("hook_variants", projected)
        self.assertNotIn(
            "Candidate variant",
            llm_eval._candidate_corpus(candidate()),
        )
        self.assertNotIn(
            "Variant 1",
            llm_eval._winner_corpus(self.assets[WINNER_REF]),
        )

    def test_variant_only_changes_do_not_change_evaluation_inputs(self) -> None:
        changed_library = copy.deepcopy(winner_library())
        for row in changed_library["winners"]:
            row["content"]["source_hook_variants"] = [
                "A completely different unused winner variant"
            ]
            row["content"]["current_localized_hook_options"] = [
                "Another unused localized option"
            ]
        changed_candidate = {
            **candidate(),
            "hook_variants": [
                "A completely different unused candidate variant"
            ],
        }

        blind, assets, evidence = llm_eval.build_winner_context(
            changed_library
        )
        self.assertEqual(blind, self.blind)
        self.assertEqual(assets, self.assets)
        self.assertEqual(evidence, self.evidence)
        self.assertEqual(
            llm_eval.candidate_prompt_projection(
                changed_candidate,
                origin="RECONCILED_CANDIDATE",
            ),
            llm_eval.candidate_prompt_projection(
                candidate(),
                origin="RECONCILED_CANDIDATE",
            ),
        )
        self.assertEqual(
            llm_eval._candidate_corpus(changed_candidate),
            llm_eval._candidate_corpus(candidate()),
        )

    def test_invalid_cross_category_winner_id_is_rejected(self) -> None:
        model = llm_eval.BlindSemanticReview.model_validate(
            {
                "candidate_id": CANDIDATE_REF,
                "semantic_profile": {
                    "target_viewer": "x",
                    "core_topic": "x",
                    "audience_promise": "x",
                    "hook_mechanisms": [],
                    "curiosity_driver": "x",
                    "payoff_type": "x",
                    "proof_form": "x",
                    "delivery_structure": "x",
                    "attention_hypothesis": "x",
                    "action_hypothesis": "x",
                    "content_risks": [],
                },
                "claim_support": {
                    "overall_status": "SUPPORTED",
                    "claims": [],
                },
                "category_comparisons": [
                    {
                        "category": category,
                        "fit_hypothesis": "WEAK",
                        "analogues": (
                            [
                                {
                                    "media_id": "not-in-top-ten",
                                    "relation": "SURFACE_ONLY",
                                    "candidate_evidence_excerpt": "x",
                                    "winner_evidence_excerpt": "x",
                                    "shared_mechanisms": [],
                                    "material_differences": [],
                                    "duration_caveat": None,
                                    "why_relevant": "x",
                                }
                            ]
                            if category == llm_eval.CATEGORIES[0]
                            else []
                        ),
                        "candidate_case": "x",
                        "counterevidence": "x",
                        "uncertainty": "x",
                    }
                    for category in llm_eval.CATEGORIES
                ],
                "source_and_topic_saturation": "x",
                "blind_review_summary": "x",
            }
        )
        errors = llm_eval.validate_blind_review(
            model,
            candidate_id=CANDIDATE_REF,
            evidence_index=self.evidence,
        )
        self.assertTrue(any("not in" in error for error in errors))

    def test_api_failure_never_falls_back_to_lexical_decision(self) -> None:
        results = llm_eval._evaluate_many(
            [candidate()],
            origin="RECONCILED_CANDIDATE",
            workers=1,
            runner=self.runner(FailingAdapter()),
            blind_pack=self.blind,
            assets=self.assets,
            evidence_index=self.evidence,
        )
        self.assertEqual(results[0]["review_status"], "API_ERROR")
        self.assertEqual(
            results[0]["analysis_method"],
            "LLM_REQUIRED_NO_FALLBACK",
        )
        self.assertEqual(
            results[0]["decision"]["label"],
            "MANUAL_REVIEW",
        )

    def test_content_addressed_cache_reuses_exact_response(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = llm_eval.RequestCache(Path(directory), enabled=True)
            adapter = FakeAdapter()
            runner = llm_eval.LLMRunner(
                adapter,  # type: ignore[arg-type]
                cache=cache,
                prompt_cache_key="test",
            )
            input_payload = [{"role": "user", "content": "x"}]
            first, first_trace = runner.run(
                schema=llm_eval.BlindSemanticReview,
                instructions="x",
                input_payload=input_payload,
                max_output_tokens=100,
            )
            second, second_trace = runner.run(
                schema=llm_eval.BlindSemanticReview,
                instructions="x",
                input_payload=input_payload,
                max_output_tokens=100,
            )
            self.assertEqual(first.model_dump(), second.model_dump())
            self.assertEqual(len(adapter.calls), 1)
            self.assertEqual(first_trace["cache_status"], "MISS")
            self.assertEqual(second_trace["cache_status"], "HIT")

    def test_structured_content_fallback_accepts_fenced_json(self) -> None:
        parsed = llm_eval._parse_structured_content_fallback(
            llm_eval.FalseNegativeScreenBatch,
            '```json\n{"reviews":[]}\n```',
        )
        self.assertIsInstance(
            parsed,
            llm_eval.FalseNegativeScreenBatch,
        )
        self.assertEqual(parsed.reviews, [])
        self.assertIsNone(
            llm_eval._parse_structured_content_fallback(
                llm_eval.FalseNegativeScreenBatch,
                "not JSON",
            )
        )

    def test_false_negative_priority_requires_exact_ranked_top_five(self) -> None:
        def review(
            index: int,
            *,
            priority: str,
            rank: int | None,
            verdict: str = "LIKELY_FALSE_NEGATIVE",
        ) -> dict[str, Any]:
            return {
                "candidate_id": f"candidate-{index}",
                "verdict": verdict,
                "claim_support_status": "SUPPORTED",
                "distinctive_payoff_present": True,
                "deep_review_priority": priority,
                "deep_review_rank": rank,
                "strongest_category_hypotheses": ["watch_depth"],
                "reason": "Distinctive and supported.",
                "required_revision": None,
            }

        valid = llm_eval.FalseNegativeScreenBatch.model_validate(
            {
                "reviews": [
                    review(index, priority="TOP_5", rank=index)
                    for index in range(1, 6)
                ]
                + [review(6, priority="SECONDARY", rank=None)]
            }
        )
        self.assertEqual(
            llm_eval.validate_false_negative_screen(
                valid,
                expected_ids=[f"candidate-{index}" for index in range(1, 7)],
            ),
            [],
        )
        invalid = llm_eval.FalseNegativeScreenBatch.model_validate(
            {
                "reviews": [
                    review(index, priority="TOP_5", rank=index)
                    for index in range(1, 5)
                ]
                + [
                    review(5, priority="SECONDARY", rank=None),
                    review(
                        6,
                        priority="TOP_5",
                        rank=5,
                        verdict="MANUAL_REVIEW",
                    ),
                ]
            }
        )
        errors = llm_eval.validate_false_negative_screen(
            invalid,
            expected_ids=[f"candidate-{index}" for index in range(1, 7)],
        )
        self.assertTrue(
            any("MANUAL_REVIEW" in error for error in errors)
        )

    def test_false_negative_deep_selection_uses_model_rank(self) -> None:
        screens = [
            {
                "candidate_id": "source:3",
                "verdict": "LIKELY_FALSE_NEGATIVE",
                "deep_review_priority": "TOP_5",
                "deep_review_rank": 3,
            },
            {
                "candidate_id": "source:secondary",
                "verdict": "LIKELY_FALSE_NEGATIVE",
                "deep_review_priority": "SECONDARY",
                "deep_review_rank": None,
            },
            {
                "candidate_id": "source:1",
                "verdict": "POSSIBLE_FALSE_NEGATIVE",
                "deep_review_priority": "TOP_5",
                "deep_review_rank": 1,
            },
            {
                "candidate_id": "source:2",
                "verdict": "LIKELY_FALSE_NEGATIVE",
                "deep_review_priority": "TOP_5",
                "deep_review_rank": 2,
            },
        ]
        _, selected = llm_eval.select_false_negative_deep_rows(
            screens,
            source_order={
                "source:3": 0,
                "source:secondary": 1,
                "source:1": 2,
                "source:2": 3,
            },
            limit=3,
        )
        self.assertEqual(
            [row["candidate_id"] for row in selected],
            ["source:1", "source:2", "source:3"],
        )

    def test_renderer_exposes_false_negative_hook_link_and_rank(self) -> None:
        report = {
            "report_metadata": {},
            "summary": {},
            "review_queue": [],
            "sources": [
                {
                    "video_id": "source",
                    "title": "Source",
                    "candidate_count": 0,
                    "status": "NO_RECONCILED_CANDIDATES",
                    "evaluations": [],
                    "false_negative_audit": {
                        "rejected_rows_screened": 1,
                        "screen_verdict_counts": {
                            "LIKELY_FALSE_NEGATIVE": 1
                        },
                        "deep_reviews_completed": 1,
                        "screen_reviews": [
                            {
                                "candidate_id": "source:1",
                                "hook": "Promising rejected hook",
                                "source_timestamp_url": (
                                    "https://youtube.test/source?t=1"
                                ),
                                "verdict": "LIKELY_FALSE_NEGATIVE",
                                "deep_review_priority": "TOP_5",
                                "deep_review_rank": 1,
                                "reason": "Supported payoff.",
                            }
                        ],
                        "deep_evaluations": [],
                    },
                }
            ],
        }
        rendered = llm_eval.render_llm_candidate_evaluation_markdown(report)
        self.assertIn(
            "[Promising rejected hook](https://youtube.test/source?t=1)",
            rendered,
        )
        self.assertIn("`TOP_5 #1`", rendered)

    def test_report_timestamp_is_displayed_in_jst(self) -> None:
        rendered = llm_eval.render_llm_candidate_evaluation_markdown(
            {
                "report_metadata": {
                    "generated_at": "2026-07-31T04:33:11+00:00",
                },
                "summary": {},
                "review_queue": [],
                "sources": [],
            }
        )
        self.assertIn(
            "Generated (JST): `2026-07-31T13:33:11+09:00`",
            rendered,
        )

    def test_nonverbatim_citations_are_humanized_and_reduce_confidence(self) -> None:
        result = llm_eval.evaluate_candidate_with_llm(
            candidate(),
            origin="RECONCILED_CANDIDATE",
            runner=self.runner(NonVerbatimFakeAdapter()),
            blind_pack=self.blind,
            assets=self.assets,
            evidence_index=self.evidence,
        )
        self.assertTrue(result["citation_warnings"])
        self.assertFalse(
            any(
                WINNER_REF in warning
                for warning in result["citation_warnings"]
            )
        )
        self.assertTrue(
            any(
                "Published winner hook 1" in warning
                for warning in result["citation_warnings"]
            )
        )
        self.assertEqual(
            result["citation_coverage"]["verified_excerpt_checks"],
            6,
        )
        self.assertEqual(result["decision"]["confidence"], "MEDIUM")
        self.assertIsNotNone(
            result["decision"]["confidence_adjustment"]
        )

    def test_metric_superlative_and_causal_hypothesis_are_bounded(self) -> None:
        self.assertEqual(
            llm_eval._sanitize_metric_language(
                "Top-decile performance across both families."
            ),
            "fixed-24h Top-10 membership across both families.",
        )
        hypothesis, warning = llm_eval._normalize_test_hypothesis(
            "Front-loading the payoff alongside B-roll will significantly "
            "lower skip."
        )
        self.assertEqual(
            hypothesis,
            "Test whether front-loading the payoff alongside B-roll may "
            "lower skip.",
        )
        self.assertIsNotNone(warning)
        self.assertEqual(
            llm_eval._sanitize_metric_language(
                'Important difference.; duration_caveat": null,'
            ),
            "Important difference.",
        )

    def test_humanizer_replaces_case_changed_opaque_winner_refs(self) -> None:
        humanized = llm_eval._humanize_model_narrative(
            f"{WINNER_REF.title()} is structurally similar.",
            assets=self.assets,
        )
        self.assertNotIn(WINNER_REF.title(), humanized)
        self.assertIn("Published winner hook 1", humanized)

    def test_merge_replaces_fail_closed_row_and_labels_reasoning_exception(self) -> None:
        def evaluation(
            *,
            status: str,
            label: str,
        ) -> dict[str, Any]:
            return {
                "candidate": {
                    "candidate_id": "scheduled:aibrief_jp:hash-1",
                    "hook": "Scheduled hook",
                    "source_timestamp_url": "https://youtube.test?t=1",
                    "schedule": {
                        "content_hash": "hash-1",
                        "scheduled_at": "2026-08-01T09:00:00+09:00",
                        "current_lane": "regular",
                    },
                },
                "review_status": status,
                "decision": {
                    "label": label,
                    "confidence": "LOW" if status == "API_ERROR" else "MEDIUM",
                    "reason": "Retry result.",
                },
            }

        base = {
            "report_metadata": {"reasoning_effort": "high"},
            "summary": {
                "candidates": 1,
                "candidate_sources": 1,
                "api_errors": 1,
                "decision_counts": {"MANUAL_REVIEW": 1},
            },
            "sources": [
                {
                    "video_id": "source",
                    "evaluations": [
                        evaluation(
                            status="API_ERROR",
                            label="MANUAL_REVIEW",
                        )
                    ],
                }
            ],
        }
        patch_report = {
            "report_metadata": {"reasoning_effort": "medium"},
            "sources": [
                {
                    "video_id": "source",
                    "evaluations": [
                        evaluation(status="COMPLETE", label="REVISE")
                    ],
                }
            ],
        }
        merged = llm_eval.merge_llm_candidate_evaluation_reports(
            base,
            [patch_report],
        )
        row = merged["sources"][0]["evaluations"][0]
        self.assertEqual(merged["summary"]["api_errors"], 0)
        self.assertEqual(
            merged["summary"]["decision_counts"],
            {"REVISE": 1},
        )
        self.assertEqual(row["reasoning_effort_used"], "medium")
        self.assertEqual(
            merged["report_metadata"]["merged_retry_rows"],
            1,
        )

    def test_exact_slug_filter_limits_candidates_before_llm_calls(self) -> None:
        unselected = {
            **candidate(),
            "candidate_id": "source-new:50000-80000",
            "slug": "002-unselected",
            "index": 2,
            "hook": "A second candidate.",
        }
        normalized_source = {
            "video_id": "source-new",
            "title": "New source",
            "status": "AVAILABLE",
            "candidate_count": 2,
            "candidate_file": "/tmp/source-new/candidates.json",
            "candidates": [candidate(), unselected],
        }
        adapter = FakeAdapter()
        with patch.object(
            llm_eval.diagnostic,
            "normalize_candidate_source",
            return_value=normalized_source,
        ):
            report = llm_eval.build_llm_candidate_evaluation(
                [Path("/tmp/source-new/candidates.json")],
                winner_library(),
                {},
                winner_library_path=None,
                model=adapter.model,
                reasoning_effort=adapter.reasoning_effort,
                timeout_seconds=30,
                workers=1,
                cache_dir=None,
                use_cache=False,
                audit_false_negatives=False,
                max_false_negative_deep_reviews=0,
                adapter=adapter,  # type: ignore[arg-type]
                candidate_slugs=["001-candidate"],
            )
        self.assertEqual(report["summary"]["candidates"], 1)
        self.assertEqual(len(adapter.calls), 2)
        selection = report["sources"][0]["candidate_selection"]
        self.assertEqual(selection["source_candidates_before_filter"], 2)
        self.assertEqual(selection["source_candidates_selected"], 1)
        self.assertEqual(
            report["report_metadata"]["candidate_slug_filter"],
            ["001-candidate"],
        )

    def test_scheduled_normalized_source_uses_primary_hook_and_keeps_schedule(self) -> None:
        scheduled_candidate = {
            **candidate(),
            "hook": "実際に予約されたフック",
            "source_selection_hook": "Internal English selection hook.",
            "schedule": {
                "content_hash": "scheduled-hash",
                "scheduled_at": "2026-08-01T09:00:00+09:00",
                "current_lane": "regular",
                "clip_slug": "001-candidate",
            },
        }
        normalized_sources = [
            {
                "video_id": "source-new",
                "title": "New source",
                "status": "AVAILABLE",
                "candidate_count": 1,
                "candidate_file": "/tmp/source-new/candidates.json",
                "candidates": [scheduled_candidate],
            }
        ]
        adapter = FakeAdapter()
        report = llm_eval.build_llm_candidate_evaluation(
            [],
            winner_library(),
            {},
            winner_library_path=None,
            model=adapter.model,
            reasoning_effort=adapter.reasoning_effort,
            timeout_seconds=30,
            workers=1,
            cache_dir=None,
            use_cache=False,
            audit_false_negatives=False,
            max_false_negative_deep_reviews=0,
            adapter=adapter,  # type: ignore[arg-type]
            normalized_sources=normalized_sources,
            candidate_origin="SCHEDULED_CANDIDATE",
            input_scope="scheduled_pipeline_exact_clips",
        )

        model_inputs = llm_eval._json(
            [call["input"] for call in adapter.calls]
        )
        self.assertIn("実際に予約されたフック", model_inputs)
        self.assertNotIn("Internal English selection hook.", model_inputs)
        result_candidate = report["sources"][0]["evaluations"][0]["candidate"]
        self.assertEqual(
            result_candidate["source_selection_hook"],
            "Internal English selection hook.",
        )
        self.assertEqual(
            result_candidate["schedule"]["content_hash"],
            "scheduled-hash",
        )
        self.assertEqual(
            report["report_metadata"]["input_scope"],
            "scheduled_pipeline_exact_clips",
        )

    def test_same_source_only_signal_family_is_not_repeatability(self) -> None:
        independence = llm_eval._analogue_independence(
            [
                {
                    "signal_family": "ATTENTION_REPLAY",
                    "analogues": [
                        {
                            "media_id": "same-source-post",
                            "source": {"video_id": "source-new"},
                            "same_source_as_candidate": True,
                        }
                    ],
                },
                {
                    "signal_family": "INTENT_ACTION",
                    "analogues": [
                        {
                            "media_id": "independent-post",
                            "source": {"video_id": "other-source"},
                            "same_source_as_candidate": False,
                        }
                    ],
                },
            ]
        )
        self.assertEqual(
            independence["same_source_only_signal_families"],
            ["ATTENTION_REPLAY"],
        )
        self.assertEqual(
            independence["independent_source_analogue_posts"],
            1,
        )
        self.assertIn("not repeatability", independence["warning"])


if __name__ == "__main__":
    unittest.main()
