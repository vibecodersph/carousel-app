#!/usr/bin/env python3
"""Channel configuration: branding + language + voice bundled per publishing channel.

A *channel* is everything that makes carousel output specific to one brand and one
language at the same time:

- the visual branding (colors, type, layout pattern) the slides are rendered with,
- the account handle shown on the slides,
- the language the AI writes copy in (Taglish, Japanese, ...),
- the audience the copy is written for,
- and the brand-voice guide the AI follows for cover copy and captions.

Swap the whole bundle by changing one setting. Channels live in
``channels/<id>/channel.json`` and each carries its own voice guide markdown.

The active channel is resolved, in priority order, from:

1. an explicit ``channel_id`` argument (e.g. the ``--channel`` CLI flag, which the
   builders surface by setting the ``CAROUSEL_CHANNEL`` env var),
2. the ``CAROUSEL_CHANNEL`` environment variable,
3. the ``default_channel`` field in ``channels/channels.json``,
4. the built-in fallback ``"vibecodersph"`` (which also reads the legacy root
   ``brand.json`` / ``brand/VIBECODERS_IG_VOICE.md`` when no channel folder exists,
   so existing checkouts keep working unchanged).

To add a channel: copy an existing ``channels/<id>/`` folder, edit its
``channel.json`` and voice guide, then point ``default_channel`` (or set
``CAROUSEL_CHANNEL``) at the new id.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
CHANNELS_DIR = ROOT / "channels"
REGISTRY_PATH = CHANNELS_DIR / "channels.json"
FALLBACK_CHANNEL_ID = "vibecodersph"


@dataclass
class Channel:
    """A resolved publishing channel: branding, language, and voice in one bundle."""

    id: str
    account_name: str  # handle text shown on the slide, e.g. "vibecodersph"
    brand_name: str  # display name used inside AI prompts, e.g. "VibeCoders PH"
    handle: str  # social handle, e.g. "@vibecodersph"
    language_name: str  # language the AI writes copy in, e.g. "Taglish", "Japanese"
    audience: str  # who the copy is for, e.g. "Filipino AI builder audience"
    brand: dict[str, Any] = field(default_factory=dict)  # colors / type / pattern (was brand.json)
    publishing: dict[str, Any] = field(default_factory=dict)
    voice_doc: Path | None = None  # markdown voice guide for this channel
    logo_path: Path | None = None  # brand logo asset for this channel (avatar/mark)
    config_path: Path | None = None

    @property
    def voice_prompt(self) -> str:
        """Cover-generation voice instruction, extracted from this channel's guide."""
        if self.voice_doc is None:
            return ""
        return extract_voice_prompt(self.voice_doc)

    @property
    def voice_doc_rel(self) -> str:
        """Repo-relative path to the voice guide, for recording in manifests."""
        if self.voice_doc is None or not self.voice_doc.exists():
            return ""
        try:
            return str(self.voice_doc.resolve().relative_to(ROOT))
        except ValueError:
            return str(self.voice_doc)

    @property
    def logo_doc_rel(self) -> str:
        """Repo-relative path to the logo asset, for recording in manifests."""
        if self.logo_path is None or not self.logo_path.exists():
            return ""
        try:
            return str(self.logo_path.resolve().relative_to(ROOT))
        except ValueError:
            return str(self.logo_path)

    def default_cover_voice(self) -> str:
        """Channel-aware fallback used when the voice guide can't be read."""
        return (
            f"Write witty {self.language_name}-native Instagram cover lines for "
            f"{self.brand_name}. Exactly one accent word must be wrapped in [brackets]."
        )


def extract_voice_prompt(voice_doc: Path) -> str:
    """Pull the automation copy block from a voice guide, or fall back to its head."""
    try:
        text = voice_doc.read_text(encoding="utf-8")
    except OSError:
        return ""
    section = re.search(
        r"## Copy-Paste Prompt Block For Automation.*?```(?:text)?\s*(.*?)```",
        text,
        flags=re.S,
    )
    if section:
        return section.group(1).strip()
    return text[:6000].strip()


def default_channel_id() -> str:
    """Resolve the default channel id from env then the channels.json registry."""
    env = os.environ.get("CAROUSEL_CHANNEL", "").strip()
    if env:
        return env
    try:
        registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        configured = str(registry.get("default_channel", "")).strip()
        if configured:
            return configured
    except (OSError, json.JSONDecodeError):
        pass
    return FALLBACK_CHANNEL_ID


