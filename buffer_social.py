#!/usr/bin/env python3
"""Post a rendered episode to TikTok, X and other channels through Buffer's API.

WHY BUFFER
----------
TikTok's own Direct Post API is closed to us. The app was rejected 2026-08-28 ("TikTok for
Developers currently does not support personal or internal company use"), and even an approved
app must show a human a confirmation screen for every post. Buffer's TikTok integration is
already audited, posts publicly, schedules ahead, and its API is on every plan, including Free
(1 key, 3,000 requests per 30 days, 3 channels). The same rail posts to X.

Buffer fetches the video from a public URL, so the mp4 is first put on R2 with the helpers
`instagram_publish.py` already uses.

COMMANDS
--------
  python buffer_social.py channels
      List the connected channels (id, service, name, state).

  python buffer_social.py post MANIFEST --channel tiktok:vibecodersph --when 2026-10-01T19:00+08:00 --publish
  python buffer_social.py post MANIFEST --channel tiktok:caisie --channel x --when draft
  python buffer_social.py post MANIFEST --channel x --when now --publish --dry-run

      --channel  SERVICE[:NAME-PART] or a Buffer channel id. Repeat for several channels.
      --when     an ISO time with offset (scheduled), `now`, `queue` (Buffer's own schedule) or
                 `draft` (a Buffer draft, never published).
      Everything except `draft` and `--dry-run` needs --publish.

  python buffer_social.py status REPORT.json      refresh status, link and errors from Buffer
  python buffer_social.py cancel REPORT.json      delete the scheduled or draft posts it created

MANIFEST is the same file the other publishers read:
  {"slides":[{"index":1,"type":"video","path":"C:/.../reel.mp4"}],
   "tiktok_caption":"...", "x_text":"...", "caption":"fallback for any channel"}

GUARDS
------
* No repeat posts: <manifest dir>/buffer_social.json records every post; a channel already in it
  is skipped unless --force.
* Two-hour gap per channel (--gap-hours to change, 0 to switch off) checked against the posts
  Buffer already holds for that channel, for `now` and explicit times.
* TikTok caption at most 2,200 characters and 5 hashtags (Buffer's limits); X text at most 280.
* The API key never prints. It is read from BUFFER_SOCIAL_API_KEY in carousel-app/.env or the
  hermes .env. (BUFFER_API_KEY belongs to a different, older Buffer account and is not used.)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parent
API_URL = "https://api.buffer.com"
KEY_NAME = "BUFFER_SOCIAL_API_KEY"
TIKTOK_MAX_CHARS, TIKTOK_MAX_TAGS, X_MAX_CHARS = 2200, 5, 280
REPORT_NAME = "buffer_social.json"
ENV_FILES = (ROOT / ".env", Path(os.environ.get("LOCALAPPDATA", "")) / "hermes" / ".env")


# ---------------------------------------------------------------- environment and HTTP

def env_value(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if value:
        return value
    for f in ENV_FILES:
        if f.exists():
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith(name + "="):
                    return line.split("=", 1)[1].strip().strip("\"'")
    return ""


class Buffer:
    def __init__(self, api_key: str):
        if not api_key:
            raise SystemExit(f"{KEY_NAME} is not set (carousel-app/.env). Create a key in Buffer > Settings > API.")
        self.key = api_key

    def q(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        req = urllib.request.Request(
            API_URL,
            data=json.dumps({"query": query, "variables": variables or {}}).encode(),
            headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json", "User-Agent": "carousel-app/1.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                out = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise SystemExit(f"Buffer API {e.code}: {e.read().decode(errors='replace')[:500].replace(self.key, '***')}") from None
        if out.get("errors"):
            msg = json.dumps(out["errors"])[:600].replace(self.key, "***")
            raise SystemExit(f"Buffer API error: {msg}")
        return out["data"]

    def organization_id(self) -> str:
        orgs = self.q("{ account { organizations { id name } } }")["account"]["organizations"]
        if not orgs:
            raise SystemExit("This Buffer account has no organization.")
        return orgs[0]["id"]

    def channels(self) -> list[dict[str, Any]]:
        org = self.organization_id()
        rows = self.q(
            "query($i: ChannelsInput!){ channels(input:$i){ id name displayName service type timezone isDisconnected isLocked isQueuePaused } }",
            {"i": {"organizationId": org}},
        )["channels"]
        for r in rows:
            r["organizationId"] = org
        return rows


# ---------------------------------------------------------------- pure helpers (unit-tested)

def resolve_channel(channels: list[dict[str, Any]], spec: str) -> dict[str, Any]:
    """`tiktok:vibes`, `x`, or a raw id -> exactly one connected, unlocked channel."""
    spec = spec.strip()
    by_id = [c for c in channels if c["id"] == spec]
    if by_id:
        matches = by_id
    else:
        service, _, part = spec.partition(":")
        service = {"twitter": "twitter", "x": "twitter"}.get(service.lower(), service.lower())
        matches = [
            c for c in channels
            if c["service"] == service and (not part or part.lower() in (c.get("name") or "").lower() or part.lower() in (c.get("displayName") or "").lower())
        ]
    if not matches:
        have = ", ".join(f"{c['service']}:{c.get('name')}" for c in channels) or "none"
        raise SystemExit(f"No channel matches '{spec}'. Connected channels: {have}")
    if len(matches) > 1:
        raise SystemExit(f"'{spec}' matches {len(matches)} channels ({', '.join(str(c.get('name')) for c in matches)}); be more specific, e.g. tiktok:NAME")
    ch = matches[0]
    if ch.get("isDisconnected"):
        raise SystemExit(f"Channel {ch['service']}:{ch['name']} is disconnected in Buffer; reconnect it first.")
    if ch.get("isLocked"):
        raise SystemExit(f"Channel {ch['service']}:{ch['name']} is locked (plan limit); free a slot in Buffer first.")
    return ch


def parse_when(value: str, now: datetime | None = None) -> dict[str, Any]:
    """`draft` | `queue` | `now` | ISO time with offset -> Buffer mode fields."""
    now = now or datetime.now(timezone.utc)
    v = value.strip().lower()
    if v == "draft":
        return {"kind": "draft", "mode": "addToQueue", "saveToDraft": True, "dueAt": None}
    if v == "queue":
        return {"kind": "queue", "mode": "addToQueue", "saveToDraft": False, "dueAt": None}
    if v == "now":
        return {"kind": "now", "mode": "shareNow", "saveToDraft": False, "dueAt": None}
    try:
        t = datetime.fromisoformat(value.strip())
    except ValueError:
        raise SystemExit(f"--when must be draft, queue, now or an ISO time like 2026-10-01T19:00+08:00 (got '{value}')") from None
    if t.tzinfo is None:
        raise SystemExit("--when needs a UTC offset, for example 2026-10-01T19:00+08:00")
    t = t.astimezone(timezone.utc)
    if t < now + timedelta(minutes=10):
        raise SystemExit("--when must be at least 10 minutes in the future")
    return {"kind": "scheduled", "mode": "customScheduled", "saveToDraft": False, "dueAt": t.strftime("%Y-%m-%dT%H:%M:%S.000Z")}


def caption_for(service: str, manifest: dict[str, Any], override: str | None = None) -> str:
    if override:
        return override.strip()
    keys = {"tiktok": ("tiktok_caption",), "twitter": ("x_text", "twitter_text")}.get(service, ())
    for k in keys + (f"{service}_caption", "caption", "instagram_caption", "facebook_caption"):
        v = str(manifest.get(k) or "").strip()
        if v:
            return v
    raise SystemExit(f"No caption for {service}: add tiktok_caption / x_text / caption to the manifest or pass --caption-file")


def validate_caption(service: str, text: str) -> None:
    if service == "tiktok":
        tags = re.findall(r"(?<!\w)#\w+", text)
        if len(text) > TIKTOK_MAX_CHARS:
            raise SystemExit(f"TikTok caption is {len(text)} characters; Buffer allows {TIKTOK_MAX_CHARS}")
        if len(tags) > TIKTOK_MAX_TAGS:
            raise SystemExit(f"TikTok caption has {len(tags)} hashtags; Buffer allows {TIKTOK_MAX_TAGS}")
    if service == "twitter" and len(text) > X_MAX_CHARS:
        raise SystemExit(f"X text is {len(text)} characters; the limit is {X_MAX_CHARS}")
    if "\u2014" in text or "\u2013" in text:
        raise SystemExit("Caption contains an em or en dash; house rule says no dashes in copy")


def build_input(channel: dict[str, Any], text: str, video_url: str, when: dict[str, Any], *, ai_generated: bool = True) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "text": text,
        "channelId": channel["id"],
        "schedulingType": "automatic",
        "mode": when["mode"],
        "saveToDraft": when["saveToDraft"],
        "assets": [{"video": {"url": video_url}}],
    }
    if when["dueAt"]:
        payload["dueAt"] = when["dueAt"]
    if channel["service"] == "tiktok":
        payload["metadata"] = {"tiktok": {"isAiGenerated": ai_generated}}
    elif channel["service"] == "twitter":
        payload["metadata"] = {"twitter": {"isAiGenerated": ai_generated}}
    return payload


def gap_conflict(existing_due: list[str], target: datetime, gap_hours: float) -> str | None:
    """Return the offending ISO time if any existing post is within gap_hours of target."""
    if gap_hours <= 0:
        return None
    for iso in existing_due:
        try:
            t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        except ValueError:
            continue
        if abs((t - target).total_seconds()) < gap_hours * 3600:
            return iso
    return None


# ---------------------------------------------------------------- Buffer operations

POST_FIELDS = "id status dueAt sentAt externalLink channelId channelService text error { message supportUrl }"


def existing_post_times(buf: Buffer, channel: dict[str, Any]) -> list[str]:
    q = f"query($i: PostsInput!){{ posts(input:$i, first: 30){{ edges {{ node {{ id status dueAt sentAt }} }} }} }}"
    data = buf.q(q, {"i": {"organizationId": channel["organizationId"],
                            "filter": {"channelIds": [channel["id"]], "status": ["scheduled", "sending", "sent"]},
                            "sort": [{"field": "dueAt", "direction": "desc"}]}})
    out = []
    for e in (data.get("posts") or {}).get("edges", []):
        n = e["node"]
        t = n.get("sentAt") or n.get("dueAt")
        if t:
            out.append(t)
    return out


def create_post(buf: Buffer, payload: dict[str, Any]) -> dict[str, Any]:
    q = """mutation($i: CreatePostInput!){ createPost(input:$i){
      ... on PostActionSuccess { post { id status dueAt text assets { id source } } }
      ... on MutationError { message } } }"""
    res = buf.q(q, {"i": payload})["createPost"]
    if "post" not in res:
        raise SystemExit(f"Buffer refused the post: {res.get('message')}")
    post = res["post"]
    if len(post.get("assets") or []) != len(payload["assets"]):
        raise SystemExit(f"Buffer kept {len(post.get('assets') or [])} of {len(payload['assets'])} assets on post {post['id']}; check it in Buffer.")
    return post


def fetch_post(buf: Buffer, post_id: str) -> dict[str, Any]:
    return buf.q(f"query($i: PostInput!){{ post(input:$i){{ {POST_FIELDS} }} }}", {"i": {"id": post_id}})["post"]


def delete_post(buf: Buffer, post_id: str) -> str:
    res = buf.q("mutation($i: DeletePostInput!){ deletePost(input:$i){ ... on DeletePostSuccess { id } ... on VoidMutationError { message } } }", {"i": {"id": post_id}})["deletePost"]
    return "deleted" if res.get("id") else f"failed: {res.get('message')}"


# ---------------------------------------------------------------- commands

def cmd_channels(args: argparse.Namespace) -> int:
    buf = Buffer(env_value(KEY_NAME))
    rows = buf.channels()
    if not rows:
        print("No channels connected in this Buffer account yet.")
        return 0
    for c in rows:
        state = "DISCONNECTED" if c["isDisconnected"] else "locked" if c["isLocked"] else "ok"
        print(f"{c['id']}  {c['service']:<10} {str(c.get('name')):<28} {c['type']:<9} {state}  {c.get('timezone')}")
    return 0


def load_report(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"posts": []}


def cmd_post(args: argparse.Namespace) -> int:
    import instagram_publish as ig
    from fetch_tweet_data import load_env_file
    load_env_file(ROOT / ".env")
    manifest_path = Path(args.manifest).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    report_path = manifest_path.with_name(REPORT_NAME)
    report = load_report(report_path)
    when = parse_when(args.when)
    if when["kind"] != "draft" and not args.dry_run and not args.publish:
        raise SystemExit("Refusing to schedule or post without --publish (use --when draft or --dry-run to test).")

    buf = Buffer(env_value(KEY_NAME))
    channels = buf.channels()
    chosen = [resolve_channel(channels, s) for s in args.channel]
    done_ids = {p["channel"]["id"] for p in report["posts"]}
    override = Path(args.caption_file).read_text(encoding="utf-8") if args.caption_file else None

    plans = []
    for ch in chosen:
        if ch["id"] in done_ids and not args.force:
            print(f"[buffer] {ch['service']}:{ch['name']} already posted for this manifest (see {REPORT_NAME}); skipping (use --force to repeat)")
            continue
        text = caption_for(ch["service"], manifest, override)
        validate_caption(ch["service"], text)
        target = datetime.fromisoformat(when["dueAt"].replace("Z", "+00:00")) if when["dueAt"] else datetime.now(timezone.utc)
        if when["kind"] in ("now", "scheduled") and args.gap_hours > 0:
            bad = gap_conflict(existing_post_times(buf, ch), target, args.gap_hours)
            if bad:
                raise SystemExit(f"Gap guard: {ch['service']}:{ch['name']} already has a post at {bad}, within {args.gap_hours} h of {target.isoformat()}. Pick another time or --gap-hours 0.")
        plans.append((ch, text))
    if not plans:
        return 0

    items = ig.build_media_items(manifest, manifest_path, media_base_url="", overrides={}, dry_run=args.dry_run)
    videos = [i for i in items if i.kind == "video"]
    if len(videos) != 1:
        raise SystemExit("The manifest must contain exactly one video slide for a TikTok or X post.")
    if not args.dry_run:
        ns = SimpleNamespace(r2_bucket="", r2_key_prefix=None, r2_public_base_url=ig.env_value("R2_PUBLIC_BASE_URL"))
        ig.upload_media_to_r2(videos, ig.r2_config(ns, manifest_path, ns.r2_public_base_url), timeout=300)
    video_url = videos[0].public_url

    for ch, text in plans:
        payload = build_input(ch, text, video_url, when, ai_generated=not args.not_ai)
        print(f"[buffer] {ch['service']}:{ch['name']}  when={args.when}  chars={len(text)}  video={video_url}")
        if args.dry_run:
            print(json.dumps(payload, indent=1, ensure_ascii=False)[:900])
            continue
        post = create_post(buf, payload)
        report["posts"].append({"channel": {"id": ch["id"], "service": ch["service"], "name": ch["name"]},
                                "post_id": post["id"], "when": args.when, "dueAt": post.get("dueAt"), "status": post.get("status"),
                                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "caption_chars": len(text)})
        report_path.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"[buffer] created post {post['id']} status={post.get('status')} dueAt={post.get('dueAt')}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    path = Path(args.report).resolve()
    if path.is_dir():
        path = path / REPORT_NAME
    report = load_report(path)
    buf = Buffer(env_value(KEY_NAME))
    for p in report["posts"]:
        try:
            now = fetch_post(buf, p["post_id"])
        except SystemExit as e:
            p["last_error"] = str(e)[:200]
            print(f"[buffer] {p['channel']['service']}:{p['channel']['name']} {p['post_id']}: {p['last_error']}")
            continue
        p.update({"status": now["status"], "dueAt": now.get("dueAt"), "sentAt": now.get("sentAt"), "url": now.get("externalLink"),
                  "error": (now.get("error") or {}).get("message")})
        print(f"[buffer] {p['channel']['service']}:{p['channel']['name']}  {p['status']}  due={p.get('dueAt')}  sent={p.get('sentAt')}  {p.get('url') or ''}  {p.get('error') or ''}")
    path.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    path = Path(args.report).resolve()
    if path.is_dir():
        path = path / REPORT_NAME
    report = load_report(path)
    buf = Buffer(env_value(KEY_NAME))
    for p in report["posts"]:
        if p.get("status") in ("sent",):
            print(f"[buffer] {p['post_id']} already sent; not deleting")
            continue
        p["cancelled"] = delete_post(buf, p["post_id"])
        print(f"[buffer] {p['channel']['service']}:{p['channel']['name']} {p['post_id']}: {p['cancelled']}")
    path.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("channels").set_defaults(fn=cmd_channels)
    p = sub.add_parser("post")
    p.add_argument("manifest")
    p.add_argument("--channel", action="append", required=True)
    p.add_argument("--when", required=True)
    p.add_argument("--caption-file")
    p.add_argument("--gap-hours", type=float, default=2.0)
    p.add_argument("--not-ai", action="store_true", help="do not set the AI-generated label")
    p.add_argument("--force", action="store_true")
    p.add_argument("--publish", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_post)
    for name, fn in (("status", cmd_status), ("cancel", cmd_cancel)):
        s = sub.add_parser(name)
        s.add_argument("report")
        s.set_defaults(fn=fn)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
