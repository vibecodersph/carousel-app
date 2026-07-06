#!/usr/bin/env python3
"""Build animated Japanese cover studies for AI Brief JP / The Batch issue 360."""
from __future__ import annotations

import argparse
import base64
import html
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "out" / "aibrief_jp_issue_360_covers"
HTML_PATH = OUT_DIR / "issue360_cover_studies.html"
JSON_PATH = OUT_DIR / "issue360_cover_candidates.json"
MD_PATH = OUT_DIR / "issue360_jp_strategy_and_translations.md"
MEDIA_DIR = OUT_DIR / "media"
MEDIA_MANIFEST_PATH = OUT_DIR / "issue360_cover_media_manifest.json"
LOGO_PATH = ROOT / "channels" / "aibrief_jp" / "logo.png"

COVER_WIDTH = 1080
COVER_HEIGHT = 1350
DEFAULT_MEDIA_DURATION_SECONDS = 6.0
DEFAULT_MEDIA_FPS = 12
POSTER_CAPTURE_SECONDS = 2.4


STORIES: list[dict[str, str]] = [
    {
        "id": "gpt56",
        "source_url": "https://www.deeplearning.ai/the-batch/gpt-5-6-lands-in-limbo",
        "feature_image": "https://charonhub.deeplearning.ai/content/images/2026/07/GPT5.6.webp",
        "title_en": "GPT-5.6 Lands in Limbo",
        "title_ja": "GPT-5.6、宙ぶらりん",
        "summary_ja": "OpenAIはSol、Terra、Lunaを含むGPT-5.6ファミリーをプレビューしました。ただし現時点では、米政府に選ばれたユーザー向けの限定提供で、広い公開はこれからです。",
    },
    {
        "id": "fugu",
        "source_url": "https://www.deeplearning.ai/the-batch/fugu-blends-models-task-by-task",
        "feature_image": "https://charonhub.deeplearning.ai/content/images/2026/07/FUGU.webp",
        "title_en": "Fugu Blends Models Task by Task",
        "title_ja": "Fugu、タスクごとにモデルを使い分ける",
        "summary_ja": "Sakana AIはFuguとFugu-Ultraを発表しました。Claude、Gemini、GPT系のエージェントをタスクごとに呼び出す、モデル・オーケストレーターです。",
    },
    {
        "id": "microsoft",
        "source_url": "https://www.deeplearning.ai/the-batch/microsoft-strikes-out-on-its-own",
        "feature_image": "https://charonhub.deeplearning.ai/content/images/2026/07/MAITHINKING1.webp",
        "title_en": "Microsoft Strikes Out on Its Own",
        "title_ja": "Microsoft、自前の推論モデルへ",
        "summary_ja": "MicrosoftはMAI-Thinking-1を明らかにしました。Claude Sonnet 4.6級とされる推論モデルで、蒸留ではなく一から開発した点が焦点です。",
    },
    {
        "id": "roboreward",
        "source_url": "https://www.deeplearning.ai/the-batch/better-reward-models-for-robots",
        "feature_image": "https://charonhub.deeplearning.ai/content/images/2026/07/ROBOREWARD.webp",
        "title_en": "Better Reward Models for Robots",
        "title_ja": "RoboReward、ロボットの報酬モデル",
        "summary_ja": "RoboRewardは、視覚言語モデルを使った報酬モデル群です。ロボット強化学習で、手作りの報酬関数との差を縮めることを狙います。",
    },
    {
        "id": "course_noise",
        "source_url": "https://www.deeplearning.ai/the-batch/how-we-decide-what-courses-to-teach-the-ai-world-is-full-of-hype-and-sales-pitches-deeplearning-ai-focuses-on-most-important-tools-and-techniques-in-ways-you-can-apply-to-any-ai-vendors",
        "feature_image": "https://charonhub.deeplearning.ai/content/images/2026/07/2026.07.03-LETTER-b-1.webp",
        "title_en": "How We Decide What Courses to Teach",
        "title_ja": "AIのノイズから学ぶべきことを選ぶ",
        "summary_ja": "DeepLearning.AIは、AI界隈のハイプや売り込みから距離を取り、どのベンダーにも応用できる重要な道具と技術に絞って教える姿勢を説明しました。",
    },
]


def story(story_id: str) -> dict[str, str]:
    for item in STORIES:
        if item["id"] == story_id:
            return item
    raise KeyError(story_id)


CANDIDATES: list[dict[str, Any]] = [
    {
        "id": "gpt_gate",
        "rank": 1,
        "story_id": "gpt56",
        "label": "GPT gate / hinomaru checkpoint",
        "style": "間 + warning gate",
        "hook": "GPT-5.6、まだ届かない",
        "kicker": "OPENAI / THE BATCH 360",
        "swipe": "スワイプで要点",
        "why": "限定提供のもどかしさを短く言い、赤いゲートの動きで「まだ入れない」を1秒で伝える。",
    },
    {
        "id": "gpt_typerain",
        "rank": 5,
        "story_id": "gpt56",
        "label": "GPT type-rain / limited access",
        "style": "縦書き雨 + diagonal alert",
        "hook": "政府先行のGPT-5.6",
        "kicker": "LIMITED PREVIEW",
        "swipe": "何が制限されている？",
        "why": "縦書きの反復でスマホ画面内の密度を上げ、斜めの赤帯でニュース性を作る。",
    },
    {
        "id": "fugu_call",
        "rank": 2,
        "story_id": "fugu",
        "label": "Fugu model call / big kanji",
        "style": "大漢字 + route nodes",
        "hook": "AIがAIを使い分ける",
        "kicker": "SAKANA AI / FUGU",
        "swipe": "モデル選びの新しい形",
        "why": "「AIがAIを使い分ける」という入れ子感が非エンジニアにも伝わり、エンジニアにはルーティングの話として刺さる。",
    },
    {
        "id": "fugu_router",
        "rank": 6,
        "story_id": "fugu",
        "label": "Fugu neon router / task switch",
        "style": "neo-Tokyo router",
        "hook": "モデル選びもAIの仕事に",
        "kicker": "TASK ROUTER",
        "swipe": "Fuguの狙いを見る",
        "why": "コストと性能の悩みに寄せ、AIビルダーが保存しやすい実務角度にする。",
    },
    {
        "id": "ms_split",
        "rank": 3,
        "story_id": "microsoft",
        "label": "Microsoft split / partner to own",
        "style": "De Stijl split-switch",
        "hook": "Microsoft、自前AIへ",
        "kicker": "MAI-THINKING-1",
        "swipe": "なぜ自前化したのか",
        "why": "提携企業の印象が強いMicrosoftに「自前AI」という違和感を置き、構図で独立の方向転換を見せる。",
    },
    {
        "id": "robo_enso",
        "rank": 4,
        "story_id": "roboreward",
        "label": "RoboReward ensō / reward loop",
        "style": "ensō loop + reward target",
        "hook": "ロボット報酬、手作業に迫る",
        "kicker": "ROBOREWARD",
        "swipe": "強化学習の地味な難所",
        "why": "「報酬を手作りする面倒さ」を擬人化せずに伝え、研究話を実務の進展として見せる。",
    },
    {
        "id": "noise_filter",
        "rank": 7,
        "story_id": "course_noise",
        "label": "Hype noise filter / clean window",
        "style": "type noise + whiteout window",
        "hook": "AIのノイズ、どう削る？",
        "kicker": "ANDREW NG LETTER",
        "swipe": "学ぶ順番の話",
        "why": "日本の技術読者が警戒しやすいハイプを先に認め、信頼側からスクロールを止める。",
    },
    {
        "id": "issue_wave",
        "rank": 8,
        "story_id": "issue",
        "label": "Issue overview / seigaiha wave",
        "style": "青海波 + story chips",
        "hook": "今週のAI、焦点はここ",
        "kicker": "THE BATCH 360",
        "swipe": "4本だけ要約",
        "why": "個別記事より広い週次まとめ用。波の反復でシリーズ感を作り、チップで中身を即見せる。",
    },
]


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def visual_units(value: str) -> float:
    units = 0.0
    for char in value:
        if char.isspace():
            continue
        if char.isascii():
            units += 0.55
        elif char in "、。！？!?「」『』（）()[]・/":
            units += 0.35
        else:
            units += 1.0
    return units


