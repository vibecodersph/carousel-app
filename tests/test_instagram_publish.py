import json
import unittest
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import instagram_publish


def image_item(index: int) -> instagram_publish.MediaItem:
    return instagram_publish.MediaItem(
        index=index,
        kind="image",
        local_path=f"/tmp/slide_{index:02d}.png",
        public_url=f"https://cdn.example.com/slide_{index:02d}.png",
        slide_type="post",
        source_url="",
    )


class InstagramPublishCredentialTests(unittest.TestCase):
    def test_resolves_channel_specific_access_token_before_global_token(self) -> None:
        manifest = {"channel_id": "vibecodersph"}
        env = {
            "META_SYSTEM_USER_ACCESS_TOKEN": "global-token",
            "META_SYSTEM_USER_ACCESS_TOKEN_VIBECODERSPH": "channel-token",
        }

        with patch.dict(instagram_publish.os.environ, env, clear=True):
            token, source = instagram_publish.resolve_instagram_access_token(
                "",
                manifest,
                instagram_publish.ROOT / "out" / "manifest.json",
            )

        self.assertEqual(token, "channel-token")
        self.assertEqual(source, "channel:vibecodersph")

    def test_resolves_access_token_from_cli_before_channel_token(self) -> None:
        manifest = {"channel_id": "vibecodersph"}
        env = {"META_SYSTEM_USER_ACCESS_TOKEN_VIBECODERSPH": "channel-token"}

        with patch.dict(instagram_publish.os.environ, env, clear=True):
            token, source = instagram_publish.resolve_instagram_access_token(
                "cli-token",
                manifest,
                instagram_publish.ROOT / "out" / "manifest.json",
            )

        self.assertEqual(token, "cli-token")
        self.assertEqual(source, "cli")


class InstagramPublishMediaItemTests(unittest.TestCase):
    def test_title_mp4_infers_video_media_kind(self) -> None:
        kind = instagram_publish.infer_media_kind({"type": "title"}, Path("/tmp/slide_01.mp4"))

        self.assertEqual(kind, "video")

    def test_rejects_manifests_over_graph_api_carousel_limit(self) -> None:
        manifest = {
            "slides": [
                {
                    "index": index,
                    "type": "post",
                    "path": f"/tmp/slide_{index:02d}.png",
                    "source_url": "",
                }
                for index in range(1, instagram_publish.MAX_CAROUSEL_ITEMS + 2)
            ]
        }

        with self.assertRaisesRegex(SystemExit, "at most 10 items"):
            instagram_publish.build_media_items(
                manifest,
                instagram_publish.ROOT / "out" / "manifest.json",
                media_base_url="https://cdn.example.com",
                overrides={},
                dry_run=True,
            )

    def test_parser_no_longer_exposes_facebook_publish_flags(self) -> None:
        help_text = instagram_publish.build_parser().format_help()

        self.assertNotIn("--facebook", help_text)
        self.assertNotIn("Facebook Page", help_text)

    def test_trial_reel_params_are_added_for_single_reel(self) -> None:
        item = instagram_publish.MediaItem(
            index=1,
            kind="video",
            local_path="/tmp/reel.mp4",
            public_url="https://cdn.example.com/reel.mp4",
            slide_type="video",
            source_url="",
        )

        params = instagram_publish.media_create_params(
            item,
            caption="caption",
            carousel_item=False,
            single_video_media_type="REELS",
            trial_reel=True,
            trial_graduation_strategy="MANUAL",
        )

        self.assertEqual(params["media_type"], "REELS")
        self.assertEqual(params["trial_params"], '{"graduation_strategy": "MANUAL"}')
        self.assertNotIn("trial_params.graduation_strategy", params)

    def test_trial_reel_rejects_non_reel_shapes(self) -> None:
        items = [image_item(1)]

        with self.assertRaisesRegex(SystemExit, "Trial Reels require exactly one video"):
            instagram_publish.validate_trial_reel_publish(
                items,
                trial_reel=True,
                single_video_media_type="REELS",
            )

    def test_manifest_trial_reel_defaults_are_read(self) -> None:
        enabled, strategy = instagram_publish.manifest_trial_reel(
            {"instagram_trial_reel": {"enabled": True, "graduation_strategy": "SS_PERFORMANCE"}}
        )

        self.assertTrue(enabled)
        self.assertEqual(strategy, "SS_PERFORMANCE")

    def test_trial_reel_report_exposes_api_step(self) -> None:
        item = instagram_publish.MediaItem(
            index=1,
            kind="video",
            local_path="/tmp/reel.mp4",
            public_url="https://cdn.example.com/reel.mp4",
            slide_type="video",
            source_url="",
        )

        report = instagram_publish.build_report(
            manifest_path=instagram_publish.ROOT / "out" / "manifest.json",
            manifest={"channel_id": "aibrief_jp"},
            items=[item],
            caption="caption",
            dry_run=True,
            graph_version="v25.0",
            graph_api_root="https://graph.instagram.com",
            instagram_user_id="17841400000000000",
            instagram_user_id_source="test",
            access_token_source="test",
            single_video_media_type="REELS",
            trial_reel=True,
            trial_graduation_strategy="SS_PERFORMANCE",
        )

        self.assertTrue(report["trial_reel"])
        self.assertEqual(report["trial_graduation_strategy"], "SS_PERFORMANCE")
        self.assertEqual(
            report["api_steps"][0]["params"]["trial_params"],
            '{"graduation_strategy": "SS_PERFORMANCE"}',
        )


