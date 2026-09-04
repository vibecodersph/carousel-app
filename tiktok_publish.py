#!/usr/bin/env python3
"""Publish a video to TikTok via the Content Posting API (Direct Post, FILE_UPLOAD).

Usage:
    python tiktok_publish.py <video_path> --title "caption text" [--privacy SELF_ONLY]
        [--access-token TOKEN] [--tokens-file .tiktok_sandbox_tokens.json]

Sandbox / unaudited apps are restricted to privacy_level=SELF_ONLY - the post
lands as a private draft visible only to the authorizing account, not the
public feed. That's expected until the app passes TikTok's review.
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error

API_ROOT = "https://open.tiktokapis.com/v2"


def load_access_token(args):
    if args.access_token:
        return args.access_token
    with open(args.tokens_file, encoding="utf-8") as f:
        return json.load(f)["access_token"]


def api_post(path, access_token, payload):
    req = urllib.request.Request(
        f"{API_ROOT}{path}",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise SystemExit(f"TikTok API error ({e.code}) on {path}: {body}")


# TikTok chunk rules (Content Posting API): a file under 64 MB may go as one
# chunk equal to its size; anything larger must be split into chunks of 5-64 MB,
# total_chunk_count = video_size // chunk_size, and the remainder rides on the
# last chunk. HistoReels 001 (85 MB at 1080p) was refused with
# "The chunk size is invalid" under the old single-chunk code.
CHUNK = 20 * 1024 * 1024


def chunk_plan(video_size):
    if video_size < 64 * 1024 * 1024:
        return video_size, 1
    return CHUNK, video_size // CHUNK


def init_upload(access_token, video_size, title, privacy_level, cover_ms=0, aigc=True):
    chunk_size, total_chunks = chunk_plan(video_size)
    payload = {
        "post_info": {
            "title": title,
            "privacy_level": privacy_level,
            "disable_duet": False,
            "disable_comment": False,
            "disable_stitch": False,
            # Pinoy Lore burns its designed thumbnail into the opening frames,
            # since this API accepts only a cover timestamp and never an
            # uploaded cover image. Point at the first frame to use it.
            "video_cover_timestamp_ms": cover_ms,
            # Episodes are AI-generated; TikTok adds the AIGC label.
            "is_aigc": aigc,
        },
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": video_size,
            "chunk_size": chunk_size,
            "total_chunk_count": total_chunks,
        },
    }
    result = api_post("/post/publish/video/init/", access_token, payload)
    if result.get("error", {}).get("code") not in (None, "ok"):
        raise SystemExit(f"init failed: {json.dumps(result)}")
    return result["data"]["publish_id"], result["data"]["upload_url"]


def upload_video(upload_url, video_path, video_size):
    chunk_size, total_chunks = chunk_plan(video_size)
    status = None
    with open(video_path, "rb") as f:
        for i in range(total_chunks):
            start = i * chunk_size
            end = video_size - 1 if i == total_chunks - 1 else start + chunk_size - 1
            f.seek(start)
            data = f.read(end - start + 1)
            req = urllib.request.Request(
                upload_url,
                data=data,
                method="PUT",
                headers={
                    "Content-Type": "video/mp4",
                    "Content-Range": f"bytes {start}-{end}/{video_size}",
                    "Content-Length": str(len(data)),
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=600) as resp:
                    status = resp.status
            except urllib.error.HTTPError as e:
                raise SystemExit(f"upload failed on chunk {i + 1}/{total_chunks} ({e.code}): {e.read().decode()}")
            if total_chunks > 1:
                print(f"  chunk {i + 1}/{total_chunks} ({len(data)} bytes) -> {status}")
    return status


def poll_status(access_token, publish_id, *, interval=5, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = api_post("/post/publish/status/fetch/", access_token, {"publish_id": publish_id})
        status = result.get("data", {}).get("status")
        print(f"  status: {status}")
        if status in ("PUBLISH_COMPLETE", "FAILED"):
            return result
        time.sleep(interval)
    raise SystemExit("Timed out waiting for publish status.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("video_path")
    parser.add_argument("--title", required=True)
    parser.add_argument("--privacy", default="SELF_ONLY")
    parser.add_argument("--cover-ms", type=int, default=0,
                        help="Cover frame timestamp in ms. Default 0: episodes burn their designed "
                             "thumbnail into the opening frames, since this API cannot take an "
                             "uploaded cover image.")
    parser.add_argument("--no-aigc", dest="aigc", action="store_false",
                        help="Do NOT label the video as AI-generated content.")
    parser.set_defaults(aigc=True)
    parser.add_argument("--access-token")
    parser.add_argument("--tokens-file", default=os.path.join(os.path.dirname(__file__), ".tiktok_sandbox_tokens.json"))
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    access_token = load_access_token(args)
    video_size = os.path.getsize(args.video_path)

    print(f"init upload ({video_size} bytes) ...")
    try:
        publish_id, upload_url = init_upload(access_token, video_size, args.title,
                                             args.privacy, args.cover_ms, args.aigc)
    except SystemExit as e:
        if "access_token_invalid" not in str(e) or args.access_token:
            raise
        print("access token invalid - refreshing via tiktok_refresh.py ...")
        subprocess.run(
            [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "tiktok_refresh.py")],
            check=False, capture_output=True,
        )
        access_token = load_access_token(args)
        publish_id, upload_url = init_upload(access_token, video_size, args.title,
                                             args.privacy, args.cover_ms, args.aigc)
    print(f"  publish_id: {publish_id}")

    print("uploading video ...")
    status_code = upload_video(upload_url, args.video_path, video_size)
    print(f"  upload PUT status: {status_code}")

    print("polling publish status ...")
    result = poll_status(access_token, publish_id)

    # The publish_id is the only durable handle on a post - it is what the
    # status endpoint takes, and the only thing tying an episode packet to the
    # upload it produced. It used to be printed and then thrown away: the
    # report saved here held just the final status poll, so the receipt could
    # not answer "which post is this?" after the terminal scrolled.
    result = dict(result)
    result["publish_id"] = publish_id
    result["privacy_level"] = args.privacy
    result["video_path"] = os.path.basename(args.video_path)
    out_path = args.out or (os.path.splitext(args.video_path)[0] + ".tiktok_publish.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    print(f"wrote {out_path}")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