def split_cover_lines(text: str, *, max_units: float = 7.2, max_lines: int = 3) -> list[str]:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not text:
        return []
    if "\n" in text:
        return [part.strip() for part in text.splitlines() if part.strip()][:max_lines]
    if visual_units(text) <= max_units:
        return [text]

    lines: list[str] = []
    rest = text
    preferred_breaks = set("、。！？!?ではがをにへと")
    while rest and len(lines) < max_lines - 1 and visual_units(rest) > max_units:
        total = visual_units(rest)
        target = min(max_units, total / max(1, max_lines - len(lines)))
        running = 0.0
        best_index = 0
        best_score = 999.0
        for index, char in enumerate(rest[:-1], start=1):
            running += visual_units(char)
            next_char = rest[index] if index < len(rest) else ""
            if char.isascii() and next_char.isascii():
                continue
            distance = abs(running - target)
            score = distance - (1.2 if char in preferred_breaks else 0)
            if score < best_score and running >= 2.8:
                best_score = score
                best_index = index
        if best_index <= 0:
            break
        lines.append(rest[:best_index].strip())
        rest = rest[best_index:].strip()
    if rest:
        lines.append(rest)
    return lines[:max_lines]


def cover_headline_lines(candidate: dict[str, Any], fallback: str = "") -> list[str]:
    lines = string_list(candidate.get("hook_lines"))
    if lines:
        return lines[:3]
    return split_cover_lines(str(candidate.get("hook") or fallback))


def cover_headline_markup(candidate: dict[str, Any], fallback: str = "") -> str:
    lines = cover_headline_lines(candidate, fallback)
    if not lines:
        return esc(fallback)
    if len(lines) == 1:
        return esc(lines[0])
    head = [esc(line) for line in lines[:-1]]
    tail = f"<span>{esc(lines[-1])}</span>"
    return "<br>".join([*head, tail])


def headline_style(candidate: dict[str, Any], default_px: int) -> str:
    raw_size = candidate.get("headline_size")
    try:
        size = int(raw_size) if raw_size else 0
    except (TypeError, ValueError):
        size = 0
    if not size:
        lines = cover_headline_lines(candidate)
        max_line = max((visual_units(line) for line in lines), default=7.0)
        size = default_px
        if max_line > 7.4:
            size -= int((max_line - 7.4) * 7)
        if len(lines) > 2:
            size -= 8 * (len(lines) - 2)
        size = max(74, min(default_px, size))
    return f' style="font-size:{size}px"'


def candidate_chips(candidate: dict[str, Any], fallback: list[str]) -> list[str]:
    chips = string_list(candidate.get("chips")) or fallback
    seen: set[str] = set()
    clean: list[str] = []
    for chip in chips:
        normalized = re.sub(r"\s+", " ", chip).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        clean.append(normalized)
    return clean[:4]


def logo_data_uri() -> str:
    data = LOGO_PATH.read_bytes()
    return f"data:image/png;base64,{base64.b64encode(data).decode('ascii')}"


def brand_logo_markup() -> str:
    return (
        f'<div class="brand-badge" aria-label="AI Brief JP">'
        f'<img src="{logo_data_uri()}" alt="AI Brief JP"></div>'
    )