def available_channels() -> list[str]:
    """List channel ids that have a channel.json on disk."""
    if not CHANNELS_DIR.exists():
        return [FALLBACK_CHANNEL_ID]
    ids = sorted(
        p.name
        for p in CHANNELS_DIR.iterdir()
        if p.is_dir() and (p / "channel.json").exists()
    )
    return ids or [FALLBACK_CHANNEL_ID]


def load_channel(channel_id: str | None = None) -> Channel:
    """Load a channel by id, or the resolved default when ``channel_id`` is falsy."""
    resolved = (channel_id or "").strip() or default_channel_id()
    config_path = CHANNELS_DIR / resolved / "channel.json"
    if config_path.exists():
        data = json.loads(config_path.read_text(encoding="utf-8"))
        return _channel_from_config(resolved, data, config_path)
    if resolved == FALLBACK_CHANNEL_ID:
        return _legacy_vibecodersph()
    available = ", ".join(available_channels()) or "none"
    raise ValueError(
        f"Unknown channel '{resolved}'. Create channels/{resolved}/channel.json. "
        f"Available channels: {available}."
    )


def _resolve_asset(base_dir: Path, rel: str | None) -> Path | None:
    """Resolve a channel asset path relative to the channel dir, then the repo root."""
    if not rel:
        return None
    candidate = (base_dir / rel).resolve()
    if candidate.exists():
        return candidate
    alt = (ROOT / rel).resolve()
    return alt if alt.exists() else candidate


def _channel_from_config(channel_id: str, data: dict[str, Any], config_path: Path) -> Channel:
    brand = data.get("brand") or {}
    language = data.get("language") or {}
    voice_rel = data.get("voice_doc") or language.get("voice_doc") or "voice.md"
    voice_doc = _resolve_asset(config_path.parent, voice_rel)
    logo_path = _resolve_asset(config_path.parent, data.get("logo") or brand.get("logo"))
    return Channel(
        id=channel_id,
        account_name=str(data.get("account_name") or channel_id),
        brand_name=str(data.get("brand_name") or brand.get("brand_name") or channel_id),
        handle=str(data.get("handle") or brand.get("handle") or f"@{channel_id}"),
        language_name=str(language.get("name") or data.get("language_name") or "English"),
        audience=str(language.get("audience") or data.get("audience") or "an AI builder audience"),
        brand=brand,
        publishing=data.get("publishing") if isinstance(data.get("publishing"), dict) else {},
        voice_doc=voice_doc,
        logo_path=logo_path,
        config_path=config_path,
    )


def _legacy_vibecodersph() -> Channel:
    """Build the default channel from legacy root files when channels/ is absent."""
    brand: dict[str, Any] = {}
    brand_path = ROOT / "brand.json"
    if brand_path.exists():
        try:
            brand = json.loads(brand_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            brand = {}
    return Channel(
        id=FALLBACK_CHANNEL_ID,
        account_name="vibecodersph",
        brand_name="VibeCoders PH",
        handle="@vibecodersph",
        language_name="Taglish",
        audience="Filipino AI builder audience",
        brand=brand,
        publishing={},
        voice_doc=ROOT / "brand" / "VIBECODERS_IG_VOICE.md",
        logo_path=_resolve_asset(CHANNELS_DIR / FALLBACK_CHANNEL_ID, "logo.png"),
        config_path=brand_path if brand_path.exists() else None,
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Inspect resolved carousel channels.")
    parser.add_argument("channel", nargs="?", help="Channel id to inspect (default: active)")
    parser.add_argument("--list", action="store_true", help="List available channel ids")
    ns = parser.parse_args()
    if ns.list:
        for cid in available_channels():
            marker = " (default)" if cid == default_channel_id() else ""
            print(f"{cid}{marker}")
    else:
        ch = load_channel(ns.channel)
        print(json.dumps(
            {
                "id": ch.id,
                "account_name": ch.account_name,
                "brand_name": ch.brand_name,
                "handle": ch.handle,
                "language_name": ch.language_name,
                "audience": ch.audience,
                "voice_doc": ch.voice_doc_rel,
                "logo": ch.logo_doc_rel,
                "voice_prompt_chars": len(ch.voice_prompt),
            },
            indent=2,
            ensure_ascii=False,
        ))
