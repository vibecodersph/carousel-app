"""Tests for Instagram Trial Reel support in instagram_publish.py. No network: graph_request is mocked."""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import instagram_publish as ip

VIDEO_URL = "https://cdn.example.com/ep.mp4"
MANUAL = json.dumps({"graduation_strategy": "MANUAL"})


def video_item(kind="video"):
    return ip.MediaItem(index=1, kind=kind, local_path="ep.mp4", public_url=VIDEO_URL, slide_type="video", source_url="")


class Env:
    """A throwaway manifest with one video slide, and a scripted Graph API."""

    def __init__(self, followers=250, trial_in_manifest=None, fail_create=None):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "ep.mp4").write_bytes(b"x")
        manifest = {"slides": [{"index": 1, "path": "ep.mp4", "type": "video", "public_url": VIDEO_URL}]}
        if trial_in_manifest:
            manifest["trial"] = trial_in_manifest
        self.manifest = self.dir / "manifest.json"
        self.manifest.write_text(json.dumps(manifest))
        self.followers = followers
        self.fail_create = fail_create
        self.calls = []

    def graph(self, path, **kw):
        method = kw.get("method", "POST")
        params = kw.get("params") or {}
        self.calls.append((method, path, dict(params)))
        if method == "GET" and path == "1784":
            return {"followers_count": self.followers, "id": "1784"}
        if method == "GET" and params.get("fields") == "status_code,status":
            return {"status_code": "FINISHED"}
        if method == "GET":
            return {"permalink": "https://www.instagram.com/reel/x/", "is_trial": True, "trial_status": "NOT_GRADUATED"}
        if path.endswith("/media_publish"):
            return {"id": "m1"}
        if self.fail_create:
            raise SystemExit(self.fail_create)
        return {"id": "c1"}

    def run(self, *flags):
        argv = ["instagram_publish.py", str(self.manifest), "--instagram-user-id", "1784", "--access-token", "tok",
                "--single-video-media-type", "REELS", "--out", str(self.dir / "rep.json"), *flags]
        out = io.StringIO()
        code = 0
        with patch.object(sys, "argv", argv), patch.object(ip, "graph_request", self.graph), redirect_stdout(out):
            try:
                ip.main()
            except SystemExit as exc:
                code = exc.code if exc.code not in (None, 0) else 0
        return code, out.getvalue()

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]

    def close(self):
        self.tmp.cleanup()


class ParserTests(unittest.TestCase):
    def test_trial_flag_takes_only_the_two_strategies(self):
        p = ip.build_parser()
        self.assertIsNone(p.parse_args([]).trial)
        self.assertEqual(p.parse_args(["--trial", "MANUAL"]).trial, "MANUAL")
        self.assertEqual(p.parse_args(["--trial", "SS_PERFORMANCE"]).trial, "SS_PERFORMANCE")
        with redirect_stdout(io.StringIO()), patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit):
                p.parse_args(["--trial", "AUTOMATIC"])

    def test_trial_force_defaults_off(self):
        p = ip.build_parser()
        self.assertFalse(p.parse_args([]).trial_force)
        self.assertTrue(p.parse_args(["--trial-force"]).trial_force)


class ParamShapeTests(unittest.TestCase):
    def test_default_request_is_unchanged(self):
        params = ip.media_create_params(video_item(), caption="hi", carousel_item=False, single_video_media_type="REELS")
        self.assertEqual(params, {"media_type": "REELS", "video_url": VIDEO_URL, "caption": "hi"})

    def test_trial_adds_one_json_key(self):
        params = ip.media_create_params(
            video_item(), caption="hi", carousel_item=False, single_video_media_type="REELS", trial="MANUAL"
        )
        self.assertEqual(params.pop("trial_params"), MANUAL)
        self.assertEqual(params, {"media_type": "REELS", "video_url": VIDEO_URL, "caption": "hi"})

    def test_trial_is_never_added_to_a_carousel_child(self):
        params = ip.media_create_params(
            video_item(), caption="hi", carousel_item=True, single_video_media_type="REELS", trial="MANUAL"
        )
        self.assertNotIn("trial_params", params)

    def test_api_steps_show_trial_only_on_the_container_call(self):
        steps = ip.api_steps([video_item()], "hi", single_video_media_type="REELS", trial="SS_PERFORMANCE")
        self.assertEqual(json.loads(steps[0]["params"]["trial_params"]), {"graduation_strategy": "SS_PERFORMANCE"})
        self.assertNotIn("trial_params", steps[1])


class ResolveTests(unittest.TestCase):
    def test_flag_beats_manifest_and_manifest_is_used(self):
        self.assertEqual(ip.resolve_trial("MANUAL", {"trial": "SS_PERFORMANCE"}), "MANUAL")
        self.assertEqual(ip.resolve_trial(None, {"trial": "SS_PERFORMANCE"}), "SS_PERFORMANCE")
        self.assertIsNone(ip.resolve_trial(None, {}))

    def test_bad_manifest_value_refuses(self):
        with self.assertRaises(SystemExit) as cm:
            ip.resolve_trial(None, {"trial": "AUTOMATIC"})
        self.assertIn("MANUAL", str(cm.exception))

    def test_only_a_single_reel_may_be_a_trial(self):
        ip.require_trial_reel([video_item()], "REELS")
        for items, media_type in (
            ([video_item()], "VIDEO"),
            ([video_item("image")], "REELS"),
            ([video_item(), video_item()], "REELS"),
        ):
            with self.assertRaises(SystemExit) as cm:
                ip.require_trial_reel(items, media_type)
            self.assertIn("REELS", str(cm.exception))


