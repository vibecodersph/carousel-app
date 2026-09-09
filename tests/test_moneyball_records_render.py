import copy
import unittest

from moneyball_records_render import (
    render_account_records_html,
    render_account_records_markdown,
)


def make_row(media_id, *, value=0.4, eligible=True):
    return {
        "media_id": media_id,
        "title": f"Hook {media_id}",
        "permalink": f"https://www.instagram.com/reel/{media_id}/",
        "published_at": "2026-09-05T00:00:00+00:00",
        "value": value,
        "eligible": eligible,
        "rank": 2 if eligible else None,
        "cohort_size": 20,
        "directional_percentile": 92.5 if eligible else None,
        "measured_rank": 2,
        "measured_cohort_size": 23,
        "measured_directional_percentile": 93.5,
        "eligibility_flags": [] if eligible else ["LOW_REACH", "LOW_ACTION_COUNT"],
        "actual_age_hours": 24.5,
        "supporting_metrics": {"saves": 440, "reach": 1100, "denominator_type": "reach"},
        "raw_metrics": {"reach": 1100, "views": 2000, "saves": 8, "duration_seconds": 10},
    }


def make_records():
    best = make_row("best", value=0.5)
    best["rank"] = 1
    board = {
        "label": "Save rate",
        "direction": "higher",
        "format": "percent_ratio",
        "eligible_count": 20,
        "measured_count": 23,
        "record_leaders": [best],
        "rows": [best, make_row("hidden-old-row")],
    }
    fresh = make_row("fresh")
    fresh.update({"record_value": 0.5, "record_leaders": [best]})
    provisional = make_row("small", value=0.95, eligible=False)
    provisional.update({"record_value": 0.5, "record_leaders": [best]})
    lifetime_board = copy.deepcopy(board)
    lifetime_board.update({"label": "Reach", "format": "integer"})
    lifetime_board["record_leaders"][0].update({"value": 123456, "actual_age_hours": 920})
    return {
        "schema_version": 1,
        "account": "aibrief.jp",
        "platform": "instagram",
        "as_of": "2026-09-07T00:00:00+00:00",
        "status": "BASELINE_ESTABLISHED",
        "tracked_post_count": 30,
        "history_start": "2026-01-01T00:00:00+00:00",
        "windows": {
            name: {"cohort_size": 25, "missing_checkpoint_count": 5, "metric_rankings": {"saves_per_reach": copy.deepcopy(board)}}
            for name in ("24h", "72h", "7d")
        },
        "lifetime": {"metric_rankings": {"reach": lifetime_board}},
        "events": [],
        "fresh": {
            "start_exclusive": "2026-09-04T00:00:00+00:00",
            "end_inclusive": "2026-09-07T00:00:00+00:00",
            "missing_checkpoint_count": 1,
            "posts": [
                {**fresh, "metrics": {"saves_per_reach": fresh}},
                {**provisional, "metrics": {"saves_per_reach": provisional}},
                {**make_row("missing"), "metrics": {}},
            ],
        },
    }


