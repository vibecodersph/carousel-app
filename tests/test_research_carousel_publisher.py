from __future__ import annotations

import argparse
import json
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import research_carousel_publisher


def write_manifest(path: Path, *, channel_id: str = "aibrief_jp") -> Path:
    path.write_text(json.dumps({"channel_id": channel_id, "slides": []}), encoding="utf-8")
    return path


class ResearchCarouselPublisherTests(unittest.TestCase):
    def test_rendered_items_includes_due_scheduled_rows_with_manifests(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            due_manifest = write_manifest(root / "due.json")
            future_manifest = write_manifest(root / "future.json")
            rendered_manifest = write_manifest(root / "rendered.json")
            other_channel_manifest = write_manifest(root / "other.json", channel_id="vibecodersph")
            queue = {
                "items": [
                    {
                        "briefId": "due-scheduled",
                        "status": "scheduled",
                        "scheduledAt": "2026-07-03T12:00:00+09:00",
                        "renderedManifestPath": str(due_manifest),
                    },
                    {
                        "briefId": "future-scheduled",
                        "status": "scheduled",
                        "scheduledAt": "2026-07-03T18:00:00+09:00",
                        "renderedManifestPath": str(future_manifest),
                    },
                    {
                        "briefId": "scheduled-without-manifest",
                        "status": "scheduled",
                        "scheduledAt": "2026-07-03T09:00:00+09:00",
                    },
                    {
                        "briefId": "rendered-no-slot",
                        "status": "rendered",
                        "renderedManifestPath": str(rendered_manifest),
                    },
                    {
                        "briefId": "wrong-channel",
                        "status": "scheduled",
                        "scheduledAt": "2026-07-03T12:00:00+09:00",
                        "renderedManifestPath": str(other_channel_manifest),
                    },
                    {
                        "briefId": "invalid-scheduled-at",
                        "status": "scheduled",
                        "scheduledAt": "not-a-time",
                        "renderedManifestPath": str(due_manifest),
                    },
                ],
            }

            candidates = research_carousel_publisher.rendered_items(
                queue,
                channel_id="aibrief_jp",
                now=datetime.fromisoformat("2026-07-03T12:30:00+09:00"),
            )

        self.assertEqual([item["briefId"] for item in candidates], ["rendered-no-slot", "due-scheduled"])
        invalid = next(item for item in queue["items"] if item["briefId"] == "invalid-scheduled-at")
        self.assertIn("Invalid scheduledAt value", invalid["lastError"])

    def test_publish_next_dry_run_preserves_scheduled_status(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = write_manifest(root / "manifest.json")
            queue_path = root / "queue.json"
            queue_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "updatedAt": "2026-07-03T00:00:00+00:00",
                        "items": [
                            {
                                "briefId": "due-scheduled",
                                "status": "scheduled",
                                "scheduledAt": "2026-07-03T12:00:00+09:00",
                                "renderedManifestPath": str(manifest),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            args = argparse.Namespace(
                queue=queue_path,
                channel="aibrief_jp",
                dry_run=True,
                no_upload_r2=True,
                now="2026-07-03T12:30:00+09:00",
            )

            with (
                patch.object(research_carousel_publisher, "load_channel"),
                patch.object(
                    research_carousel_publisher.subprocess,
                    "run",
                    return_value=SimpleNamespace(returncode=0),
                ) as run,
            ):
                result = research_carousel_publisher.publish_next(args)

            updated = json.loads(queue_path.read_text(encoding="utf-8"))

        self.assertEqual(result, 0)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(updated["items"][0]["status"], "scheduled")
        self.assertIn("dryRunReportPath", updated["items"][0])

    def test_publish_next_skips_future_scheduled_rows(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = write_manifest(root / "manifest.json")
            queue_path = root / "queue.json"
            queue_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "updatedAt": "2026-07-03T00:00:00+00:00",
                        "items": [
                            {
                                "briefId": "future-scheduled",
                                "status": "scheduled",
                                "scheduledAt": "2026-07-03T18:00:00+09:00",
                                "renderedManifestPath": str(manifest),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            args = argparse.Namespace(
                queue=queue_path,
                channel="aibrief_jp",
                dry_run=True,
                no_upload_r2=True,
                now="2026-07-03T12:30:00+09:00",
            )

            with (
                patch.object(research_carousel_publisher, "load_channel"),
                patch.object(research_carousel_publisher.subprocess, "run") as run,
            ):
                result = research_carousel_publisher.publish_next(args)

            updated = json.loads(queue_path.read_text(encoding="utf-8"))

        self.assertEqual(result, 0)
        self.assertEqual(run.call_count, 0)
        self.assertEqual(updated["items"][0]["status"], "scheduled")


if __name__ == "__main__":
    unittest.main()
