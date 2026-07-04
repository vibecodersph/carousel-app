#!/usr/bin/env python3
"""Render a text-led Instagram Reel from a structured brief.

This is intentionally render-only. It creates local media assets and a manifest
that can be dry-run through ``instagram_publish.py`` as a single-item Reel.
"""

from __future__ import annotations

import argparse
import html
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from channel import Channel, load_channel


ROOT = Path(__file__).resolve().parent
DEFAULT_BRIEF = ROOT / "out" / "aibrief_jp_growth_sprint_2026-07-03" / "day2_hidden_agents_reel_brief.json"
DEFAULT_OUT_DIR = ROOT / "out" / "aibrief_jp_growth_sprint_2026-07-03" / "rendered_hidden_agents_reel"
DEFAULT_MUSIC = ROOT / "assets" / "music" / "clips" / "signal-glow-f861f584-v45-60s.mp3"
REEL_W = 1080
REEL_H = 1920
DEFAULT_FPS = 30


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run(command: list[str]) -> None:
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        if exc.stdout:
            print(exc.stdout)
        if exc.stderr:
            print(exc.stderr)
        raise SystemExit(f"command failed: {' '.join(command)}") from exc


def require(binary: str) -> str:
    path = shutil.which(binary)
    if not path:
        raise SystemExit(f"{binary} is required to render text reels")
    return path


def string_value(value: object) -> str:
    return str(value or "").strip()


def scene_duration(scene: dict[str, Any]) -> float:
    try:
        start = float(scene.get("start") or 0)
        end = float(scene.get("end") or 0)
    except (TypeError, ValueError):
        return 3.0
    return max(0.5, end - start)


def total_duration(scenes: list[dict[str, Any]]) -> float:
    return round(sum(scene_duration(scene) for scene in scenes), 3)


def headline_size(text: str) -> int:
    n = len(text)
    if n > 34:
        return 64
    if n > 24:
        return 74
    if n > 14:
        return 88
    return 106


def resolve_theme(channel: Channel) -> dict[str, str]:
    brand = channel.brand if isinstance(channel.brand, dict) else {}
    reel = brand.get("reel") if isinstance(brand.get("reel"), dict) else {}
    return {
        "background": string_value(reel.get("background")) or "#0B0B0C",
        "surface": string_value(reel.get("surface")) or "#16140F",
        "text": string_value(reel.get("text")) or "#FFFFFF",
        "muted": string_value(reel.get("muted")) or "#8B98A5",
        "accent": string_value(reel.get("accent")) or "#C0552E",
    }


def default_cards() -> list[tuple[str, str]]:
    return [
        ("PR", "visible"),
        ("commits", "hidden"),
        ("bots", "3.3%"),
        ("Claude Code", "850,157"),
        ("maintenance", "quiet"),
    ]


def brief_cards(brief: dict[str, Any]) -> list[tuple[str, str]]:
    raw_cards = brief.get("cards")
    cards: list[tuple[str, str]] = []
    if isinstance(raw_cards, list):
        for item in raw_cards:
            if not isinstance(item, dict):
                continue
            label = string_value(item.get("label"))
            value = string_value(item.get("value"))
            if label and value:
                cards.append((label, value))
    return cards or default_cards()


def cards_markup(cards_source: list[tuple[str, str]], scene_index: int) -> str:
    cards: list[str] = []
    for offset, (label, value) in enumerate(cards_source):
        active = " active" if (scene_index + offset) % 3 == 0 else ""
        cards.append(
            f'<div class="code-card{active}"><span>{html.escape(label)}</span>'
            f'<strong>{html.escape(value)}</strong></div>'
        )
    return "\n".join(cards)


def source_metadata(brief: dict[str, Any]) -> tuple[str, str]:
    source_url = string_value(brief.get("sourceUrl"))
    label = string_value(brief.get("sourceLabel"))
    note = string_value(brief.get("sourceNote"))
    if not label:
        label = "arXiv 2606.24429" if "2606.24429" in source_url else "source linked"
    if not note:
        note = "AI coding agents in open source" if "2606.24429" in source_url else string_value(brief.get("topic"))
    return label, note


