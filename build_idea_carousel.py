#!/usr/bin/env python3
"""Render one idea-engine carousel JSON object into carousel media.

Input is the batch JSON produced by ``./idea-engine``. Each carousel object is
already shaped as cover_page, item_1, item_2, ..., cta. This renderer keeps the
normal animated X-carousel title cover and uses concise item pages with generated art.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from build_article_carousel import clamp_words, normalize_space
from build_video_slide import run
from build_x_carousel import (
    DEFAULT_ACCOUNT_NAME,
    SLIDE_H,
    SLIDE_W,
    cover_poster_path,
    dot_markup,
    openai_title_image_model,
    openai_title_image_size,
    render_animated_title_slide,
    render_cta_slide,
    render_html_slide,
    shared_css,
    string_value,
)
from channel import load_channel
from generate_cover import generate_openai, openai_api_key

ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT = ROOT / "out" / "idea-engine" / "gemini_ph_builder_carousels.json"
DEFAULT_OUT = ROOT / "out" / "idea-engine" / "idea_carousel_render"
DEFAULT_IDEA_ITEM_IMAGE_SIZE = "2048x1152"
DEFAULT_COVER_STYLE = "default"
KINETIC_FLY_COVER_STYLE = "kinetic-fly"
KINETIC_FLY_CYCLE_SECONDS = 5.2
KINETIC_FLY_FPS = 30


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def carousel_slug(carousel: dict[str, Any], index: int) -> str:
    raw = string_value(carousel.get("id")) or f"carousel-{index + 1}"
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-._")
    return slug[:72] or f"carousel-{index + 1}"


def selected_carousel(payload: dict[str, Any], index: int) -> dict[str, Any]:
    carousels = payload.get("carousels")
    if not isinstance(carousels, list) or not carousels:
        raise SystemExit("input JSON has no carousels[]")
    if index < 0 or index >= len(carousels):
        raise SystemExit(f"--index must be between 0 and {len(carousels) - 1}")
    carousel = carousels[index]
    if not isinstance(carousel, dict):
        raise SystemExit(f"carousels[{index}] is not an object")
    return carousel


def item_keys(carousel: dict[str, Any]) -> list[str]:
    order = carousel.get("page_order")
    if not isinstance(order, list):
        return sorted(
            key
            for key, value in carousel.items()
            if re.fullmatch(r"item_\d+", key) and isinstance(value, dict)
        )
    return [
        key
        for key in order
        if isinstance(key, str)
        and re.fullmatch(r"item_\d+", key)
        and isinstance(carousel.get(key), dict)
    ]


def first_source_url(page: dict[str, Any]) -> str:
    sources = page.get("sources")
    if not isinstance(sources, list):
        return ""
    for source in sources:
        if isinstance(source, dict) and string_value(source.get("url")):
            return string_value(source.get("url"))
    return ""


def image_prompt(base: str, topic: str) -> str:
    base = normalize_space(base)
    topic = normalize_space(topic)
    if not base:
        base = f"Editorial illustration for {topic}"
    return normalize_space(
        f"{base}. 16:9 horizontal editorial artwork for an Instagram carousel. "
        "Cream paper, dark ink, terracotta accent, warm print texture. "
        "No text, no logos, no UI, no charts, no readable marks, no neon, no rainbow colors."
    )


def cover_image_prompt(base: str, topic: str) -> str:
    base = normalize_space(base)
    topic = normalize_space(topic)
    if not base:
        base = f"Editorial cover illustration for {topic}"
    return normalize_space(
        f"{base}. 16:9 horizontal landscape editorial artwork for the top of an Instagram carousel cover. "
        "Create a complete landscape illustration with clear subject detail in the upper portion; "
        "the rendered cover will place animated title text below the focal art. "
        "Keep the lower portion quiet, warm, and uncluttered so it can softly vanish into the brand paper color; no hard edge, no busy details, and no important objects there. "
        "Cream paper, dark ink, terracotta accent, warm print texture. "
        "No text, no logos, no UI, no charts, no readable marks, no neon, no rainbow colors."
    )


def openai_item_image_size() -> str:
    return os.environ.get("OPENAI_ITEM_IMAGE_SIZE") or DEFAULT_IDEA_ITEM_IMAGE_SIZE


def generated_image_path(out_dir: Path, topic: str, prompt: str) -> Path:
    digest = hashlib.sha1(f"{topic}\n{prompt}".encode("utf-8")).hexdigest()[:10]
    return out_dir / "generated_assets" / f"{digest}.png"


def cover_image_composition(path: Path | None) -> str:
    return "top_art" if path else ""


def load_reusable_assets(path: Path | None) -> dict[str, Any]:
    if not path:
        return {"cover": None, "cover_composition": "", "items": {}}
    manifest = read_json(path)
    assets: dict[str, Any] = {"cover": None, "cover_composition": "", "items": {}}
    slides = manifest.get("slides")
    if not isinstance(slides, list):
        return assets
    for slide in slides:
        if not isinstance(slide, dict):
            continue
        raw_image_path = string_value(slide.get("image_path"))
        if not raw_image_path:
            continue
        image_path = Path(raw_image_path)
        if not image_path.exists():
            continue
        if slide.get("type") == "title" and assets["cover"] is None:
            assets["cover"] = image_path
            assets["cover_composition"] = (
                string_value(slide.get("image_composition")).replace("spot_illustration", "top_art")
                or cover_image_composition(image_path)
            )
        item_name = string_value(slide.get("item_name"))
        if item_name:
            assets["items"][item_name.lower()] = image_path
    return assets


def maybe_generate_image(
    out_dir: Path,
    *,
    topic: str,
    prompt: str,
    generate_images: bool,
    size: str | None = None,
) -> Path | None:
    if not generate_images:
        return None
    if not openai_api_key():
        print("[openai] OPENAI_API_KEY not set; using local visual fallback")
        return None
    path = generated_image_path(out_dir, topic, prompt)
    if path.exists():
        print(f"[openai] using cached GPT Image asset -> {path}")
        return path
    try:
        generate_openai(
            prompt,
            path,
            model=openai_title_image_model(),
            size=size or openai_title_image_size(),
        )
    except (SystemExit, Exception) as exc:
        print(f"[openai] image generation failed for {topic}; using fallback ({exc})")
        return None
    return path


def asset_uri(path: Path | None) -> str:
    if not path:
        return ""
    return path.resolve().as_uri()


def normalize_cover_style(value: str | None) -> str:
    style = normalize_space(value).lower().replace("_", "-")
    if not style or style in {"default", "usual", "animated", "text-motion", "text-motion-lines"}:
        return DEFAULT_COVER_STYLE
    if style in {"fly", "fly-cover", "kinetic", "kinetic-fly"}:
        return KINETIC_FLY_COVER_STYLE
    raise SystemExit(f"unknown cover style: {value}")


def contains_japanese(value: str) -> bool:
    return bool(re.search(r"[\u3040-\u30ff\u3400-\u9fff]", value or ""))


def bracket_plain_text(value: str) -> str:
    return normalize_space((value or "").replace("[", "").replace("]", ""))


def kinetic_fly_raw_lines(cover: dict[str, Any]) -> list[list[dict[str, Any]]]:
    raw = cover.get("kinetic_fly_lines") or cover.get("fly_lines")
    if not isinstance(raw, list):
        return []
    lines: list[list[dict[str, Any]]] = []
    for raw_line in raw:
        if not isinstance(raw_line, list):
            continue
        line: list[dict[str, Any]] = []
        for item in raw_line:
            if isinstance(item, str):
                text = item
                size = 0.68
                accent = False
            elif isinstance(item, dict):
                text = string_value(item.get("text") or item.get("t"))
                size = float(item.get("size") or item.get("s") or 0.68)
                accent = bool(item.get("accent"))
            else:
                continue
            if text:
                line.append({"text": text, "size": size, "accent": accent})
        if line:
            lines.append(line)
    return lines[:3]


def kinetic_fly_tokens(headline: str, *, japanese: bool) -> list[str]:
    headline = bracket_plain_text(headline)
    if not headline:
        return []
    if japanese and " " not in headline:
        tokens = re.findall(r"[A-Za-z0-9.+#/-]+|[\u3040-\u30ff\u3400-\u9fffー々]{1,6}", headline)
        return [token for token in tokens if token.strip()]
    return [token for token in headline.split() if token.strip()]


def kinetic_word_size(token: str, *, accent: bool) -> float:
    length = len(token)
    if contains_japanese(token):
        if length <= 2:
            return 0.96
        if length <= 4:
            return 0.76
        return 0.58 if accent else 0.54
    if length <= 2:
        return 0.64
    if length <= 5:
        return 0.86 if accent else 0.74
    return 0.82 if accent else 0.58


def kinetic_fly_lines(cover: dict[str, Any], channel_language: str) -> list[list[dict[str, Any]]]:
    explicit = kinetic_fly_raw_lines(cover)
    if explicit:
        return explicit

    headline = string_value(cover.get("headline"))
    japanese = channel_language.lower().startswith("japanese") or contains_japanese(headline)
    tokens = kinetic_fly_tokens(headline, japanese=japanese)
    if not tokens:
        return [[{"text": "AI", "size": 0.86, "accent": True}], [{"text": "Brief", "size": 0.88, "accent": False}]]

    if len(tokens) <= 3:
        grouped = [[token] for token in tokens]
    elif len(tokens) == 4:
        grouped = [tokens[:2], [tokens[2]], [tokens[3]]]
    else:
        grouped = [tokens[:3], tokens[3:4], tokens[4:]]

    lines: list[list[dict[str, Any]]] = []
    for line_index, group in enumerate(grouped[:3]):
        line: list[dict[str, Any]] = []
        for token_index, token in enumerate(group):
            accent = line_index == 0 and token_index == 0 or line_index == len(grouped[:3]) - 1
            line.append({"text": token, "size": kinetic_word_size(token, accent=accent), "accent": accent})
        if line:
            lines.append(line)
    return lines


def kinetic_fly_items(carousel: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for key in item_keys(carousel):
        page = carousel.get(key)
        if isinstance(page, dict):
            name = string_value(page.get("item_name"))
            if name:
                names.append(name)
    while len(names) < 3:
        names.append("Compare first")
    return names[:3]


def kinetic_fly_subline(cover: dict[str, Any], *, japanese: bool) -> str:
    explicit = string_value(cover.get("kinetic_subline") or cover.get("subline"))
    if explicit:
        return explicit
    subheadline = string_value(cover.get("subheadline"))
    if subheadline and contains_japanese(subheadline):
        return clamp_words(subheadline, 18, ellipsis=False)
    if japanese:
        return "いつもの選択に戻る前に、候補を一度だけ並べて見る。"
    return "Compare the route before defaulting to the obvious choice."


def kinetic_fly_handle(channel: Any) -> str:
    handle = string_value(getattr(channel, "handle", "")).lstrip("@")
    return handle.lower() if handle else string_value(getattr(channel, "account_name", "")).lower()


def kinetic_fly_headline_markup(lines: list[list[dict[str, Any]]]) -> str:
    in_vectors = [
        ("-128vw", "8vh", "-18deg", 1.45),
        ("116vw", "-14vh", "14deg", 0.48),
        ("-96vw", "-42vh", "22deg", 1.75),
        ("64vw", "36vh", "-20deg", 0.58),
        ("108vw", "18vh", "10deg", 1.58),
        ("0vw", "72vh", "-8deg", 2.05),
    ]
    out_vectors = [
        ("78vw", "-34vh", "18deg"),
        ("-92vw", "22vh", "-16deg"),
        ("76vw", "42vh", "24deg"),
        ("-62vw", "-34vh", "-22deg"),
        ("94vw", "-12vh", "12deg"),
        ("0vw", "-74vh", "-10deg"),
    ]
    rows: list[str] = []
    word_index = 0
    for line in lines:
        words: list[str] = []
        for word in line:
            in_x, in_y, in_rotate, in_scale = in_vectors[word_index % len(in_vectors)]
            out_x, out_y, out_rotate = out_vectors[word_index % len(out_vectors)]
            delay = word_index * 78
            classes = "word is-accent" if word.get("accent") else "word"
            style = (
                f"--word-size:{float(word.get('size') or 0.68):.3f};"
                f"--cycle:{KINETIC_FLY_CYCLE_SECONDS:.2f}s;"
                f"--in-x:{in_x};--in-y:{in_y};--in-rotate:{in_rotate};--in-scale:{in_scale};"
                f"--out-x:{out_x};--out-y:{out_y};--out-rotate:{out_rotate};"
            )
            words.append(
                f'<span class="{classes}" data-kinetic data-delay-ms="{delay}" '
                f'style="{style}">{html.escape(string_value(word.get("text")))}</span>'
            )
            word_index += 1
        rows.append(f'<div class="line">{"".join(words)}</div>')
    return "\n".join(rows)


def kinetic_fly_cover_css() -> str:
    return f"""
{shared_css()}
body {{ background: var(--bg); }}
.slide {{
  width: {SLIDE_W}px;
  height: {SLIDE_H}px;
  overflow: hidden;
  isolation: isolate;
  background:
    linear-gradient(180deg, var(--bg-top) 0%, var(--bg) 44%, #efe5d3 100%);
}}
.slide::before,
.slide::after {{
  content: "";
  position: absolute;
  inset: 0;
  pointer-events: none;
}}
.slide::before {{
  z-index: 1;
  background:
    linear-gradient(90deg, rgba(var(--primary-rgb), 0.12) 0 1px, transparent 1px 100px),
    linear-gradient(0deg, rgba(var(--fg-rgb), 0.08) 0 1px, transparent 1px 100px);
  opacity: 0.65;
  animation: gridDrift 10s linear infinite;
}}
.slide::after {{
  z-index: 30;
  border: 18px solid rgba(var(--fg-rgb), 0.08);
  background:
    linear-gradient(180deg, transparent 0%, transparent 58%, rgba(var(--bg-rgb), 0.44) 76%, rgba(var(--bg-rgb), 0.88) 100%),
    radial-gradient(circle at 18% 22%, rgba(var(--primary-rgb), 0.18), transparent 300px);
  mix-blend-mode: multiply;
}}
.brand-bar,
.head,
.option-row,
.subline,
.fly-footer {{
  position: absolute;
  z-index: 40;
}}
.brand-bar {{
  top: 78px;
  left: 92px;
  right: 92px;
  display: flex;
  align-items: center;
  gap: 22px;
}}
.brand-logo,
.brand-fallback {{
  width: 112px;
  height: 112px;
  display: grid;
  place-items: center;
  border: 3px solid var(--primary);
  border-radius: 50%;
  background: var(--bg);
  object-fit: cover;
  overflow: hidden;
  animation: logoSnap {KINETIC_FLY_CYCLE_SECONDS:.2f}s cubic-bezier(0.16, 1, 0.3, 1) infinite;
}}
.brand-fallback {{
  color: var(--primary);
  font-size: 34px;
  font-weight: 900;
}}
.brand-name,
.brand-handle {{
  display: block;
}}
.brand-name {{
  color: var(--fg);
  font-size: 32px;
  font-weight: 900;
  line-height: 1;
  text-transform: uppercase;
}}
.brand-handle {{
  margin-top: 10px;
  color: var(--muted);
  font-size: 24px;
  font-weight: 800;
  line-height: 1;
  text-transform: none;
}}
.route-map {{
  position: absolute;
  inset: 0;
  z-index: 12;
  pointer-events: none;
}}
.route {{
  position: absolute;
  left: 90px;
  width: 1040px;
  height: 4px;
  transform-origin: 0 50%;
  background: linear-gradient(90deg, transparent, rgba(var(--primary-rgb), 0.8), rgba(var(--fg-rgb), 0.18), transparent);
  clip-path: inset(0 100% 0 0);
  animation: routeDraw {KINETIC_FLY_CYCLE_SECONDS:.2f}s linear infinite;
}}
.route-one {{ top: 404px; transform: rotate(-18deg); }}
.route-two {{ top: 626px; transform: rotate(4deg); }}
.route-three {{ top: 848px; transform: rotate(22deg); }}
.node {{
  position: absolute;
  width: 104px;
  height: 104px;
  border: 3px solid var(--fg);
  background: var(--bg);
  box-shadow: 16px 16px 0 rgba(var(--primary-rgb), 0.18);
  animation: nodePop {KINETIC_FLY_CYCLE_SECONDS:.2f}s cubic-bezier(0.16, 1, 0.3, 1) infinite;
}}
.node-one {{ left: 104px; top: 492px; }}
.node-two {{ right: 156px; top: 376px; border-color: var(--primary); }}
.node-three {{ right: 110px; top: 664px; }}
.node-four {{ right: 192px; top: 920px; border-color: var(--primary); }}
.head {{
  inset: 0;
  display: flex;
  flex-direction: column;
  justify-content: center;
  padding: 78px 88px 88px;
}}
.line {{
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 0.16em;
  min-height: 210px;
}}
.word {{
  display: inline-block;
  color: var(--fg);
  font-size: calc(var(--word-size) * 255px);
  font-weight: 900;
  line-height: 0.88;
  text-transform: uppercase;
  transform-origin: center;
  will-change: transform, filter, opacity;
  animation: flyWord var(--cycle) 0s infinite;
  text-shadow: 0 12px 0 rgba(var(--primary-rgb), 0.12);
}}
.word.is-accent {{ color: var(--primary); }}
.option-row {{
  left: 92px;
  right: 92px;
  bottom: 236px;
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 16px;
}}
.option-row span {{
  min-height: 66px;
  display: grid;
  place-items: center;
  border: 2px solid var(--fg);
  background: rgba(var(--bg-rgb), 0.72);
  color: var(--fg);
  font-size: 24px;
  font-weight: 800;
  text-align: center;
  text-transform: uppercase;
  animation: chipLift {KINETIC_FLY_CYCLE_SECONDS:.2f}s cubic-bezier(0.16, 1, 0.3, 1) infinite;
}}
.option-row span:nth-child(2) {{ border-color: var(--primary); color: var(--primary); }}
.subline {{
  left: 92px;
  right: 176px;
  bottom: 148px;
  margin: 0;
  color: var(--ink-soft);
  font-size: 32px;
  font-weight: 800;
  line-height: 1.12;
}}
.fly-footer {{
  left: 92px;
  right: 92px;
  bottom: 78px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  color: var(--muted);
  font-size: 23px;
  font-weight: 900;
  text-transform: uppercase;
}}
.progress {{
  display: flex;
  align-items: center;
  gap: 12px;
}}
.progress i {{
  display: block;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: var(--rule);
}}
.progress i:first-child {{
  width: 78px;
  border-radius: 999px;
  background: var(--primary);
}}
@keyframes flyWord {{
  0% {{
    transform: translate(var(--in-x), var(--in-y)) rotate(var(--in-rotate)) scale(var(--in-scale));
    filter: blur(16px);
    opacity: 0;
    animation-timing-function: cubic-bezier(0.16, 1.38, 0.3, 1);
  }}
  16% {{
    transform: translate(0, 0) rotate(0deg) scale(1);
    filter: blur(0);
    opacity: 1;
    animation-timing-function: linear;
  }}
  64% {{
    transform: translate(0, 0) rotate(0deg) scale(1);
    filter: blur(0);
    opacity: 1;
    animation-timing-function: cubic-bezier(0.7, 0, 0.9, 0.25);
  }}
  80% {{
    transform: translate(var(--out-x), var(--out-y)) rotate(var(--out-rotate)) scale(1.46);
    filter: blur(18px);
    opacity: 0;
  }}
  100% {{ opacity: 0; }}
}}
@keyframes routeDraw {{
  0%, 18% {{ opacity: 0; clip-path: inset(0 100% 0 0); }}
  30%, 58% {{ opacity: 1; clip-path: inset(0 0 0 0); }}
  78%, 100% {{ opacity: 0; clip-path: inset(0 0 0 100%); }}
}}
@keyframes nodePop {{
  0%, 10%, 88%, 100% {{ transform: translateY(20px) rotate(8deg) scale(0.78); opacity: 0; }}
  24%, 66% {{ transform: translateY(0) rotate(0deg) scale(1); opacity: 0.72; }}
}}
@keyframes chipLift {{
  0%, 14%, 84%, 100% {{ transform: translateY(28px); opacity: 0; }}
  28%, 68% {{ transform: translateY(0); opacity: 1; }}
}}
@keyframes logoSnap {{
  0%, 100% {{ transform: translateY(0) rotate(0deg) scale(1); }}
  18% {{ transform: translateY(-6px) rotate(2deg) scale(1.06); }}
  28%, 70% {{ transform: translateY(0) rotate(0deg) scale(1); }}
}}
@keyframes gridDrift {{
  0% {{ transform: translate(0, 0); }}
  100% {{ transform: translate(100px, 100px); }}
}}
"""


def kinetic_fly_progress_script() -> str:
    return f"""
<script>
(() => {{
  const cycleMs = {KINETIC_FLY_CYCLE_SECONDS * 1000:.0f};
  window.__setKineticFlyProgress = (progress) => {{
    const clamped = Math.max(0, Math.min(1, Number(progress) || 0));
    const currentMs = clamped * cycleMs;
    document.querySelectorAll("[data-kinetic]").forEach((element) => {{
      const delayMs = Number(element.dataset.delayMs || 0);
      element.style.animationDelay = `${{(delayMs - currentMs) / 1000}}s`;
      element.style.animationPlayState = "paused";
    }});
  }};
  window.__kineticFlyCoverReady = true;
}})();
</script>
"""


def kinetic_fly_cover_html(carousel: dict[str, Any], *, count: int, channel: Any) -> str:
    cover = carousel.get("cover_page")
    cover = cover if isinstance(cover, dict) else {}
    japanese = channel.language_name.lower().startswith("japanese")
    lines = kinetic_fly_lines(cover, channel.language_name)
    headline_text = " ".join(string_value(word.get("text")) for line in lines for word in line)
    items = kinetic_fly_items(carousel)
    logo_src = asset_uri(getattr(channel, "logo_path", None))
    if logo_src:
        logo_markup = f'<img class="brand-logo" src="{html.escape(logo_src, quote=True)}" alt="{html.escape(channel.brand_name)}" data-kinetic data-delay-ms="0">'
    else:
        fallback = html.escape((string_value(channel.account_name) or "AI")[:2].upper())
        logo_markup = f'<span class="brand-fallback" data-kinetic data-delay-ms="0">{fallback}</span>'
    swipe = "スワイプして比較" if japanese else "Swipe for the comparison"
    return f"""<!doctype html>
<html lang="{'ja' if japanese else 'en'}"><head><meta charset="utf-8"><style>
{kinetic_fly_cover_css()}
</style></head>
<body>
<div class="slide" data-cover-style="kinetic-fly" aria-label="{html.escape(headline_text, quote=True)}">
  <header class="brand-bar">
    {logo_markup}
    <div>
      <span class="brand-name">{html.escape(string_value(channel.account_name or channel.brand_name))}</span>
      <span class="brand-handle">{html.escape(kinetic_fly_handle(channel))}</span>
    </div>
  </header>
  <div class="route-map" aria-hidden="true">
    <span class="route route-one" data-kinetic data-delay-ms="0"></span>
    <span class="route route-two" data-kinetic data-delay-ms="180"></span>
    <span class="route route-three" data-kinetic data-delay-ms="360"></span>
    <span class="node node-one" data-kinetic data-delay-ms="0"></span>
    <span class="node node-two" data-kinetic data-delay-ms="120"></span>
    <span class="node node-three" data-kinetic data-delay-ms="240"></span>
    <span class="node node-four" data-kinetic data-delay-ms="360"></span>
  </div>
  <section class="head" aria-label="{html.escape(headline_text, quote=True)}">
    {kinetic_fly_headline_markup(lines)}
  </section>
  <div class="option-row">
    <span data-kinetic data-delay-ms="0">{html.escape(items[0])}</span>
    <span data-kinetic data-delay-ms="80">{html.escape(items[1])}</span>
    <span data-kinetic data-delay-ms="160">{html.escape(items[2])}</span>
  </div>
  <p class="subline">{html.escape(kinetic_fly_subline(cover, japanese=japanese))}</p>
  <footer class="fly-footer">
    <span>{html.escape(swipe)}</span>
    <span class="progress" aria-hidden="true"><i></i><i></i><i></i></span>
  </footer>
</div>
{kinetic_fly_progress_script()}
</body></html>"""


def render_kinetic_fly_cover(
    carousel: dict[str, Any],
    out_path: Path,
    *,
    count: int,
    channel: Any,
    duration_seconds: float = KINETIC_FLY_CYCLE_SECONDS,
    fps: int = KINETIC_FLY_FPS,
) -> Path:
    try:
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError as exc:
        raise SystemExit("playwright is required to render kinetic fly covers") from exc
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SystemExit("ffmpeg is required to render kinetic fly cover MP4s")

    duration_seconds = max(0.1, float(duration_seconds))
    fps = max(1, int(fps))
    frame_count = max(2, int(round(duration_seconds * fps)))
    poster_index = min(frame_count - 1, max(0, int(round(frame_count * 0.36))))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    poster_path = cover_poster_path(out_path)
    html_path = out_path.with_suffix(".html")
    html_path.write_text(kinetic_fly_cover_html(carousel, count=count, channel=channel), encoding="utf-8")

    print(f"[cover] rendering kinetic fly cover -> {out_path}")
    try:
        with tempfile.TemporaryDirectory(prefix=f"{out_path.stem}_fly_frames_") as tmp:
            frames_dir = Path(tmp)
            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page(viewport={"width": SLIDE_W, "height": SLIDE_H}, device_scale_factor=1)
                page.goto(html_path.resolve().as_uri())
                page.wait_for_load_state("networkidle")
                page.evaluate(
                    "() => (document.fonts && document.fonts.ready ? "
                    "document.fonts.ready.then(() => true) : true)"
                )
                page.wait_for_function("() => window.__kineticFlyCoverReady === true")
                slide = page.locator(".slide")
                for frame_index in range(frame_count):
                    progress = frame_index / (frame_count - 1)
                    frame_path = frames_dir / f"frame_{frame_index:04d}.png"
                    page.evaluate("(progress) => window.__setKineticFlyProgress(progress)", progress)
                    slide.screenshot(path=str(frame_path))
                browser.close()

            shutil.copyfile(frames_dir / f"frame_{poster_index:04d}.png", poster_path)
            run(
                [
                    ffmpeg,
                    "-y",
                    "-framerate",
                    str(fps),
                    "-start_number",
                    "0",
                    "-i",
                    str(frames_dir / "frame_%04d.png"),
                    "-an",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-movflags",
                    "+faststart",
                    str(out_path),
                ]
            )
    except SystemExit:
        raise
    except Exception as exc:
        raise SystemExit(
            "could not render the kinetic fly cover. If this is a fresh setup, run "
            "`uv run python -m playwright install chromium` once."
        ) from exc
    return out_path


def bracket_markup(text: str) -> str:
    escaped = html.escape(text or "")
    return escaped.replace("[", '<span class="accent">').replace("]", "</span>")


def concise_body(page: dict[str, Any]) -> str:
    body = normalize_space(page.get("body"))
    if body:
        first_sentence = re.split(r"(?<=[.!?。])\s+", body, maxsplit=1)[0]
        return clamp_words(first_sentence or body, 18, ellipsis=False)
    best_for = normalize_space(page.get("best_for"))
    watch_out = normalize_space(page.get("watch_out"))
    return clamp_words(" ".join(part for part in [best_for, watch_out] if part), 18, ellipsis=False)


def concise_takeaway(page: dict[str, Any]) -> str:
    best_for = normalize_space(page.get("best_for"))
    if best_for:
        return clamp_words(best_for, 14, ellipsis=False)
    takeaway = normalize_space(page.get("takeaway"))
    if takeaway:
        return clamp_words(takeaway, 12, ellipsis=False)
    watch_out = normalize_space(page.get("watch_out"))
    return clamp_words(watch_out, 12, ellipsis=False)


def title_context(
    carousel: dict[str, Any],
    out_dir: Path,
    *,
    generate_images: bool,
    reusable_image: Path | None = None,
    reusable_image_composition: str = "",
) -> tuple[dict[str, Any], dict[str, str], Path | None]:
    channel = load_channel()
    cover = carousel.get("cover_page")
    cover = cover if isinstance(cover, dict) else {}
    headline = string_value(cover.get("headline"))
    prompt = cover_image_prompt(string_value(cover.get("image_prompt")), headline)
    if reusable_image:
        cover_image = reusable_image
        print(f"[asset] reusing title image -> {cover_image}")
    else:
        cover_image = maybe_generate_image(
            out_dir,
            topic=headline or string_value(carousel.get("id")) or "idea carousel cover",
            prompt=prompt,
            generate_images=generate_images,
            size=openai_title_image_size(),
        )
    cover_copy = {
        "kicker": string_value(cover.get("kicker")) or "CURATION",
        "headline": headline,
        "accent_words": [],
        "swipe_line": "スワイプで続きへ" if channel.language_name.lower().startswith("japanese") else "Swipe for the stack",
    }
    context = {
        "topic": headline,
        "topic_image_path": cover_image,
        "image_provider": "openai" if cover_image else "",
        "image_composition": string_value(reusable_image_composition) or cover_image_composition(cover_image),
        "cover_copy": cover_copy,
        "cover_animation": "text-motion-lines",
        "companies": [],
        "ceos": [],
        "source_people": [],
        "topic_entity": None,
        "post_explanations": [],
        "instagram_caption": string_value(carousel.get("instagram_caption")),
        "brand_voice_doc": channel.voice_doc_rel,
        "google_enabled": False,
        "provider": "idea_engine",
        "openai_image_model": openai_title_image_model() if openai_api_key() else "",
        "openai_image_size": openai_title_image_size() if openai_api_key() else "",
        "generated_image_prompt": prompt,
    }
    return context, cover_copy, cover_image


def item_slide_css() -> str:
    return f"""
{shared_css()}
body {{ background: #777; }}
.slide {{ width: {SLIDE_W}px; height: {SLIDE_H}px; }}
.handle {{
  position: absolute;
  top: 58px;
  left: 64px;
  font-size: 24px;
  font-weight: 820;
  letter-spacing: .08em;
  text-transform: uppercase;
  color: rgba(20, 18, 14, .72);
  z-index: 5;
}}
.count {{
  position: absolute;
  top: 58px;
  right: 64px;
  font-size: 23px;
  font-weight: 820;
  color: rgba(20, 18, 14, .43);
  z-index: 5;
}}
.item-visual {{
  position: absolute;
  left: 0;
  right: 0;
  top: 0;
  height: 565px;
  overflow: hidden;
  background:
    linear-gradient(135deg, rgba(var(--primary-rgb), .72), rgba(22, 20, 15, .94)),
    repeating-linear-gradient(90deg, rgba(var(--bg-rgb), .12) 0 2px, transparent 2px 18px);
  -webkit-mask-image: linear-gradient(180deg, #000 0%, #000 76%, rgba(0, 0, 0, .55) 90%, transparent 100%);
  mask-image: linear-gradient(180deg, #000 0%, #000 76%, rgba(0, 0, 0, .55) 90%, transparent 100%);
}}
.item-visual.has-image {{
  background-position: center;
  background-size: cover;
}}
.item-visual::after {{
  content: '';
  position: absolute;
  inset: 0;
  background:
    linear-gradient(180deg, rgba(var(--bg-rgb), 0) 0%, rgba(var(--bg-rgb), .1) 66%, rgba(var(--bg-rgb), .45) 100%);
}}
.item-cluster {{
  position: absolute;
  left: 56px;
  right: 56px;
  top: 610px;
  bottom: 124px;
  display: flex;
  flex-direction: column;
  justify-content: flex-start;
}}
.item-rule {{
  display: flex;
  align-items: center;
  gap: 22px;
  margin-bottom: 28px;
  color: var(--primary);
}}
.item-rule::before,
.item-rule::after {{
  content: '';
  flex: 1;
  height: 2px;
  background: var(--rule);
}}
.item-rule span {{
  font-size: 23px;
  font-weight: 780;
  letter-spacing: .08em;
  line-height: 1;
  text-transform: uppercase;
  color: var(--primary);
}}
.item-title {{
  font-size: 74px;
  line-height: .98;
  letter-spacing: 0;
  font-weight: 900;
  color: var(--fg);
  text-wrap: balance;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}}
.item-title .accent {{
  color: var(--primary);
}}
.item-body {{
  margin-top: 34px;
  max-width: 880px;
  font-size: 34px;
  line-height: 1.18;
  font-weight: 650;
  color: var(--ink-soft);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}}
.takeaway {{
  margin-top: 40px;
  max-width: 880px;
  padding: 24px 30px;
  border-left: 8px solid var(--primary);
  background: rgba(255, 255, 255, .36);
  font-size: 31px;
  line-height: 1.12;
  font-weight: 850;
  color: var(--fg);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}}
.source {{
  position: absolute;
  left: 58px;
  right: 58px;
  bottom: 92px;
  color: rgba(20, 18, 14, .48);
  font-size: 21px;
  font-weight: 650;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}}
.dots {{ bottom: 48px; }}
"""


def render_item_slide(
    page: dict[str, Any],
    out_path: Path,
    *,
    active: int,
    count: int,
    image_path: Path | None,
) -> None:
    channel = load_channel()
    visual_style = (
        f' style="background-image: url({html.escape(asset_uri(image_path), quote=True)})"'
        if image_path
        else ""
    )
    visual_class = "item-visual has-image" if image_path else "item-visual"
    item_name = string_value(page.get("item_name"))
    html_path = out_path.with_suffix(".html")
    html_text = f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
{item_slide_css()}
</style></head>
<body>
<div class="slide">
  <div class="{visual_class}"{visual_style}></div>
  <div class="handle">{html.escape(channel.handle.strip() or f"@{channel.account_name}")}</div>
  <div class="count">{active:02d} / {count:02d}</div>
  <div class="item-cluster">
    <div class="item-rule"><span>{html.escape(item_name)}</span></div>
    <h1 class="item-title">{bracket_markup(string_value(page.get("headline")))}</h1>
    <p class="item-body">{html.escape(concise_body(page))}</p>
    <div class="takeaway">{html.escape(concise_takeaway(page))}</div>
  </div>
  <div class="source">Source: {html.escape(first_source_url(page))}</div>
  <div class="dots">{dot_markup(active, count)}</div>
</div>
</body></html>"""
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(html_text, encoding="utf-8")
    render_html_slide(html_path, out_path)


def render_carousel(
    carousel: dict[str, Any],
    *,
    out_dir: Path,
    generate_images: bool = True,
    channel_id: str | None = None,
    reusable_assets: dict[str, Any] | None = None,
    cover_style: str = DEFAULT_COVER_STYLE,
) -> Path:
    channel = load_channel(channel_id or string_value(carousel.get("channel_id")) or None)
    os.environ["CAROUSEL_CHANNEL"] = channel.id
    out_dir.mkdir(parents=True, exist_ok=True)
    keys = item_keys(carousel)
    total = len(keys) + 2
    reusable_assets = reusable_assets or {"cover": None, "items": {}}
    cover_style = normalize_cover_style(cover_style)
    cover = carousel.get("cover_page")
    cover = cover if isinstance(cover, dict) else {}
    slides: list[dict[str, Any]] = []
    cover_path = out_dir / "slide_01.mp4"
    cover_poster = cover_poster_path(cover_path)
    if cover_style == KINETIC_FLY_COVER_STYLE:
        cover_image = None
        image_composition = ""
        render_kinetic_fly_cover(
            carousel,
            cover_path,
            count=total,
            channel=channel,
        )
    else:
        context, _cover_copy, cover_image = title_context(
            carousel,
            out_dir,
            generate_images=generate_images,
            reusable_image=reusable_assets.get("cover"),
            reusable_image_composition=string_value(reusable_assets.get("cover_composition")),
        )
        image_composition = string_value(context.get("image_composition"))
        post = {
            "id": string_value(carousel.get("id")),
            "url": f"https://idea-engine.local/carousels/{string_value(carousel.get('id'))}",
            "author": channel.brand_name,
            "handle": channel.handle,
            "text": " ".join(
                part
                for part in [string_value(cover.get("headline")), string_value(cover.get("subheadline"))]
                if part
            ),
            "date": string_value(carousel.get("generated_at")),
        }
        render_animated_title_slide(
            post,
            cover_path,
            total,
            None,
            context,
            channel.account_name or DEFAULT_ACCOUNT_NAME,
        )
    slides.append(
        {
            "index": 1,
            "type": "title",
            "path": str(cover_path),
            "poster": str(cover_poster),
            "image_path": str(cover_image or ""),
            "image_composition": image_composition,
            "cover_style": cover_style,
            "alt_text": string_value(cover.get("alt_text")),
        }
    )

    for offset, key in enumerate(keys, start=2):
        page = carousel[key]
        prompt = image_prompt(
            string_value(page.get("image_prompt")),
            string_value(page.get("item_name")),
        )
        reusable_image = reusable_assets.get("items", {}).get(string_value(page.get("item_name")).lower())
        if reusable_image:
            image_path = reusable_image
            print(f"[asset] reusing {page.get('item_name')} image -> {image_path}")
        else:
            image_path = maybe_generate_image(
                out_dir,
                topic=string_value(page.get("item_name")) or key,
                prompt=prompt,
                generate_images=generate_images,
                size=openai_item_image_size(),
            )
        slide_path = out_dir / f"slide_{offset:02d}.png"
        render_item_slide(
            page,
            slide_path,
            active=offset,
            count=total,
            image_path=image_path,
        )
        slides.append(
            {
                "index": offset,
                "type": "item",
                "path": str(slide_path),
                "item_name": string_value(page.get("item_name")),
                "image_path": str(image_path or ""),
                "source_url": first_source_url(page),
                "alt_text": string_value(page.get("alt_text")),
            }
        )

    cta = carousel.get("cta")
    cta = cta if isinstance(cta, dict) else {}
    cta_path = out_dir / f"slide_{total:02d}.png"
    render_cta_slide(
        cta_path,
        total,
        total,
        {
            "kicker": "FOLLOW",
            "headline": string_value(cta.get("headline")) or "Follow for more",
            "body": string_value(cta.get("body")),
            "action": string_value(cta.get("action")) or "Follow + Save",
        },
    )
    slides.append({
        "index": total,
        "type": "cta",
        "path": str(cta_path),
        "alt_text": string_value(cta.get("alt_text")),
    })
    manifest = {
        "source": "idea_engine",
        "carousel_id": string_value(carousel.get("id")),
        "channel_id": channel.id,
        "slide_count": total,
        "cover_style": cover_style,
        "cover_image_provider": "reused" if cover_image and reusable_assets.get("cover") else "openai" if cover_image else "",
        "slides": slides,
    }
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Render one idea-engine carousel")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--index", type=int, default=0, help="0-based carousel index")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--channel", help="Render with a different channel id")
    parser.add_argument("--asset-manifest", type=Path, help="Reuse generated images from another render manifest")
    parser.add_argument("--no-generate-images", action="store_true")
    parser.add_argument(
        "--cover-style",
        default=os.environ.get("IDEA_COVER_STYLE", DEFAULT_COVER_STYLE),
        help="Cover renderer: default/usual or kinetic-fly/fly",
    )
    args = parser.parse_args()

    payload = read_json(args.input)
    carousel = selected_carousel(payload, args.index)
    out_dir = args.out_dir
    if args.out_dir == DEFAULT_OUT:
        out_dir = DEFAULT_OUT / carousel_slug(carousel, args.index)
    manifest_path = render_carousel(
        carousel,
        out_dir=out_dir,
        generate_images=not args.no_generate_images,
        channel_id=args.channel,
        reusable_assets=load_reusable_assets(args.asset_manifest),
        cover_style=args.cover_style,
    )
    print(manifest_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
