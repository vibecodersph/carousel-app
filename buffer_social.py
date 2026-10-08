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
  python buffer_social.py post episodes/05-x/publish/youtube/manifest.json --channel youtube --when now --publish
      YouTube Short: the manifest's vertical video. Title and description are the manifest's youtube_title and
      youtube_description (youtube_made_for_kids optional); without them they are built from the approved
      publish/caption.txt and publish/first-comment.txt two directories above the manifest, see youtube_texts.

      --channel  SERVICE[:NAME-PART] or a Buffer channel id. Repeat for several channels.
      --when     an ISO time with offset (scheduled), `now`, `queue` (Buffer's own schedule) or
                 `draft` (a Buffer draft, never published).
      Everything except `draft` and `--dry-run` needs --publish.

  python buffer_social.py status REPORT.json      refresh status, link and errors from Buffer
  python buffer_social.py cancel REPORT.json      delete the scheduled or draft posts it created

MANIFEST is the same file the other publishers read:
  {"slides":[{"index":1,"type":"video","path":"C:/.../reel.mp4"}],
   "tiktok_caption":"...", "x_text":"...", "caption":"fallback for any channel",
   "x_thread":["reply under the X post", "..."], "cover_ms":3000}
  Keep one manifest per voice: X is Viron's personal account, TikTok is the Vibe Coders PH brand.

GUARDS
------
* No repeat posts: <manifest dir>/buffer_social.json records every post; a channel already in it
  is skipped unless --force.
* Two-hour gap per channel (--gap-hours to change, 0 to switch off) checked against the posts
  Buffer already holds for that channel, for `now` and explicit times.
* TikTok caption at most 2,200 characters and 5 hashtags (Buffer's limits); X text at most 280; YouTube title 1 to 100
  and description 1 to 5,000 characters.
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
YOUTUBE_TITLE_MAX, YOUTUBE_DESC_MAX = 100, 5000
YOUTUBE_META = {"categoryId": "27", "privacy": "public", "madeForKids": False, "notifySubscribers": False, "embeddable": True}   # 27 = Education
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


def youtube_title(caption: str) -> str:
    """The hook question: the first line up to and including its first "?" (the first line when it has none), cut at a word boundary to 100."""
    lines = [ln.strip() for ln in caption.strip().splitlines() if ln.strip()]
    if not lines:
        raise SystemExit("YouTube title: the caption is empty")
    first = lines[0]
    q = first.find("?")
    title = first[:q + 1] if q >= 0 else first
    if len(title) > YOUTUBE_TITLE_MAX:
        cut = title[:YOUTUBE_TITLE_MAX]
        if title[YOUTUBE_TITLE_MAX] != " " and " " in cut:
            cut = cut.rsplit(" ", 1)[0]
        title = cut.rstrip()
    return title


YOUTUBE_WORDING = (("in the first comment", "below"), ("Nasa comments ang sources", "Nasa baba ang sources"))


def youtube_wording(text: str) -> str:
    """The captions say the sources are in the first comment; on YouTube they sit right below in the description, so only those words change."""
    for old, new in YOUTUBE_WORDING:
        text = text.replace(old, new)
    return text


def youtube_description(caption: str, comment: str) -> str:
    """The whole caption, a blank line, then the sources (first comment); over 5,000 the caption stays whole and the sources lose whole lines from the end."""
    caption, comment = youtube_wording(caption.strip()), comment.strip()
    if not comment:
        return caption
    room = YOUTUBE_DESC_MAX - len(caption) - 2
    keep: list[str] = []
    used = 0
    for ln in comment.splitlines():
        used += len(ln) + (1 if keep else 0)
        if used > room:
            break
        keep.append(ln)
    return (caption + "\n\n" + "\n".join(keep).rstrip()) if keep else caption


def validate_youtube(title: str, description: str) -> None:
    if not 1 <= len(title.strip()) <= YOUTUBE_TITLE_MAX:
        raise SystemExit(f"YouTube title is {len(title.strip())} characters; it must be 1 to {YOUTUBE_TITLE_MAX}")
    if not 1 <= len(description.strip()) <= YOUTUBE_DESC_MAX:
        raise SystemExit(f"YouTube description is {len(description.strip())} characters; it must be 1 to {YOUTUBE_DESC_MAX}")


def youtube_texts(publish_dir: Path, title_override: str | None = None) -> tuple[str, str]:
    """(title, description) for the episode's YouTube Short, built only from the two files Viron approved:
    <publish_dir>/caption.txt and <publish_dir>/first-comment.txt (a title he approved by hand may override the derived one).
    This is the one place the derivation lives."""
    try:
        caption = (publish_dir / "caption.txt").read_text(encoding="utf-8")
        comment = (publish_dir / "first-comment.txt").read_text(encoding="utf-8")
    except OSError as e:
        raise SystemExit(f"YouTube needs the approved caption.txt and first-comment.txt in {publish_dir}: {e}") from None
    title, description = (title_override or "").strip() or youtube_title(caption), youtube_description(caption, comment)
    validate_youtube(title, description)
    return title, description


def thread_for(service: str, manifest: dict[str, Any]) -> list[str]:
    """Follow-up posts for an X thread (manifest key x_thread); other services have none."""
    if service != "twitter":
        return []
    raw = manifest.get("x_thread")
    if raw in (None, []):
        return []
    if not isinstance(raw, list) or not all(isinstance(t, str) and t.strip() for t in raw):
        raise SystemExit("x_thread must be a list of non-empty strings, one per follow-up post")
    out = [t.strip() for t in raw]
    for t in out:
        validate_caption("twitter", t)
    return out


def build_input(channel: dict[str, Any], text: str, video_url: str, when: dict[str, Any], *, ai_generated: bool = True, cover_ms: int | None = None, thread: list[str] | None = None, title: str | None = None, made_for_kids: bool = False) -> dict[str, Any]:
    video: dict[str, Any] = {"url": video_url}
    if cover_ms is not None and channel["service"] in ("tiktok", "instagram", "pinterest"):
        video["metadata"] = {"thumbnailOffset": int(cover_ms)}   # cover frame; the first frame of our films is black (fade-in)
    payload: dict[str, Any] = {
        "text": text,
        "channelId": channel["id"],
        "schedulingType": "automatic",
        "mode": when["mode"],
        "saveToDraft": when["saveToDraft"],
        "assets": [{"video": video}],
    }
    if when["dueAt"]:
        payload["dueAt"] = when["dueAt"]
    if channel["service"] == "tiktok":
        payload["metadata"] = {"tiktok": {"isAiGenerated": ai_generated}}
    elif channel["service"] == "instagram":
        # a video is a Reel that also shows on the grid; type and shouldShareToFeed are required by the schema
        payload["metadata"] = {"instagram": {"type": "reel", "shouldShareToFeed": True, "isAiGenerated": ai_generated}}
    elif channel["service"] == "twitter":
        meta: dict[str, Any] = {"isAiGenerated": ai_generated}
        if thread:
            # Buffer publishes exactly what `thread` holds, so the root post (same text, carrying the
            # video) comes first and the replies follow. Listing only the replies drops the video.
            meta["thread"] = [{"text": text, "assets": payload["assets"]}] + [{"text": t, "assets": []} for t in thread]
        payload["metadata"] = {"twitter": meta}
    elif channel["service"] == "youtube":
        if not title:
            raise SystemExit("A YouTube post needs a title (youtube_texts builds it from the approved caption)")
        payload["metadata"] = {"youtube": {"title": title, **{**YOUTUBE_META, "madeForKids": bool(made_for_kids)}, "isAiGenerated": ai_generated}}   # the description is `text`
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
      ... on PostActionSuccess { post { id status dueAt text assets { id source }
        metadata { ... on TwitterPostMetadata { thread { text assets { id source } } } } } }
      ... on MutationError { message } } }"""
    res = buf.q(q, {"i": payload})["createPost"]
    if "post" not in res:
        raise SystemExit(f"Buffer refused the post: {res.get('message')}")
    post = res["post"]
    problem = post_problem(payload, post)
    if problem:
        # never leave a defective post scheduled (an X post that lost its video would go out as text only)
        gone = delete_post(buf, post["id"])
        raise SystemExit(f"{problem} Post {post['id']} was created and then {gone}.")
    return post


def post_problem(payload: dict[str, Any], post: dict[str, Any]) -> str | None:
    """Compare what Buffer stored with what we sent: assets on the post and, for X, the whole thread."""
    if len(post.get("assets") or []) != len(payload["assets"]):
        return f"Buffer kept {len(post.get('assets') or [])} of {len(payload['assets'])} assets."
    sent = ((payload.get("metadata") or {}).get("twitter") or {}).get("thread")
    if sent:
        got = (post.get("metadata") or {}).get("thread") or []
        if len(got) != len(sent):
            return f"Buffer kept {len(got)} of {len(sent)} thread posts."
        for i, (s, g) in enumerate(zip(sent, got)):
            if len(g.get("assets") or []) != len(s["assets"]):
                return f"Buffer kept {len(g.get('assets') or [])} of {len(s['assets'])} assets on thread post {i + 1}."
    return None


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
    done_ids = {p["channel"]["id"] for p in report["posts"] if not p.get("cancelled") and p.get("when") != "draft"}
    override = Path(args.caption_file).read_text(encoding="utf-8") if args.caption_file else None

    plans = []
    for ch in chosen:
        if ch["id"] in done_ids and not args.force:
            print(f"[buffer] {ch['service']}:{ch['name']} already posted for this manifest (see {REPORT_NAME}); skipping (use --force to repeat)")
            continue
        title = None
        if ch["service"] == "youtube":
            if manifest.get("youtube_title") and manifest.get("youtube_description"):     # written by the autopublisher from the approved files
                title, text = str(manifest["youtube_title"]).strip(), str(manifest["youtube_description"]).strip()
                validate_youtube(title, text)
            else:
                title, text = youtube_texts(manifest_path.parent.parent)                  # <episode>/publish/youtube/manifest.json -> <episode>/publish
        else:
            text = caption_for(ch["service"], manifest, override)
            validate_caption(ch["service"], text)
        thread = thread_for(ch["service"], manifest)
        target = datetime.fromisoformat(when["dueAt"].replace("Z", "+00:00")) if when["dueAt"] else datetime.now(timezone.utc)
        if when["kind"] in ("now", "scheduled") and args.gap_hours > 0:
            bad = gap_conflict(existing_post_times(buf, ch), target, args.gap_hours)
            if bad:
                raise SystemExit(f"Gap guard: {ch['service']}:{ch['name']} already has a post at {bad}, within {args.gap_hours} h of {target.isoformat()}. Pick another time or --gap-hours 0.")
        plans.append((ch, text, thread, title))
    if not plans:
        return 0

    base = ig.env_value("R2_PUBLIC_BASE_URL", "INSTAGRAM_MEDIA_BASE_URL", "IG_MEDIA_BASE_URL").strip()
    items = ig.build_media_items(manifest, manifest_path, media_base_url=base, overrides={}, dry_run=args.dry_run)
    videos = [i for i in items if i.kind == "video"]
    if len(videos) != 1:
        raise SystemExit("The manifest must contain exactly one video slide for a TikTok, X or YouTube post.")
    if not args.dry_run:
        ns = SimpleNamespace(r2_bucket="", r2_key_prefix=None, r2_public_base_url=base)
        ig.upload_media_to_r2(videos, ig.r2_config(ns, manifest_path, base), timeout=300)
    video_url = videos[0].public_url
    cover_ms = args.cover_ms if args.cover_ms is not None else manifest.get("cover_ms")

    for ch, text, thread, title in plans:
        payload = build_input(ch, text, video_url, when, ai_generated=not args.not_ai, cover_ms=cover_ms, thread=thread, title=title,
                              made_for_kids=bool(manifest.get("youtube_made_for_kids")))
        print(f"[buffer] {ch['service']}:{ch['name']}  when={args.when}  chars={len(text)}  thread={len(thread)}  video={video_url}")
        if args.dry_run:
            print(json.dumps(payload, indent=1, ensure_ascii=False)[:3000])
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
        if p.get("cancelled"):
            continue
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
    p.add_argument("--cover-ms", type=int, help="cover frame in milliseconds (TikTok); manifest key cover_ms is the default")
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