def brief_manifest_metadata(brief: dict[str, Any]) -> dict[str, Any]:
    replacement_fields = {
        "priority": brief.get("replacementPriority"),
        "replaces_content_hash": string_value(brief.get("replacesContentHash")),
        "replaces_scheduled_at": string_value(brief.get("replacesScheduledAt")),
        "direction": string_value(brief.get("replacementDirection")),
    }
    replacement = {
        key: value
        for key, value in replacement_fields.items()
        if value not in ("", None)
    }
    metadata: dict[str, Any] = {
        "recommended_publish_at": string_value(brief.get("recommendedPublishAt"))
        or string_value(brief.get("replacesScheduledAt")),
        "source_label": string_value(brief.get("sourceLabel")),
        "source_note": string_value(brief.get("sourceNote")),
        "source_chip": string_value(brief.get("sourceChip")),
    }
    if replacement:
        metadata["replacement"] = replacement
    return metadata


def scene_html(
    *,
    brief: dict[str, Any],
    scene: dict[str, Any],
    scene_index: int,
    scene_count: int,
    channel: Channel,
) -> str:
    theme = resolve_theme(channel)
    overlay = string_value(scene.get("overlay"))
    source_label, source_note = source_metadata(brief)
    source_text = f"出典: {source_label}"
    if source_note:
        source_text += f" / {source_note}"
    source_chip = string_value(brief.get("sourceChip")) or "論文ベース"
    bottom_cta = string_value(brief.get("bottomCta")) or "エンジニアに共有"
    logo_markup = ""
    if channel.logo_path and channel.logo_path.exists():
        logo_markup = f'<img class="logo" src="{channel.logo_path.resolve().as_uri()}" alt="">'
    progress_pct = round(scene_index * 100 / scene_count, 4)
    size = headline_size(overlay)

    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; width: {REEL_W}px; height: {REEL_H}px; background: {theme['background']}; }}
