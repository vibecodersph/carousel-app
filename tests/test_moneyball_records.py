import copy
import json
import unittest
from datetime import datetime, timedelta, timezone

from moneyball_records import build_account_records


AS_OF = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)


def make_post(media_id, *, published=None, windows=("24h",), **raw_overrides):
    published = published or AS_OF - timedelta(hours=48)
    raw = {
        "reach": 1000, "views": 2000, "interactions": 50, "saves": 10, "shares": 20,
        "average_watch_time_seconds": 5, "duration_seconds": 10,
        "reels_skip_rate": 50,
    } | raw_overrides
    ages = {"24h": 24, "72h": 72, "7d": 168, "latest": 200}
    return {
        "identity": {
            "account": "aibrief_jp", "platform": "instagram", "media_id": media_id,
            "published_at": published.isoformat(),
            "permalink": f"https://www.instagram.com/reel/{media_id}/",
        },
        "content_metadata": {"hook_text": f"Hook {media_id}"},
        "maturity_windows": {
            window: {
                "maturity_window": window, "actual_age_hours": ages[window],
                "captured_at": (published + timedelta(hours=ages[window])).isoformat(),
                "raw_metrics": dict(raw),
            } for window in windows
        },
    }


def report(*posts, as_of=AS_OF):
    return {
        "report_metadata": {"account": "aibrief_jp", "as_of": as_of.isoformat()},
        "posts": list(posts),
    }


def board(records, metric="reach", window="24h"):
    data = records["lifetime"] if window == "lifetime" else records["windows"][window]
    return data["metric_rankings"][metric]


def events(records, metric="reach", window="24h"):
    return [event for event in records["events"]
            if event["metric_key"] == metric and event["window"] == window]


