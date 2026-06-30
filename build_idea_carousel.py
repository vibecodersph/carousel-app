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
from pathlib import Path
from typing import Any

from build_article_carousel import clamp_words, normalize_space
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
) -> Path:
    channel = load_channel(channel_id or string_value(carousel.get("channel_id")) or None)
    os.environ["CAROUSEL_CHANNEL"] = channel.id
    out_dir.mkdir(parents=True, exist_ok=True)
    keys = item_keys(carousel)
    total = len(keys) + 2
    reusable_assets = reusable_assets or {"cover": None, "items": {}}
    context, _cover_copy, cover_image = title_context(
        carousel,
        out_dir,
        generate_images=generate_images,
        reusable_image=reusable_assets.get("cover"),
        reusable_image_composition=string_value(reusable_assets.get("cover_composition")),
    )
    cover = carousel.get("cover_page")
    cover = cover if isinstance(cover, dict) else {}
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
    slides: list[dict[str, Any]] = []
    cover_path = out_dir / "slide_01.mp4"
    cover_poster = cover_poster_path(cover_path)
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
            "image_composition": string_value(context.get("image_composition")),
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
    slides.append({"index": total, "type": "cta", "path": str(cta_path)})
    manifest = {
        "source": "idea_engine",
        "carousel_id": string_value(carousel.get("id")),
        "channel_id": channel.id,
        "slide_count": total,
        "cover_image_provider": "reused" if reusable_assets.get("cover") else "openai" if cover_image else "",
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
    )
    print(manifest_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