body {{
  font-family: "Noto Sans JP", "Hiragino Sans", "Yu Gothic", "Helvetica Neue", Arial, sans-serif;
}}
.reel {{
  position: relative;
  width: {REEL_W}px;
  height: {REEL_H}px;
  overflow: hidden;
  color: {theme['text']};
  background: {theme['background']};
}}
.grid {{
  position: absolute;
  inset: 0;
  background-image:
    linear-gradient(rgba(255,255,255,0.055) 1px, transparent 1px),
    linear-gradient(90deg, rgba(255,255,255,0.055) 1px, transparent 1px);
  background-size: 72px 72px;
  opacity: 0.58;
}}
.scan {{
  position: absolute;
  inset: 0;
  background:
    linear-gradient(180deg, rgba(192,85,46,0.14), transparent 24%, transparent 72%, rgba(192,85,46,0.09)),
    radial-gradient(circle at 50% 20%, rgba(255,255,255,0.08), transparent 34%);
}}
.top {{
  position: absolute;
  left: 58px;
  right: 58px;
  top: 62px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
}}
.brand {{
  display: flex;
  align-items: center;
  min-width: 0;
  gap: 16px;
  font-weight: 800;
  letter-spacing: 0;
}}
.logo {{
  width: 54px;
  height: 54px;
  border-radius: 12px;
}}
.handle {{
  color: {theme['muted']};
  font-size: 25px;
  font-weight: 700;
  margin-top: 2px;
}}
.chip {{
  border: 1px solid rgba(255,255,255,0.18);
  background: rgba(255,255,255,0.08);
  color: {theme['text']};
  padding: 15px 20px;
  border-radius: 999px;
  font-size: 24px;
  font-weight: 800;
}}
.cards {{
  position: absolute;
  left: 64px;
  right: 64px;
  top: 250px;
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 18px;
  opacity: 0.92;
}}
.code-card {{
  min-height: 104px;
  border: 1px solid rgba(255,255,255,0.13);
  background: rgba(255,255,255,0.06);
  border-radius: 22px;
  padding: 20px 22px;
}}
.code-card span {{
  display: block;
  color: {theme['muted']};
  font-size: 22px;
  font-weight: 800;
}}
.code-card strong {{
  display: block;
  margin-top: 8px;
  font-size: 31px;
  line-height: 1.05;
}}
.code-card.active {{
  border-color: rgba(192,85,46,0.86);
  background: rgba(192,85,46,0.17);
}}
.headline {{
  position: absolute;
  left: 70px;
  right: 70px;
  top: 720px;
  min-height: 440px;
  display: flex;
  align-items: center;
  justify-content: center;
  text-align: center;
}}
h1 {{
  margin: 0;
  font-size: {size}px;
  line-height: 1.16;
  font-weight: 900;
  letter-spacing: 0;
  overflow-wrap: anywhere;
}}
.source {{
  position: absolute;
  left: 74px;
  right: 74px;
  bottom: 276px;
  padding: 24px 28px;
  border-left: 8px solid {theme['accent']};
  background: rgba(255,255,255,0.075);
  color: rgba(255,255,255,0.86);
  font-size: 30px;
  line-height: 1.35;
  font-weight: 750;
}}
.bottom {{
  position: absolute;
  left: 58px;
  right: 58px;
  bottom: 62px;
}}
.progress {{
  width: 100%;
  height: 10px;
  border-radius: 999px;
  background: rgba(255,255,255,0.16);
  overflow: hidden;
}}
.progress span {{
  display: block;
  width: {progress_pct}%;
  height: 100%;
  background: {theme['accent']};
}}
.bottom-row {{
  display: flex;
  justify-content: space-between;
  color: {theme['muted']};
  font-size: 24px;
  font-weight: 800;
  margin-top: 22px;
}}
</style>
</head>
<body>
<div class="reel">
  <div class="grid"></div>
  <div class="scan"></div>
  <div class="top">
    <div class="brand">{logo_markup}<div><div>{html.escape(channel.account_name or channel.brand_name)}</div><div class="handle">{html.escape(channel.handle)}</div></div></div>
    <div class="chip">{html.escape(source_chip)}</div>
  </div>
  <div class="cards">{cards_markup(brief_cards(brief), scene_index)}</div>
  <main class="headline"><h1>{html.escape(overlay)}</h1></main>
  <div class="source">{html.escape(source_text)}</div>
  <div class="bottom">
    <div class="progress"><span></span></div>
    <div class="bottom-row"><span>{scene_index:02d}/{scene_count:02d}</span><span>{html.escape(bottom_cta)}</span></div>
  </div>