def cover_markup(candidate: dict[str, Any]) -> str:
    hook = esc(candidate["hook"])
    kicker = esc(candidate["kicker"])
    swipe = esc(candidate["swipe"])
    cover_id = candidate["id"]
    headline = cover_headline_markup(candidate)

    if cover_id == "gpt_gate":
        mega = esc(candidate.get("mega") or "GPT-5.6")
        vertical = esc(candidate.get("vertical") or "限定公開")
        return f"""
          <div class="sun sun-gate"></div>
          <div class="gate-lines"><span></span><span></span><span></span><span></span></div>
          <div class="scanline"></div>
          <div class="topline">{kicker}</div>
          <div class="latin-mega">{mega}</div>
          <h1 class="headline gate-headline"{headline_style(candidate, 122)}>{headline}</h1>
          <div class="vertical-mark">{vertical}</div>
          <div class="bottomline">{swipe}</div>
        """
    if cover_id == "gpt_typerain":
        rain_text = esc(candidate.get("ticker") or "限定公開・政府先行・GPT・まだ入れない・プレビュー・待機中・")
        sash = esc(candidate.get("sash") or "入口が狭い")
        cols = "".join(
            f'<div class="rain-col rain-{i}">{rain_text}</div>'
            for i in range(1, 8)
        )
        return f"""
          <div class="rain-field">{cols}</div>
          <div class="red-sash"><span>{sash}</span></div>
          <div class="topline dark-top">{kicker}</div>
          <h1 class="headline typerain-headline"{headline_style(candidate, 98)}>{headline}</h1>
          <div class="bottomline dark-bottom">{swipe}</div>
        """
    if cover_id == "fugu_call":
        nodes = candidate_chips(candidate, ["Claude", "Gemini", "GPT"])
        while len(nodes) < 3:
            nodes.append(["Claude", "Gemini", "GPT"][len(nodes)])
        kanji = esc(candidate.get("kanji") or "呼")
        return f"""
          <div class="route-map">
            <span class="route r1"></span><span class="route r2"></span><span class="route r3"></span>
            <span class="node n1">{esc(nodes[0])}</span><span class="node n2">{esc(nodes[1])}</span><span class="node n3">{esc(nodes[2])}</span>
          </div>
          <div class="kanji-bg">{kanji}</div>
          <div class="topline">{kicker}</div>
          <h1 class="headline call-headline"{headline_style(candidate, 118)}>{headline}</h1>
          <div class="bottomline">{swipe}</div>
        """
    if cover_id == "fugu_router":
        routes = candidate_chips(candidate, ["CODE", "SEARCH", "REVIEW"])
        while len(routes) < 3:
            routes.append(["CODE", "SEARCH", "REVIEW"][len(routes)])
        return f"""
          <div class="neon-grid"></div>
          <div class="router-card">
            <span>topic</span><b>{esc(routes[0])}</b><i>→ KEY POINT</i>
            <span>topic</span><b>{esc(routes[1])}</b><i>→ CONTEXT</i>
            <span>topic</span><b>{esc(routes[2])}</b><i>→ IMPACT</i>
          </div>
          <div class="neon-kicker">{kicker}</div>
          <h1 class="headline neon-headline"{headline_style(candidate, 116)}>{headline}</h1>
          <div class="neon-bottom">{swipe}</div>
        """
    if cover_id == "ms_split":
        left_word = esc(candidate.get("left_word") or "提携")
        right_word = esc(candidate.get("right_word") or "自前")
        return f"""
          <div class="stijl-grid">
            <span class="block red"></span><span class="block blue"></span><span class="block yellow"></span>
            <span class="line h1"></span><span class="line h2"></span><span class="line v1"></span><span class="line v2"></span>
          </div>
          <div class="split-word left-word">{left_word}</div>
          <div class="split-word right-word">{right_word}</div>
          <div class="switch-bar"></div>
          <div class="topline">{kicker}</div>
          <h1 class="headline ms-headline"{headline_style(candidate, 96)}>{headline}</h1>
          <div class="bottomline">{swipe}</div>
        """
    if cover_id == "robo_enso":
        kanji = esc(candidate.get("kanji_word") or candidate.get("kanji") or "報酬")
        return f"""
          <svg class="enso" viewBox="0 0 760 760" aria-hidden="true">
            <circle cx="380" cy="380" r="292" fill="none" stroke="currentColor" stroke-width="58" stroke-linecap="round" stroke-dasharray="1740 460"></circle>
          </svg>
          <div class="reward-dots"><span></span><span></span><span></span><span></span></div>
          <div class="robot-arm"><span></span></div>
          <div class="topline">{kicker}</div>
          <div class="kanji-reward">{kanji}</div>
          <h1 class="headline robo-headline"{headline_style(candidate, 104)}>{headline}</h1>
          <div class="bottomline">{swipe}</div>
        """
    if cover_id == "noise_filter":
        tick = esc(candidate.get("ticker") or "HYPE・SALES・PITCH・NOISE・AI・TOOL・MODEL・COURSE・")
        return f"""
          <div class="noise-ticker t1">{tick * 3}</div>
          <div class="noise-ticker t2">{tick * 3}</div>
          <div class="noise-ticker t3">{tick * 3}</div>
          <div class="clean-window"></div>
          <div class="topline dark-top">{kicker}</div>
          <h1 class="headline noise-headline"{headline_style(candidate, 118)}>{headline}</h1>
          <div class="bottomline dark-bottom">{swipe}</div>
        """
    if cover_id == "issue_wave":
        chips = candidate_chips(candidate, ["GPT-5.6", "Fugu", "MAI", "RoboReward"])
        chips_markup = "".join(f"<b>{esc(chip)}</b>" for chip in chips)
        return f"""
          <div class="wave-sun"></div>
          <div class="wave-stack"><span></span><span></span><span></span><span></span><span></span><span></span></div>
          <div class="story-chips">{chips_markup}</div>
          <div class="topline wave-top">{kicker}</div>
          <h1 class="headline wave-headline"{headline_style(candidate, 118)}>{headline}</h1>
          <div class="bottomline wave-bottom">{swipe}</div>
        """
    raise ValueError(cover_id)


