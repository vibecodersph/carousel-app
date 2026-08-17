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
            "Shot as real editorial magazine photography. Hasselblad X2D, 80mm f/2.8, "
            "natural window light or single key light, subtle film grain, desaturated "
            "color grade. Tactile materials, real textures, shallow depth of field. "
            "Reference: Wired magazine feature openers, Bloomberg Businessweek."
        ),
    },
    {
        "key": "cinematic_film",
        "label": "cinematic film still",
        "direction": (
            "Cinematic film still, ARRI Alexa 35, anamorphic lens, 2.39:1 letterbox "
            "within frame. Moody directional lighting, atmospheric haze, practical "
            "sources. Color grade: muted teal and amber or cold blue dawn. "
            "Reference: Deakins, Lubezki, Villeneuve. One frame from a movie."
        ),
    },
    {
        "key": "editorial_collage",
        "label": "editorial collage",
        "direction": (
            "Physical cut-paper editorial collage. Torn edges, scanned textures, "
            "photocopy grain, layered archival fragments, visible tape and glue marks. "
            "Surprising juxtapositions, one clear visual idea, not digital clutter. "
            "Reference: Bloomberg Businessweek collage covers, New York Magazine."
        ),
    },
    {
        "key": "product_showcase",
        "label": "high-end product showcase",
        "direction": (
            "Premium product launch hero image. Apple-keynote-level product photography. "
            "One designed object dominates the frame on a clean set. Precise materials, "
            "controlled studio lighting, macro detail, dramatic negative space. "
            "Reference: Nike product films, Nothing phone campaigns."
        ),
    },
    {
        "key": "surreal_poster",
        "label": "surreal conceptual poster",
        "direction": (
            "Surreal conceptual poster art. Rene Magritte logic meets modern editorial. "
            "One impossible idea made visually clean and striking. Minimal, strange, "
            "memorable. Clean composition, negative space, strong single focal point."
        ),
    },
    {
        "key": "oil_painting",
        "label": "oil painting / fine art",
        "direction": (
            "Oil on canvas painting. Visible brushwork, impasto highlights, slightly "
            "cracked varnish. Real painted texture, canvas weave visible. "
            "Reference: Edward Hopper (lonely interiors), Andrew Wyeth (rural drama), "
            "Jeremy Mann (moody urban nocturnes). Not a digital filter."
        ),
    },
    {
        "key": "anime_keyframe",
        "label": "anime production keyframe",
        "direction": (
            "Hand-drawn anime production keyframe. Painterly skies, volumetric light, "
            "cel-shaded foreground, soft anime palette, visible brushwork on backgrounds. "
            "Crisp clean lines on foreground, impressionistic backgrounds. "
            "Reference: Makoto Shinkai, Studio Ghibli backgrounds (Kazuo Oga)."
        ),
    },
    {
        "key": "comic_book",
        "label": "graphic novel / comic book",
        "direction": (
            "Graphic novel page art. Heavy ink blacks, bold brush strokes, limited "
            "3-color palette (black + one warm accent + one cool accent), visible "
            "halftone dots, crosshatching for midtones. "
            "Reference: Frank Miller Sin City, Mike Mignola Hellboy."
        ),
    },
    {
        "key": "claymation",
        "label": "stop-motion claymation",
        "direction": (
            "Stop-motion claymation. Visible fingerprints in clay, slight asymmetry, "
            "real studio lighting on a miniature set, handmade felt and wood props, "
            "shallow depth of field. Reference: Aardman, Laika, Wes Anderson Isle of Dogs."
        ),
    },
    {
        "key": "watercolor_storybook",
        "label": "watercolor storybook illustration",
        "direction": (
            "Watercolor illustration on textured cold-press paper. Soft wet-on-wet "
            "bleeds, visible paper grain, gentle pencil underdrawing showing through. "
            "Limited earth-tone palette with one bright accent. "
            "Reference: Quentin Blake, Beatrice Alemagna, Jon Klassen."
        ),
    },
    {
        "key": "retro_airbrush",
        "label": "1980s retro airbrush sci-fi",
        "direction": (
            "1980s airbrush illustration. Chrome highlights, soft airbrush gradients, "
            "slight CRT scanlines. Palette: magenta, cyan, deep purple, black, grid "
            "floor or starfield. Reference: Syd Mead, Hajime Sorayama, Drew Struzan."
        ),
    },
    {
        "key": "pixel_art",
        "label": "high-end pixel art",
        "direction": (
            "Hand-crafted pixel art illustration. Limited 32-color palette, deliberate "
            "dithering for gradients, crisp per-pixel placement, nearest-neighbor scaling. "
            "Reference: Octopath Traveler HD-2D, Celeste backgrounds, Eastward."
        ),
    },
    {
        "key": "low_poly_3d",
        "label": "low-poly stylized 3D",
        "direction": (
            "Low-polygon stylized 3D illustration. Flat-shaded triangular facets, no "
            "smoothing, limited palette of 8-12 flat colors, single directional light, "
            "soft ambient occlusion. Reference: Monument Valley, Alto's Odyssey."
        ),
    },
    {
        "key": "blueprint_diagram",
        "label": "blueprint / technical diagram",
        "direction": (
            "Technical blueprint poster. Patent-drawing style, exploded-view diagram, "
            "architecture plan. Fine linework, precise grids, vellum paper texture, "
            "cyanotype or black-and-white drafting table feel. Use shapes and systems."
        ),
    },
    {
        "key": "luxury_packshot",
        "label": "luxury packshot / perfume ad",
        "direction": (
            "Luxury packshot. Perfume-ad or watch-ad level product photography. One "
            "symbolic object on a premium material surface, dramatic reflection, "
            "immaculate lighting, sensual materiality. Dark and glossy, not flat."
        ),
    },
    {
        "key": "isometric_tech",
        "label": "isometric editorial illustration",
        "direction": (
            "Isometric 3/4 perspective illustration. Flat shading with subtle gradients, "
            "clean vector-inspired shapes, limited palette. The New York Times tech "
            "section or Wired feature opener style. Clean, intentional, not clip-art."
        ),
    },
    {
        "key": "toy_photography",
        "label": "toy photography / miniature scene",
        "direction": (
            "Practical miniature scene photographed with macro lens. Action figures, "
            "model buildings, tiny props, tabletop set, real shadows, visible dust, "
            "shallow depth of field. Slinkachu-style street miniatures. Handmade feel."
        ),
    },
    {
        "key": "hyperreal_multiverse",
        "label": "hyperreal cinematic multiverse",
        "direction": (
            "Hyperreal cinematic spectacle. Blend real photography with high-end 3D "
            "finish. Dimensional portals, volumetric light, impossible architecture, "
            "expressive motion, wonder, humor, bright discovery. Dramatic foreground "
            "and background storytelling, Easter-egg details, tactile materials. "
            "Marvel multiverse poster energy meets Disney/Pixar polish."
        ),
    },
]

