import assert from "node:assert/strict";
import { mkdir, mkdtemp, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import test from "node:test";
import { dedupeSourceItems, rememberSourceItems } from "../../sourcing/dedup.ts";
import type { SourceItem } from "../../sourcing/types.ts";
import { stableSourceItemId } from "../../sourcing/utils.ts";
import { rankSourceItems } from "../../ranking/ranker.ts";
import { mergeRankedItemsIntoQueue } from "../../sourcing/queue.ts";
import { summarizeSourceRun } from "../../sourcing/report.ts";
import { redditCommentChildrenToTopReply, redditPostToSourceItem } from "../../sourcing/connectors/reddit.ts";
import { fetchXSourceItems, xPostToSourceItem } from "../../sourcing/connectors/x.ts";

function item(overrides: Partial<SourceItem> & { source: string; externalId: string; title: string }): SourceItem {
  return {
    id: stableSourceItemId(overrides.source, overrides.externalId),
    source: overrides.source,
    externalId: overrides.externalId,
    url: overrides.url ?? `https://example.com/${overrides.externalId}`,
    title: overrides.title,
    body: overrides.body ?? "",
    author: overrides.author ?? "tester",
    createdAt: overrides.createdAt ?? "2026-06-21T00:00:00.000Z",
    subreddit: overrides.subreddit,
    metrics: overrides.metrics ?? { upvotes: 100, comments: 10, score: 100 },
    media: overrides.media ?? { hasVideo: false },
    topReply: overrides.topReply,
    raw: overrides.raw,
  };
}

test("dedupe drops exact reruns and near duplicate titles", async () => {
  const dir = await mkdtemp(join(tmpdir(), "source-dedup-"));
  const statePath = join(dir, "dedup.json");
  const first = item({
    source: "reddit",
    externalId: "t3_a",
    title: "Someone built an AI agent that controls a browser",
    subreddit: "LocalLLaMA",
  });
  const duplicateTitle = item({
    source: "x",
    externalId: "123",
    title: "Someone built an AI agent that controls the browser",
    metrics: { likes: 200, replies: 20, score: 220 },
  });

  const result = await dedupeSourceItems([first, first, duplicateTitle], {
    statePath,
    remember: true,
    similarityThreshold: 0.82,
  });
  assert.equal(result.items.length, 1);
  assert.equal(result.dropped[0].reason, "batch_id");
  assert.equal(result.dropped[1].reason, "title_duplicate");

  const rerun = await dedupeSourceItems([first], { statePath, remember: true });
  assert.equal(rerun.items.length, 0);
  assert.equal(rerun.dropped[0].reason, "seen_id");
});

test("dedupe remembering can be deferred until video media is usable", async () => {
  const dir = await mkdtemp(join(tmpdir(), "source-media-dedup-"));
  const statePath = join(dir, "dedup.json");
  const video = item({
    source: "reddit",
    externalId: "t3_video_retry",
    title: "Realtime AI demo with a downloadable video",
    media: { hasVideo: true },
  });

  const first = await dedupeSourceItems([video], { statePath, remember: false });
  assert.equal(first.items.length, 1);
  const retry = await dedupeSourceItems([video], { statePath, remember: false });
  assert.equal(retry.items.length, 1);

  await rememberSourceItems([{ ...video, media: { hasVideo: true, localPath: "/tmp/video.mp4" } }], { statePath });
  const afterRemember = await dedupeSourceItems([video], { statePath, remember: false });
  assert.equal(afterRemember.items.length, 0);
  assert.equal(afterRemember.dropped[0].reason, "seen_id");
});

test("reddit mapper emits SourceItems and filters top replies from fixture JSON", () => {
  const rawPost = {
    name: "t3_ai_demo",
    id: "ai_demo",
    subreddit: "aivideo",
    permalink: "/r/aivideo/comments/ai_demo/someone_built_a_robot/",
    title: "Someone built an AI robot that folds laundry on video",
    selftext: "A visible demo with a surprising result.",
    author: "demo_builder",
    created_utc: 1782000000,
    ups: 1500,
    score: 1620,
    num_comments: 180,
    total_awards_received: 3,
    upvote_ratio: 0.94,
    is_video: false,
    media: {
      reddit_video: {
        fallback_url: "https://v.redd.it/abc123/DASH_720.mp4",
        duration: 38,
      },
    },
  };
  const mapped = redditPostToSourceItem(rawPost);
  assert.ok(mapped);
  assert.equal(mapped.id, stableSourceItemId("reddit", "t3_ai_demo"));
  assert.equal(mapped.source, "reddit");
  assert.equal(mapped.subreddit, "aivideo");
  assert.equal(mapped.metrics.upvotes, 1500);
  assert.equal(mapped.metrics.comments, 180);
  assert.equal(mapped.media.hasVideo, true);
  assert.equal(mapped.media.provider, "reddit_video");
  assert.equal(mapped.media.durationSeconds, 38);

  const topReply = redditCommentChildrenToTopReply([
    { kind: "t1", data: { author: "AutoModerator", body: "removed", score: 999, created_utc: 1782000100 } },
    { kind: "t1", data: { author: "human_a", body: "This is genuinely useful if it works.", score: 42, created_utc: 1782000200 } },
    { kind: "t1", data: { author: "human_b", body: "wow", score: 100, created_utc: 1782000300 } },
    { kind: "t1", data: { author: "human_c", body: "The surprising part is that the arm recovers when the shirt slips.", score: 88, created_utc: 1782000400 } },
  ]);
  assert.equal(topReply?.author, "human_c");
  assert.equal(topReply?.score, 88);
});

test("reddit mapping handles 50+ fixture posts with metrics and media flags", () => {
  const mapped = Array.from({ length: 60 }, (_, index) => redditPostToSourceItem({
    name: `t3_batch_${index}`,
    id: `batch_${index}`,
    subreddit: index % 2 ? "LocalLLaMA" : "StableDiffusion",
    permalink: `/r/test/comments/batch_${index}/title/`,
    title: `AI demo fixture ${index}`,
    author: "fixture",
    created_utc: 1782000000 - index * 60,
    ups: 100 + index,
    score: 120 + index,
    num_comments: 10 + index,
    is_video: index % 3 === 0,
    url: index % 3 === 0 ? `https://v.redd.it/video_${index}` : `https://example.com/image_${index}.jpg`,
  })).filter((value): value is SourceItem => Boolean(value));

  assert.equal(mapped.length, 60);
  assert.ok(mapped.every((entry) => typeof entry.metrics.comments === "number"));
  assert.ok(mapped.some((entry) => entry.media.hasVideo));
  assert.ok(mapped.some((entry) => !entry.media.hasVideo));
});

test("source run report exposes acceptance status and source issues", () => {
  const items = Array.from({ length: 50 }, (_, index) => item({
    source: index % 2 ? "reddit" : "x",
    externalId: `report_${index}`,
    title: `AI source report item ${index}`,
    metrics: { upvotes: 100 + index, comments: 10 },
    media: { hasVideo: index % 5 === 0, localPath: index % 5 === 0 ? `/tmp/${index}.mp4` : undefined },
  }));
  const ok = summarizeSourceRun({
    rawItems: items,
    outputItems: items,
    droppedCount: 0,
    mediaFailureCount: 0,
    minItems: 50,
  });
  assert.equal(ok.acceptance.ok, true);
  assert.equal(ok.bySource.reddit, 25);
  assert.equal(ok.videos, 10);
  assert.equal(ok.withMetrics, 50);

  const failed = summarizeSourceRun({
    rawItems: items.slice(0, 49),
    outputItems: items.slice(0, 49),
    droppedCount: 1,
    mediaFailureCount: 0,
    minItems: 50,
    issues: [{ source: "reddit", code: "source_unavailable", message: "HTTP 403" }],
  });
  assert.equal(failed.acceptance.ok, false);
  assert.ok(failed.acceptance.reasons.some((reason) => reason.includes("below minimum")));
  assert.ok(failed.acceptance.reasons.some((reason) => reason.includes("source listing")));
});

test("x connector emits SourceItems from existing scout queue records", async () => {
  const dir = await mkdtemp(join(tmpdir(), "x-queue-"));
  const queuePath = join(dir, "candidates.json");
  await writeFile(queuePath, JSON.stringify({
    version: 1,
    candidates: [
      {
        id: "x_legacy",
        status: "candidate",
        post: {
          id: "2064431111154053187",
          text: "Someone built a tiny AI debugger that explains failing tests",
          author: "Builder",
          handle: "@builder",
          date: "2026-06-20T10:00:00Z",
          likes: 1500,
          retweets: 120,
          replies: 80,
          views: 100000,
          has_video: true,
          url: "https://twitter.com/builder/status/2064431111154053187",
        },
      },
    ],
  }));

  const items = await fetchXSourceItems({ queuePath, includeTopReply: false });
  assert.equal(items.length, 1);
  assert.equal(items[0].id, stableSourceItemId("x", "2064431111154053187"));
  assert.equal(items[0].url, "https://x.com/builder/status/2064431111154053187");
  assert.equal(items[0].metrics.comments, 80);
  assert.equal(items[0].media.hasVideo, true);
});

test("ranking is stable, route-aware, and reads changed weights", async () => {
  const dir = await mkdtemp(join(tmpdir(), "ranker-"));
  const weightsPath = join(dir, "weights.json");
  const cachePath = join(dir, "spectacle.json");
  const queuePath = join(dir, "queue.json");
  await mkdir(dir, { recursive: true });
  await writeFile(weightsPath, JSON.stringify({
    velocity: 0.22,
    engagement: 0.12,
    recency: 0.12,
    authority: 0.12,
    hasVideo: 0.15,
    spectacle: 0.2,
    novelty: 0.07,
  }));
  await writeFile(queuePath, JSON.stringify({ version: 1, candidates: [] }));

  const video = item({
    source: "reddit",
    externalId: "t3_video",
    title: "A visible realtime AI video demo built from scratch",
    subreddit: "aivideo",
    metrics: { upvotes: 600, comments: 120, score: 650 },
    media: { hasVideo: true },
    createdAt: "2026-06-20T23:00:00.000Z",
  });
  const text = item({
    source: "reddit",
    externalId: "t3_text",
    title: "Incremental pricing update for an AI API",
    subreddit: "OpenAI",
    metrics: { upvotes: 900, comments: 10, score: 900 },
    media: { hasVideo: false },
    createdAt: "2026-06-20T23:30:00.000Z",
  });

  const ranked = await rankSourceItems([text, video], {
    weightsPath,
    now: new Date("2026-06-21T00:00:00.000Z"),
    spectacle: { cachePath, provider: "local" },
    novelty: { queuePath },
  });
  assert.equal(ranked[0].id, video.id);
  assert.deepEqual(ranked[0].routing.routes, ["reel", "carousel"]);
  assert.equal(ranked[1].routing.primary, "carousel");
  assert.ok(ranked[0].components.spectacle > 0);
  assert.equal(ranked[0].components.novelty, 1);

  await writeFile(weightsPath, JSON.stringify({
    velocity: 0,
    engagement: 0,
    recency: 0,
    authority: 0,
    hasVideo: 0,
    spectacle: 0,
    novelty: 1,
  }));
  const reranked = await rankSourceItems([text, video], {
    weightsPath,
    now: new Date("2026-06-21T00:00:00.000Z"),
    spectacle: { cachePath, provider: "local" },
    novelty: { queuePath },
  });
  assert.equal(reranked[0].score, 1);
  assert.equal(reranked[1].score, 1);
});

test("queue merge is idempotent and preserves approved statuses", async () => {
  const dir = await mkdtemp(join(tmpdir(), "queue-"));
  const weightsPath = join(dir, "weights.json");
  const cachePath = join(dir, "spectacle.json");
  const queuePath = join(dir, "candidates.json");
  await writeFile(weightsPath, JSON.stringify({
    velocity: 0.22,
    engagement: 0.12,
    recency: 0.12,
    authority: 0.12,
    hasVideo: 0.15,
    spectacle: 0.2,
    novelty: 0.07,
  }));
  await writeFile(queuePath, JSON.stringify({ version: 1, updated_at: "", candidates: [] }));

  const source = item({
    source: "reddit",
    externalId: "t3_queue",
    title: "Someone built a tiny AI coding agent with a video demo",
    subreddit: "ChatGPTCoding",
    media: { hasVideo: true, localPath: "/tmp/demo.mp4" },
  });
  const ranked = await rankSourceItems([source], {
    weightsPath,
    now: new Date("2026-06-21T00:00:00.000Z"),
    spectacle: { cachePath, provider: "local" },
    novelty: { queuePath },
  });

  const first = await mergeRankedItemsIntoQueue(ranked, { queuePath });
  assert.equal(first.queued, 1);
  assert.equal(first.queue.candidates.length, 1);
  assert.equal(first.queue.candidates[0].status, "candidate");
  assert.equal(first.queue.candidates[0].source_type, "reel_candidate");
  assert.equal(first.queue.candidates[0].requires_human_approval, true);
  assert.equal((first.queue.candidates[0].story_object as Record<string, unknown>).language, "ja");
  assert.deepEqual(
    ((first.queue.candidates[0].story_object as Record<string, unknown>).approval as Record<string, unknown>),
    { required: true, irreversibleAfterApproval: true },
  );

  first.queue.candidates[0].status = "approved";
  await writeFile(queuePath, JSON.stringify(first.queue));
  const second = await mergeRankedItemsIntoQueue(ranked, { queuePath });
  assert.equal(second.queued, 0);
  assert.equal(second.queue.candidates.length, 1);
  assert.equal(second.queue.candidates[0].status, "approved");
});

test("queue merge prevents legacy X URL duplicates while preserving approved ids", async () => {
  const dir = await mkdtemp(join(tmpdir(), "queue-legacy-"));
  const weightsPath = join(dir, "weights.json");
  const cachePath = join(dir, "spectacle.json");
  const queuePath = join(dir, "candidates.json");
  await writeFile(weightsPath, JSON.stringify({
    velocity: 0.22,
    engagement: 0.12,
    recency: 0.12,
    authority: 0.12,
    hasVideo: 0.15,
    spectacle: 0.2,
    novelty: 0.07,
  }));

  const source = xPostToSourceItem({
    id: "2064431111154053187",
    text: "Someone built a tiny AI debugger that explains failing tests",
    handle: "@builder",
    date: "2026-06-20T10:00:00Z",
    likes: 1500,
    retweets: 120,
    replies: 80,
    views: 100000,
    has_video: false,
    url: "https://x.com/builder/status/2064431111154053187?ref=old",
  });
  assert.ok(source);
  const ranked = await rankSourceItems([source], {
    weightsPath,
    now: new Date("2026-06-21T00:00:00.000Z"),
    spectacle: { cachePath, provider: "local" },
    novelty: { queuePath },
  });

  await writeFile(queuePath, JSON.stringify({
    version: 1,
    updated_at: "",
    candidates: [
      {
        id: "x_legacy_url_hash",
        status: "candidate",
        score: 10,
        post: {
          id: "2064431111154053187",
          text: "Old queue copy",
          url: "https://twitter.com/builder/status/2064431111154053187",
        },
      },
    ],
  }));
  const migrated = await mergeRankedItemsIntoQueue(ranked, { queuePath });
  assert.equal(migrated.queue.candidates.length, 1);
  assert.equal(migrated.queue.candidates[0].id, source.id);
  assert.equal(migrated.queue.candidates[0].source_item_id, source.id);

  await writeFile(queuePath, JSON.stringify({
    version: 1,
    updated_at: "",
    candidates: [
      {
        id: "x_approved_legacy",
        status: "approved",
        score: 10,
        post: {
          id: "2064431111154053187",
          text: "Approved old queue copy",
          url: "https://twitter.com/builder/status/2064431111154053187",
        },
      },
    ],
  }));
  const preserved = await mergeRankedItemsIntoQueue(ranked, { queuePath });
  assert.equal(preserved.queue.candidates.length, 1);
  assert.equal(preserved.queue.candidates[0].id, "x_approved_legacy");
  assert.equal(preserved.queue.candidates[0].source_item_id, source.id);
  assert.equal(preserved.queue.candidates[0].status, "approved");
});