CSS = """
:root {
  color-scheme: dark;
  --page-bg: #0b0c12;
  --paper: #f4f2ec;
  --paper-2: #e7e1d2;
  --ink: #16140f;
  --muted: rgba(22, 20, 15, .58);
  --red: #c84a2b;
  --red-2: #e1392d;
  --blue: #2358c7;
  --yellow: #e4b020;
  --cyan: #25d7f2;
  --pink: #ff4a97;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--page-bg);
  color: #f6f3ea;
  font-family: -apple-system, BlinkMacSystemFont, "Hiragino Sans", "Yu Gothic", "Yu Gothic UI", "Noto Sans JP", sans-serif;
}
.sheet {
  padding: 42px 48px 56px;
}
.sheet-head {
  max-width: 1120px;
  margin-bottom: 28px;
}
.sheet-head h1 {
  margin: 0 0 12px;
  font-size: 32px;
  line-height: 1.18;
  letter-spacing: 0;
}
.sheet-head p {
  margin: 0;
  color: #aeb4c3;
  font-size: 14px;
  line-height: 1.7;
}
.principles {
  display: grid;
  grid-template-columns: repeat(5, minmax(150px, 1fr));
  gap: 10px;
  margin: 22px 0 34px;
  max-width: 1300px;
}
.principles span {
  border: 1px solid rgba(255,255,255,.12);
  background: rgba(255,255,255,.045);
  border-radius: 8px;
  padding: 12px 14px;
  color: #d8dce7;
  font-size: 12px;
  line-height: 1.45;
}
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(432px, 1fr));
  gap: 34px;
  align-items: start;
}
.option {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.option-meta {
  min-height: 80px;
}
.option-meta .rank {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 32px;
  height: 24px;
  margin-right: 8px;
  border-radius: 4px;
  background: #ffe45c;
  color: #16140f;
  font: 800 12px/1 ui-monospace, SFMono-Regular, Menlo, monospace;
}
.option-meta b {
  font-size: 14px;
  line-height: 1.35;
}
.option-meta p {
  margin: 8px 0 0;
  color: #aeb4c3;
  font-size: 12px;
  line-height: 1.55;
}
.frame {
  width: 432px;
  height: 540px;
  overflow: hidden;
  border-radius: 12px;
  box-shadow: 0 28px 70px rgba(0,0,0,.48), 0 0 0 1px rgba(255,255,255,.08);
  background: #111;
}
.cover {
  position: relative;
  width: 1080px;
  height: 1350px;
  transform: scale(.4);
  transform-origin: top left;
  overflow: hidden;
  isolation: isolate;
  background: var(--paper);
  color: var(--ink);
}
.cover::after {
  content: "";
  position: absolute;
  inset: 0;
  border: 28px solid rgba(22,20,15,.06);
  pointer-events: none;
  z-index: 20;
}
.brand-badge {
  position: absolute;
  top: 54px;
  right: 58px;
  z-index: 36;
  width: 146px;
  height: 146px;
  display: grid;
  place-items: center;
  border: 3px solid var(--red);
  border-radius: 50%;
  background: rgba(244,242,236,.86);
  box-shadow: 12px 12px 0 rgba(22,20,15,.08);
  overflow: hidden;
  animation: brandPulse 6s cubic-bezier(.16, 1, .3, 1) infinite;
}
.brand-badge img {
  width: 124px;
  height: 124px;
  display: block;
  object-fit: contain;
  border-radius: 50%;
}
.dark-top ~ .brand-badge,
.cover-gpt_typerain .brand-badge,
.cover-noise_filter .brand-badge,
.cover-issue_wave .brand-badge {
  border-color: var(--red);
  background: rgba(244,242,236,.9);
  box-shadow: 12px 12px 0 rgba(0,0,0,.22);
}
.topline,
.bottomline {
  position: absolute;
  left: 76px;
  right: 76px;
  z-index: 10;
  font: 800 25px/1.2 ui-monospace, SFMono-Regular, Menlo, monospace;
  letter-spacing: .18em;
}
.topline { top: 76px; }
.bottomline {
  bottom: 76px;
  color: var(--muted);
  letter-spacing: .12em;
}
.headline {
  position: absolute;
  z-index: 12;
  margin: 0;
  font-weight: 900;
  line-height: 1.04;
  letter-spacing: 0;
}
.headline span { color: var(--red); }
.latin-mega {
  position: absolute;
  left: 70px;
  top: 260px;
  z-index: 8;
  font: 950 190px/.9 ui-sans-serif, system-ui, sans-serif;
  letter-spacing: -.04em;
}
.sun {
  position: absolute;
  width: 360px;
  height: 360px;
  border-radius: 50%;
  background: var(--red);
  box-shadow: 0 0 80px rgba(200,74,43,.3);
}
.sun-gate {
  right: 84px;
  top: 230px;
  animation: breathe 4.8s ease-in-out infinite;
}
.gate-lines {
  position: absolute;
  inset: 0;
  z-index: 9;
}
.gate-lines span {
  position: absolute;
  top: -40px;
  bottom: -40px;
  width: 34px;
  background: rgba(22,20,15,.88);
  transform: rotate(8deg);
  animation: gate 4.8s cubic-bezier(.64,0,.24,1) infinite;
}
.gate-lines span:nth-child(1) { left: 528px; animation-delay: 0s; }
.gate-lines span:nth-child(2) { left: 614px; animation-delay: .1s; }
.gate-lines span:nth-child(3) { left: 700px; animation-delay: .2s; }
.gate-lines span:nth-child(4) { left: 786px; animation-delay: .3s; }
.scanline {
  position: absolute;
  left: -10%;
  right: -10%;
  top: 630px;
  height: 9px;
  z-index: 11;
  background: var(--red);
  opacity: .82;
  animation: scan 3.6s ease-in-out infinite;
}
.gate-headline {
  left: 76px;
  bottom: 265px;
  width: 760px;
  font-size: 122px;
}
.vertical-mark {
  position: absolute;
  right: 74px;
  top: 705px;
  z-index: 13;
  writing-mode: vertical-rl;
  font: 900 62px/1 "Hiragino Mincho ProN", "Yu Mincho", serif;
  letter-spacing: .18em;
  color: var(--red);
}
.cover-gpt_typerain {
  background: #090a0e;
  color: #f4f2ec;
}
.rain-field {
  position: absolute;
  inset: 0;
  display: grid;
  grid-template-columns: repeat(7, 1fr);
  opacity: .9;
}
.rain-col {
  writing-mode: vertical-rl;
  text-orientation: upright;
  white-space: nowrap;
  font: 900 52px/1.9 "Hiragino Sans", "Yu Gothic", sans-serif;
  color: rgba(244,242,236,.18);
  animation: vtick 14s linear infinite;
}
.rain-2, .rain-5 { color: rgba(225,57,45,.72); animation-name: vtick-r; animation-duration: 10s; }
.rain-3, .rain-6 { color: rgba(244,242,236,.42); animation-duration: 17s; }
.rain-4 { animation-name: vtick-r; animation-duration: 12s; }
.red-sash {
  position: absolute;
  left: -90px;
  right: -90px;
  top: 510px;
  z-index: 8;
  transform: rotate(-6deg);
  background: var(--red-2);
  padding: 42px 0 50px;
  text-align: center;
  box-shadow: 0 36px 70px rgba(0,0,0,.48);
}
.red-sash span {
  font: 950 178px/.92 "Hiragino Sans", "Yu Gothic", sans-serif;
  color: #fff;
  text-shadow: 8px 8px 0 #7d1712;
}
.typerain-headline {
  left: 70px;
  right: 70px;
  bottom: 210px;
  font-size: 98px;
  color: #f4f2ec;
}
.dark-top, .dark-bottom { color: rgba(244,242,236,.76); }
.cover-fugu_call {
  background: #f1eadc;
}
.kanji-bg {
  position: absolute;
  left: 68px;
  top: 222px;
  z-index: 2;
  font: 900 650px/.8 "Hiragino Mincho ProN", "Yu Mincho", serif;
  color: rgba(22,20,15,.96);
}
.route-map {
  position: absolute;
  inset: 0;
  z-index: 6;
}
.route {
  position: absolute;
  height: 8px;
  background: linear-gradient(90deg, transparent, var(--red), transparent);
  transform-origin: left center;
  animation: route 3.8s ease-in-out infinite;
}
.r1 { left: 575px; top: 390px; width: 350px; transform: rotate(-22deg); }
.r2 { left: 560px; top: 560px; width: 420px; transform: rotate(2deg); animation-delay: .2s; }
.r3 { left: 520px; top: 735px; width: 390px; transform: rotate(26deg); animation-delay: .4s; }
.node {
  position: absolute;
  min-width: 170px;
  padding: 18px 22px;
  border: 4px solid var(--ink);
  border-radius: 999px;
  background: #f7f2e8;
  color: var(--ink);
  text-align: center;
  font: 900 35px/1 ui-sans-serif, system-ui, sans-serif;
  animation: popnode 3.8s ease-in-out infinite;
}
.n1 { right: 70px; top: 298px; }
.n2 { right: 50px; top: 514px; animation-delay: .18s; }
.n3 { right: 94px; top: 732px; animation-delay: .36s; }
.call-headline {
  left: 78px;
  bottom: 202px;
  width: 780px;
  font-size: 118px;
  color: var(--ink);
  text-shadow: none;
}
.call-headline span { color: var(--red); }
.cover-fugu_router {
  background: #07080d;
  color: #f2f7ff;
}
.neon-grid {
  position: absolute;
  inset: 0;
  background:
    linear-gradient(rgba(37,215,242,.08) 2px, transparent 2px),
    linear-gradient(90deg, rgba(37,215,242,.08) 2px, transparent 2px),
    radial-gradient(circle at 70% 25%, rgba(255,74,151,.2), transparent 30%),
    #07080d;
  background-size: 68px 68px, 68px 68px, 100% 100%, 100% 100%;
  animation: bgshift 7s linear infinite;
}
.router-card {
  position: absolute;
  right: 72px;
  top: 184px;
  z-index: 6;
  width: 372px;
  padding: 28px;
  border: 4px solid var(--cyan);
  box-shadow: 0 0 44px rgba(37,215,242,.26), inset 0 0 24px rgba(37,215,242,.14);
  display: grid;
  grid-template-columns: 70px 1fr;
  gap: 14px 18px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  animation: flicker 5.2s steps(1) infinite;
}
.router-card span { color: rgba(242,247,255,.56); }
.router-card b { font-size: 34px; color: #fff; }
.router-card i {
  grid-column: 1 / -1;
  color: var(--cyan);
  font-style: normal;
  font-size: 26px;
  margin-top: -8px;
}
.neon-kicker {
  position: absolute;
  left: 72px;
  top: 82px;
  z-index: 7;
  font: 900 28px/1 ui-monospace, SFMono-Regular, Menlo, monospace;
  letter-spacing: .28em;
  color: var(--cyan);
}
.neon-headline {
  left: 72px;
  bottom: 234px;
  right: 72px;
  font-size: 116px;
  color: #fff;
  text-shadow: 0 0 28px rgba(255,74,151,.28);
  animation: glitch 4.8s linear infinite;
}
.neon-headline span { color: var(--pink); }
.neon-bottom {
  position: absolute;
  left: 72px;
  bottom: 80px;
  z-index: 8;
  font: 900 28px/1.2 "Hiragino Sans", "Yu Gothic", sans-serif;
  color: rgba(242,247,255,.64);
}
.cover-ms_split {
  background: #f6f2e8;
}
.stijl-grid {
  position: absolute;
  inset: 0;
  z-index: 1;
}
.line {
  position: absolute;
  background: #17140f;
}
.line.h1 { left: 0; right: 0; top: 348px; height: 28px; }
.line.h2 { left: 0; right: 0; top: 910px; height: 28px; }
.line.v1 { top: 0; bottom: 0; left: 330px; width: 28px; }
.line.v2 { top: 0; bottom: 0; left: 785px; width: 28px; }
.block {
  position: absolute;
  z-index: 2;
  animation: blockswitch 6s cubic-bezier(.65,0,.35,1) infinite;
}
.block.red { left: 74px; top: 118px; width: 210px; height: 190px; background: var(--red); }
.block.blue { right: 86px; top: 448px; width: 182px; height: 220px; background: var(--blue); animation-delay: .3s; }
.block.yellow { left: 132px; bottom: 128px; width: 178px; height: 148px; background: var(--yellow); animation-delay: .6s; }
.split-word {
  position: absolute;
  z-index: 4;
  top: 415px;
  font: 950 192px/.9 "Hiragino Sans", "Yu Gothic", sans-serif;
  color: rgba(22,20,15,.16);
  writing-mode: vertical-rl;
}
.left-word { left: 86px; }
.right-word { right: 106px; color: rgba(200,74,43,.24); }
.switch-bar {
  position: absolute;
  left: 50%;
  top: 360px;
  z-index: 5;
  width: 30px;
  height: 510px;
  background: var(--red);
  transform: translateX(-50%);
  animation: switchbar 4.8s ease-in-out infinite;
}
.ms-headline {
  left: 74px;
  right: 74px;
  bottom: 190px;
  font-size: 96px;
  z-index: 14;
  padding: 18px 24px 24px;
  background: rgba(246,242,232,.86);
  box-shadow: 16px 16px 0 rgba(200,74,43,.16);
}
.cover-ms_split .topline {
  font-size: 18px;
  letter-spacing: .12em;
  right: 320px;
}
.cover-robo_enso {
  background: #f4f0e5;
}
.enso {
  position: absolute;
  left: 156px;
  top: 170px;
  z-index: 2;
  width: 768px;
  height: 768px;
  color: rgba(22,20,15,.92);
  transform: rotate(118deg);
}
.enso circle {
  stroke-dashoffset: 1740;
  animation: draw 5.8s cubic-bezier(.65,0,.35,1) infinite;
}
.reward-dots span {
  position: absolute;
  z-index: 8;
  width: 52px;
  height: 52px;
  border-radius: 50%;
  background: var(--red);
  animation: dotreward 4s ease-in-out infinite;
}
.reward-dots span:nth-child(1) { left: 330px; top: 402px; }
.reward-dots span:nth-child(2) { left: 595px; top: 365px; animation-delay: .25s; }
.reward-dots span:nth-child(3) { left: 710px; top: 612px; animation-delay: .5s; }
.reward-dots span:nth-child(4) { left: 465px; top: 720px; animation-delay: .75s; }
.robot-arm {
  position: absolute;
  right: 94px;
  top: 216px;
  z-index: 5;
  width: 170px;
  height: 230px;
  border-left: 20px solid var(--ink);
  border-bottom: 20px solid var(--ink);
  transform-origin: 40px 190px;
  animation: arm 4.4s ease-in-out infinite;
}
.robot-arm span {
  position: absolute;
  right: -38px;
  bottom: -42px;
  width: 86px;
  height: 56px;
  border: 16px solid var(--ink);
  border-top: 0;
}
.kanji-reward {
  position: absolute;
  left: 76px;
  top: 356px;
  z-index: 4;
  font: 900 244px/.9 "Hiragino Mincho ProN", "Yu Mincho", serif;
  color: rgba(22,20,15,.1);
}
.robo-headline {
  left: 74px;
  right: 70px;
  bottom: 214px;
  font-size: 104px;
}
.cover-noise_filter {
  background: #090a0d;
  color: #f4f2ec;
}
.noise-ticker {
  position: absolute;
  left: -10%;
  width: 240%;
  white-space: nowrap;
  font: 950 84px/1 ui-monospace, SFMono-Regular, Menlo, monospace;
  letter-spacing: .08em;
  color: rgba(244,242,236,.12);
  animation: ticker 20s linear infinite;
}
.noise-ticker.t1 { top: 220px; }
.noise-ticker.t2 { top: 470px; color: rgba(200,74,43,.3); animation-direction: reverse; animation-duration: 18s; }
.noise-ticker.t3 { top: 720px; animation-duration: 24s; }
.clean-window {
  position: absolute;
  left: 104px;
  top: 258px;
  z-index: 5;
  width: 872px;
  height: 690px;
  background: #f4f2ec;
  box-shadow: 24px 24px 0 var(--red);
  animation: windowcut 5.8s ease-in-out infinite;
}
.noise-headline {
  left: 146px;
  right: 146px;
  top: 410px;
  z-index: 8;
  color: var(--ink);
  font-size: 118px;
}
.cover-issue_wave {
  background: #0d1c3f;
  color: #f4f0e5;
}
.wave-sun {
  position: absolute;
  right: 135px;
  top: 192px;
  width: 282px;
  height: 282px;
  border-radius: 50%;
  background: var(--red);
  box-shadow: 0 0 70px rgba(200,74,43,.45);
  animation: sunrise 7s ease-in-out infinite;
}
.wave-stack {
  position: absolute;
  left: -110px;
  right: -110px;
  top: 426px;
  bottom: -50px;
  z-index: 3;
}
.wave-stack span {
  display: block;
  height: 168px;
  margin-top: -82px;
  background-image: radial-gradient(circle at 108px 170px,#172957 0 22px,#8fa4d8 22px 30px,#172957 30px 52px,#8fa4d8 52px 60px,#172957 60px 82px,#8fa4d8 82px 90px,#172957 90px 106px,transparent 107px);
  background-size: 216px 168px;
  animation: wave 15s linear infinite;
}
.wave-stack span:nth-child(even) {
  animation-direction: reverse;
  animation-duration: 19s;
}
.story-chips {
  position: absolute;
  left: 76px;
  right: 76px;
  top: 280px;
  z-index: 7;
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
}
.story-chips b {
  padding: 12px 18px;
  border: 3px solid rgba(244,240,229,.8);
  border-radius: 999px;
  background: rgba(13,28,63,.72);
  color: #f4f0e5;
  font: 900 28px/1 ui-monospace, SFMono-Regular, Menlo, monospace;
  animation: chip 4.8s ease-in-out infinite;
}
.story-chips b:nth-child(2) { animation-delay: .15s; }
.story-chips b:nth-child(3) { animation-delay: .3s; }
.story-chips b:nth-child(4) { animation-delay: .45s; }
.wave-top, .wave-bottom { color: rgba(244,240,229,.74); }
.wave-headline {
  left: 76px;
  right: 76px;
  top: 568px;
  z-index: 9;
  font-size: 118px;
  color: #f4f0e5;
  text-shadow: 0 8px 0 rgba(0,0,0,.18);
}
.wave-headline span { color: #f7cd5a; }
@keyframes breathe {
  0%, 100% { transform: scale(.94); }
  50% { transform: scale(1.08); }
}
@keyframes gate {
  0%, 16%, 100% { transform: translateX(520px) rotate(8deg); }
  38%, 82% { transform: translateX(0) rotate(8deg); }
}
@keyframes scan {
  0%, 12%, 100% { transform: translateY(-260px); opacity: 0; }
  28%, 76% { opacity: .86; }
  84% { transform: translateY(330px); opacity: 0; }
}
@keyframes vtick {
  from { transform: translateY(0); }
  to { transform: translateY(-50%); }
}
@keyframes vtick-r {
  from { transform: translateY(-50%); }
  to { transform: translateY(0); }
}
@keyframes route {
  0%, 16%, 100% { opacity: 0; clip-path: inset(0 100% 0 0); }
  34%, 76% { opacity: 1; clip-path: inset(0 0 0 0); }
}
@keyframes popnode {
  0%, 20%, 100% { transform: scale(.86); opacity: .66; }
  42%, 78% { transform: scale(1); opacity: 1; }
}
@keyframes bgshift {
  from { background-position: 0 0, 0 0, 0 0, 0 0; }
  to { background-position: 68px 68px, 68px 68px, 0 0, 0 0; }
}
@keyframes flicker {
  0%, 8%, 18%, 100% { opacity: 1; }
  10%, 12% { opacity: .45; }
  60%, 62% { opacity: .65; }
}
@keyframes glitch {
  0%, 92%, 100% { transform: translate(0,0); }
  94% { transform: translate(-12px,5px); }
  97% { transform: translate(9px,-4px); }
}
@keyframes blockswitch {
  0%, 12%, 88%, 100% { transform: translate(0,0); }
  46%, 58% { transform: translate(405px, 120px); }
}
@keyframes switchbar {
  0%, 100% { transform: translateX(-50%) scaleY(.14); opacity: .2; }
  24%, 70% { transform: translateX(-50%) scaleY(1); opacity: 1; }
}
@keyframes draw {
  0%, 10%, 100% { stroke-dashoffset: 1740; opacity: .45; }
  48%, 82% { stroke-dashoffset: 0; opacity: 1; }
}
@keyframes dotreward {
  0%, 18%, 100% { transform: scale(.2); opacity: 0; }
  36%, 74% { transform: scale(1); opacity: 1; }
}
@keyframes arm {
  0%, 100% { transform: rotate(-8deg); }
  42%, 62% { transform: rotate(12deg); }
}
@keyframes ticker {
  from { transform: translateX(0); }
  to { transform: translateX(-50%); }
}
@keyframes windowcut {
  0%, 100% { transform: translateY(28px) scale(.96); }
  44%, 72% { transform: translateY(0) scale(1); }
}
@keyframes sunrise {
  0%, 12%, 100% { transform: translateY(210px); }
  48%, 82% { transform: translateY(0); }
}
@keyframes wave {
  from { background-position-x: 0; }
  to { background-position-x: -216px; }
}
@keyframes chip {
  0%, 18%, 100% { transform: translateY(14px); opacity: .55; }
  36%, 76% { transform: translateY(0); opacity: 1; }
}
@keyframes brandPulse {
  0%, 100% { transform: translateY(0) scale(1); }
  28%, 64% { transform: translateY(-4px) scale(1.04); }
}
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation-duration: .001ms !important; animation-iteration-count: 1 !important; }
}
@media (max-width: 720px) {
  .sheet { padding: 28px 16px 40px; }
  .principles { grid-template-columns: 1fr; }
  .grid { grid-template-columns: 1fr; gap: 28px; }
  .frame { width: 360px; height: 450px; }
  .cover { transform: scale(.333333); }
}
"""


