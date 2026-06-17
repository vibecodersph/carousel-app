#!/usr/bin/env python3
"""Generate vibecodersph-branded cover art for carousel slides.

Supports two backends:
  1. OpenAI GPT Image 2.0 (default) — requires OPENAI_API_KEY
  2. xAI Grok Imagine — requires XAI_API_KEY or Hermes xAI OAuth token

Uses brand.json colors/style for prompt consistency.

Usage:
    uv run python generate_cover.py "Fable 5 changes everything"
    uv run python generate_cover.py "Fable 5 changes everything" --provider gemini --model nano-banana-pro
    uv run python generate_cover.py "Why reasoning models win" --provider xai
    uv run python generate_cover.py "The prompt" --out out/my_cover.png --style abstract
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
BRAND_PATH = ROOT / "brand.json"
OUT = ROOT / "out"
DEFAULT_OPENAI_IMAGE_MODEL = "gpt-image-2"
DEFAULT_GEMINI_IMAGE_MODEL = "gemini-3.1-flash-image"
GEMINI_API_ROOT = "https://generativelanguage.googleapis.com/v1beta"
IMAGE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
GEMINI_MODEL_ALIASES = {
    "nano-banana": "gemini-2.5-flash-image",
    "nanobanana": "gemini-2.5-flash-image",
    "nano-banana-2": "gemini-3.1-flash-image",
    "nanobanana-2": "gemini-3.1-flash-image",
    "nanobanana2": "gemini-3.1-flash-image",
    "nano-banana-pro": "gemini-3-pro-image",
    "nanobanana-pro": "gemini-3-pro-image",
    "nanobananapro": "gemini-3-pro-image",
}


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
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


def load_local_env() -> None:
    load_env_file(ROOT / ".env")
    load_env_file(Path.home() / ".hermes" / ".env")


def load_brand() -> dict[str, Any]:
    """Load brand config for prompt guidance."""
    if BRAND_PATH.exists():
        return json.loads(BRAND_PATH.read_text())
    return {}


def build_prompt(topic: str, style: str) -> str:
    """Build an image generation prompt from topic + vibecodersph brand.

    The vibecodersph aesthetic is "Whiteout / ink on light" — cream paper
    background with dark ink typography and rust/terracotta accents.
    """
    brand = load_brand()
    colors = brand.get("colors", {})
    bg = colors.get("bg", "#F4F2EC")
    primary = colors.get("primary", "#C0552E")
    fg = colors.get("fg", "#16140F")

    style_direction = {
        "abstract": "abstract geometric composition, editorial magazine aesthetic",
        "typographic": "bold typographic layout on cream paper, ink-bleed texture",
        "minimal": "minimalist with dramatic negative space, single focal element",
        "illustrative": "editorial illustration, ink wash technique, grain texture",
        "photo": "cinematic photography with cream matte and grain overlay",
    }.get(style, style)

    return (
        f"Square editorial cover art for an Instagram carousel about '{topic}'. "
        f"Cream/off-white paper background ({bg}). "
        f"Dark ink ({fg}) and rust/terracotta accent color ({primary}). "
        f"{style_direction}. "
        f"High-end publication quality, 1080x1080 composition. "
        f"No text, no logos, no watermarks. "
        f"The image should feel like a premium print magazine cover — "
        f"textured paper, editorial gravitas, intellectual but not cold."
    )


def add_ceo_context(prompt: str, ceo: str | None, company: str | None) -> str:
    ceo = (ceo or "").strip()
    company = (company or "").strip()
    if not ceo:
        return prompt
    ceo_line = f"{ceo} of {company}" if company else ceo
    company_line = f" The company context is {company}, but do not show logos or brand marks." if company else ""
    return (
        f"{prompt} Add a tasteful editorial portrait element of the CEO: {ceo_line}."
        f"{company_line} Keep the CEO portrait integrated into the same premium print magazine cover style, "
        "not a corporate headshot or office photo. The topic should remain the main concept. "
        "Do not include UI panels, dashboards, code snippets, charts, labels, tiny interface text, "
        "or any marks that look like readable text."
    )


def openai_api_key() -> str | None:
    load_local_env()
    return os.environ.get("OPENAI_API_KEY") or os.environ.get("VCPH_OPENAI_API_KEY")


def gemini_api_key() -> str | None:
    load_local_env()
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")


def normalize_model_alias(model: str | None) -> str:
    if not model:
        return ""
    return re.sub(r"[\s_]+", "-", model.strip().lower())


def resolve_gemini_model(model: str | None) -> str:
    model = model or os.environ.get("GEMINI_IMAGE_MODEL") or DEFAULT_GEMINI_IMAGE_MODEL
    return GEMINI_MODEL_ALIASES.get(normalize_model_alias(model), model)


def aspect_ratio_from_size(size: str) -> str:
    match = re.match(r"^(\d+)x(\d+)$", size.strip().lower())
    if not match:
        return "1:1"
    width, height = int(match.group(1)), int(match.group(2))
    if width == height:
        return "1:1"
    from math import gcd

    divisor = gcd(width, height)
    return f"{width // divisor}:{height // divisor}"


def gemini_image_size_from_size(size: str) -> str:
    match = re.match(r"^(\d+)x(\d+)$", size.strip().lower())
    if not match:
        return "1K"
    longest = max(int(match.group(1)), int(match.group(2)))
    if longest >= 4096:
        return "4K"
    if longest >= 2048:
        return "2K"
    return "1K"


def image_extension(mime_type: str) -> str:
    return IMAGE_EXTENSIONS.get(mime_type.split(";", 1)[0].strip().lower(), ".png")


def with_image_extension(path: Path, mime_type: str) -> Path:
    ext = image_extension(mime_type)
    if path.suffix.lower() in IMAGE_EXTENSIONS.values() and path.suffix.lower() != ext:
        return path.with_suffix(ext)
    return path


def generate_openai(
    prompt: str,
    out_path: Path,
    *,
    model: str | None = None,
    size: str = "1024x1024",
    quality: str | None = None,
) -> Path:
    """Generate image via OpenAI GPT Image 2.0 (chat-based image output)."""
    api_key = openai_api_key()
    if not api_key:
        raise SystemExit(
            "OPENAI_API_KEY not set. Set it in ~/.hermes/.env or export it."
        )

    try:
        from openai import OpenAI
    except ImportError:
        raise SystemExit(
            "openai package not installed. Run: uv add openai"
        )

    client = OpenAI(api_key=api_key)
    model = model or os.environ.get("OPENAI_IMAGE_MODEL") or DEFAULT_OPENAI_IMAGE_MODEL
    request: dict[str, object] = {
        "model": model,
        "prompt": prompt,
        "n": 1,
        "size": size,
    }
    if quality:
        request["quality"] = quality

    print(f"Generating cover art via {model}...")
    response = client.images.generate(**request)

    # GPT Image 2 returns image data as b64_json (primary) or url
    image_data = response.data[0]
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if hasattr(image_data, 'b64_json') and image_data.b64_json:
        import base64
        out_path.write_bytes(base64.b64decode(image_data.b64_json))
        print(f"Saved {out_path}")
        return out_path
    elif hasattr(image_data, 'url') and image_data.url:
        import urllib.request
        urllib.request.urlretrieve(image_data.url, str(out_path))
        print(f"Saved {out_path}")
        return out_path

    raise SystemExit("GPT Image 2 returned no image data")


def gemini_parts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        return parts
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("content")
        if not isinstance(content, dict):
            continue
        raw_parts = content.get("parts")
        if isinstance(raw_parts, list):
            parts.extend(part for part in raw_parts if isinstance(part, dict))
    return parts


def extract_gemini_image(payload: dict[str, Any]) -> tuple[bytes, str] | None:
    for part in gemini_parts(payload):
        inline_data = part.get("inlineData") or part.get("inline_data")
        if not isinstance(inline_data, dict):
            continue
        data = inline_data.get("data")
        if not isinstance(data, str):
            continue
        mime_type = inline_data.get("mimeType") or inline_data.get("mime_type") or "image/png"
        try:
            return base64.b64decode(data), str(mime_type)
        except (ValueError, TypeError):
            continue
    return None


def gemini_payloads(prompt: str, model: str, aspect_ratio: str, image_size: str) -> list[dict[str, Any]]:
    image_config: dict[str, str] = {"aspectRatio": aspect_ratio}
    if model != "gemini-2.5-flash-image":
        image_config["imageSize"] = image_size
    configured = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseModalities": ["TEXT", "IMAGE"],
            "imageConfig": image_config,
        },
    }
    base = {"contents": [{"parts": [{"text": prompt}]}]}
    return [configured, base]


def generate_gemini(
    prompt: str,
    out_path: Path,
    *,
    model: str | None = None,
    aspect_ratio: str = "1:1",
    image_size: str = "1K",
) -> Path:
    """Generate image via Gemini Nano Banana image models."""
    api_key = gemini_api_key()
    if not api_key:
        raise SystemExit("GOOGLE_API_KEY or GEMINI_API_KEY not set. Add it to .env or export it.")

    model = resolve_gemini_model(model)
    print(f"Generating cover art via {model}...")
    for payload in gemini_payloads(prompt, model, aspect_ratio, image_size):
        req = Request(
            f"{GEMINI_API_ROOT}/models/{model}:generateContent",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": api_key,
                "User-Agent": "carousel-app/1.0",
            },
        )
        try:
            with urlopen(req, timeout=120) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = ""
            try:
                error_payload = json.loads(exc.read().decode("utf-8"))
                error = error_payload.get("error")
                if isinstance(error, dict) and isinstance(error.get("message"), str):
                    detail = f": {error['message'][:180]}"
            except (OSError, json.JSONDecodeError):
                pass
            print(f"Gemini {model} returned HTTP {exc.code}{detail}")
            continue
        except (OSError, URLError, json.JSONDecodeError) as exc:
            print(f"Gemini {model} request failed: {exc}")
            continue

        image = extract_gemini_image(result)
        if not image:
            continue
        data, mime_type = image
        out_path = with_image_extension(out_path, mime_type)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(data)
        print(f"Saved {out_path} ({mime_type})")
        return out_path

    raise SystemExit(f"Gemini {model} returned no image data")


def _get_xai_token() -> str:
    """Get xAI bearer token from env or Hermes OAuth store."""
    # Prefer explicit API key
    api_key = os.environ.get("XAI_API_KEY")
    if api_key:
        return api_key

    # Try Hermes OAuth token via hermes CLI
    try:
        result = subprocess.run(
            ["hermes", "auth", "get-token", "xai-oauth"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            token_data = json.loads(result.stdout)
            return token_data.get("access_token", "")
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        pass

    raise SystemExit(
        "No xAI credentials found. Set XAI_API_KEY or run: hermes auth add xai-oauth"
    )


def generate_xai(prompt: str, out_path: Path) -> Path:
    """Generate image via xAI Grok Imagine API."""
    token = _get_xai_token()

    # xAI Images API — using the OpenAI-compatible endpoint
    try:
        from openai import OpenAI
    except ImportError:
        raise SystemExit(
            "openai package not installed. Run: uv add openai"
        )

    client = OpenAI(
        api_key=token,
        base_url="https://api.x.ai/v1",
    )

    print(f"Generating cover art via Grok Imagine...")
    response = client.images.generate(
        model="grok-imagine-image",
        prompt=prompt,
        n=1,
    )

    image_url = response.data[0].url
    if not image_url:
        raise SystemExit("xAI returned no image URL")

    import urllib.request

    out_path.parent.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve(image_url, str(out_path))
    print(f"Saved {out_path}")
    return out_path


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Generate vibecodersph-branded cover art for carousel slides"
    )
    ap.add_argument("topic", help="The carousel topic/headline")
    ap.add_argument(
        "--provider",
        choices=["openai", "gemini", "xai"],
        default="openai",
        help="Image generation backend (default: openai)",
    )
    ap.add_argument(
        "--model",
        help=(
            "Image model override. Examples: gpt-image-2, nano-banana-pro, "
            "nano-banana-2, gemini-3-pro-image"
        ),
    )
    ap.add_argument(
        "--out",
        "-o",
        type=Path,
        help="Output path (default: out/cover_<topic_slug>.png)",
    )
    ap.add_argument(
        "--style",
        choices=["abstract", "typographic", "minimal", "illustrative", "photo"],
        default="abstract",
        help="Visual style direction (default: abstract)",
    )
    ap.add_argument("--ceo", help="Add a CEO portrait element to the generated cover")
    ap.add_argument("--company", help="Company context for --ceo")
    ap.add_argument(
        "--prompt-only",
        action="store_true",
        help="Print the prompt without generating (for manual use)",
    )
    ap.add_argument(
        "--size",
        default="1024x1024",
        help="Image size for OpenAI, and aspect/quality hint for Gemini (default: 1024x1024)",
    )
    ap.add_argument(
        "--aspect-ratio",
        help="Gemini output aspect ratio override, e.g. 1:1 or 16:9",
    )
    ap.add_argument(
        "--image-size",
        choices=["1K", "2K", "4K"],
        help="Gemini image size override for Gemini 3 image models",
    )
    args = ap.parse_args()

    prompt = add_ceo_context(build_prompt(args.topic, args.style), args.ceo, args.company)

    if args.prompt_only:
        print(prompt)
        return 0

    out_path = args.out or OUT / f"cover_{re.sub(r'[^a-z0-9]+', '_', args.topic.lower()).strip('_')}.png"

    if args.provider == "openai":
        generate_openai(prompt, out_path, model=args.model, size=args.size)
    elif args.provider == "gemini":
        generate_gemini(
            prompt,
            out_path,
            model=args.model,
            aspect_ratio=args.aspect_ratio or aspect_ratio_from_size(args.size),
            image_size=args.image_size or gemini_image_size_from_size(args.size),
        )
    else:
        generate_xai(prompt, out_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
