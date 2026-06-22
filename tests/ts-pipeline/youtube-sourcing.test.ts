import assert from "node:assert/strict";
import { mkdtemp } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import test from "node:test";
import { dedupeSourceItems } from "../../sourcing/dedup.ts";
import { youtubeEntryToSourceItem } from "../../sourcing/connectors/youtubeShorts.ts";
import { rankSourceItems } from "../../ranking/ranker.ts";
import type { SourceItem } from "../../sourcing/types.ts";
import { stableSourceItemId } from "../../sourcing/utils.ts";

test("youtube mapper emits a video SourceItem from a flat-playlist entry", () => {
  const item = youtubeEntryToSourceItem({
    id: "abc123",
    title: "Someone built a self-flying drone with GPT",
    url: "https://www.youtube.com/watch?v=abc123",
    uploader: "AI Builders",
    duration: 42,
    view_count: 250_000,
    timestamp: 1_718_000_000,
  });
  assert.ok(item);
  assert.equal(item?.source, "youtube_shorts");
  assert.equal(item?.id, stableSourceItemId("youtube_shorts", "abc123"));
  assert.equal(item?.media.hasVideo, true);
  assert.equal(item?.media.durationSeconds, 42);
  assert.equal(item?.metrics.views, 250_000);
  assert.notEqual(item?.createdAt, new Date(0).toISOString());
});

test("youtube mapper rejects placeholders and incomplete entries", () => {
  assert.equal(youtubeEntryToSourceItem({ id: "x", title: "[Private video]", url: "https://y/watch?v=x" }), null);
  assert.equal(youtubeEntryToSourceItem({ title: "no id", url: "https://y" }), null);
  assert.equal(youtubeEntryToSourceItem({ id: "x" }), null); // no title
  // url is derivable from id, so a missing url still yields a valid item
  assert.equal(youtubeEntryToSourceItem({ id: "x", title: "has title" })?.url, "https://www.youtube.com/watch?v=x");
});

function xLikeItem(index: number): SourceItem {
  const externalId = `x-${index}`;
  return {
    id: stableSourceItemId("x", externalId),
    source: "x",
    externalId,
    url: `https://x.com/user/status/${index}`,
    title: `X post number ${index} about a distinct AI launch ${index}`,
    body: "",
    author: "user",
    createdAt: new Date(Date.now() - index * 3_600_000).toISOString(),
    metrics: { likes: 1000 + index, comments: 10 + index, score: 1000 + index },
    media: { hasVideo: index % 5 === 0 },
  };
}

function ytItem(index: number): SourceItem {
  return youtubeEntryToSourceItem({
    id: `yt-${index}`,
    title: `YouTube short ${index}: a wild AI demo you have to see ${index}`,
    url: `https://www.youtube.com/watch?v=yt-${index}`,
    uploader: "channel",
    duration: 30 + index,
    view_count: 500_000 - index * 1000,
    timestamp: 1_718_000_000 + index,
  })!;
}

test("combined X + YouTube batch dedupes to >= 50 items with metrics and resolved hasVideo", async () => {
  const dir = await mkdtemp(join(tmpdir(), "yt-accept-"));
  const items = [
    ...Array.from({ length: 49 }, (_unused, i) => xLikeItem(i + 1)),
    ...Array.from({ length: 8 }, (_unused, i) => ytItem(i + 1)),
  ];
  const result = await dedupeSourceItems(items, {
    remember: false,
    statePath: join(dir, "dedup-state.json"),
  });
  assert.ok(result.items.length >= 50, `expected >=50 deduped, got ${result.items.length}`);
  for (const item of result.items) {
    assert.equal(typeof item.metrics, "object");
    assert.equal(typeof item.media.hasVideo, "boolean");
  }
  const videoItems = result.items.filter((item) => item.media.hasVideo);
  assert.ok(videoItems.length >= 8, "youtube items should resolve hasVideo=true");
});

test("ranking routes youtube video items to reels and text X items to carousel", async () => {
  const ranked = await rankSourceItems([ytItem(1), xLikeItem(2)], {
    spectacle: { provider: "local" },
  });
  const yt = ranked.find((item) => item.sourceItem.source === "youtube_shorts");
  const x = ranked.find((item) => item.sourceItem.source === "x" && !item.sourceItem.media.hasVideo);
  assert.equal(yt?.routing.primary, "reel");
  assert.equal(yt?.routing.reelEligible, true);
  assert.equal(x?.routing.primary, "carousel");
  assert.equal(x?.routing.reelEligible, false);
  // spectacle + novelty populated for every ranked item
  for (const item of ranked) {
    assert.ok(item.components.spectacle >= 0 && item.components.spectacle <= 1);
    assert.ok(item.components.novelty >= 0 && item.components.novelty <= 1);
    assert.ok(item.spectacleReason.length > 0);
  }
});