class FollowerGateTests(unittest.TestCase):
    def test_under_200_refuses_and_says_why(self):
        with self.assertRaises(SystemExit) as cm:
            ip.follower_gate(162, force=False)
        msg = str(cm.exception)
        self.assertIn("162", msg)
        self.assertIn("200", msg)
        self.assertIn("--trial-force", msg)

    def test_200_and_over_passes(self):
        ip.follower_gate(200, force=False)
        ip.follower_gate(5000, force=False)

    def test_force_passes_under_200(self):
        ip.follower_gate(162, force=True)

    def test_unreadable_count_refuses_unless_forced(self):
        with self.assertRaises(SystemExit):
            ip.follower_gate(None, force=False)
        ip.follower_gate(None, force=True)


class MainFlowTests(unittest.TestCase):
    def test_default_run_sends_the_same_request_as_before(self):
        env = Env()
        self.addCleanup(env.close)
        code, out = env.run()
        self.assertEqual(code, 0, out)
        self.assertEqual(env.writes()[0], ("POST", "1784/media", {"media_type": "REELS", "video_url": VIDEO_URL}))
        self.assertFalse([c for c in env.calls if c[1] == "1784"], "no follower lookup without --trial")
        self.assertNotIn("is_trial", out)

    def test_low_followers_stop_before_any_write(self):
        env = Env(followers=162)
        self.addCleanup(env.close)
        code, out = env.run("--trial", "MANUAL")
        self.assertNotEqual(code, 0)
        self.assertEqual(env.writes(), [])
        self.assertIn("162", str(code))

    def test_qualifying_account_sends_trial_params_and_reads_back(self):
        env = Env(followers=250)
        self.addCleanup(env.close)
        code, out = env.run("--trial", "SS_PERFORMANCE")
        self.assertEqual(code, 0, out)
        create = env.writes()[0]
        self.assertEqual(create[1], "1784/media")
        self.assertEqual(json.loads(create[2]["trial_params"]), {"graduation_strategy": "SS_PERFORMANCE"})
        publish = [c for c in env.writes() if c[1].endswith("/media_publish")][0]
        self.assertNotIn("trial_params", publish[2])
        self.assertIn("is_trial=True", out)
        self.assertIn("trial_status=NOT_GRADUATED", out)

    def test_force_sends_it_at_162(self):
        env = Env(followers=162)
        self.addCleanup(env.close)
        code, out = env.run("--trial", "MANUAL", "--trial-force")
        self.assertEqual(code, 0, out)
        self.assertEqual(env.writes()[0][2]["trial_params"], MANUAL)

    def test_manifest_key_turns_it_on(self):
        env = Env(followers=250, trial_in_manifest="MANUAL")
        self.addCleanup(env.close)
        code, _ = env.run()
        self.assertEqual(code, 0)
        self.assertEqual(env.writes()[0][2]["trial_params"], MANUAL)

    def test_non_reels_refused_before_any_call(self):
        env = Env()
        self.addCleanup(env.close)
        argv_type = ["--single-video-media-type", "VIDEO"]
        code, _ = env.run("--trial", "MANUAL", *argv_type)   # later flag wins over the harness default
        self.assertNotEqual(code, 0)
        self.assertEqual(env.calls, [])

    def test_instagrams_own_words_come_through_verbatim(self):
        env = Env(followers=250, fail_create="Instagram Graph API error 400: Application does not have permission for this action")
        self.addCleanup(env.close)
        code, _ = env.run("--trial", "MANUAL")
        self.assertIn("Application does not have permission for this action", str(code))

    def test_dry_run_with_trial_makes_no_call_and_shows_the_param(self):
        env = Env()
        self.addCleanup(env.close)
        code, out = env.run("--trial", "MANUAL", "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertEqual(env.calls, [])
        report = json.loads((env.dir / "rep.json").read_text())
        self.assertEqual(report["trial"], "MANUAL")
        self.assertEqual(report["api_steps"][0]["params"]["trial_params"], MANUAL)


class ErrorTextTests(unittest.TestCase):
    """Instagram's generic message hides the cause; error_user_msg carries it (probe 2026-09-30)."""

    def fail_with(self, payload):
        import urllib.error
        err = urllib.error.HTTPError("https://graph.facebook.com/x", 400, "Bad Request", {}, io.BytesIO(json.dumps(payload).encode()))
        with patch.object(ip.urllib.request, "urlopen", side_effect=err):
            with self.assertRaises(SystemExit) as cm:
                ip.graph_request("1784/media", access_token="tok", graph_version="v23.0",
                                 graph_api_root="https://graph.facebook.com", params={})
        return str(cm.exception)

    def test_user_message_is_appended_when_present(self):
        msg = self.fail_with({"error": {
            "message": "Application does not have permission for this action", "code": 10,
            "error_user_title": "Trial Reel Not Enough Followers",
            "error_user_msg": "The instagram account does not meet the trial reel follower requirement to create trial reels."}})
        self.assertTrue(msg.startswith("Instagram Graph API error 400: Application does not have permission for this action"))
        self.assertIn("Trial Reel Not Enough Followers: The instagram account does not meet", msg)

    def test_plain_errors_read_exactly_as_before(self):
        msg = self.fail_with({"error": {"message": "Invalid parameter", "code": 100}})
        self.assertEqual(msg, "Instagram Graph API error 400: Invalid parameter")


if __name__ == "__main__":
    unittest.main()