EXPORT_CSS = """
body.single-export {
  width: 1080px;
  height: 1350px;
  margin: 0;
  overflow: hidden;
  background: #0b0c12;
}
body.single-export .export-stage {
  width: 1080px;
  height: 1350px;
  overflow: hidden;
}
body.single-export .cover {
  transform: none;
  transform-origin: initial;
}
"""


CAPTURE_SCRIPT = """
<script>
window.__coverCaptureReady = false;
window.__setCoverCaptureTime = (seconds) => {
  const captureTime = Math.max(0, Number(seconds) || 0) * 1000;
  const animations = document.getAnimations({ subtree: true });
  for (const animation of animations) {
    animation.pause();
    animation.currentTime = captureTime;
  }
  return animations.length;
};
(async () => {
  if (document.fonts && document.fonts.ready) {
    await document.fonts.ready;
  }
  window.__setCoverCaptureTime(0);
  window.__coverCaptureReady = true;
})();
</script>
"""


def media_basename(candidate: dict[str, Any]) -> str:
    return f"{int(candidate['rank']):02d}_{candidate['id']}"


def candidate_media_paths(candidate: dict[str, Any]) -> dict[str, Path]:
    base = MEDIA_DIR / media_basename(candidate)
    return {
        "html": base.with_suffix(".html"),
        "poster": base.with_suffix(".png"),
        "mp4": base.with_suffix(".mp4"),
    }