</div>
</body>
</html>"""


def render_scene_images(
    brief: dict[str, Any],
    channel: Channel,
    out_dir: Path,
) -> list[dict[str, Any]]:
    try:
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError as exc:
        raise SystemExit("playwright is required to render text reels") from exc

    raw_scenes = brief.get("scenes")
    if not isinstance(raw_scenes, list) or not raw_scenes:
        raise SystemExit("Reel brief has no scenes")
    scenes = [scene for scene in raw_scenes if isinstance(scene, dict)]
    out_dir.mkdir(parents=True, exist_ok=True)

    rendered: list[dict[str, Any]] = []
    print(f"[text-reel 1/5] rendering {len(scenes)} scene image(s)")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": REEL_W, "height": REEL_H}, device_scale_factor=1)
        for idx, scene in enumerate(scenes, start=1):
            html_path = out_dir / f"scene_{idx:02d}.html"
            image_path = out_dir / f"scene_{idx:02d}.png"
            html_path.write_text(
                scene_html(
                    brief=brief,
                    scene=scene,
                    scene_index=idx,
                    scene_count=len(scenes),
                    channel=channel,
                ),
                encoding="utf-8",
            )
            page.goto(html_path.resolve().as_uri())
            page.wait_for_load_state("networkidle")
            page.evaluate("() => (document.fonts && document.fonts.ready ? document.fonts.ready.then(() => true) : true)")
            page.locator(".reel").screenshot(path=str(image_path))
            rendered.append(
                {
                    "index": idx,
                    "start": scene.get("start"),
                    "end": scene.get("end"),
                    "duration": scene_duration(scene),
                    "overlay": string_value(scene.get("overlay")),
                    "html": str(html_path.resolve()),
                    "path": str(image_path.resolve()),
                    "visualDirection": string_value(scene.get("visualDirection")),
                }
            )
        browser.close()
    return rendered


def render_segments(scene_assets: list[dict[str, Any]], out_dir: Path, fps: int) -> list[Path]:
    ffmpeg = require("ffmpeg")
    segments: list[Path] = []
    print("[text-reel 2/5] rendering video segments")
    for scene in scene_assets:
        index = int(scene["index"])
        segment = out_dir / f"segment_{index:02d}.mp4"
        run(
            [
                ffmpeg,
                "-y",
                "-loop",
                "1",
                "-t",
                f"{float(scene['duration']):.3f}",
                "-i",
                str(scene["path"]),
                "-vf",
                f"fps={fps},format=yuv420p",
                "-c:v",
                "libx264",
                "-crf",
                "18",
                "-preset",
                "medium",
                "-pix_fmt",
                "yuv420p",
                str(segment),
            ]
        )
        segments.append(segment)
    return segments


def concat_segments(segments: list[Path], out_path: Path) -> Path:
    ffmpeg = require("ffmpeg")
    list_path = out_path.with_name("segments.txt")
    list_path.write_text(
        "".join(f"file '{segment.resolve()}'\n" for segment in segments),
        encoding="utf-8",
    )
    print(f"[text-reel 3/5] concatenating segments -> {out_path}")
    run([ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(list_path), "-c", "copy", str(out_path)])
    return out_path


def add_music(video_path: Path, music_path: Path | None, out_path: Path, duration: float) -> Path:
    ffmpeg = require("ffmpeg")
    if not music_path or not music_path.exists():
        shutil.copyfile(video_path, out_path)
        return out_path
    fade_start = max(0.0, duration - 2.0)
    print(f"[text-reel 4/5] adding music -> {out_path}")
    run(
        [
            ffmpeg,
            "-y",
            "-i",
            str(video_path),
            "-stream_loop",
            "-1",
            "-i",
            str(music_path),
            "-t",
            f"{duration:.3f}",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-af",
            f"afade=t=out:st={fade_start:.3f}:d=2",
            "-shortest",
            "-movflags",
            "+faststart",
            str(out_path),
        ]
    )
    return out_path


def make_poster(video_path: Path, poster_path: Path) -> Path:
    ffmpeg = require("ffmpeg")
    run([ffmpeg, "-y", "-i", str(video_path), "-frames:v", "1", "-update", "1", str(poster_path)])
    return poster_path


def make_contact_sheet(out_dir: Path, scene_count: int, contact_sheet: Path) -> Path:
    ffmpeg = require("ffmpeg")
    columns = 4
    rows = max(1, (scene_count + columns - 1) // columns)
    run(
        [
            ffmpeg,
            "-y",
            "-framerate",
            "1",
            "-i",
            str(out_dir / "scene_%02d.png"),
            "-vf",
            f"scale=270:480,tile={columns}x{rows}:padding=12:margin=12:color=white",
            "-frames:v",
            "1",
            str(contact_sheet),
        ]
    )
    return contact_sheet


def extract_qa_frames(video_path: Path, out_dir: Path) -> list[Path]:
    ffmpeg = require("ffmpeg")
    frames: list[Path] = []
    for pct in (10, 50, 90):
        frame = out_dir / f"qa_frame_{pct:02d}.png"
        timestamp = 30 * pct / 100
        run([ffmpeg, "-y", "-ss", f"{timestamp:.3f}", "-i", str(video_path), "-frames:v", "1", "-update", "1", str(frame)])
        frames.append(frame)
    return frames


def build_text_reel(
    *,
    brief_path: Path,
    channel_id: str,
    out_dir: Path,
    fps: int,
    music_path: Path | None,
) -> dict[str, Any]:
    brief = load_json(brief_path)
    channel = load_channel(channel_id or string_value(brief.get("channelId")) or "aibrief_jp")
    out_dir.mkdir(parents=True, exist_ok=True)

    scene_assets = render_scene_images(brief, channel, out_dir)
    duration = total_duration([{"start": s.get("start"), "end": s.get("end")} for s in scene_assets])
    segments = render_segments(scene_assets, out_dir, fps)
    silent_video = concat_segments(segments, out_dir / "reel_no_audio.mp4")
    reel_path = add_music(silent_video, music_path, out_dir / "reel.mp4", duration)
    poster = make_poster(reel_path, out_dir / "poster.png")
    print("[text-reel 5/5] writing contact sheet, QA frames, and manifest")
    contact_sheet = make_contact_sheet(out_dir, len(scene_assets), out_dir / "contact_sheet.jpg")
    qa_frames = extract_qa_frames(reel_path, out_dir)
    caption = string_value(brief.get("caption"))
    (out_dir / "caption.txt").write_text(caption + "\n", encoding="utf-8")
    description = string_value(brief.get("description")) or "Text-led original AI Brief Reel based on the supplied brief."
    topic = string_value(brief.get("topic")) or string_value(brief.get("hookOverlay"))

    brief_metadata = brief_manifest_metadata(brief)
    manifest = {
        "source": "text_reel_builder",
        "source_type": "local_text_reel",
        "brief_id": string_value(brief.get("id")),
        "channel_id": channel.id,
        "channel": {
            "id": channel.id,
            "account_name": channel.account_name,
            "brand_name": channel.brand_name,
            "handle": channel.handle,
        },
        "handle": channel.handle,
        "account_name": channel.account_name,
        "format": "instagram_reel",
        "status": "draft_video_ready_music_only",
        "recommended_publish_at": brief_metadata["recommended_publish_at"],
        "topic": topic,
        "description": description,
        "size": f"{REEL_W}x{REEL_H}",
        "fps": fps,
        "duration_seconds": duration,
        "source_url": string_value(brief.get("sourceUrl")),
        "source_urls": brief.get("sourceUrls") if isinstance(brief.get("sourceUrls"), list) else [],
        "source_label": brief_metadata["source_label"],
        "source_note": brief_metadata["source_note"],
        "source_chip": brief_metadata["source_chip"],
        **({"replacement": brief_metadata["replacement"]} if "replacement" in brief_metadata else {}),
        "hook_overlay": string_value(brief.get("hookOverlay")),
        "voiceover": brief.get("voiceover") if isinstance(brief.get("voiceover"), list) else [],
        "instagram_caption": caption,
        "caption_path": str((out_dir / "caption.txt").resolve()),
        "pinned_first_comment": string_value(brief.get("pinnedFirstComment")),
        "story_poll": brief.get("storyPoll") if isinstance(brief.get("storyPoll"), dict) else {},
        "trial_reel": brief.get("trialReel") if isinstance(brief.get("trialReel"), dict) else {},
        "music": str(music_path.resolve()) if music_path and music_path.exists() else "",
        "poster": str(poster.resolve()),
        "contact_sheet": str(contact_sheet.resolve()),
        "qa_frames": [str(frame.resolve()) for frame in qa_frames],
        "scenes": scene_assets,
        "slides": [
            {
                "index": 1,
                "type": "video",
                "path": str(reel_path.resolve()),
                "poster": str(poster.resolve()),
                "source_url": string_value(brief.get("sourceUrl")),
                "alt_text": string_value(brief.get("hookOverlay")),
            }
        ],
    }
    write_json(out_dir / "manifest.json", manifest)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render a text-led Instagram Reel from a JSON brief")
    parser.add_argument("--brief", type=Path, default=DEFAULT_BRIEF)
    parser.add_argument("--channel", default="")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--fps", type=int, default=DEFAULT_FPS)
    parser.add_argument("--music", type=Path, default=DEFAULT_MUSIC)
    parser.add_argument("--no-music", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    music = None if args.no_music else args.music
    manifest = build_text_reel(
        brief_path=args.brief,
        channel_id=args.channel,
        out_dir=args.out_dir,
        fps=max(1, args.fps),
        music_path=music,
    )
    print(f"[text-reel] wrote {manifest['slides'][0]['path']}")
    print(f"[text-reel] manifest {args.out_dir / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