class AccountRecordTests(unittest.TestCase):
    def test_history_outside_28_days_and_all_windows_stay_separate(self):
        old = make_post("old", published=AS_OF - timedelta(days=90),
                        windows=("24h", "72h", "7d", "latest"), reach=3000)
        old["maturity_windows"]["72h"]["raw_metrics"]["reach"] = 5000
        old["maturity_windows"]["7d"]["raw_metrics"]["reach"] = 7000
        old["maturity_windows"]["latest"]["raw_metrics"]["reach"] = 9000
        fresh = make_post("fresh", reach=2000)
        records = build_account_records(report(fresh, old))
        self.assertEqual(records["status"], "BASELINE_ESTABLISHED")
        self.assertEqual(records["events"], [])
        for window, value in (("24h", 3000), ("72h", 5000), ("7d", 7000), ("lifetime", 9000)):
            self.assertEqual(board(records, window=window)["record_leaders"][0]["value"], value)
        self.assertEqual(set(records["lifetime"]["metric_rankings"]), {"reach"})
        standing = records["fresh"]["posts"][0]["metrics"]["reach"]
        self.assertEqual((standing["rank"], standing["cohort_size"], standing["gap_to_record"]), (2, 2, 1000))
        self.assertEqual(standing["record_leaders"][0]["media_id"], "old")
        self.assertEqual(standing["actual_age_hours"], 24)
        self.assertTrue(standing["captured_at"])

    def test_missing_checkpoint_never_uses_latest_or_other_window(self):
        post = make_post("no24", published=AS_OF - timedelta(days=10), windows=("latest",), reach=9000)
        records = build_account_records(report(post))
        self.assertEqual(board(records)["measured_count"], 0)
        self.assertEqual(records["windows"]["24h"]["missing_checkpoint_count"], 1)
        self.assertEqual(board(records, window="lifetime")["measured_count"], 1)

    def test_fixed_window_boundaries_and_future_capture_are_checked(self):
        posts = []
        for media_id, age in (("early", 23.99), ("start", 24), ("end", 28), ("late", 28.01)):
            post = make_post(media_id)
            post["maturity_windows"]["24h"]["captured_at"] = (AS_OF - timedelta(hours=48-age)).isoformat()
            posts.append(post)
        future = make_post("future", published=AS_OF - timedelta(hours=12))
        records = build_account_records(report(*posts, future))
        self.assertEqual({row["media_id"] for row in board(records)["rows"]}, {"start", "end"})

    def test_configured_maturity_tolerance_is_respected(self):
        post = make_post("late")
        post["maturity_windows"]["24h"]["captured_at"] = (AS_OF - timedelta(hours=18)).isoformat()
        source = report(post)
        source["maturity_windows"] = {"24h": {"target": {"target_hours": 24, "max_hours_after_target": 8}}}
        self.assertEqual(build_account_records(source)["windows"]["24h"]["cohort_size"], 1)

    def test_fresh_cohort_has_exclusive_start_inclusive_end_and_missing_coverage(self):
        posts = [make_post(str(age), published=AS_OF - timedelta(hours=age)) for age in (100, 99, 28, 27)]
        missing = make_post("missing", windows=())
        records = build_account_records(report(*posts, missing))
        self.assertEqual({item["media_id"] for item in records["fresh"]["posts"]}, {"99", "28", "missing"})
        self.assertEqual(records["fresh"]["missing_checkpoint_count"], 1)

    def test_low_base_and_low_counts_keep_raw_standing_but_cannot_set_records(self):
        small = make_post("tiny", reach=1, views=10, saves=1, shares=1, reels_skip_rate=0)
        enough = make_post("eligible", reach=100, views=100, saves=5, shares=5)
        low_count = make_post("few", reach=100, views=100, saves=4, shares=4)
        records = build_account_records(report(small, enough, low_count))
        for metric in ("saves_per_reach", "shares_per_view"):
            result = board(records, metric)
            raw_winner = result["rows"][0]
            self.assertEqual(raw_winner["media_id"], "tiny")
            self.assertEqual(raw_winner["measured_rank"], 1)
            self.assertIsNone(raw_winner["rank"])
            self.assertEqual(raw_winner["eligibility_flags"], ["LOW_BASE", "LOW_COUNT"])
            self.assertEqual(result["record_leaders"][0]["media_id"], "eligible")
            self.assertEqual(result["eligible_count"], 1)
        for metric in ("three_second_skip_rate", "views_per_reached_account"):
            tiny = next(row for row in board(records, metric)["rows"] if row["media_id"] == "tiny")
            self.assertEqual(tiny["eligibility_flags"], ["LOW_BASE"])
        self.assertEqual(board(records, "reach")["eligible_count"], 3)

    def test_missing_invalid_negative_and_zero_metrics_are_distinct(self):
        missing = make_post("missing", reach=None, views=None, saves=None, shares=None, interactions=None,
                            average_watch_time_seconds=None, reels_skip_rate=None)
        invalid = make_post("invalid", reach=0, views=float("nan"), saves=-1, shares=-1, interactions=-1,
                            average_watch_time_seconds=-1, duration_seconds=0, reels_skip_rate=101)
        zero = make_post("zero", saves=0, shares=0, interactions=0, reels_skip_rate=0)
        records = build_account_records(report(missing, invalid, zero))
        for metric in ("saves_per_reach", "shares_per_view", "views_per_reached_account", "three_second_skip_rate"):
            self.assertEqual(board(records, metric)["measured_count"], 1)
        self.assertEqual(board(records, "three_second_skip_rate")["record_leaders"][0]["value"], 0)
        self.assertEqual(board(records, "saves_per_reach")["eligible_count"], 0)
        self.assertEqual(board(records, "shares_per_view")["eligible_count"], 0)
        json.dumps(records, allow_nan=False)

    def test_stale_derived_values_are_recomputed_with_valid_denominators(self):
        post = make_post("stale", reach=0, views=0)
        post["maturity_windows"]["24h"]["derived_metrics"] = {
            "saves_per_reach": 100, "shares_per_view": 100, "views_per_reached_account": 100,
        }
        records = build_account_records(report(post))
        for metric in ("saves_per_reach", "shares_per_view", "views_per_reached_account"):
            self.assertEqual(board(records, metric)["measured_count"], 0)

    def test_direct_skip_with_missing_reach_is_provisional(self):
        records = build_account_records(report(make_post("missing", reach=None)))
        row = board(records, "three_second_skip_rate")["rows"][0]
        self.assertEqual(row["eligibility_flags"], ["MISSING_BASE"])
        self.assertIsNone(row["rank"])

    def test_share_ratio_uses_views_and_save_rate_uses_reach(self):
        small_reach = make_post("share-eligible", reach=10, views=1000, shares=50, saves=5)
        small_views = make_post("save-eligible", reach=1000, views=10, shares=5, saves=50)
        records = build_account_records(report(small_reach, small_views))
        save_rows = {row["media_id"]: row for row in board(records, "saves_per_reach")["rows"]}
        share_rows = {row["media_id"]: row for row in board(records, "shares_per_view")["rows"]}
        self.assertEqual(save_rows["share-eligible"]["value"], 0.5)
        self.assertEqual(save_rows["save-eligible"]["value"], 0.05)
        self.assertEqual(share_rows["share-eligible"]["value"], 0.05)
        self.assertEqual(share_rows["save-eligible"]["value"], 0.5)
        self.assertTrue(share_rows["share-eligible"]["eligible"])
        self.assertFalse(save_rows["share-eligible"]["eligible"])
        self.assertTrue(save_rows["save-eligible"]["eligible"])
        self.assertFalse(share_rows["save-eligible"]["eligible"])
        self.assertEqual(share_rows["share-eligible"]["supporting_metrics"]["denominator_type"], "views")
        self.assertEqual(save_rows["save-eligible"]["supporting_metrics"]["denominator_type"], "reach")
        missing_views = build_account_records(report(make_post("missing-views", views=None)))
        self.assertEqual(board(missing_views, "shares_per_view")["measured_count"], 0)

    def test_only_selected_five_metrics_participate_and_policy_signature_covers_rules(self):
        source = report(make_post("selected", average_watch_time_seconds=999, interactions=99999))
        records = build_account_records(source)
        expected = ("three_second_skip_rate", "views_per_reached_account", "saves_per_reach", "shares_per_view", "reach")
        for window in ("24h", "72h", "7d"):
            self.assertEqual(tuple(records["windows"][window]["metric_rankings"]), expected)
        self.assertEqual(tuple(records["fresh"]["posts"][0]["metrics"]), expected)
        for metric in expected:
            supporting = board(records, metric)["rows"][0]["supporting_metrics"]
            self.assertNotIn("interactions", supporting)
            self.assertNotIn("average_watch_time_seconds", supporting)
        self.assertFalse(any(key.endswith((":watch_depth", ":total_interactions_per_reach", ":saves_per_1000_reach")) for key in records["current_records"]))
        self.assertNotIn("24h:views", records["current_records"])
        self.assertNotIn("lifetime:views", records["current_records"])
        policy = records["ranking_policy"]
        self.assertEqual(policy["lifetime_metrics"], ["reach"])
        self.assertEqual([item["key"] for item in policy["metrics"]], list(expected))
        self.assertEqual([item["direction"] for item in policy["metrics"]], ["lower", "higher", "higher", "higher", "higher"])
        self.assertEqual(policy["metrics"][3]["eligibility"]["base_metric"], "views")
        self.assertEqual(policy["metrics"][3]["eligibility"]["minimum_base"], 100)
        self.assertEqual(policy["metrics"][3]["eligibility"]["minimum_count"], 5)
        self.assertEqual(len(policy["signature"]), 64)
        self.assertEqual(policy, build_account_records(source)["ranking_policy"])
        changed_window = copy.deepcopy(source)
        changed_window["maturity_windows"] = {"24h": {"target": {"target_hours": 24, "max_hours_after_target": 8}}}
        self.assertNotEqual(policy["signature"], build_account_records(changed_window)["ranking_policy"]["signature"])

    def test_ties_use_competition_rank_and_directional_midrank_percentiles(self):
        records = build_account_records(report(
            make_post("b", reach=300, reels_skip_rate=10),
            make_post("a", reach=300, reels_skip_rate=10),
            make_post("c", reach=200, reels_skip_rate=20),
        ))
        for metric in ("reach", "three_second_skip_rate"):
            rows = board(records, metric)["rows"]
            self.assertEqual([row["media_id"] for row in rows], ["a", "b", "c"])
            self.assertEqual([row["rank"] for row in rows], [1, 1, 3])
            self.assertAlmostEqual(rows[0]["directional_percentile"], 200 / 3)
            self.assertEqual(len(board(records, metric)["record_leaders"]), 2)

    def test_new_records_show_prior_holder_signed_delta_and_percent_improvement(self):
        previous_as_of = AS_OF - timedelta(days=3)
        old = make_post("old", published=AS_OF - timedelta(days=10), reach=1000, reels_skip_rate=40)
        previous = build_account_records(report(old, as_of=previous_as_of))
        fresh = make_post("fresh", reach=1500, reels_skip_rate=30)
        records = build_account_records(report(old, fresh), previous_history=previous)
        reach_event = events(records)[0]
        self.assertEqual(reach_event["status"], "NEW_RECORD")
        self.assertEqual((reach_event["previous_value"], reach_event["value"], reach_event["delta"]), (1000, 1500, 500))
        self.assertEqual(reach_event["improvement_percent"], 50)
        self.assertEqual(reach_event["previous_leaders"][0]["media_id"], "old")
        skip_event = events(records, "three_second_skip_rate")[0]
        self.assertEqual(skip_event["status"], "NEW_RECORD")
        self.assertEqual((skip_event["delta"], skip_event["improvement_percent"]), (-10, 25))
        self.assertEqual(build_account_records(report(old, fresh), previous_history=records)["events"], [])

    def test_new_tie_is_not_a_record_break(self):
        old = make_post("old", published=AS_OF - timedelta(days=10))
        previous = build_account_records(report(old, as_of=AS_OF - timedelta(days=3)))
        records = build_account_records(report(old, make_post("tie")), previous_history=previous)
        self.assertEqual(events(records)[0]["status"], "RECORD_TIED")
        self.assertEqual(events(records)[0]["delta"], 0)

    def test_fixed_checkpoint_revisions_are_not_new_publication_records(self):
        old = make_post("old", published=AS_OF - timedelta(days=10))
        previous = build_account_records(report(old, as_of=AS_OF - timedelta(days=3)))
        changed = copy.deepcopy(old)
        changed["maturity_windows"]["24h"]["raw_metrics"]["reach"] = 5000
        records = build_account_records(report(changed), previous_history=previous)
        self.assertEqual(events(records)[0]["status"], "RECORD_REVISED")
        lowered = copy.deepcopy(changed)
        lowered["maturity_windows"]["24h"]["raw_metrics"]["reach"] = 500
        self.assertEqual(events(build_account_records(report(lowered), previous_history=records))[0]["status"], "RECORD_REVISED")

    def test_historical_backfill_does_not_claim_a_new_current_record(self):
        old = make_post("old", published=AS_OF - timedelta(days=10))
        previous = build_account_records(report(old, as_of=AS_OF - timedelta(days=3)))
        backfill = make_post("backfill", published=AS_OF - timedelta(days=80), reach=5000)
        records = build_account_records(report(old, backfill), previous_history=previous)
        self.assertEqual(events(records)[0]["status"], "HISTORICAL_RECORD_DISCOVERED")

    def test_disappearing_champion_is_a_revision_with_preserved_previous_holder(self):
        old = make_post("old", published=AS_OF - timedelta(days=10))
        previous = build_account_records(report(old, as_of=AS_OF - timedelta(days=3)))
        records = build_account_records(report(), previous_history=previous)
        event = events(records)[0]
        self.assertEqual(event["status"], "RECORD_REVISED")
        self.assertEqual(event["leaders"], [])
        self.assertIsNone(event["value"])
        self.assertEqual(event["previous_leaders"][0]["media_id"], "old")

    def test_recurring_revision_transitions_have_distinct_event_ids(self):
        old = make_post("old", published=AS_OF - timedelta(days=10), reach=100)
        baseline = build_account_records(report(old, as_of=AS_OF - timedelta(days=3)))
        history, seen_ids = baseline, []
        for days, reach in ((2, 120), (1, 100), (0, 120)):
            changed = copy.deepcopy(old)
            changed["maturity_windows"]["24h"]["raw_metrics"]["reach"] = reach
            source = report(changed, as_of=AS_OF - timedelta(days=days))
            current = build_account_records(source, previous_history=history)
            self.assertEqual(current, build_account_records(source, previous_history=history))
            seen_ids.append(events(current)[0]["event_id"])
            history = current
        self.assertEqual(len(set(seen_ids)), 3)

    def test_lifetime_growth_of_same_post_is_a_record_but_remains_separate(self):
        old = make_post("old", published=AS_OF - timedelta(days=20), windows=("24h", "latest"))
        previous = build_account_records(report(old, as_of=AS_OF - timedelta(days=3)))
        changed = copy.deepcopy(old)
        latest = changed["maturity_windows"]["latest"]
        latest["captured_at"] = AS_OF.isoformat()
        latest["raw_metrics"]["reach"] = 10000
        records = build_account_records(report(changed), previous_history=previous)
        self.assertEqual(events(records, "reach", "lifetime")[0]["status"], "NEW_RECORD")
        self.assertEqual(events(records, "reach", "24h"), [])

    def test_lifetime_raw_views_are_context_and_never_select_a_leader_or_emit_records(self):
        old_date = AS_OF - timedelta(days=20)
        reach_winner = make_post("reach-winner", published=old_date, windows=("latest",), reach=5000, views=6000)
        most_views = make_post("most-views", published=old_date, windows=("latest",), reach=1000, views=100000)
        previous = build_account_records(report(reach_winner, most_views, as_of=AS_OF - timedelta(days=3)))
        leader = board(previous, "reach", "lifetime")["record_leaders"][0]
        self.assertEqual(leader["media_id"], "reach-winner")
        self.assertEqual(leader["raw_metrics"]["views"], 6000)
        changed = copy.deepcopy(most_views)
        changed["maturity_windows"]["latest"]["captured_at"] = AS_OF.isoformat()
        changed["maturity_windows"]["latest"]["raw_metrics"]["views"] = 1000000
        records = build_account_records(report(reach_winner, changed), previous_history=previous)
        self.assertEqual(records["events"], [])
        self.assertEqual(set(records["lifetime"]["metric_rankings"]), {"reach"})

    def test_zero_previous_value_does_not_divide_by_zero(self):
        old = make_post("old", published=AS_OF - timedelta(days=10), reach=0)
        previous = build_account_records(report(old, as_of=AS_OF - timedelta(days=3)))
        records = build_account_records(report(old, make_post("new")), previous_history=previous)
        self.assertIsNone(events(records, "reach")[0]["improvement_percent"])

    def test_platforms_do_not_mix_and_account_mismatches_fail(self):
        instagram = make_post("ig")
        facebook = make_post("fb", reach=100000)
        facebook["identity"]["platform"] = "facebook"
        records = build_account_records(report(instagram, facebook))
        self.assertEqual(records["tracked_post_count"], 1)
        with self.assertRaisesRegex(ValueError, "mismatch"):
            build_account_records(report(instagram), previous_history={"account": "elsewhere", "platform": "instagram"})
        wrong = copy.deepcopy(instagram)
        wrong["identity"]["account"] = "other"
        with self.assertRaisesRegex(ValueError, "account"):
            build_account_records(report(wrong))
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            build_account_records(report(instagram, instagram))

    def test_future_history_rejected_and_input_order_is_deterministic_without_mutation(self):
        a, b = make_post("a"), make_post("b")
        source = report(b, a)
        before = copy.deepcopy(source)
        first = build_account_records(source)
        second = build_account_records(report(a, b))
        self.assertEqual(first, second)
        self.assertEqual(source, before)
        with self.assertRaisesRegex(ValueError, "future history"):
            build_account_records(report(a, as_of=AS_OF - timedelta(days=1)), previous_history=first)


if __name__ == "__main__":
    unittest.main()
