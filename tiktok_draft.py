#!/usr/bin/env python3
"""Push a rendered episode into the TikTok app's drafts. No audit required.

WHY THIS EXISTS
---------------
The Content Posting API's **Direct Post** is closed to this app. The audit was
rejected 2026-08-28 for category, not craft:

    "App will not be approved for personal or company internal use.
     TikTok for Developers currently does not support personal or internal
     company use."

Posting to accounts you own is not an approvable use case, so `video.publish`
and `video.list` are unavailable and resubmitting the same app will not pass.

BUT the **inbox (draft) endpoint works right now, unaudited**, on the
`video.upload` scope this app already holds. Verified 2026-09-08:

    /post/publish/inbox/video/init/   -> code "ok", upload URL issued
    /post/publish/video/init/         -> 403 unaudited_client_can_only_post...

So the file transfer is automatable even though publishing is not. This script
uploads the rendered mp4 straight into TikTok's drafts; you open the app and
tap Post. That removes the AirDrop/USB/re-encode step, which was the actual
friction - not the tapping.

WHAT IT DOES NOT DO
-------------------
It does not publish. TikTok does not accept a caption on inbox uploads, so the
caption is printed for you to paste, and written next to the video as
`<name>.tiktok-caption.txt` if one was supplied.

USAGE
-----
    python tiktok_draft.py <video.mp4> [--caption-file <caption.txt>]
    python tiktok_draft.py <video.mp4> --caption-file cap.txt --json

Token comes from .tiktok_sandbox_tokens.json; it is refreshed automatically if
older than ~20 hours (access tokens last 24h).
"""
import argparse
import json
import math
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
TOKENS = HERE / ".tiktok_sandbox_tokens.json"
API = "https://open.tiktokapis.com/v2"
CHUNK_MIN = 5 * 1024 * 1024        # TikTok requires >=5MB chunks (except a lone final one)
CHUNK_MAX = 64 * 1024 * 1024
SINGLE_MAX = 64 * 1024 * 1024      # <=64MB may go as one chunk


def log(msg):
    print(msg, flush=True)


def load_token(refresh_if_stale=True):
    if refresh_if_stale and TOKENS.exists():
        age = time.time() - TOKENS.stat().st_mtime
        if age > 20 * 3600:
            log(f"[token] {age/3600:.1f}h old, refreshing")
            subprocess.run([sys.executable, str(HERE / "tiktok_refresh.py")],
                           check=False, cwd=str(HERE))
    return json.loads(TOKENS.read_text(encoding="utf-8"))["access_token"]


def api_post(path, token, body):
    req = urllib.request.Request(
        API + path, data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:  # noqa: BLE001
            return {"error": {"code": f"http_{e.code}", "message": "unparseable"}}


def plan_chunks(size):
    """TikTok: every chunk >=5MB and <=64MB, final chunk may be larger than
    the rest but the total must match exactly. One chunk if it fits."""
    if size <= SINGLE_MAX:
        return size, 1
    count = math.floor(size / CHUNK_MIN)
    count = max(1, min(count, 1000))
    chunk = CHUNK_MIN
    while size / count > CHUNK_MAX:
        count += 1
    return chunk, count


def upload(url, path, size, chunk, count):
    data = path.read_bytes()
    for i in range(count):
        start = i * chunk
        end = size - 1 if i == count - 1 else start + chunk - 1
        part = data[start:end + 1]
        req = urllib.request.Request(url, data=part, method="PUT")
        req.add_header("Content-Type", "video/mp4")
        req.add_header("Content-Length", str(len(part)))
        req.add_header("Content-Range", f"bytes {start}-{end}/{size}")
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                log(f"[upload] chunk {i+1}/{count}  HTTP {r.status}")
        except urllib.error.HTTPError as e:
            log(f"[upload] chunk {i+1}/{count} FAILED HTTP {e.code}: {e.read().decode()[:200]}")
            return False
    return True


def poll(token, publish_id, tries=30):
    for n in range(1, tries + 1):
        r = api_post("/post/publish/status/fetch/", token, {"publish_id": publish_id})
        err = r.get("error") or {}
        if err.get("code") not in ("ok", None, ""):
            # transient 5xx must not be read as failure - the file is already up
            log(f"[status] {n}: {err.get('code')} (retrying)")
            time.sleep(5)
            continue
        st = (r.get("data") or {}).get("status")
        log(f"[status] {n}: {st}")
        if st in ("PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"):
            return True, st
        if st == "FAILED":
            return False, (r.get("data") or {}).get("fail_reason", "unknown")
        time.sleep(5)
    return False, "timeout"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--caption-file")
    ap.add_argument("--json", action="store_true", help="machine-readable result line")
    a = ap.parse_args()

    path = pathlib.Path(a.video).expanduser().resolve()
    if not path.exists():
        log(f"ERROR: {path} not found")
        return 1
    size = path.stat().st_size
    if size > 4 * 1024 ** 3:
        log("ERROR: over TikTok's 4GB limit")
        return 1

    caption = ""
    if a.caption_file:
        cp = pathlib.Path(a.caption_file).expanduser()
        if cp.exists():
            caption = cp.read_text(encoding="utf-8").strip()

    token = load_token()
    chunk, count = plan_chunks(size)
    log(f"[init] {path.name}  {size/1048576:.1f}MB  {count} chunk(s)")

    r = api_post("/post/publish/inbox/video/init/", token, {
        "source_info": {"source": "FILE_UPLOAD", "video_size": size,
                        "chunk_size": chunk, "total_chunk_count": count}})
    err = r.get("error") or {}
    if err.get("code") not in ("ok", None, ""):
        log(f"ERROR init: {err.get('code')} - {err.get('message','')[:200]}")
        if err.get("code") == "unaudited_client_can_only_post_to_private_accounts":
            log("  (that error means Direct Post, not inbox - check the endpoint)")
        return 1

    d = r.get("data") or {}
    publish_id, url = d.get("publish_id"), d.get("upload_url")
    log(f"[init] publish_id {publish_id}")

    if not upload(url, path, size, chunk, count):
        return 1

    ok, state = poll(token, publish_id)
    if not ok:
        log(f"FAILED: {state}")
        return 1

    log("")
    log("=" * 62)
    log("  IN YOUR TIKTOK DRAFTS. Open the app -> Inbox notification -> Post.")
    log("=" * 62)
    if caption:
        cap_path = path.with_suffix(".tiktok-caption.txt")
        cap_path.write_text(caption, encoding="utf-8")
        log(f"\nCaption (TikTok does not accept one on inbox uploads - paste it):")
        log("-" * 62)
        log(caption)
        log("-" * 62)
        log(f"also saved to {cap_path.name}")

    if a.json:
        print(json.dumps({"ok": True, "publish_id": publish_id, "status": state,
                          "video": str(path), "size": size}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
