#!/usr/bin/env python3
"""Generate the per-channel brand logo assets (channels/<id>/logo.png).

These are committed brand assets used as the reel avatar/mark. Re-run after a brand
tweak, or drop in a hand-made ``logo.png`` to override. The marks reuse the channel
"ink on light" palette and the embedded Archivo face so they render offline.

    uv run python make_channel_logos.py
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent
FONTS = ROOT / "assets" / "archivo.css"
SIZE = 1024

# Cream / ink / accent, shared by both channels (see channels/*/channel.json).
BG = "#F4F2EC"
INK = "#16140F"
TAUPE = "#9A9182"
ACCENT = "#C0552E"

# Each mark is a two-line wordmark: a small spaced top line and a heavy lower line
# closed by the accent dot, echoing the AI Brief reference.
LOGOS = {
    "aibrief_jp": {"top": "A I", "main": "Brief"},
    "vibecodersph": {"top": "VIBE", "main": "Coders"},
}


def logo_html(top: str, main: str) -> str:
    font_css = FONTS.read_text()
    # Scale the heavy line down for longer words so the mark + dot stay inside the coin.
    main_size = 290 if len(main) <= 5 else int(290 * 5 / len(main))
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
{font_css}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: 'Archivo', sans-serif; }}
.coin {{
  width: {SIZE}px; height: {SIZE}px;
  background: {BG};
  display: flex; flex-direction: column;
  align-items: center; justify-content: center;
}}
.top {{
  font-size: 132px; font-weight: 700; letter-spacing: 0.30em;
  color: {TAUPE}; text-indent: 0.30em; margin-bottom: 8px;
}}
.main {{
  font-size: {main_size}px; font-weight: 800; letter-spacing: -0.02em;
  color: {INK}; line-height: 0.9; display: flex; align-items: flex-end;
}}
.dot {{
  width: 96px; height: 96px; border-radius: 50%;
  background: {ACCENT}; margin-left: 18px; margin-bottom: 14px;
}}
</style></head>
<body>
<div class="coin">
  <div class="top">{top}</div>
  <div class="main">{main}<span class="dot"></span></div>
</div>
</body></html>
"""


def render(top: str, main: str, out_path: Path) -> None:
    from playwright.sync_api import sync_playwright

    html_path = out_path.parent / "_logo.html"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(logo_html(top, main))
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        page = browser.new_page(viewport={"width": SIZE, "height": SIZE}, device_scale_factor=1)
        page.goto(html_path.as_uri())
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(200)
        page.locator(".coin").screenshot(path=str(out_path))
        browser.close()
    html_path.unlink(missing_ok=True)
    print(f"wrote {out_path}")


def main() -> int:
    for channel_id, parts in LOGOS.items():
        render(parts["top"], parts["main"], ROOT / "channels" / channel_id / "logo.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