class AccountRecordsRenderingTests(unittest.TestCase):
    def test_markdown_baseline_links_ranks_raw_support_and_scoped_limitations(self):
        output = render_account_records_markdown(make_records())
        for expected in (
            "Baseline established",
            "no new-record claims",
            "[Hook fresh](https://www.instagram.com/reel/fresh/)",
            "[Hook best](https://www.instagram.com/reel/best/)",
            "#2 / 20 · P92.5",
            "#2 / 23 · P93.5",
            "24.5h",
            "saves: 440",
            "denominator: reach",
            "reach: 1,100",
            "PROVISIONAL",
            "LOW\\_REACH",
            "24h checkpoint unavailable",
            "20 eligible / 23 measured",
            "5 tracked posts without this checkpoint",
            "24h all-time eligible leaders",
            "72h all-time eligible leaders",
            "7d all-time eligible leaders",
            "Lifetime reach · descriptive context",
            "123,456",
            "920h",
            "not Instagram-wide records",
            "Missing checkpoints",
            "Save rate uses reach; share ratio uses views",
            "do not establish follower conversion",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, output)
        self.assertNotIn("hidden-old-row", output)

    def test_html_is_fragment_with_collapsible_later_windows(self):
        output = render_account_records_html(make_records())
        self.assertTrue(output.startswith('<section id="instagram-account-records"'))
        self.assertTrue(output.endswith("</section>"))
        self.assertNotIn("<html", output)
        self.assertIn('class="panel full ranking-shell"', output)
        self.assertIn("<h3>24h all-time eligible leaders</h3>", output)
        self.assertIn("<details><summary>72h all-time eligible leaders</summary>", output)
        self.assertIn("<details><summary>7d all-time eligible leaders</summary>", output)
        self.assertIn('rel="noopener noreferrer"', output)
        self.assertEqual(output.count("<details>"), 2)
        self.assertIn("Eligible standing", output)
        self.assertIn("Measured standing", output)

    def test_new_records_show_previous_and_new_links_and_directional_delta(self):
        records = make_records()
        records["status"] = "UPDATED"
        previous = make_row("previous")
        previous.update({"actual_age_hours": 25.12, "supporting_metrics": {"saves": 789, "reach": 1972, "denominator_type": "reach"}})
        current = make_row("new")
        current.update({"actual_age_hours": 26.34, "supporting_metrics": {"saves": 987, "reach": 1974, "denominator_type": "reach"}})
        records["events"] = [
            {
                "status": "NEW_RECORD", "window": "24h", "metric_key": "saves_per_reach",
                "label": "Save rate", "direction": "higher", "format": "percent_ratio",
                "previous_value": 0.4, "value": 0.5, "delta": 0.1, "improvement_percent": 25,
                "previous_leaders": [previous], "leaders": [current],
            },
            {
                "status": "NEW_RECORD", "window": "72h", "metric_key": "three_second_skip_rate",
                "label": "3-second skip rate", "direction": "lower", "format": "percent_direct",
                "previous_value": 40, "value": 30, "delta": -10, "improvement_percent": 25,
                "previous_leaders": [make_row("old-skip")], "leaders": [make_row("new-skip")],
            },
        ]
        for render in (render_account_records_html, render_account_records_markdown):
            output = render(records)
            with self.subTest(renderer=render.__name__):
                self.assertIn("New records · strict improvements", output)
                self.assertIn("+10 pp; 25% better", output)
                self.assertIn("−10 pp; 25% better", output)
                self.assertIn("https://www.instagram.com/reel/previous/", output)
                self.assertIn("https://www.instagram.com/reel/new/", output)
                self.assertIn("40%", output)
                self.assertIn("50%", output)
                event_section = output[output.index("New records · strict improvements"):output.index("Fresh posts in all-time 24h context")]
                self.assertIn("25.12h", event_section)
                self.assertIn("26.34h", event_section)
                self.assertIn("saves: 789", event_section)
                self.assertIn("saves: 987", event_section)
                self.assertIn("previous analytics refresh", output)
                self.assertIn("persisted record history since the previous pulse", output)

    def test_each_tied_leader_has_its_own_age_and_raw_support(self):
        records = make_records()
        tied = make_row("second-tied-holder", value=0.5)
        tied.update({"actual_age_hours": 27.89, "raw_metrics": {"reach": 2468}, "supporting_metrics": {"saves": 1234, "reach": 2468, "denominator_type": "reach"}})
        records["windows"]["24h"]["metric_rankings"]["saves_per_reach"]["record_leaders"].append(tied)
        for render in (render_account_records_html, render_account_records_markdown):
            output = render(records)
            start = output.index("24h all-time eligible leaders")
            end = output.index("72h all-time eligible leaders")
            leader_section = output[start:end]
            self.assertIn("https://www.instagram.com/reel/second-tied-holder/", leader_section)
            self.assertIn("27.89h", leader_section)
            self.assertIn("reach: 2,468", leader_section)
            self.assertEqual(leader_section.count("(tied leader)"), 2)
            if render is render_account_records_html:
                tied_row = next(row for row in leader_section.split("<tr>") if "second-tied-holder" in row)
            else:
                tied_row = next(row for row in leader_section.splitlines() if "second-tied-holder" in row)
            self.assertIn("27.89h", tied_row)
            self.assertIn("reach: 2,468", tied_row)
            self.assertNotIn("reach: 1,100", tied_row)

    def test_lifetime_events_are_separate_descriptive_totals_and_new_metric_baselines_are_visible(self):
        records = make_records()
        records["status"] = "UPDATED"
        records["events"] = [
            {
                "status": "NEW_RECORD", "window": "lifetime", "metric_key": "reach", "label": "Reach",
                "direction": "higher", "format": "integer", "previous_value": 1000,
                "value": 2000, "improvement_percent": 100,
                "previous_leaders": [make_row("old-lifetime")], "leaders": [make_row("new-lifetime")],
            },
            {
                "status": "BASELINE_ESTABLISHED", "window": "72h", "metric_key": "saves_per_reach",
                "label": "Save rate newly available", "format": "percent_ratio", "value": 0.5,
                "leaders": [make_row("new-baseline")],
            },
            {
                "status": "NEW_RECORD", "window": "lifetime", "metric_key": "views", "label": "REMOVED VIEWS EVENT",
                "direction": "higher", "format": "integer", "previous_value": 1000, "value": 999999,
                "previous_leaders": [make_row("old-views")], "leaders": [make_row("ignored-views-holder")],
            },
        ]
        removed_views = copy.deepcopy(records["lifetime"]["metric_rankings"]["reach"])
        removed_views["label"] = "REMOVED VIEWS LEADERBOARD"
        records["lifetime"]["metric_rankings"]["views"] = removed_views
        for render in (render_account_records_html, render_account_records_markdown):
            output = render(records)
            self.assertIn("New descriptive lifetime reach totals · unequal ages", output)
            self.assertIn("Lifetime · descriptive total", output)
            self.assertIn("100% higher", output)
            self.assertNotIn("100% better", output)
            self.assertNotIn("New records · strict improvements", output)
            self.assertIn("Baseline established", output)
            self.assertIn("Save rate newly available", output)
            self.assertIn("https://www.instagram.com/reel/new-baseline/", output)
            self.assertIn("Raw views accompany the reach-selected leader as context", output)
            self.assertIn("views: 2,000", output)
            self.assertNotIn("REMOVED VIEWS EVENT", output)
            self.assertNotIn("REMOVED VIEWS LEADERBOARD", output)
            self.assertNotIn("ignored-views-holder", output)

    def test_baseline_cannot_claim_new_records_even_with_stale_events(self):
        records = make_records()
        records["events"] = [{
            "status": "NEW_RECORD", "value": 9, "previous_value": 1,
            "label": "Misleading event", "leaders": [make_row("stale-event")],
        }]
        for render in (render_account_records_html, render_account_records_markdown):
            output = render(records)
            self.assertNotIn("Misleading event", output)
            self.assertNotIn("stale-event", output)
            self.assertNotIn("New records · strict improvements", output)

    def test_methodology_migration_is_explicit_and_cannot_claim_new_records(self):
        records = make_records()
        records["status"] = "METHODOLOGY_BASELINE_ESTABLISHED"
        records["events"] = [{
            "status": "NEW_RECORD", "metric_key": "saves_per_reach", "window": "24h",
            "value": 0.5, "previous_value": 0.4, "format": "percent_ratio",
            "label": "Should not claim migration record", "leaders": [make_row("migration-stale")],
        }]
        for render in (render_account_records_html, render_account_records_markdown):
            output = render(records)
            self.assertIn("Ranking metrics changed", output)
            self.assertIn("old records are retained as historical context", output)
            self.assertIn("no new-record claims", output)
            self.assertNotIn("Should not claim migration record", output)
            self.assertNotIn("New records · strict improvements", output)

    def test_selected_five_metrics_have_correct_denominators_and_exclude_removed_boards(self):
        records = make_records()
        records["methodology"] = {
            "minimum_rate_reach": 100, "minimum_rate_views": 100,
            "minimum_saves": 5, "minimum_shares": 5,
        }
        boards = records["windows"]["24h"]["metric_rankings"]
        fresh_metrics = records["fresh"]["posts"][0]["metrics"]
        for key, label, format_name, value, support in (
            ("three_second_skip_rate", "Skip rate", "percent_direct", 20, {"reels_skip_rate": 20, "reach": 1100}),
            ("views_per_reached_account", "Looping", "ratio", 2, {"views": 2200, "reach": 1100, "denominator_type": "reach"}),
            ("shares_per_view", "Share ratio", "percent_ratio", 0.01, {"shares": 22, "views": 2200, "denominator_type": "views"}),
            ("reach", "Raw reach", "count", 1100, {"reach": 1100}),
            ("watch_depth", "REMOVED WATCH", "percent_ratio", 9, {"average_watch_time_seconds": 999}),
            ("total_interactions_per_reach", "REMOVED INTERACTIONS", "percent_ratio", 9, {"interactions": 99999}),
            ("views", "REMOVED FIXED VIEWS", "count", 99999, {"views": 99999}),
        ):
            board = copy.deepcopy(boards["saves_per_reach"])
            board.update({"label": label, "format": format_name})
            board["record_leaders"][0].update({"value": value, "supporting_metrics": support})
            boards[key] = board
            fresh_metrics[key] = {**copy.deepcopy(fresh_metrics["saves_per_reach"]), "value": value, "supporting_metrics": support}
        for render in (render_account_records_html, render_account_records_markdown):
            output = render(records)
            for expected in ("Skip rate", "Looping", "Save rate", "Share ratio", "Raw reach", "denominator: views", "shares: 22"):
                self.assertIn(expected, output)
            self.assertIn("Share-ratio records require 100 views and 5 shares", output)
            self.assertIn("Save-rate records require 100 reach and 5 saves", output)
            self.assertIn("follows remain separate and are excluded from combined rankings", output)
            for removed in ("REMOVED WATCH", "REMOVED INTERACTIONS", "REMOVED FIXED VIEWS", "Watch depth", "avg watch (s)"):
                self.assertNotIn(removed, output)

    def test_ties_revisions_and_discoveries_are_not_new_record_claims(self):
        records = make_records()
        records["status"] = "UPDATED"
        records["events"] = [
            {"status": status, "window": "24h", "metric_key": "saves_per_reach", "label": label, "value": 0.5, "leaders": [make_row("best")]}
            for status, label in (
                ("RECORD_TIED", "Tied metric"),
                ("RECORD_REVISED", "Revised metric"),
                ("HISTORICAL_RECORD_DISCOVERED", "Discovered metric"),
            )
        ] + [{"status": "NEW_RECORD", "label": "Malformed tie", "previous_value": 1, "value": 1}]
        for render in (render_account_records_html, render_account_records_markdown):
            output = render(records)
            self.assertIn("No new strict-improvement records", output)
            self.assertIn("Record tied", output)
            self.assertIn("Record revised", output)
            self.assertIn("Historical record discovered", output)
            self.assertNotIn("New records · strict improvements", output)
            self.assertNotIn("Malformed tie", output)

    def test_untrusted_titles_and_urls_are_escaped_in_html_and_markdown(self):
        records = make_records()
        post = records["fresh"]["posts"][0]
        post["title"] = '<script>alert("x")</script> | [click]\n*bold*'
        post["permalink"] = "javascript:alert(1)"
        records["account"] = '<img src=x onerror="alert(1)">'
        html_output = render_account_records_html(records)
        md_output = render_account_records_markdown(records)
        for output in (html_output, md_output):
            self.assertNotIn("<script>", output)
            self.assertNotIn("<img ", output)
            self.assertNotIn("javascript:", output)
            self.assertIn("&lt;script&gt;", output)
        self.assertIn("\\| \\[click\\] \\*bold\\*", md_output)
        self.assertNotIn("\n*bold*", md_output)

        post["permalink"] = 'https://example.com/a(b)|[c]?q="hello"&a=1'
        html_output = render_account_records_html(records)
        md_output = render_account_records_markdown(records)
        self.assertIn('q=&quot;hello&quot;&amp;a=1', html_output)
        self.assertIn("https://example.com/a%28b%29%7C%5Bc%5D?q=%22hello%22&a=1", md_output)

    def test_invalid_urls_and_nonfinite_values_remain_plain_or_unavailable(self):
        for url in ("data:text/html,<script>x</script>", "https://x.test/\nfoo", "//x.test/", "https://[", "file:///tmp/x"):
            records = make_records()
            post = records["fresh"]["posts"][0]
            post["permalink"] = url
            post["metrics"]["saves_per_reach"]["value"] = float("nan")
            output = render_account_records_html(records)
            with self.subTest(url=url):
                self.assertNotIn('>Hook fresh</a>', output)
                self.assertNotIn("nan", output)

    def test_empty_payload_is_explanatory_and_renderer_does_not_mutate_input(self):
        for render in (render_account_records_html, render_account_records_markdown):
            empty = render({})
            self.assertIn("No observations available", empty)
            self.assertIn("not Instagram-wide records", empty)
            records = make_records()
            before = copy.deepcopy(records)
            first = render(records)
            self.assertEqual(records, before)
            self.assertEqual(first, render(records))


if __name__ == "__main__":
    unittest.main()
