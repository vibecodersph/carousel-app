#!/usr/bin/env python3
"""Local VibeCoders PH Daily Drop magazine-cover generator.

This module keeps the Daily Drop cover workflow inside carousel-app so team
members do not need private Hermes scripts. It exposes generate_full_cover(), the
small API that build_daily_carousel.py expects.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = ROOT / "out" / "daily_carousel" / "images"
ISSUE_FILE = ROOT / "out" / "daily_carousel" / "issue_number.txt"
LOGO_PATH = ROOT / "assets" / "vibecodersph_logo.png"
IMAGE_SIZE = "1536x2304"
DEFAULT_MODEL = "gpt-image-2"

STYLE_ROTATION = [
    {
        "key": "editorial_photo",
        "label": "premium editorial photography",
        "direction": (
            "Real editorial magazine photography, tactile materials, controlled "
            "studio or practical light, subtle grain, clean hierarchy, premium "
            "print finish. Avoid generic neon dashboards and fake screens."
        ),
    },
    {
        "key": "editorial_collage",
        "label": "editorial collage",
        "direction": (
            "Layered editorial collage with torn paper, scanned textures, cutout "
            "objects, magazine feature energy, one clear visual idea, not clutter."
        ),
    },
    {
        "key": "product_showcase",
        "label": "high-end product showcase",
        "direction": (
            "A premium product launch style hero object, dramatic negative space, "
            "precise materials, clean set design, no generic tablet nameplate."
        ),
    },
    {
        "key": "surreal_poster",
        "label": "surreal conceptual poster",
        "direction": (
            "A clean surreal visual metaphor, strange but legible, one hero idea, "
            "magazine-poster composition, restrained palette."
        ),
    },
    {
        "key": "infographic_poster",
        "label": "editorial infographic poster",
        "direction": (
            "One bold editorial information-design idea, max three colors plus "
            "white, no dashboard wallpaper, no tiny fake labels, no icon clutter."
        ),
    },
]


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value.startswith(("'", '"')):
            quote = value[0]
            end = value.find(quote, 1)
            value = value[1:end] if end > 0 else value[1:]
        else:
            value = re.split(r"\s+#", value, 1)[0].strip()
        if key and key not in os.environ:
            os.environ[key] = value


def _load_openai_key() -> str | None:
    load_env_file(ROOT / ".env")
    return os.environ.get("OPENAI_API_KEY") or os.environ.get("VCPH_OPENAI_API_KEY")


def strip_em_dashes(text: Any) -> str:
    return str(text or "").replace("\u2014", ", ").replace("\u2013", "-")


def cover_safe_text(text: Any, *, max_words: int, max_chars: int) -> str:
    cleaned = re.sub(r"\s+", " ", strip_em_dashes(text)).strip().strip('"')
    words = cleaned.split()
    if len(words) > max_words:
        cleaned = " ".join(words[:max_words])
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars].rsplit(" ", 1)[0].rstrip(" ,;:.")
    return cleaned


def _story_rows(voice_stories: list[dict[str, Any]]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for item in voice_stories[:5]:
        if not isinstance(item, dict):
            continue
        rows.append({
            "headline": cover_safe_text(item.get("headline", ""), max_words=14, max_chars=90),
            "blurb": cover_safe_text(item.get("blurb", ""), max_words=26, max_chars=160),
        })
    return rows


def _next_issue_number() -> int:
    ISSUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    current = 0
    if ISSUE_FILE.exists():
        try:
            current = int(ISSUE_FILE.read_text(encoding="utf-8").strip() or "0")
        except ValueError:
            current = 0
    return current + 1


def _commit_issue_number(issue_number: int) -> None:
    ISSUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    ISSUE_FILE.write_text(str(int(issue_number)), encoding="utf-8")


def _style_for_issue(issue_number: int, override_style: str = "") -> dict[str, str]:
    if override_style:
        for style in STYLE_ROTATION:
            if style["key"] == override_style:
                return style
    forced = os.environ.get("VCPH_FORCE_COVER_STYLE", "").strip()
    if forced:
        for style in STYLE_ROTATION:
            if style["key"] == forced:
                return style
        print(f"  [cover] Unknown VCPH_FORCE_COVER_STYLE={forced}; using rotation")
    return STYLE_ROTATION[(max(issue_number, 1) - 1) % len(STYLE_ROTATION)]


def _de_risk_subject(subject: str, hero_line: str, hero_blurb: str) -> str:
    lower = subject.lower()
    risky_terms = [
        "handshake", "shaking hands", "holding hands", "high five", "fist bump",
        "two ceos", "two founders", "two executives", "sam altman", "elon musk",
        "satya nadella", "mark zuckerberg", "jensen huang", "sundar pichai",
    ]
    if not any(term in lower for term in risky_terms):
        return subject
    angle = cover_safe_text(hero_line or hero_blurb or "today's hero story", max_words=12, max_chars=90)
    print("  [cover guard] using symbolic still-life to avoid risky human anatomy")
    return (
        "A premium symbolic editorial still-life for " + angle + ": separate glass "
        "monoliths and paper artifacts on a clean studio table, split by one precise "
        "burnt-orange seam of light. No humans, no faces, no hands, no arms, no fingers."
    )


def _overlay_logo_and_save(raw_bytes: bytes, output_path: Path) -> None:
    try:
        from PIL import Image
    except ImportError:
        output_path.write_bytes(raw_bytes)
        print("  [cover] Pillow not installed; saved cover without logo overlay")
        return

    image = Image.open(BytesIO(raw_bytes)).convert("RGBA")
    if LOGO_PATH.exists():
        logo = Image.open(LOGO_PATH).convert("RGBA")
        target_w = max(180, int(image.width * 0.176))
        ratio = target_w / logo.width
        resampling_enum = getattr(Image, "Resampling", None)
        resampling = getattr(resampling_enum, "LANCZOS", 1)
        logo = logo.resize((target_w, int(logo.height * ratio)), resampling)
        pad = max(36, int(image.width * 0.035))
        image.alpha_composite(logo, (image.width - logo.width - pad, image.height - logo.height - pad))
    else:
        print(f"  [cover] Logo asset missing at {LOGO_PATH}")

    image.convert("RGB").save(output_path, "JPEG", quality=92)


def _fallback_output_path(rows: list[dict[str, str]], hero_line: str, cover_subject: str) -> Path:
    payload = json.dumps({"rows": rows, "hero_line": hero_line, "subject": cover_subject}, sort_keys=True)
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:10]
    return DEFAULT_OUTPUT_DIR / f"daily-drop-cover-{digest}.jpg"


def generate_full_cover(
    voice_stories: list[dict[str, Any]],
    hero_cover_line: str = "",
    cover_subject: str = "",
    cover_style: str = "",
    output_path: str | Path | None = None,
    skip_logo_overlay: bool = False,
    cover_size: str = "",
) -> Path | None:
    """Generate a complete VibeCoders PH Daily Drop magazine cover.

    Args:
        voice_stories: Five story rows with headline and blurb fields.
        hero_cover_line: Short cover line for story 1.
        cover_subject: Art direction for story 1.
        cover_style: Optional style key from STYLE_ROTATION.
        output_path: Optional exact JPEG path to write.
        skip_logo_overlay: If True, skip the bottom-right logo overlay
            (the carousel frame already supplies branding).
        cover_size: Optional gpt-image-2 size string, e.g. "1024x1280"
            for Instagram 4:5 carousel. Defaults to IMAGE_SIZE (1536x2304).
    """
    key = _load_openai_key()
    if not key:
        print("  [cover] No OPENAI_API_KEY or VCPH_OPENAI_API_KEY; skipping magazine cover")
        return None

    rows = _story_rows(voice_stories)
    if len(rows) < 5:
        print("  [cover] Magazine cover needs 5 stories; using carousel fallback")
        return None

    issue_number = _next_issue_number()
    issue_str = f"ISSUE {issue_number:03d}"
    today = datetime.now().strftime("%b %d, %Y").upper()
    style = _style_for_issue(issue_number, cover_style)

    hero = rows[0]
    hero_line = cover_safe_text(hero_cover_line or hero["headline"], max_words=14, max_chars=100)
    hero_blurb = cover_safe_text(hero["blurb"], max_words=26, max_chars=160)
    if not cover_subject:
        cover_subject = (
            "A fresh premium editorial-magazine visual metaphor for the hero story: "
            + hero_line
            + ". Make it specific and immediately recognizable, not generic AI wallpaper."
        )
    cover_subject = _de_risk_subject(strip_em_dashes(cover_subject), hero_line, hero_blurb)

    cover_list = []
    for index, story in enumerate(rows[1:5], start=2):
        cover_list.append(f'{index:02d}  "{story["headline"]}"')
    cover_block = "\n".join(cover_list)

    effective_size = cover_size or os.environ.get("DAILY_DROP_COVER_SIZE") or IMAGE_SIZE
    ratio_label = "portrait 4:5 (Instagram carousel)" if "1280" in effective_size else "portrait 2:3"

    if skip_logo_overlay:
        prompt = f"""Create a premium VibeCoders PH Daily Drop COVER PHOTO BACKGROUND, {ratio_label} aspect ratio, print-editorial quality.

