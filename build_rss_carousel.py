#!/usr/bin/env python3
"""Standalone script to auto-generate carousels from RSS/Atom feeds.

It:
1. Scans article sources configured in config.
2. Filters out duplicates that already exist in candidates database.
3. Ranks the new articles by score.
4. Identifies the highest-scoring article above threshold.
5. Invokes build_article_carousel.py to generate slides.
6. Updates candidates database.
7. Optionally uploads to R2 and posts to Instagram.
"""
from __future__ import annotations

import sys
import os
import argparse
import subprocess
from pathlib import Path

# Add parent directory to path to resolve local imports cleanly.
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from story_scout import (
    load_config,
    load_queue,
    save_queue,
    fetch_article_items,
    score_article_item,
    article_candidate_id,
    feed_item_datetime,
    utc_now,
    record_outcome_event,
    DEFAULT_QUEUE
)

def main() -> int:
    ap = argparse.ArgumentParser(description="Auto-generate carousels from RSS/Atom feeds.")
    ap.add_argument(
        "--config",
        type=Path,
        default=ROOT / "story_sources.json",
        help="Path to config JSON (default: story_sources.json)"
    )
    ap.add_argument(
        "--queue",
        type=Path,
        default=DEFAULT_QUEUE,
        help="Path to candidates queue JSON (default: out/automation/candidates.json)"
    )
    ap.add_argument(
        "--channel",
        default=os.environ.get("CAROUSEL_CHANNEL"),
        help="Active channel ID selecting branding/language/voice"
    )
    ap.add_argument(
        "--min-score",
        type=int,
        help="Override min score to build (default: resolved from config)"
    )
    ap.add_argument(
        "--max-pages",
        type=int,
        default=6,
        help="Maximum article-section slides in the carousel (default: 6)"
    )
    ap.add_argument(
        "--curation-backend",
        default="auto",
        choices=("auto", "gemini", "local"),
        help=" Curation backend: gemini or local heuristics (default: auto)"
    )
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "out" / "rss_carousel",
        help="Target output directory (default: out/rss_carousel)"
    )
    ap.add_argument(
        "--publish",
        action="store_true",
        help="Publish the generated carousel to Instagram"
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Perform dry-run/preview upload only when publishing"
    )
    ap.add_argument(
        "--upload-r2",
        action="store_true",
        help="Upload built assets to Cloudflare R2 when publishing"
    )
    ap.add_argument(
        "--no-title-enrichment",
        action="store_true",
        help="Skip Gemini/OpenAI title enrichment and use raw feed titles"
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=30,
        help="Max RSS feed entries to evaluate per scan (default: 30)"
    )
    args = ap.parse_args()

    # Resolve active channel.
    if args.channel:
        os.environ["CAROUSEL_CHANNEL"] = args.channel

    if not args.config.exists():
        print(f"Error: Config file not found at {args.config}", file=sys.stderr)
        return 1

    config = load_config(args.config)
    sources = config.get("article_sources") or []
    if not sources:
        print("Warning: No 'article_sources' configured in config file. Checking example config.", file=sys.stderr)
        # Attempt to check if example config is fallback.
        example_config = ROOT / "story_sources.example.json"
        if example_config.exists():
            print(f"Reading article sources from example config: {example_config}")
            config = load_config(example_config)
            sources = config.get("article_sources") or []
        
        if not sources:
            print("Error: No article/RSS sources found in either config. Exiting.", file=sys.stderr)
            return 1

    # Determine min_score threshold.
    min_score = args.min_score
    if min_score is None:
        min_score = int(config.get("article_min_score") or config.get("min_score") or 45)

    print(f"[rss] Scanning RSS feeds... min_score threshold: {min_score}")
    
    # Fetch article candidates from feeds.
    items = fetch_article_items(config, limit=args.limit)
    if not items:
        print("[rss] No recent articles found in RSS feeds.")
        return 0

    # Load candidates queue database.
    queue = load_queue(args.queue)
    existing_by_id = {str(c.get("id")): c for c in queue.get("candidates", [])}

    top_item = None
    top_score = 0
    top_reasons = []

    for item in items:
        source_config = item.get("_source_config") or {}
        score, reasons = score_article_item(item, source_config, config)
        if score < min_score:
            continue
        
        cid = article_candidate_id(item["url"])
        previous = existing_by_id.get(cid)
        if previous:
            # Duplication Guarding: Skip if already built, published, previewed, approved, or failed.
            status = previous.get("status")
            if status in {"built", "published", "publish_previewed", "approved", "failed"}:
                print(f"[rss] Skipping duplicate with ID {cid} (Status: {status}): {item['title']}")
                continue

        top_item = item
        top_score = score
        top_reasons = reasons
        break

    if not top_item:
        print("[rss] No new RSS articles passed the scoring filter and min_score threshold.")
        return 0

    cid = article_candidate_id(top_item["url"])
    print(f"[rss] Top candidate selected: {top_item['title']}")
    print(f"      URL:   {top_item['url']}")
    print(f"      ID:    {cid}")
    print(f"      Score: {top_score} ({', '.join(top_reasons)})")

    # Set up target build directory.
    build_dir = args.out_dir / cid
    build_dir.mkdir(parents=True, exist_ok=True)

    # Build the command for build_article_carousel.py.
    build_cmd = [
        sys.executable,
        str(ROOT / "build_article_carousel.py"),
        top_item["url"],
        "--out-dir",
        str(build_dir),
        "--max-pages",
        str(args.max_pages),
        "--min-score",
        str(min_score),
        "--curation-backend",
        args.curation_backend,
    ]
    if args.channel:
        build_cmd.extend(["--channel", args.channel])
    if args.no_title_enrichment:
        build_cmd.append("--no-title-enrichment")

    now = utc_now()
    previous = existing_by_id.get(cid) or {}
    candidate = {
        **previous,
        "id": cid,
        "source_type": "article",
        "status": "approved",
        "score": top_score,
        "score_reasons": top_reasons,
        "source_account": top_item.get("source_name", ""),
        "article": top_item,
        "created_at": previous.get("created_at") or now,
        "updated_at": now,
        "build_started_at": now,
        "build_dir": str(build_dir),
    }
    existing_by_id[cid] = candidate
    queue["candidates"] = list(existing_by_id.values())
    save_queue(args.queue, queue)

    print(f"[rss] Running build script: {' '.join(build_cmd)}")
    build_res = subprocess.run(build_cmd, check=False)
    
    candidate["build_finished_at"] = utc_now()
    candidate["build_returncode"] = build_res.returncode

    if build_res.returncode == 0:
        candidate["status"] = "built"
        candidate["manifest_path"] = str(build_dir / "manifest.json")
        record_outcome_event(candidate, "built")
        print(f"[rss] Build successfully finished. Carousel manifest -> {build_dir}/manifest.json")
    else:
        candidate["status"] = "failed"
        candidate["failure"] = f"build_article_carousel.py exited {build_res.returncode}"
        record_outcome_event(candidate, "build_failed", detail=candidate["failure"])
        save_queue(args.queue, queue)
        print(f"[rss] Error: build_article_carousel.py failed with exit code: {build_res.returncode}", file=sys.stderr)
        return build_res.returncode

    # Optional publishing trigger.
    if args.publish:
        pub_cmd = [
            sys.executable,
            str(ROOT / "instagram_publish.py"),
            str(build_dir / "manifest.json"),
        ]
        if args.dry_run:
            pub_cmd.append("--dry-run")
        if args.upload_r2:
            pub_cmd.append("--upload-r2")

        print(f"[rss] Publishing generated slides: {' '.join(pub_cmd)}")
        candidate["instagram_publish_started_at"] = utc_now()
        candidate["instagram_publish_dry_run"] = args.dry_run

        pub_res = subprocess.run(pub_cmd, check=False)
        candidate["instagram_publish_finished_at"] = utc_now()
        candidate["instagram_publish_returncode"] = pub_res.returncode
        candidate["instagram_publish_report_path"] = str(build_dir / "instagram_publish.json")

        if pub_res.returncode == 0:
            candidate["status"] = "publish_previewed" if args.dry_run else "published"
            record_outcome_event(
                candidate,
                "instagram_previewed" if args.dry_run else "instagram_published",
            )
            print("[rss] Publishing finished successfully.")
        else:
            candidate["status"] = "publish_failed"
            candidate["failure"] = f"instagram_publish.py exited {pub_res.returncode}"
            record_outcome_event(candidate, "instagram_publish_failed", detail=candidate["failure"])
            print(f"[rss] Error: Publishing failed with exit code: {pub_res.returncode}", file=sys.stderr)

    save_queue(args.queue, queue)
    return 0

if __name__ == "__main__":
    sys.exit(main())
