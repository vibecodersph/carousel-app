import copy
import json
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from pathlib import Path

import source_feedback as sf
from scripts import source_recommendations as cli


START = datetime(2026, 8, 1, tzinfo=timezone.utc)
AS_OF = START + timedelta(days=35)


def post(index, day, sid=None, quality="baseline", mode=False, clip=None, bucket="26–45 seconds"):
    sid = sid or f"{index:011d}"
    published = START + timedelta(days=day)
    reach, looping, saves, shares, skip = {
        "baseline": (1000, 1.3, 10, 5, 35),
        "weak": (200, 1.1, 0, 0, 65),
        "strong": (5000, 1.5, 100, 25, 20),
    }[quality]
    windows = {}
    for window, age in (("24h", 25), ("7d", 170)):
        windows[window] = {"captured_at": (published + timedelta(hours=age)).isoformat(),
                           "raw_metrics": {"reach": reach, "views": reach * looping,
                                           "saves": saves, "shares": shares, "reels_skip_rate": skip}}
    return {"identity": {"account": "aibrief_jp", "platform": "instagram", "media_id": str(index),
                         "published_at": published.isoformat(), "permalink": f"https://www.instagram.com/reel/{index}/"},
            "content_metadata": {"source": f"https://www.youtube.com/watch?v={sid}", "trial_reel": mode,
                                 "duration_bucket": bucket, "hook_text": f"Hook {index}"},
            "generation_artifact": {"clip_dir": clip or f"/{sid}/clips/{index}", "source_title": sid,
                                    "source_uploader": "Test publisher"}, "maturity_windows": windows}


def report(targets=None, mode=False):
    return {"report_metadata": {"account": "aibrief_jp", "as_of": AS_OF.isoformat()},
            "posts": [post(i, i / 2, mode=mode) for i in range(12)] + (targets or [])}


def candidate(sid="target00001", **changes):
    return {"source_url": f"https://www.youtube.com/watch?v={sid}", "title": sid,
            "uploader": "Test publisher", "speaker": "Named speaker", "topic": "coding_workflow",
            "rationale": "An explicit before/after with a demonstrated result", "expected_metric": "reach",
            "distribution_mode": "regular", **changes}


def rehash(feedback):
    feedback["feedback_id"] = sf.digest({k: v for k, v in feedback.items() if k != "feedback_id"})


