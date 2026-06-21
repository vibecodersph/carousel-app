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
