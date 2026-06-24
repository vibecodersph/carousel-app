#!/usr/bin/env python3
"""TikTok OAuth helper: mint, refresh, and smoke-test user access tokens.

The Content Posting API needs a per-user OAuth access token, not a long-lived
system token like Meta. This CLI walks the standard authorization-code flow and
stores tokens in ``state/tiktok_tokens.json`` (keyed by channel), then prints
the ``.env`` line to paste so ``tiktok_publish.py`` and ``reel_scheduler.py``
pick the token up.

Prerequisites (see TIKTOK_SETUP.md): a registered TikTok app with the Content
Posting API product, and these in ``.env``:

    TIKTOK_CLIENT_KEY=awxxxx
    TIKTOK_CLIENT_SECRET=xxxx
    TIKTOK_REDIRECT_URI=https://your.site/tiktok/callback

Flow:
    uv run python tiktok_auth.py url --channel vibecodersph     # open, authorize
    uv run python tiktok_auth.py exchange <code> --channel vibecodersph
    uv run python tiktok_auth.py creator-info --channel vibecodersph   # smoke test
    uv run python tiktok_auth.py refresh --channel vibecodersph        # when expired
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import instagram_publish as ig
from fetch_tweet_data import load_env_file

ROOT = Path(__file__).resolve().parent
TOKEN_STORE = ROOT / "state" / "tiktok_tokens.json"

AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
DEFAULT_SCOPES = "user.info.basic,video.upload,video.publish"


def channel_value(channel_id: str, *bases: str) -> str:
    if channel_id:
        scoped = ig.channel_env_value(channel_id, *bases)
        if scoped:
            return scoped
    return ig.env_value(*bases)


def store_key(channel_id: str) -> str:
    return channel_id or "default"


def load_store() -> dict[str, Any]:
    if TOKEN_STORE.exists():
        try:
            return json.loads(TOKEN_STORE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_tokens(channel_id: str, tokens: dict[str, Any]) -> None:
    store = load_store()
    expires_in = int(tokens.get("expires_in") or 0)
    refresh_expires_in = int(tokens.get("refresh_expires_in") or 0)
    now = datetime.now(timezone.utc)
    record = {
        "access_token": tokens.get("access_token"),
        "refresh_token": tokens.get("refresh_token"),
        "open_id": tokens.get("open_id"),
        "scope": tokens.get("scope"),
        "expires_at": (now + timedelta(seconds=expires_in)).isoformat() if expires_in else None,
        "refresh_expires_at": (
            (now + timedelta(seconds=refresh_expires_in)).isoformat() if refresh_expires_in else None
        ),
        "updated_at": now.isoformat(),
    }
    store[store_key(channel_id)] = record
    TOKEN_STORE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_STORE.write_text(json.dumps(store, indent=2, ensure_ascii=False) + "\n")


def env_line(channel_id: str, access_token: str) -> str:
    suffix = ig.env_key_suffix(channel_id)
    name = f"TIKTOK_ACCESS_TOKEN_{suffix}" if suffix else "TIKTOK_ACCESS_TOKEN"
    return f"{name}={access_token}"


def token_request(form: dict[str, str]) -> dict[str, Any]:
    data = urllib.parse.urlencode(form).encode("utf-8")
    request = urllib.request.Request(
        TOKEN_URL,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Cache-Control": "no-cache",
            "User-Agent": "carousel-app/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - surface the body if present
        body = getattr(exc, "read", lambda: b"")()
        raise SystemExit(f"TikTok token request failed: {exc} {body[:300]!r}") from exc
    if payload.get("error"):
        raise SystemExit(f"TikTok token error: {payload.get('error')} {payload.get('error_description')}")
    return payload


def require(value: str, name: str) -> str:
    if not value:
        raise SystemExit(f"Missing {name}. Set it in .env (see TIKTOK_SETUP.md).")
    return value


def url_command(args: argparse.Namespace) -> int:
    client_key = require(channel_value(args.channel, "TIKTOK_CLIENT_KEY"), "TIKTOK_CLIENT_KEY")
    redirect_uri = require(channel_value(args.channel, "TIKTOK_REDIRECT_URI"), "TIKTOK_REDIRECT_URI")
    state = args.state or (f"channel_{args.channel}" if args.channel else "carousel_app")
    query = urllib.parse.urlencode(
        {
            "client_key": client_key,
            "scope": args.scope,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )
    print(f"{AUTHORIZE_URL}?{query}")
    print("\nOpen this URL, authorize, then copy the `code` query param from the redirect.")
    print(f"Next: uv run python tiktok_auth.py exchange <code> --channel {args.channel or ''}".rstrip())
    return 0


def exchange_command(args: argparse.Namespace) -> int:
    client_key = require(channel_value(args.channel, "TIKTOK_CLIENT_KEY"), "TIKTOK_CLIENT_KEY")
    client_secret = require(channel_value(args.channel, "TIKTOK_CLIENT_SECRET"), "TIKTOK_CLIENT_SECRET")
    redirect_uri = require(channel_value(args.channel, "TIKTOK_REDIRECT_URI"), "TIKTOK_REDIRECT_URI")
    tokens = token_request(
        {
            "client_key": client_key,
            "client_secret": client_secret,
            "code": urllib.parse.unquote(args.code),
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
    )
    save_tokens(args.channel, tokens)
    print(f"[tiktok-auth] saved tokens for {store_key(args.channel)} -> {TOKEN_STORE}")
    print(f"[tiktok-auth] scopes: {tokens.get('scope')}")
    print("\nAdd this line to .env:\n")
    print(env_line(args.channel, str(tokens.get("access_token") or "")))
    return 0


def refresh_command(args: argparse.Namespace) -> int:
    client_key = require(channel_value(args.channel, "TIKTOK_CLIENT_KEY"), "TIKTOK_CLIENT_KEY")
    client_secret = require(channel_value(args.channel, "TIKTOK_CLIENT_SECRET"), "TIKTOK_CLIENT_SECRET")
    refresh_token = args.refresh_token or channel_value(args.channel, "TIKTOK_REFRESH_TOKEN")
    if not refresh_token:
        record = load_store().get(store_key(args.channel)) or {}
        refresh_token = str(record.get("refresh_token") or "")
    refresh_token = require(refresh_token, "refresh token (saved store, env, or --refresh-token)")
    tokens = token_request(
        {
            "client_key": client_key,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
    )
    save_tokens(args.channel, tokens)
    print(f"[tiktok-auth] refreshed tokens for {store_key(args.channel)}")
    print("\nUpdate this line in .env:\n")
    print(env_line(args.channel, str(tokens.get("access_token") or "")))
    return 0


def creator_info_command(args: argparse.Namespace) -> int:
    import tiktok_publish

    token = args.access_token or channel_value(args.channel, "TIKTOK_ACCESS_TOKEN", "TIKTOK_TOKEN")
    if not token:
        record = load_store().get(store_key(args.channel)) or {}
        token = str(record.get("access_token") or "")
    token = require(token, "TIKTOK access token")
    info = tiktok_publish.query_creator_info(token)
    print(json.dumps(info, indent=2, ensure_ascii=False))
    allowed = info.get("privacy_level_options")
    if isinstance(allowed, list):
        print(f"\n[tiktok-auth] account allows privacy levels: {allowed}")
        if allowed == ["SELF_ONLY"]:
            print("[tiktok-auth] only SELF_ONLY -> app is unaudited; use --mode inbox for public posts.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TikTok OAuth helper for the reel pipeline")
    sub = parser.add_subparsers(dest="command", required=True)

    url = sub.add_parser("url", help="Print the authorization URL")
    url.add_argument("--channel", default="", help="Channel id (uses per-channel TIKTOK_* env)")
    url.add_argument("--scope", default=DEFAULT_SCOPES, help="Comma-separated scopes")
    url.add_argument("--state", default="", help="OAuth state value")

    exchange = sub.add_parser("exchange", help="Exchange an auth code for tokens")
    exchange.add_argument("code", help="The `code` from the redirect URL")
    exchange.add_argument("--channel", default="")

    refresh = sub.add_parser("refresh", help="Refresh an access token")
    refresh.add_argument("--channel", default="")
    refresh.add_argument("--refresh-token", default="", help="Override the stored refresh token")

    info = sub.add_parser("creator-info", help="Smoke-test the token + show allowed privacy levels")
    info.add_argument("--channel", default="")
    info.add_argument("--access-token", default="")
    return parser


def main() -> int:
    load_env_file(ROOT / ".env")
    args = build_parser().parse_args()
    if args.command == "url":
        return url_command(args)
    if args.command == "exchange":
        return exchange_command(args)
    if args.command == "refresh":
        return refresh_command(args)
    if args.command == "creator-info":
        return creator_info_command(args)
    raise SystemExit(f"Unknown command: {args.command}")


if __name__ == "__main__":
    sys.exit(main())
