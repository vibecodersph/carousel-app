from __future__ import annotations

import argparse
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import research_carousel_queue_renderer


class ResearchCarouselQueueRendererTests(unittest.TestCase):
    def test_render_candidates_requires_due_unrendered_rows(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            brief_path = root / "carousel_briefs.json"
            brief_path.write_text(json.dumps({"carousels": []}), encoding="utf-8")
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps({"channel_id": "aibrief_jp"}), encoding="utf-8")
            queue = {
                "items": [
                    {
                        "briefId": "due",
                        "status": "scheduled",
                        "scheduledAt": "2026-07-03T12:00:00+09:00",
                        "briefPath": str(brief_path),
                    },
                    {
                        "briefId": "future",
                        "status": "scheduled",
                        "scheduledAt": "2026-07-03T18:00:00+09:00",
                        "briefPath": str(brief_path),
                    },
                    {
                        "briefId": "already-rendered",
                        "status": "scheduled",
                        "scheduledAt": "2026-07-03T12:00:00+09:00",
                        "briefPath": str(brief_path),
                        "renderedManifestPath": str(manifest_path),
                    },
                ],
            }

            candidates = research_carousel_queue_renderer.render_candidates(
                queue,
                channel_id="aibrief_jp",
                now=research_carousel_queue_renderer.parse_instant("2026-07-03T12:30:00+09:00"),
                include_future=False,
            )

        self.assertEqual([item["briefId"] for item in candidates], ["due"])

    def test_render_candidates_force_includes_already_rendered_rows(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            brief_path = root / "carousel_briefs.json"
            brief_path.write_text(json.dumps({"carousels": []}), encoding="utf-8")
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps({"channel_id": "aibrief_jp"}), encoding="utf-8")
            queue = {
                "items": [
                    {
                        "briefId": "already-rendered",
                        "status": "scheduled",
                        "scheduledAt": "2026-07-03T12:00:00+09:00",
                        "briefPath": str(brief_path),
                        "renderedManifestPath": str(manifest_path),
                    },
                ],
            }

            candidates = research_carousel_queue_renderer.render_candidates(
                queue,
                channel_id="aibrief_jp",
                now=research_carousel_queue_renderer.parse_instant("2026-07-03T12:30:00+09:00"),
                include_future=False,
                force=True,
            )

        self.assertEqual([item["briefId"] for item in candidates], ["already-rendered"])

    def test_render_queue_writes_rendered_manifest_path(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            brief_path = root / "carousel_briefs.json"
            brief_path.write_text(json.dumps({"carousels": [{"id": "brief"}]}), encoding="utf-8")
            queue_path = root / "queue.json"
            queue_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "updatedAt": "2026-07-03T00:00:00+00:00",
                        "items": [
                            {
                                "id": "queue-row",
                                "briefId": "brief",
                                "status": "scheduled",
                                "scheduledAt": "2026-07-03T12:00:00+09:00",
                                "briefPath": str(brief_path),
                                "briefIndex": 0,
                                "channelId": "aibrief_jp",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            out_root = root / "rendered"
            args = argparse.Namespace(
                queue=queue_path,
                channel="aibrief_jp",
                out_root=out_root,
                limit=1,
                include_future=False,
                force=False,
                cover_style="kinetic-fly",
                cover_template="auto",
                generate_images=False,
                no_generate_images=False,
                generate_images_by_default=True,
                localize_copy=False,
                no_carousel_music=True,
                now="2026-07-03T12:30:00+09:00",
            )

            def fake_run(command: list[str], check: bool = False) -> SimpleNamespace:
                self.assertIn("--input", command)
                self.assertNotIn("--no-generate-images", command)
                manifest_path = out_root / "queue-row" / "manifest.json"
                manifest_path.parent.mkdir(parents=True, exist_ok=True)
                manifest_path.write_text(json.dumps({"channel_id": "aibrief_jp"}), encoding="utf-8")
                return SimpleNamespace(returncode=0)

            with (
                patch.object(research_carousel_queue_renderer, "load_channel"),
                patch.object(research_carousel_queue_renderer.subprocess, "run", side_effect=fake_run),
            ):
                result = research_carousel_queue_renderer.render_queue(args)

            updated = json.loads(queue_path.read_text(encoding="utf-8"))

        self.assertEqual(result, 0)
        self.assertEqual(updated["items"][0]["status"], "scheduled")
        self.assertTrue(updated["items"][0]["renderedManifestPath"].endswith("rendered/queue-row/manifest.json"))
        self.assertIn("renderedAt", updated["items"][0])

    def test_no_generate_images_flag_opts_out_of_default_generation(self) -> None:
        command = research_carousel_queue_renderer.build_render_command(
            {
                "briefPath": "briefs.json",
                "briefIndex": 0,
            },
            Path("rendered"),
            channel_id="aibrief_jp",
            cover_style="kinetic-fly",
            cover_template="auto",
            generate_images=False,
            no_carousel_music=True,
            localize_copy=False,
        )

        self.assertIn("--no-generate-images", command)

    def test_localize_copy_flag_is_forwarded_to_builder(self) -> None:
        command = research_carousel_queue_renderer.build_render_command(
            {
                "briefPath": "briefs.json",
                "briefIndex": 0,
            },
            Path("rendered"),
            channel_id="aibrief_jp",
            cover_style="aibrief-study",
            cover_template="auto",
            generate_images=True,
            no_carousel_music=False,
            localize_copy=True,
        )

        self.assertIn("--localize-copy", command)

    def test_cover_template_for_item_uses_queue_override_when_requested_auto(self) -> None:
        template = research_carousel_queue_renderer.cover_template_for_item(
            {"coverTemplate": "gpt_typerain"},
            "auto",
        )

        self.assertEqual(template, "gpt_typerain")

    def test_cover_template_for_item_keeps_explicit_cli_template(self) -> None:
        template = research_carousel_queue_renderer.cover_template_for_item(
            {"coverTemplate": "gpt_typerain"},
            "noise_filter",
        )

        self.assertEqual(template, "noise_filter")

    def test_parser_defaults_to_aibrief_study_cover_style(self) -> None:
        args = research_carousel_queue_renderer.build_parser().parse_args([])

        self.assertEqual(args.cover_style, "aibrief-study")


if __name__ == "__main__":
    unittest.main()