def path_from_root(path: Path) -> str:
    return str(path.relative_to(ROOT))


def candidate_media_refs(candidate: dict[str, Any]) -> dict[str, str]:
    paths = candidate_media_paths(candidate)
    return {key: path_from_root(path) for key, path in paths.items()}


def candidate_for_export(candidate: dict[str, Any]) -> dict[str, Any]:
    if candidate["story_id"] == "issue":
        source = {
            "id": "issue",
            "source_url": "https://www.deeplearning.ai/the-batch/issue-360",
            "title_en": "OpenAI's GPT-5.6 Family, New Ways to Train Robots, Models Invoking Models",
            "title_ja": "Issue 360全体",
            "summary_ja": "GPT-5.6、ロボット学習、モデル・オーケストレーションを中心にした週次まとめです。",
        }
    else:
        source = story(candidate["story_id"])
    return {
        key: candidate[key]
        for key in ("id", "rank", "label", "style", "hook", "kicker", "swipe", "why")
    } | {
        "story": source,
        "media": candidate_media_refs(candidate),
    }


def render_html() -> str:
    cards = []
    for candidate in sorted(CANDIDATES, key=lambda item: item["rank"]):
        source = candidate_for_export(candidate)["story"]
        cards.append(f"""
        <article class="option" id="{esc(candidate['id'])}">
          <div class="option-meta">
            <span class="rank">#{esc(candidate['rank'])}</span><b>{esc(candidate['label'])}</b>
            <p>{esc(source['title_ja'])} / {esc(candidate['style'])}<br>{esc(candidate['why'])}</p>
          </div>
          <div class="frame">
            <section class="cover cover-{esc(candidate['id'])}" aria-label="{esc(candidate['hook'])}">
              {cover_markup(candidate)}
              {brand_logo_markup()}
            </section>
          </div>
        </article>
        """)
    return f"""<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AI Brief JP / Issue 360 Animated Cover Studies</title>
  <style>{CSS}</style>
</head>
<body>
  <main class="sheet">
    <header class="sheet-head">
      <h1>AI Brief JP / The Batch Issue 360 animated cover studies</h1>
      <p>Source issue verified as DeepLearning.AI The Batch issue 360, published July 3, 2026. These are scroll-stop cover candidates, not final carousels: each frame is a loop-ready 1080 x 1350 Japanese Instagram cover.</p>
      <div class="principles">
        <span>1. まず違和感: 「なぜ政府から？」「AIがAIを呼ぶ」など、説明前に引っかかる構文。</span>
        <span>2. 1秒で読める: 主フックは25字前後、補足は下部に逃がす。</span>
        <span>3. 信頼の抑制: 煽らず、出典と固有名詞でニュース感を担保。</span>
        <span>4. 日本語の身体性: 縦書き、大漢字、余白、判子色でローカルな視線を作る。</span>
        <span>5. 動きに意味: ゲート、経路、分割、円相など、ニュースの構造を動かす。</span>
      </div>
    </header>
    <section class="grid">
      {''.join(cards)}
    </section>
  </main>
</body>
</html>
"""


