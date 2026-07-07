import type { SourceItem } from "../../sourcing/types.ts";
import { fetchRedditSourceItems } from "../../sourcing/connectors/reddit.ts";
import { normalizeWhitespace } from "../../sourcing/utils.ts";
import { fetchGitHubSourceItems } from "./github.ts";
import { fetchHackerNewsSourceItems } from "./hackerNews.ts";
import { fetchTheBatchSourceItems } from "./theBatch.ts";
import { fetchAiNewsSourceItems } from "./aiNews.ts";
import type { ResearchQueries, ResearchSourceName } from "../types.ts";

export interface LiveCollectionOptions {
  sources: ResearchSourceName[];
  queries: ResearchQueries;
  days: number;
  now?: Date;
  maxItemsPerSource?: number;
  redditItems?: SourceItem[];
  redditLive?: boolean;
  theBatchLive?: boolean;
  theBatchIssueUrl?: string;
  aiNewsLive?: boolean;
  aiNewsIssueUrl?: string;
}

function isRecent(item: SourceItem, days: number, now = new Date()): boolean {
  const created = Date.parse(item.createdAt);
  if (!Number.isFinite(created)) return false;
  return now.getTime() - created <= Math.max(1, days) * 86_400_000;
}

function normalizeUrl(value: string): string {
  try {
    const url = new URL(value);
    url.hash = "";
    url.search = "";
    return url.toString().replace(/\/$/, "");
  } catch {
    return normalizeWhitespace(value).replace(/[?#].*$/, "").replace(/\/$/, "");
  }
}

export function dedupeResearchItems(items: SourceItem[]): SourceItem[] {
  const selected: SourceItem[] = [];
  const keyToIndex = new Map<string, number>();
  for (const item of items) {
    const keys = [item.id, normalizeUrl(item.url)].filter(Boolean);
    const existingIndex = keys
      .map((key) => keyToIndex.get(key))
      .find((index): index is number => typeof index === "number");
    if (existingIndex === undefined) {
      const index = selected.length;
      selected.push(item);
      for (const key of keys) keyToIndex.set(key, index);
      continue;
    }

    const previous = selected[existingIndex];
    const winner = (item.metrics.score ?? 0) > (previous.metrics.score ?? 0) ? item : previous;
    selected[existingIndex] = winner;
    const allKeys = new Set([
      previous.id,
      normalizeUrl(previous.url),
      item.id,
      normalizeUrl(item.url),
      winner.id,
      normalizeUrl(winner.url),
    ].filter(Boolean));
    for (const key of allKeys) keyToIndex.set(key, existingIndex);
  }
  return selected;
}

export async function collectLiveSourceItems(options: LiveCollectionOptions): Promise<SourceItem[]> {
  const batches: SourceItem[][] = [];
  const maxItems = options.maxItemsPerSource ?? 80;
  if (options.sources.includes("reddit") && options.redditItems?.length) {
    batches.push(options.redditItems.filter((item) => isRecent(item, options.days, options.now)));
  }
  if (options.sources.includes("reddit") && options.redditLive) {
    const redditItems = await fetchRedditSourceItems({
      subreddits: options.queries.redditSubreddits,
      maxItems,
      includeTopReply: true,
      limitPerListing: 25,
    });
    batches.push(redditItems.filter((item) => isRecent(item, options.days, options.now)));
  }
  if (options.sources.includes("github")) {
    batches.push(await fetchGitHubSourceItems({
      queries: options.queries.github,
      maxItems,
      perQueryLimit: 10,
    }));
  }
  if (options.sources.includes("hacker_news")) {
    batches.push(await fetchHackerNewsSourceItems({
      queries: options.queries.hn,
      days: options.days,
      now: options.now,
      maxItems,
      perQueryLimit: 10,
    }));
  }
  if (options.sources.includes("the_batch")) {
    batches.push(await fetchTheBatchSourceItems({
      issueTagUrl: options.theBatchIssueUrl,
      days: options.days,
      now: options.now,
      maxItems,
    }));
  }
  if (options.sources.includes("ai_news")) {
    batches.push(await fetchAiNewsSourceItems({
      issueUrl: options.aiNewsIssueUrl,
      days: options.days,
      now: options.now,
      maxItems,
    }));
  }
  return dedupeResearchItems(batches.flat());
}