class InstagramPublishCarouselMusicTests(unittest.TestCase):
    def test_applies_carousel_music_to_first_video_item_before_upload(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "slide_01.mp4"
            image = root / "slide_02.png"
            audio = root / "signal.mp3"
            manifest_path = root / "manifest.json"
            library = root / "music.json"
            video.write_bytes(b"video")
            image.write_bytes(b"image")
            audio.write_bytes(b"audio")
            library.write_text(
                json.dumps(
                    {
                        "tracks": [
                            {
                                "id": "signal-glow",
                                "path": str(audio),
                                "duration_seconds": 24,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            manifest = {
                "carousel_id": "brief-router",
                "slides": [
                    {"index": 1, "type": "title", "path": str(video)},
                    {"index": 2, "type": "item", "path": str(image)},
                ],
            }
            items = instagram_publish.build_media_items(
                manifest,
                manifest_path,
                media_base_url="https://cdn.example.com/run",
                overrides={},
                dry_run=False,
            )

            with patch.object(instagram_publish, "add_music_to_video") as add_music:
                result = instagram_publish.apply_carousel_music(
                    items,
                    manifest,
                    manifest_path,
                    media_base_url="https://cdn.example.com/run",
                    music_library=library,
                    music_clip_id="signal-glow",
                    music_duration_seconds=18,
                    enabled=True,
                    single_video_media_type="VIDEO",
                )

        self.assertEqual(result["clip_id"], "signal-glow")
        self.assertEqual(result["duration_seconds"], 18)
        self.assertTrue(items[0].local_path.endswith("instagram_music/slide_01_signal-glow_18s.mp4"))
        self.assertEqual(
            items[0].public_url,
            "https://cdn.example.com/run/slide_01_signal-glow_18s.mp4",
        )
        self.assertEqual(items[0].audio["clip_id"], "signal-glow")
        add_music.assert_called_once()
        self.assertTrue(add_music.call_args.kwargs["loop_video"])

    def test_skips_carousel_music_for_reels_and_single_video_publishes(self) -> None:
        item = instagram_publish.MediaItem(
            index=1,
            kind="video",
            local_path="/tmp/reel.mp4",
            public_url="https://cdn.example.com/reel.mp4",
            slide_type="video",
            source_url="",
        )

        with patch.object(instagram_publish, "add_music_to_video") as add_music:
            result = instagram_publish.apply_carousel_music(
                [item],
                {"source_type": "scheduled_reel"},
                instagram_publish.ROOT / "out" / "manifest.json",
                media_base_url="https://cdn.example.com",
                music_library=None,
                music_clip_id="",
                music_duration_seconds=18,
                enabled=True,
                single_video_media_type="REELS",
            )

        self.assertEqual(result, {})
        add_music.assert_not_called()

    def test_skips_carousel_music_when_manifest_already_has_audio(self) -> None:
        items = [
            instagram_publish.MediaItem(
                index=1,
                kind="video",
                local_path="/tmp/slide_01_music.mp4",
                public_url="https://cdn.example.com/slide_01_music.mp4",
                slide_type="title",
                source_url="",
                audio={"clip_id": "signal-glow"},
            ),
            image_item(2),
        ]

        with patch.object(instagram_publish, "add_music_to_video") as add_music:
            result = instagram_publish.apply_carousel_music(
                items,
                {"carousel_music": {"clip_id": "signal-glow"}},
                instagram_publish.ROOT / "out" / "manifest.json",
                media_base_url="https://cdn.example.com",
                music_library=None,
                music_clip_id="",
                music_duration_seconds=18,
                enabled=True,
                single_video_media_type="VIDEO",
            )

        self.assertEqual(result, {})
        add_music.assert_not_called()


class InstagramPublishRetryTests(unittest.TestCase):
    def test_retries_failed_pre_publish_container_with_fresh_containers(self) -> None:
        items = [image_item(1), image_item(2)]

        with ExitStack() as stack:
            create_container = stack.enter_context(
                patch.object(
                    instagram_publish,
                    "create_container",
                    side_effect=[
                        "child_1a",
                        "child_2a",
                        "parent_a",
                        "child_1b",
                        "child_2b",
                        "parent_b",
                    ],
                )
            )
            wait_for_container = stack.enter_context(
                patch.object(
                    instagram_publish,
                    "wait_for_container",
                    side_effect=[
                        instagram_publish.InstagramContainerWaitError(
                            "Instagram media container parent_a failed: ERROR Error: Media upload has failed"
                        ),
                        {"status_code": "FINISHED"},
                    ],
                )
            )
            publish_container = stack.enter_context(
                patch.object(instagram_publish, "publish_container", return_value={"id": "media_1"})
            )
            stack.enter_context(
                patch.object(
                    instagram_publish,
                    "fetch_permalink",
                    return_value={"permalink": "https://www.instagram.com/p/media_1/"},
                )
            )
            sleep = stack.enter_context(patch.object(instagram_publish.time, "sleep"))

            result = instagram_publish.publish_to_instagram_with_retries(
                items,
                caption="caption",
                instagram_user_id="17841400000000000",
                access_token="token",
                graph_version="v23.0",
                graph_api_root="https://graph.instagram.com",
                wait_timeout=600,
                wait_interval=10,
                single_video_media_type="VIDEO",
                publish_retries=1,
                publish_retry_delay=1,
            )

        self.assertEqual(result["published"], {"id": "media_1"})
        self.assertEqual(result["publish_container_id"], "parent_b")
        self.assertEqual(create_container.call_count, 6)
        self.assertEqual(wait_for_container.call_count, 2)
        publish_container.assert_called_once()
        sleep.assert_called_once_with(1)

    def test_does_not_retry_after_publish_call_errors(self) -> None:
        items = [image_item(1), image_item(2)]

        with ExitStack() as stack:
            create_container = stack.enter_context(
                patch.object(
                    instagram_publish,
                    "create_container",
                    side_effect=["child_1", "child_2", "parent"],
                )
            )
            stack.enter_context(
                patch.object(instagram_publish, "wait_for_container", return_value={"status_code": "FINISHED"})
            )
            publish_container = stack.enter_context(
                patch.object(
                    instagram_publish,
                    "publish_container",
                    side_effect=SystemExit("Instagram did not return a published media id"),
                )
            )
            sleep = stack.enter_context(patch.object(instagram_publish.time, "sleep"))

            with self.assertRaises(SystemExit):
                instagram_publish.publish_to_instagram_with_retries(
                    items,
                    caption="caption",
                    instagram_user_id="17841400000000000",
                    access_token="token",
                    graph_version="v23.0",
                    graph_api_root="https://graph.instagram.com",
                    wait_timeout=600,
                    wait_interval=10,
                    single_video_media_type="VIDEO",
                    publish_retries=2,
                    publish_retry_delay=1,
                )

        self.assertEqual(create_container.call_count, 3)
        publish_container.assert_called_once()
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
