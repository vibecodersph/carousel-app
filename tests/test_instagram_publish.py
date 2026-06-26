import unittest
from contextlib import ExitStack
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


def video_item(index: int = 1) -> instagram_publish.MediaItem:
    return instagram_publish.MediaItem(
        index=index,
        kind="video",
        local_path=f"/tmp/reel_{index:02d}.mp4",
        public_url=f"https://cdn.example.com/reel_{index:02d}.mp4",
        slide_type="reel",
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

    def test_resolves_channel_specific_facebook_config_before_global_values(self) -> None:
        manifest = {"channel_id": "vibecodersph"}
        env = {
            "FACEBOOK_PAGE_ID": "global-page",
            "FACEBOOK_PAGE_ID_VIBECODERSPH": "channel-page",
            "FACEBOOK_PAGE_ACCESS_TOKEN": "global-token",
            "FACEBOOK_PAGE_ACCESS_TOKEN_VIBECODERSPH": "channel-token",
        }

        with patch.dict(instagram_publish.os.environ, env, clear=True):
            page_id, page_source = instagram_publish.resolve_facebook_page_id(
                "",
                manifest,
                instagram_publish.ROOT / "out" / "manifest.json",
            )
            token, token_source = instagram_publish.resolve_facebook_access_token(
                "",
                manifest,
                instagram_publish.ROOT / "out" / "manifest.json",
            )

        self.assertEqual(page_id, "channel-page")
        self.assertEqual(page_source, "channel:vibecodersph")
        self.assertEqual(token, "channel-token")
        self.assertEqual(token_source, "channel:vibecodersph")

    def test_facebook_publish_auto_enables_when_page_id_resolves(self) -> None:
        manifest = {"channel_id": "vibecodersph"}

        with patch.dict(instagram_publish.os.environ, {}, clear=True):
            self.assertTrue(
                instagram_publish.facebook_publish_enabled(
                    force=False,
                    skip=False,
                    manifest=manifest,
                    manifest_path=instagram_publish.ROOT / "out" / "manifest.json",
                    facebook_page_id="12345",
                )
            )
            self.assertFalse(
                instagram_publish.facebook_publish_enabled(
                    force=False,
                    skip=True,
                    manifest=manifest,
                    manifest_path=instagram_publish.ROOT / "out" / "manifest.json",
                    facebook_page_id="12345",
                )
            )

    def test_derives_facebook_page_credentials_from_connected_instagram_account(self) -> None:
        with patch.object(
            instagram_publish,
            "graph_request",
            return_value={
                "data": [
                    {
                        "id": "page_1",
                        "name": "AI Brief",
                        "access_token": "page-token",
                        "instagram_business_account": {
                            "id": "17841411137200252",
                            "username": "aibrief.jp",
                        },
                    }
                ]
            },
        ) as graph_request:
            page_id, page_source, token, token_source = instagram_publish.derive_facebook_page_credentials(
                facebook_page_id="",
                facebook_page_id_source="env:FACEBOOK_PAGE_ID",
                access_token="system-token",
                access_token_source="env:FACEBOOK_PAGE_ACCESS_TOKEN",
                graph_version="v23.0",
                graph_api_root="https://graph.facebook.com",
                instagram_user_id="17841411137200252",
                instagram_username="aibrief.jp",
            )

        self.assertEqual(page_id, "page_1")
        self.assertEqual(page_source, "env:FACEBOOK_PAGE_ACCESS_TOKEN:me/accounts")
        self.assertEqual(token, "page-token")
        self.assertEqual(token_source, "env:FACEBOOK_PAGE_ACCESS_TOKEN:me/accounts:page_1")
        graph_request.assert_called_once()


class InstagramPublishMediaItemTests(unittest.TestCase):
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


class FacebookPublishTests(unittest.TestCase):
    def test_publishes_images_as_unpublished_photos_attached_to_feed_post(self) -> None:
        items = [image_item(1), image_item(2)]
        calls: list[tuple[str, dict]] = []
        photo_ids = iter(["photo_1", "photo_2"])

        def fake_graph_request(path: str, **kwargs):
            calls.append((path, kwargs))
            if path == "page_1/photos":
                return {"id": next(photo_ids)}
            if path == "page_1/feed":
                return {"id": "page_1_post_1"}
            if path == "page_1_post_1":
                return {"permalink_url": "https://www.facebook.com/page/posts/1"}
            raise AssertionError(f"unexpected path {path}")

        with patch.object(instagram_publish, "graph_request", side_effect=fake_graph_request):
            result = instagram_publish.publish_to_facebook_page(
                items,
                message="caption",
                facebook_page_id="page_1",
                access_token="page-token",
                graph_version="v23.0",
                graph_api_root="https://graph.facebook.com",
            )

        self.assertEqual(result["kind"], "photo_feed")
        self.assertEqual(result["published"], {"id": "page_1_post_1"})
        self.assertEqual([path for path, _ in calls], [
            "page_1/photos",
            "page_1/photos",
            "page_1/feed",
            "page_1_post_1",
        ])
        self.assertEqual(calls[0][1]["params"], {"url": items[0].public_url, "published": "false"})
        self.assertEqual(calls[1][1]["params"], {"url": items[1].public_url, "published": "false"})
        feed_params = calls[2][1]["params"]
        self.assertEqual(feed_params["message"], "caption")
        self.assertEqual(feed_params["attached_media[0]"], '{"media_fbid":"photo_1"}')
        self.assertEqual(feed_params["attached_media[1]"], '{"media_fbid":"photo_2"}')
        self.assertTrue(all(call[1]["api_name"] == "Facebook" for call in calls))

    def test_publishes_single_video_to_page_videos(self) -> None:
        item = video_item()
        calls: list[tuple[str, dict]] = []

        def fake_graph_request(path: str, **kwargs):
            calls.append((path, kwargs))
            if path == "page_1/videos":
                return {"id": "video_1"}
            if path == "video_1":
                return {"permalink_url": "https://www.facebook.com/page/videos/1"}
            raise AssertionError(f"unexpected path {path}")

        with patch.object(instagram_publish, "graph_request", side_effect=fake_graph_request):
            result = instagram_publish.publish_to_facebook_page(
                [item],
                message="caption",
                facebook_page_id="page_1",
                access_token="page-token",
                graph_version="v23.0",
                graph_api_root="https://graph.facebook.com",
            )

        self.assertEqual(result["kind"], "video")
        self.assertEqual(result["published"], {"id": "video_1"})
        self.assertEqual([path for path, _ in calls], ["page_1/videos", "video_1"])
        self.assertEqual(
            calls[0][1]["params"],
            {"file_url": item.public_url, "description": "caption"},
        )
        self.assertEqual(calls[0][1]["timeout"], 180)


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
