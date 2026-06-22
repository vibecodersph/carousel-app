import type { RankedItem, SourceItem } from "./types.ts";
import { sourceItemToStoryObject } from "./storyObject.ts";
import { compactText, normalizeWhitespace, readJsonFile, writeJsonFile } from "./utils.ts";

export interface QueueOptions {
  queuePath?: string;
  limit?: number;
}

interface CandidateQueue {
  version: number;
  updated_at: string;
  candidates: Array<Record<string, unknown>>;
}

const IRREVERSIBLE_OR_LOCKED_STATUSES = new Set([
  "approved",
  "built",
  "publish_previewed",
  "published",
  "publish_failed",
]);

function sourceTypeFor(item: SourceItem): string {
  if (item.source === "x") return "x_post";
  if (item.media.hasVideo) return "reel_candidate";
  return "source_item";
}

function normalizeSourceUrl(value: unknown): string {
  const raw = normalizeWhitespace(value).replace("twitter.com", "x.com");
  if (!raw) return "";
  try {
    const url = new URL(raw);
    url.hash = "";
    url.search = "";
    return url.toString().replace(/\/$/, "");
  } catch {
    return raw.replace(/[?#].*$/, "").replace(/\/$/, "");
  }
}

function candidateSourceUrls(candidate: Record<string, unknown>): string[] {
  const urls = new Set<string>();
  const sourceItem = candidate.source_item as Record<string, unknown> | undefined;
  const post = candidate.post as Record<string, unknown> | undefined;
  const article = candidate.article as Record<string, unknown> | undefined;
  const ranking = candidate.ranking as Record<string, unknown> | undefined;
  const rankedSourceItem = ranking?.sourceItem as Record<string, unknown> | undefined;
  for (const value of [
    sourceItem?.url,
    post?.url,
    article?.url,
    rankedSourceItem?.url,
  ]) {
    const normalized = normalizeSourceUrl(value);
    if (normalized) urls.add(normalized);
  }
  return [...urls];
}

function postCompat(item: SourceItem): Record<string, unknown> | undefined {
  if (item.source !== "x") return undefined;
  return {
    id: item.externalId,
    text: item.title,
    author: item.author,
    handle: item.author.startsWith("@") ? item.author : "",
    date: item.createdAt,
    likes: item.metrics.likes ?? item.metrics.upvotes ?? 0,
    retweets: item.metrics.retweets ?? 0,
    replies: item.metrics.replies ?? item.metrics.comments ?? 0,
    views: item.metrics.views ?? 0,
    has_video: item.media.hasVideo,
    url: item.url,
  };
}

function rankedToCandidate(ranked: RankedItem, previous: Record<string, unknown> | undefined): Record<string, unknown> {
  const item = ranked.sourceItem;
  const now = new Date().toISOString();
  const locked = previous?.status && IRREVERSIBLE_OR_LOCKED_STATUSES.has(String(previous.status));
  const candidateId = locked ? normalizeWhitespace(previous?.id) || item.id : item.id;
  return {
    ...(previous ?? {}),
    id: candidateId,
    source_item_id: item.id,
    source_type: previous?.source_type ?? sourceTypeFor(item),
    status: previous?.status ?? "candidate",
    score: Math.round(ranked.score * 1000),
    score_reasons: [
      `rank=${ranked.score.toFixed(3)}`,
      `spectacle=${ranked.components.spectacle.toFixed(2)}: ${compactText(ranked.spectacleReason, 120)}`,
      `novelty=${ranked.components.novelty.toFixed(2)}`,
      `route=${ranked.routing.primary}`,
    ],
    score_components: ranked.components,
    ranking: ranked,
    routing: ranked.routing,
    source_account: item.subreddit ? `r/${item.subreddit}` : item.author,
    source_item: item,
    story_object: sourceItemToStoryObject(item, ranked.routing.routes),
    post: postCompat(item) ?? previous?.post,
    created_at: previous?.created_at ?? now,
    updated_at: locked ? previous?.updated_at ?? now : now,
    requires_human_approval: true,
    automation_stage: "pre_validation_ranked",
  };
}

export async function mergeRankedItemsIntoQueue(rankedItems: RankedItem[], options: QueueOptions = {}): Promise<{ queue: CandidateQueue; queued: number }> {
  const queuePath = options.queuePath ?? "out/automation/candidates.json";
  const queue = await readJsonFile<CandidateQueue>(queuePath, {
    version: 1,
    updated_at: new Date().toISOString(),
    candidates: [],
  });
  queue.version = 1;
  queue.candidates = Array.isArray(queue.candidates) ? queue.candidates : [];
  const existingById = new Map(queue.candidates.map((candidate) => [normalizeWhitespace(candidate.id), candidate]));
  const existingByUrl = new Map<string, Record<string, unknown>>();
  for (const candidate of queue.candidates) {
    for (const url of candidateSourceUrls(candidate)) {
      if (!existingByUrl.has(url)) existingByUrl.set(url, candidate);
    }
  }
  let queued = 0;
  for (const ranked of rankedItems.slice(0, options.limit ?? rankedItems.length)) {
    const previous = existingById.get(ranked.id) ?? existingByUrl.get(normalizeSourceUrl(ranked.sourceItem.url));
    const candidate = rankedToCandidate(ranked, previous);
    if (!previous) queued += 1;
    const previousId = normalizeWhitespace(previous?.id);
    const candidateId = normalizeWhitespace(candidate.id);
    if (previousId && previousId !== candidateId) {
      existingById.delete(previousId);
    }
    existingById.set(candidateId, candidate);
    for (const url of candidateSourceUrls(candidate)) {
      existingByUrl.set(url, candidate);
    }
  }
  queue.candidates = [...existingById.values()].sort((a, b) => {
    const statusCompare = normalizeWhitespace(a.status).localeCompare(normalizeWhitespace(b.status));
    if (statusCompare) return statusCompare;
    return Number(b.score ?? 0) - Number(a.score ?? 0);
  });
  queue.updated_at = new Date().toISOString();
  await writeJsonFile(queuePath, queue);
  return { queue, queued };
}