def render_single_cover_html(candidate: dict[str, Any]) -> str:
    document_title = str(
        candidate.get("document_title")
        or f"AI Brief JP Issue 360 / #{candidate['rank']} {candidate['id']}"
    )
    return f"""<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width={COVER_WIDTH}, initial-scale=1">
  <title>{esc(document_title)}</title>
  <style>{CSS}</style>
  <style>{EXPORT_CSS}</style>
</head>
<body class="single-export">
  <main class="export-stage">
    <section class="cover cover-{esc(candidate['id'])}" aria-label="{esc(candidate['hook'])}">
      {cover_markup(candidate)}
      {brand_logo_markup()}
    </section>
  </main>
  {CAPTURE_SCRIPT}
</body>
</html>
"""


def selected_candidates(candidate_ids: list[str] | None) -> list[dict[str, Any]]:
    candidates = sorted(CANDIDATES, key=lambda item: item["rank"])
    if not candidate_ids:
        return candidates
    wanted = set(candidate_ids)
    known = {candidate["id"] for candidate in CANDIDATES}
    unknown = sorted(wanted - known)
    if unknown:
        raise SystemExit(f"Unknown candidate id(s): {', '.join(unknown)}")
    return [candidate for candidate in candidates if candidate["id"] in wanted]


def launch_chromium(playwright: Any) -> Any:
    try:
        return playwright.chromium.launch()
    except Exception:
        return playwright.chromium.launch(channel="chrome")