This is only the photographic / illustrated background asset for an Instagram carousel.
The app will add all masthead, logo, headline, deck, story list, counters, and swipe text later.

Hero image subject:
{cover_subject}

Hero image style: {style['label']}
{style['direction']}

Hard rules:
- No text of any kind. No masthead, no letters, no numbers, no labels, no captions, no UI, no logos, no brand marks, no watermarks.
- Do not place OpenAI, Microsoft, Anthropic, school, publication, or fake product logos. Prefer symbolic objects.
- Keep the lower-left third and bottom edge visually calm enough for overlaid white headline text.
- Keep the top-left and top-right corners readable for small carousel chrome.
- Use warm cream, deep ink, and one restrained burnt-orange or terracotta accent so it fits the VibeCoders PH brand.
- Do not use purple, pink, violet, or magenta accents.
- Avoid generic glowing dashboards, cute robots, clipart icons, random charts, and screen wallpaper.
- Make one specific visual metaphor immediately readable from the hero story.

Overall mood: premium Filipino builder media brand, tactile, useful, sharp, modern, never corporate stock art."""
    else:
        prompt = f"""Create a premium VIBECODERSPH Daily Drop MAGAZINE COVER, {ratio_label} aspect ratio, print-editorial quality.

Hero image subject:
{cover_subject}