class SourceFeedbackTests(unittest.TestCase):
    def target_group(self, feedback, sid="target00001", window="7d"):
        return next(g for g in feedback["groups"] if g["kind"] == "source" and g["key"] == sid and g["window"] == window)

    def test_weak_and_strong_sources_change_priority_on_mature_evidence(self):
        for quality, status in (("weak", "REDUCE"), ("strong", "PREFER")):
            data = report([post(20+i, 14+i, "target00001", quality) for i in range(3)])
            result = sf.build_feedback(data)
            group = self.target_group(result)
            self.assertEqual(group["status"], status)
            self.assertEqual(group["comparable_clips"], 3)
            self.assertEqual(sf.source_context(result, "target00001", "regular")["priority"], status)
            self.assertEqual(result["coverage"]["historical_posts_without_recommendation"], 15)
            self.assertEqual(result["recommendation_audit"], [])

    def test_reposts_do_not_create_independent_evidence(self):
        targets = [post(20+i, 14+i, "target00001", "weak", clip="/same/clip") for i in range(5)]
        group = self.target_group(sf.build_feedback(report(targets)))
        self.assertEqual(group["distinct_clips"], 1)
        self.assertEqual(group["status"], "INSUFFICIENT_EVIDENCE")

    def test_mode_duration_future_and_same_source_do_not_leak_into_baseline(self):
        target = post(90, 14, "target00001", "weak")
        for peers in ([post(i, i/2, mode=True) for i in range(12)],
                      [post(i, i/2, bucket="0–15 seconds") for i in range(12)],
                      [post(i, 20+i/2) for i in range(12)],
                      [post(i, i/2, sid="target00001") for i in range(12)]):
            data = report()
            data["posts"] = peers + [target]
            result = sf.build_feedback(data)
            observed = next(x for x in result["observations"] if x["media_id"] == "90")
            self.assertEqual(observed["baseline_counts"]["reach"], 0)
            self.assertIsNone(observed["balanced_percentile"])

    def test_24h_cannot_change_priority_and_missing_is_not_zero(self):
        targets = [post(20+i, 14+i, "target00001", "weak") for i in range(3)]
        for p in targets:
            p["maturity_windows"]["7d"] = None
        result = sf.build_feedback(report(targets))
        self.assertEqual(self.target_group(result, window="24h")["status"], "REDUCE")
        self.assertEqual(sf.source_context(result, "target00001", "regular")["priority"], "EXPLORE")
        targets[0]["maturity_windows"]["24h"]["raw_metrics"]["shares"] = None
        result = sf.build_feedback(report(targets))
        obs = next(x for x in result["observations"] if x["media_id"] == "20")
        self.assertIsNone(obs["metrics"]["share_rate"])
        self.assertIsNone(obs["balanced_percentile"])

    def test_no_lifetime_substitution_or_out_of_window_snapshots(self):
        target = post(20, 14, "target00001", "weak")
        target["maturity_windows"]["latest"] = target["maturity_windows"]["7d"]
        target["maturity_windows"]["24h"]["captured_at"] = (START + timedelta(days=16)).isoformat()
        target["maturity_windows"]["7d"]["captured_at"] = (AS_OF + timedelta(days=1)).isoformat()
        result = sf.build_feedback(report([target]))
        self.assertFalse(any(x["media_id"] == "20" for x in result["observations"]))

    def test_graduated_trials_and_unknown_modes_do_not_qualify(self):
        target = post(20, 14, "target00001", "weak", mode=True)
        target["trial_experiment"] = {"graduated_at": (START + timedelta(days=15, hours=12)).isoformat()}
        result = sf.build_feedback(report([target], mode=True))
        self.assertEqual([x["window"] for x in result["observations"] if x["media_id"] == "20"], ["24h"])
        target["content_metadata"]["trial_reel"] = None
        result = sf.build_feedback(report([target], mode=None))
        self.assertTrue(all(x["balanced_percentile"] is None for x in result["observations"]))

    def test_recommendations_join_only_future_publications_once(self):
        baseline = sf.build_feedback(report())
        baseline["as_of"] = (START + timedelta(days=13)).isoformat()
        rehash(baseline)
        first = sf.recommend(baseline, [candidate()], account="aibrief_jp", batch_id="first", count=1,
                             now=START + timedelta(days=13))
        second = sf.recommend(baseline, [candidate()], account="aibrief_jp", batch_id="second", count=1,
                              now=START + timedelta(days=14))
        data = report([post(19, 12, "target00001", "weak")] +
                      [post(20+i, 15+i, "target00001", "weak") for i in range(3)])
        result = sf.build_feedback(data, [first, second])
        self.assertIsNone(next(x for x in result["observations"] if x["media_id"] == "19")["recommendation_id"])
        audits = {x["batch_id"]: x for x in result["recommendation_audit"]}
        self.assertEqual(audits["first"]["published_reels"], 0)
        self.assertEqual(audits["second"]["published_reels"], 3)
        self.assertEqual(audits["second"]["windows"]["7d"]["expected_metric_result"], "MISS")

    def test_thin_action_counts_do_not_establish_a_recommendation_hit(self):
        baseline = sf.build_feedback(report())
        baseline["as_of"] = (START + timedelta(days=13)).isoformat()
        rehash(baseline)
        batch = sf.recommend(baseline, [candidate(expected_metric="share_rate")], account="aibrief_jp",
                             batch_id="shares", count=1, now=START + timedelta(days=13))
        result = sf.build_feedback(report([post(20+i, 15+i, "target00001", "weak") for i in range(3)]), [batch])
        self.assertEqual(result["recommendation_audit"][0]["windows"]["7d"]["expected_metric_result"], "INSUFFICIENT_EVIDENCE")

    def test_portfolio_caps_exploration_and_requires_changed_hypothesis_for_reduced(self):
        result = sf.build_feedback(report([post(20+i, 14+i, "target00001", "weak") for i in range(3)]))
        candidates = [candidate()] + [candidate(f"newsrc{i:05d}") for i in range(6)]
        batch = sf.recommend(result, candidates, account="aibrief_jp", batch_id="test", count=5, now=AS_OF)
        self.assertFalse(batch["entries"][0]["selected"])
        self.assertEqual(batch["selected_count"], 1)
        self.assertEqual(batch["status"], "PARTIAL_EVIDENCE_LIMITED")
        retry = sf.recommend(result, [candidate(changed_hypothesis="Show the actual command instead of abstract discussion")],
                             account="aibrief_jp", batch_id="retry", count=5, now=AS_OF)
        self.assertEqual(retry["selected_count"], 1)

    def test_stale_wrong_account_and_tampered_evidence_rejected(self):
        result = sf.build_feedback(report())
        for changes in ({"now": AS_OF + timedelta(days=5)}, {"account": "another"}):
            kwargs = dict(account="aibrief_jp", batch_id="x", count=1, now=AS_OF)
            kwargs.update(changes)
            with self.assertRaises(ValueError):
                sf.recommend(result, [candidate()], **kwargs)
        result["coverage"]["published_posts"] = 99
        with self.assertRaisesRegex(ValueError, "integrity"):
            sf.recommend(result, [candidate()], account="aibrief_jp", batch_id="x", count=1, now=AS_OF)

    def test_history_and_feedback_are_deterministic_and_detect_tampering(self):
        data = report([post(20+i, 14+i, "target00001", "weak") for i in range(3)])
        self.assertEqual(sf.build_feedback(data), sf.build_feedback(copy.deepcopy(data)))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = cli.write_feedback(data, root / "batches", root / "feedback.json")
            batch = sf.recommend(result, [candidate("newsrc00001")], account="aibrief_jp", batch_id="test", count=1, now=AS_OF)
            path = root / "batches/test.json"
            path.write_text(json.dumps(batch))
            self.assertEqual(sf.read_batches(root / "batches", "aibrief_jp"), [batch])
            batch["recommended_at"] = START.isoformat()
            path.write_text(json.dumps(batch))
            with self.assertRaisesRegex(ValueError, "metadata"):
                sf.read_batches(root / "batches", "aibrief_jp")
            self.assertTrue((root / "feedback.md").exists())

    def test_cli_records_immutable_batch_and_refresh_attributes_results(self):
        before = report()
        before["report_metadata"]["as_of"] = (START + timedelta(days=13)).isoformat()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            batches = root / "batches"
            cli.write_feedback(before, batches, root / "feedback.json")
            (root / "candidates.json").write_text(json.dumps({"candidates": [candidate()]}))
            args = ["--batches-dir", str(batches), "recommend", "--feedback", str(root / "feedback.json"),
                    "--input", str(root / "candidates.json"), "--batch-id", "prospective", "--count", "1",
                    "--markdown-out", str(root / "shortlist.md")]
            with patch.object(cli, "datetime") as clock:
                clock.now.return_value = START + timedelta(days=13)
                self.assertEqual(cli.main(args), 0)
            original = (batches / "prospective.json").read_bytes()
            with self.assertRaises(SystemExit):
                cli.main(args)
            self.assertEqual((batches / "prospective.json").read_bytes(), original)
            after = report([post(20+i, 14+i, "target00001", "weak") for i in range(3)])
            for p in after["posts"][-3:]:
                p["identity"]["content_hash"] = p["identity"]["media_id"]
                p["source_recommendation_binding"] = {"batch_id": "prospective", "content_hash": p["identity"]["media_id"],
                                                        "sealed_at": (START + timedelta(days=13)).isoformat()}
            result = cli.write_feedback(after, batches, root / "feedback.json")
            self.assertEqual(result["recommendation_audit"][0]["windows"]["7d"]["expected_metric_result"], "MISS")

    def test_one_video_cannot_create_uploader_prior(self):
        targets = [post(20+i, 14+i/2, "target00001", "strong") for i in range(6)]
        result = sf.build_feedback(report(targets))
        # Assign the six target clips their own uploader, preserving source count.
        for target in targets:
            target["generation_artifact"]["source_uploader"] = "Single source uploader"
        result = sf.build_feedback(report(targets))
        context = sf.source_context(result, "newsrc00001", "regular", uploader="Single source uploader")
        self.assertEqual(context["priority"], "EXPLORE")

    def test_low_base_and_duplicate_media_fail_safely(self):
        target = post(20, 14, "target00001", "weak")
        target["maturity_windows"]["7d"]["raw_metrics"]["reach"] = 50
        result = sf.build_feedback(report([target]))
        row = next(x for x in result["observations"] if x["media_id"] == "20" and x["window"] == "7d")
        self.assertIn("LOW_BASE_REACH", row["flags"])
        self.assertIsNone(row["balanced_percentile"])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            sf.build_feedback(report([target, copy.deepcopy(target)]))

    def test_small_comparable_subset_cannot_determine_source_priority(self):
        targets = [post(20+i, 14+i/2, "target00001", "weak") for i in range(8)]
        for p in targets[3:]:
            p["content_metadata"]["duration_bucket"] = "over-45 seconds"
        group = self.target_group(sf.build_feedback(report(targets)))
        self.assertEqual(group["comparable_clips"], 3)
        self.assertEqual(group["status"], "INSUFFICIENT_EVIDENCE")

    def test_refresh_preserves_newer_feedback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cli.write_feedback(report(), root / "batches", root / "feedback.json")
            original = (root / "feedback.json").read_bytes()
            earlier = report()
            earlier["report_metadata"]["as_of"] = (AS_OF - timedelta(days=1)).isoformat()
            with self.assertRaisesRegex(ValueError, "backwards"):
                cli.write_feedback(earlier, root / "batches", root / "feedback.json")
            self.assertEqual((root / "feedback.json").read_bytes(), original)

    def test_candidate_reports_include_failures_without_rewriting_clip_verdicts(self):
        feedback = sf.build_feedback(report([post(20+i, 14+i, "target00001", "weak") for i in range(3)]))
        review = {"report_metadata": {"account": "aibrief_jp"},
                  "sources": [{"video_id": "target00001", "evaluations": [{"decision": "REVISE"}]}]}
        appendix = sf.attach_candidate_context(review, feedback)
        self.assertIn("REDUCE", appendix)
        self.assertEqual(review["sources"][0]["evaluations"], [{"decision": "REVISE"}])

    def test_source_identity_is_exact(self):
        self.assertEqual(sf.source_id("https://youtu.be/target00001?t=30"), "target00001")
        self.assertIsNone(sf.source_id("https://notyoutube.com/watch?v=target00001"))
        self.assertIsNone(sf.source_id("a title about target00001"))


if __name__ == "__main__":
    unittest.main()
