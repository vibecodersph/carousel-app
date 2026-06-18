# Carousel Automation Status

Last updated: 2026-06-18

This file is the owner-facing truth board for the automation system. The README
is the command reference; this document tracks what exists, how the pieces fit
together, and what is still worth building.

## Goal

Move the app from a one-URL carousel renderer to a mostly autonomous content
system:

1. Watch high-signal X accounts and article feeds.
2. Suggest only stories worth turning into channel-branded carousels.
3. Ask for quick human approval.
4. Build the right carousel automatically after approval.
5. Preview or publish the approved carousel without manual asset handling.

The human should spend time deciding what is worth running, not checking feeds,
downloading media, or pushing files between tools.

## Current Operating Flow

```text
story_sources.json
  -> story_scout.py scan
  -> out/automation/candidates.json
  -> CLI or Telegram approval
  -> one-URL builder in out/automation/builds/<candidate_id>/
  -> manifest.json
  -> optional publish preview or live publish
  -> instagram_publish.json
```

Weekly roundups use the same candidate queue as source material:

```text
out/automation/candidates.json or stories.json
  -> build_weekly_carousel.py
  -> weekly_verifier.py checks
  -> out/weekly_carousel/manifest.json
```

## Capability Status

| Capability | Status | Main files | Notes |
| --- | --- | --- | --- |
| Channel-specific branding, language, and voice | Done | `channel.py`, `channels/` | Builders resolve `--channel`, `CAROUSEL_CHANNEL`, then `channels/channels.json`. |
| X post/thread carousel | Done | `build_x_carousel.py`, `fetch_tweet_data.py` | xAI thread discovery preferred; Playwright remains fallback/rendering path. |
| Web article carousel | Done | `build_article_carousel.py` | Gemini curation when available, local scoring fallback. |
| X Article carousel | Done | `build_x_article_carousel.py` | Uses xAI lookup, then reuses the article pipeline. |
| Story scout for X posts | Done | `story_scout.py` | Scores and queues account posts from configured handles. |
| Story scout for article sources | Done | `story_scout.py`, `story_sources.example.json` | Supports feeds, sitemaps, JSON/API sources, direct URLs, and xAI search sources. |
| Durable candidate queue | Done | `story_scout.py` | Preserves approval/build/publish state across scans. |
| CLI approval and rejection | Done | `story_scout.py` | `approve`, `reject`, `run-approved`, and filtered `list` commands exist. |
| Telegram approval callbacks | Done | `story_scout.py` | `scan --notify` and `telegram-poll --watch` support approve/reject callbacks. |
| Approved build orchestration | Done | `story_scout.py` | X and article candidates build into `out/automation/builds/<candidate_id>/`. |
| Weekly roundup carousel | Done | `build_weekly_carousel.py` | Builds cover, story slides, outro, and channel-specific captions/copy. |
| Weekly copy/source verification | Done | `weekly_verifier.py`, `tests/test_weekly_verifier.py` | `build_weekly_carousel.py --verify` writes `run_manifest.json` and blocks bad claims. |
| Instagram Graph publishing | Done | `instagram_publish.py` | Supports R2 upload, dry run, live publish, captions, mixed media, and publish reports. |
| Publish orchestration from approval | Done | `story_scout.py` | `--publish-instagram` can run immediately after build. |
| Manifest/report trail | Partial | builders, publishers | Build manifests and publish reports exist; cross-run metrics are still thin. |
| Generic visual QA | Missing | planned | Weekly has claim checks, but there is no manifest-wide dimensions/contact-sheet gate yet. |
| Scheduling/deployment docs | Missing | planned | Commands are ready, but launchd/cron/worker setup is not documented. |

## Queue States

`story_scout.py` uses the queue as the main automation ledger. Important states:

- `candidate`: discovered and waiting for a decision
- `approved`: approved but not built yet
- `rejected`: intentionally skipped
- `built`: carousel built successfully
- `failed`: build failed
- `publish_previewed`: Instagram dry run succeeded
- `published`: live publish succeeded
- `publish_failed`: publish step failed

## Remaining Gaps

1. **Generic pre-publish QA**
   Add a manifest-level checker that verifies every slide exists, confirms image
   and video dimensions, checks carousel item count, and writes a compact contact
   sheet for review.

2. **Second approval gate**
   Add an optional "build approved, publish pending" state so generated art,
   videos, and mixed-media builds can require a final human confirmation before
   live Instagram publishing.

3. **Duplicate publish guard**
   Prevent accidental double-publishing by checking canonical source URLs,
   manifest IDs, and prior publish reports before live publish.

4. **Run logs and metrics**
   Keep a simple append-only automation log for scans, notifications, approvals,
   builds, publish previews, live publishes, failures, and elapsed times.

5. **Scheduling docs**
   Document the production-ish loop for local launchd/cron or a hosted worker:
   scheduled `scan --notify`, long-running `telegram-poll --watch`, and a safe
   restart story.

6. **Operator status command**
   Add a concise status/report command that summarizes queue counts, recent
   failures, pending approvals, built-but-unpublished items, and last scan time.

## Next Milestones

1. Build `manifest_qa.py` for generic slide existence, dimensions, media count,
   and contact-sheet generation.
2. Add a publish-pending approval state and wire it into CLI and Telegram flows.
3. Add duplicate-publish checks to `instagram_publish.py`.
4. Add scheduling docs with one recommended local setup and one hosted-worker
   setup.
5. Add an automation status command or report that reads
   `out/automation/candidates.json`.

## Launch Checklist

- `story_sources.json` has the accounts and article feeds for the active channel.
- `.env` has only the credentials needed for the chosen path.
- `uv run python story_scout.py scan --config story_sources.json --notify` works.
- `uv run python story_scout.py telegram-poll --watch` can receive callbacks.
- A dry-run publish writes `instagram_publish.json`.
- Live publish is only enabled after the operator has checked the generated
  manifest and media.
