import copy
import json
import tempfile
import unittest
from pathlib import Path

from moneyball_record_history import (
    build_record_history, comparison_history, load_record_history, record_history_lock,
)


def report(as_of="2026-09-06T12:00:00+00:00"):
    return {"report_metadata": {"as_of": as_of}, "posts": [{"media_id": "one"}]}


def records(as_of="2026-09-06T12:00:00+00:00"):
    return {
        "account": "aibrief_jp", "platform": "instagram", "as_of": as_of,
        "current_records": {"24h:reach": {"value": 100, "leaders": [
            {"media_id": "one", "value": 100, "captured_at": "2026-09-05T00:00:00+00:00"}
        ]}},
        "observations": {"24h:one": "hash"},
        "events": [{"event_id": "baseline", "status": "BASELINE_ESTABLISHED"}],
    }


def library(hook="Original hook"):
    return {"winners": [{
        "identity": {"media_id": "one"},
        "content": {"published_hook": {"value": hook}, "japanese_script": {"text": "Script"}},
        "source": {"url": "https://example.com/source"},
        "winner_evidence": {"ranking_memberships": [{"rank": 1}], "aggregate": {"rank": 1}},
    }]}


class MoneyballRecordHistoryTests(unittest.TestCase):
    def setUp(self):
        self.baseline, self.fingerprint = comparison_history(report(), None)
        self.history = build_record_history(
            records(), library(), previous_history=None,
            baseline=self.baseline, input_fingerprint=self.fingerprint,
        )

    def test_same_source_replays_original_comparison_and_does_not_duplicate_history(self):
        baseline, fingerprint = comparison_history(report(), self.history)
        self.assertIsNone(baseline)
        repeated = build_record_history(
            records(), library(), previous_history=self.history,
            baseline=baseline, input_fingerprint=fingerprint,
        )
        self.assertEqual(repeated, self.history)

    def test_past_winners_and_changed_hooks_survive_departure(self):
        newer = "2026-09-09T12:00:00+00:00"
        baseline, fingerprint = comparison_history(report(newer), self.history)
        updated = build_record_history(
            records(newer), library("Updated hook"), previous_history=self.history,
            baseline=baseline, input_fingerprint=fingerprint,
        )
        entry = updated["winner_archive"]["one"]
        self.assertEqual(entry["first_snapshot"]["content"]["published_hook"]["value"], "Original hook")
        self.assertEqual(len(entry["content_versions"]), 2)
        departed = build_record_history(
            records("2026-09-12T12:00:00+00:00"), {"winners": []},
            previous_history=updated, baseline=baseline, input_fingerprint=fingerprint,
        )
        self.assertFalse(departed["winner_archive"]["one"]["current_member"])
        self.assertEqual(len(departed["winner_archive"]["one"]["content_versions"]), 2)
        self.assertEqual(len(departed["record_events"]), 1)
        self.assertTrue(self.history["winner_archive"]["one"]["current_member"])

    def test_time_reversal_and_changed_same_timestamp_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "predates"):
            comparison_history(report("2026-09-01T12:00:00+00:00"), self.history)
        changed = copy.deepcopy(report())
        changed["posts"].append({"media_id": "two"})
        with self.assertRaisesRegex(ValueError, "source changed"):
            comparison_history(changed, self.history)

    def test_champion_outside_winner_library_survives_missing_data(self):
        initial = build_record_history(
            records(), {"winners": []}, previous_history=None,
            baseline=None, input_fingerprint=self.fingerprint,
        )
        absent = records("2026-09-09T12:00:00+00:00")
        absent["current_records"] = {"24h:reach": {"value": None, "leaders": []}}
        updated = build_record_history(
            absent, {"winners": []}, previous_history=initial,
            baseline=None, input_fingerprint=self.fingerprint,
        )
        self.assertEqual(updated["champion_archive"]["24h:reach"][0]["snapshot"]["media_id"], "one")

    def test_overlapping_runs_cannot_take_the_same_history_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            with record_history_lock(path):
                with self.assertRaisesRegex(RuntimeError, "another run"):
                    with record_history_lock(path):
                        self.fail("Concurrent history writer was allowed")
            with record_history_lock(path):
                self.assertFalse(path.exists())

    def test_new_timestamp_compares_against_last_saved_records(self):
        baseline, _ = comparison_history(report("2026-09-09T12:00:00+00:00"), self.history)
        self.assertEqual(baseline["current_records"], self.history["current_records"])
        self.assertNotIn("winner_archive", baseline)

    def test_new_metric_policy_starts_baseline_and_preserves_old_records(self):
        policy = {"version": 2, "signature": "new-five-metrics"}
        changed = copy.deepcopy(report())
        changed["posts"][0]["new_derived_metric"] = 0.01
        baseline, fingerprint = comparison_history(changed, self.history, ranking_policy=policy)
        self.assertIsNone(baseline)
        new_records = records()
        new_records.update(ranking_policy=policy, status="METHODOLOGY_BASELINE_ESTABLISHED", events=[])
        updated = build_record_history(
            new_records, library(), previous_history=self.history,
            baseline=baseline, input_fingerprint=fingerprint,
        )
        self.assertEqual(updated["archived_methodologies"][0]["current_records"], self.history["current_records"])
        self.assertEqual(updated["record_events"], self.history["record_events"])
        self.assertEqual(updated["last_report_status"], "METHODOLOGY_BASELINE_ESTABLISHED")
        memberships = updated["winner_archive"]["one"]["membership_history"]
        self.assertEqual(len(memberships), 2)
        self.assertEqual(memberships[-1]["ranking_policy_signature"], "new-five-metrics")
        repeat_baseline, repeated_fingerprint = comparison_history(changed, updated, ranking_policy=policy)
        self.assertIsNone(repeat_baseline)
        repeated = build_record_history(
            new_records, library(), previous_history=updated,
            baseline=repeat_baseline, input_fingerprint=repeated_fingerprint,
        )
        self.assertEqual(repeated, updated)

    def test_corrupt_or_wrong_account_history_is_never_silently_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            self.assertIsNone(load_record_history(path, account="aibrief_jp"))
            path.write_text(json.dumps(self.history), encoding="utf-8")
            self.assertEqual(load_record_history(path, account="aibrief_jp"), self.history)
            with self.assertRaisesRegex(ValueError, "account/platform"):
                load_record_history(path, account="another_account")
            path.write_text("{broken", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Cannot safely read"):
                load_record_history(path, account="aibrief_jp")


if __name__ == "__main__":
    unittest.main()