Hero image style: {style['label']}
{style['direction']}

Render all typography natively as part of the magazine cover, integrated with the light and texture. Do not draw flat pasted-on boxes.

Hard rules:
- Masthead must read exactly "VIBECODERSPH" as one continuous wordmark.
- Render "VIBECODERS" in warm cream #F0E4CD and "PH" in burnt orange #C0552E.
- No extra mastheads, no URLs, no barcodes, no watermarks, no fake UI text.
- No generic glowing dashboards, no cute robots, no clipart icons, no random labels.
- Keep the bottom-right corner visually quiet for a small logo overlay.
- Use clean sans-serif editorial hierarchy and high contrast readable text.

Masthead:
Top center, huge ultra-condensed all caps: "VIBECODERSPH".
Directly below it, centered small off-white text: "{issue_str} · {today}".

Kicker:
Left aligned above the hero line: "TODAY'S STORY" in burnt orange, small caps, with a short orange rule above it.

Hero cover line:
White bold sentence case, 2 to 3 balanced lines, left aligned:
"{hero_line}"

Deck:
Off-white regular sans-serif, max 2 lines:
"{hero_blurb}"

Lower section:
Centered orange label: "ALSO IN THIS DROP".
Below it, four numbered cover lines, left aligned, muted grey numbers and white headlines, with thin rules between items:
{cover_block}

Overall mood: lively, premium, useful, Filipino builder media brand. Sharp and modern, never corporate stock art."""

    output = Path(output_path) if output_path else _fallback_output_path(rows, hero_line, cover_subject)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        print(f"  [cover] using cached Daily Drop magazine cover -> {output.name}")
        return output

    try:
        from openai import OpenAI
    except ImportError:
        print("  [cover] openai package not installed; skipping magazine cover")
        return None

    image_model = DEFAULT_MODEL
    print(f"  [cover] Art style: {style['key']} - {style['label']}")
    print(f"  [cover] Image model: {image_model}")
    print(f"  [cover] Prompt length: {len(prompt)} chars")

    try:
        client = OpenAI(api_key=key)
        response = client.images.generate(
            model=image_model,
            prompt=prompt,
            size=effective_size,
            n=1,
        )
        if not response.data:
            print("  [cover] OpenAI response had no image data")
            return None
        b64 = response.data[0].b64_json
        if not b64:
            print("  [cover] OpenAI response had no b64_json")
            return None
        if skip_logo_overlay:
            output.write_bytes(base64.b64decode(b64))
            print("  [cover] saved Daily Drop cover without logo overlay (carousel mode)")
        else:
            _overlay_logo_and_save(base64.b64decode(b64), output)
        _commit_issue_number(issue_number)
        print(f"  [cover] generated Daily Drop magazine cover -> {output.name}")
        return output
    except Exception as exc:
        print(f"  [cover] Daily Drop magazine cover failed: {exc}")
        return None