def run_ffmpeg(command: list[str]) -> None:
    try:
        subprocess.run(command, check=True, text=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        if stderr:
            stderr = "\n" + stderr[-2000:]
        raise SystemExit(f"ffmpeg failed while rendering cover media.{stderr}") from exc


def render_individual_media(
    candidates: list[dict[str, Any]],
    *,
    duration_seconds: float,
    fps: int,
    render_mp4: bool,
) -> list[dict[str, Any]]:
    try:
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError as exc:
        raise SystemExit("playwright is required to render individual cover media") from exc

    ffmpeg = shutil.which("ffmpeg")
    if render_mp4 and not ffmpeg:
        raise SystemExit("ffmpeg is required to render individual cover MP4s")

    duration_seconds = max(0.1, float(duration_seconds))
    fps = max(1, int(fps))
    frame_count = max(2, int(round(duration_seconds * fps)))
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)

    outputs: list[dict[str, Any]] = []
    for candidate in candidates:
        paths = candidate_media_paths(candidate)
        paths["html"].write_text(render_single_cover_html(candidate), encoding="utf-8")

    print(
        f"[media] rendering {len(candidates)} cover(s) at "
        f"{COVER_WIDTH}x{COVER_HEIGHT}, {duration_seconds:g}s, {fps}fps"
    )
    try:
        with sync_playwright() as p:
            browser = launch_chromium(p)
            for index, candidate in enumerate(candidates, start=1):
                paths = candidate_media_paths(candidate)
                print(f"[media] {index}/{len(candidates)} {candidate['id']} -> {paths['poster'].name}")
                page = browser.new_page(
                    viewport={"width": COVER_WIDTH, "height": COVER_HEIGHT},
                    device_scale_factor=1,
                )
                page.goto(paths["html"].resolve().as_uri())
                page.wait_for_load_state("networkidle")
                page.evaluate(
                    "() => (document.fonts && document.fonts.ready ? "
                    "document.fonts.ready.then(() => true) : true)"
                )
                page.wait_for_function("() => window.__coverCaptureReady === true")
                cover = page.locator(".cover")

                page.evaluate(
                    "(seconds) => window.__setCoverCaptureTime(seconds)",
                    min(POSTER_CAPTURE_SECONDS, duration_seconds),
                )
                cover.screenshot(path=str(paths["poster"]), timeout=15000)

                if render_mp4:
                    print(f"[media] {candidate['id']} -> {paths['mp4'].name}")
                    with tempfile.TemporaryDirectory(prefix=f"{media_basename(candidate)}_frames_") as tmp:
                        frames_dir = Path(tmp)
                        for frame_index in range(frame_count):
                            if frame_index == 0 or (frame_index + 1) % fps == 0 or frame_index == frame_count - 1:
                                print(
                                    f"[media] {candidate['id']} frame "
                                    f"{frame_index + 1}/{frame_count}",
                                    flush=True,
                                )
                            page.evaluate(
                                "(seconds) => window.__setCoverCaptureTime(seconds)",
                                frame_index / fps,
                            )
                            cover.screenshot(
                                path=str(frames_dir / f"frame_{frame_index:04d}.png"),
                                timeout=15000,
                            )
                        run_ffmpeg(
                            [
                                str(ffmpeg),
                                "-y",
                                "-hide_banner",
                                "-loglevel",
                                "error",
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
                                str(paths["mp4"]),
                            ]
                        )
                page.close()

                output = {
                    "id": candidate["id"],
                    "rank": candidate["rank"],
                    "html": path_from_root(paths["html"]),
                    "poster": path_from_root(paths["poster"]),
                }
                if render_mp4:
                    output["mp4"] = path_from_root(paths["mp4"])
                outputs.append(output)
            browser.close()
    except SystemExit:
        raise
    except Exception as exc:
        raise SystemExit(
            "could not render individual cover media. If this is a fresh setup, run "
            "`uv run python -m playwright install chromium` once."
        ) from exc

    MEDIA_MANIFEST_PATH.write_text(
        json.dumps(
            {
                "coverSize": {"width": COVER_WIDTH, "height": COVER_HEIGHT},
                "durationSeconds": duration_seconds,
                "fps": fps,
                "posterCaptureSeconds": min(POSTER_CAPTURE_SECONDS, duration_seconds),
                "renderedMp4": render_mp4,
                "candidates": outputs,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return outputs


def render_markdown() -> str:
    lines = [
        "# AI Brief JP Issue 360: Japanese Cover Strategy",
        "",
        "Source issue: DeepLearning.AI The Batch issue 360, published July 3, 2026.",
        "",
        "## Stop-Scroll Heuristics For Japanese AI Instagram",
        "",
        "- Start with a concrete contradiction or access gap, not a generic benefit.",
        "- Keep the cover hook short enough to parse in one glance on mobile.",
        "- Let one motion metaphor explain the story structure: gate, route, split, loop, filter.",
        "- Use Japanese visual cues deliberately: vertical type, large kanji, red seal/sun, controlled whitespace.",
        "- Sound like a working engineer: restrained, sourced, slightly witty, never hype.",
        "",
        "## Story Translations",
        "",
    ]
    for item in STORIES:
        lines.extend(
            [
                f"### {item['title_ja']}",
                "",
                f"- English source title: {item['title_en']}",
                f"- Source: {item['source_url']}",
                f"- Japanese summary: {item['summary_ja']}",
                "",
            ]
        )
    lines.extend(["## Ranked Cover Candidates", ""])
    for candidate in sorted(CANDIDATES, key=lambda item: item["rank"]):
        exported = candidate_for_export(candidate)
        lines.extend(
            [
                f"### #{candidate['rank']} {candidate['hook']}",
                "",
                f"- Story: {exported['story']['title_ja']}",
                f"- Style: {candidate['style']}",
                f"- Kicker: {candidate['kicker']}",
                f"- Swipe line: {candidate['swipe']}",
                f"- Rationale: {candidate['why']}",
                "",
            ]
        )
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build AI Brief JP Issue 360 cover studies and individual media files."
    )
    media_group = parser.add_mutually_exclusive_group()
    media_group.add_argument(
        "--render-media",
        dest="render_media",
        action="store_true",
        default=True,
        help="Render per-candidate HTML, PNG posters, and MP4s. This is the default.",
    )
    media_group.add_argument(
        "--no-media",
        dest="render_media",
        action="store_false",
        help="Only write the preview sheet, JSON, and markdown strategy notes.",
    )
    parser.add_argument(
        "--poster-only",
        action="store_true",
        help="Write per-candidate HTML and PNG posters without composing MP4 files.",
    )
    parser.add_argument(
        "--candidate",
        action="append",
        help="Limit media rendering to one candidate id. Repeat to render multiple candidates.",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=DEFAULT_MEDIA_DURATION_SECONDS,
        help=f"MP4 duration in seconds. Default: {DEFAULT_MEDIA_DURATION_SECONDS:g}.",
    )
    parser.add_argument(
        "--fps",
        type=int,
        default=DEFAULT_MEDIA_FPS,
        help=f"MP4 frame rate. Default: {DEFAULT_MEDIA_FPS}.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    HTML_PATH.write_text(render_html(), encoding="utf-8")
    JSON_PATH.write_text(
        json.dumps(
            {
                "issue": {
                    "title": "OpenAI's GPT-5.6 Family, New Ways to Train Robots, Models Invoking Models",
                    "issue": "360",
                    "publishedDate": "2026-07-03",
                    "url": "https://www.deeplearning.ai/the-batch/issue-360",
                    "tagUrl": "https://www.deeplearning.ai/the-batch/tag/jul-03-2026",
                },
                "stories": STORIES,
                "candidates": [candidate_for_export(candidate) for candidate in sorted(CANDIDATES, key=lambda item: item["rank"])],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    MD_PATH.write_text(render_markdown(), encoding="utf-8")
    media_outputs: list[dict[str, Any]] = []
    if args.render_media:
        media_outputs = render_individual_media(
            selected_candidates(args.candidate),
            duration_seconds=args.duration,
            fps=args.fps,
            render_mp4=not args.poster_only,
        )
    print(
        json.dumps(
            {
                "html": str(HTML_PATH),
                "json": str(JSON_PATH),
                "markdown": str(MD_PATH),
                "mediaManifest": str(MEDIA_MANIFEST_PATH) if media_outputs else None,
                "media": media_outputs,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