# Color palette rotation: the accent color that appears as a restrained note
# in the generated image. VibeCoders PH brand uses burnt orange (#C0552E),
# but the cover photo can feature varied accent colors for visual freshness.
COLOR_ROTATION = [
    {"name": "burnt orange + warm cream + deep ink", "hex": "#C0552E"},
    {"name": "terracotta + sand + charcoal", "hex": "#D4724A"},
    {"name": "amber gold + off-white + espresso", "hex": "#C8922A"},
    {"name": "copper rust + bone + midnight blue", "hex": "#B87333"},
    {"name": "deep crimson + cream + slate", "hex": "#A03030"},
    {"name": "olive + warm ivory + dark brown", "hex": "#7A8B3E"},
    {"name": "teal + warm white + ink black", "hex": "#2A7A7A"},
    {"name": "mustard + eggshell + dark walnut", "hex": "#D4A020"},
    {"name": "rose gold + cream + charcoal", "hex": "#B87878"},
    {"name": "sienna + bone + espresso", "hex": "#A0522D"},
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


def _color_for_issue(issue_number: int) -> dict[str, str]:
    return COLOR_ROTATION[(max(issue_number, 1) - 1) % len(COLOR_ROTATION)]


def _de_risk_subject(subject: str, hero_line: str, hero_blurb: str) -> str:
    lower = subject.lower()
    risky_terms = [
        "handshake", "shaking hands", "holding hands", "high five", "fist bump",
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
    cover_color_palette: str = "",
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
    color = None
    if cover_color_palette:
        for palette in COLOR_ROTATION:
            if palette["name"] == cover_color_palette:
                color = palette
                break
    if not color:
        color = _color_for_issue(issue_number)

    hero = rows[0]
    hero_line = cover_safe_text(hero_cover_line or hero["headline"], max_words=14, max_chars=100)
    hero_blurb = cover_safe_text(hero["blurb"], max_words=26, max_chars=160)
    if not cover_subject:
        cover_subject = (
            "A fresh premium editorial visual metaphor for the hero story: "
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
        prompt = f"""Create a visually striking COVER PHOTO BACKGROUND, {ratio_label} aspect ratio.

This is the hero image for an Instagram carousel cover. The app will overlay all
branding, headlines, and UI elements later. Focus entirely on the visual scene.

SUBJECT (HYPERREALISTIC MONTAGE combining ALL stories):
{cover_subject}

ART STYLE: {style['label']}
{style['direction']}

COLOR PALETTE: {color['name']}
Use these as your primary colors. The accent color ({color['hex']}) should appear
as a restrained note in the composition. No other accent colors.

LIGHTING: Cinematic, sculpted, directional. Real shadows, real falloff, real
specular highlights. Embrace imperfection: dust, fingerprints, wrinkles, slight
motion blur, uneven wear. If skin: natural texture with pores and fine hair, not
plastic AI-smooth. If objects: real material qualities. Slight 35mm film grain.
Color grade: editorial, slightly desaturated, rich blacks, clean whites.

COMPOSITION: Rule of thirds. Asymmetrical framing. One clear focal point with
supporting context. The lower-left third should be calm enough for overlaid text.
Top corners should be readable for small UI chrome. Generous negative space.

HARD RULES:
- HYPERREALISTIC MONTAGE: combine multiple subjects into ONE unified scene.
  Every story mentioned in the subject must have a recognizable visual presence.
- NO text, letters, numbers, labels, captions, UI, logos, brand marks, watermarks.
- NO corporate logos (OpenAI, Microsoft, Anthropic, etc.). Use symbolic objects.
- NO generic glowing dashboards, cute robots, clipart icons, chart wallpaper.
- NO neon cyberpunk, no purple/pink/magenta/violet accents.
- NO device nameplates (tablet/laptop/phone with model name on screen).
- NO handshakes, high-fives, close-up hands, two people touching, visible fingers.

AVOID AI SLOP: Do not produce the default AI-generated look. No interchangeable
tech scene, no label-swap concept, no smooth plastic renders. Make it look like a
deliberate art-directed concept, not a generic prompt output. The image should
provoke curiosity, not recognition fatigue."""
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
