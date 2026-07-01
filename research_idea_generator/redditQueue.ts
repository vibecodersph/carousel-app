import { dirname } from "node:path";
import { mkdir } from "node:fs/promises";
import type { SourceItem } from "../sourcing/types.ts";
import { readJsonFile, writeJsonFile } from "../sourcing/utils.ts";
import { fetchRedditSourceItems } from "../sourcing/connectors/reddit.ts";
import { dedupeResearchItems } from "./sources/index.ts";
import { buildResearchQueries, loadTaxonomy } from "./taxonomy.ts";

export interface RedditSourceQueue {
  version: 1;
  updatedAt: string;
  items: SourceItem[];
  seenIds: string[];
  readIds: string[];
}

export interface RedditQueueCollectOptions {
  queuePath?: string;
  taxonomyPath?: string;
  maxItems?: number;
  limitPerListing?: number;
  maxSubreddits?: number;
  concurrency?: number;
  rssDelayMs?: number;
  now?: Date;
}

export interface RedditQueueReadOptions {
  queuePath?: string;
  limit?: number;
  markRead?: boolean;
}

export function defaultRedditQueuePath(): string {
  return "out/research_idea_generator/reddit_unread_sources.json";
}

function emptyQueue(): RedditSourceQueue {
  return {
    version: 1,
    updatedAt: new Date(0).toISOString(),
    items: [],
    seenIds: [],
    readIds: [],
  };
}

async function loadQueue(path = defaultRedditQueuePath()): Promise<RedditSourceQueue> {
  const queue = await readJsonFile<RedditSourceQueue>(path, emptyQueue());
  return {
    version: 1,
    updatedAt: queue.updatedAt || new Date(0).toISOString(),
    items: Array.isArray(queue.items) ? queue.items : [],
    seenIds: Array.isArray(queue.seenIds) ? queue.seenIds : [],
    readIds: Array.isArray(queue.readIds) ? queue.readIds : [],
  };
}

async function saveQueue(path: string, queue: RedditSourceQueue): Promise<void> {
  await mkdir(dirname(path), { recursive: true });
  await writeJsonFile(path, queue);
}

export async function collectRedditSourceQueue(options: RedditQueueCollectOptions = {}): Promise<{
  queue: RedditSourceQueue;
  fetched: number;
  added: number;
  unread: number;
}> {
  const queuePath = options.queuePath ?? defaultRedditQueuePath();
  const queue = await loadQueue(queuePath);
  const taxonomy = await loadTaxonomy(options.taxonomyPath);
  const queries = buildResearchQueries(taxonomy, { days: 7, now: options.now });
  const subreddits = queries.redditSubreddits.slice(0, options.maxSubreddits ?? 2);
  const fetched = await fetchRedditSourceItems({
    subreddits,
    maxItems: options.maxItems ?? 80,
    limitPerListing: options.limitPerListing ?? 5,
    concurrency: options.concurrency ?? 1,
    rssDelayMs: options.rssDelayMs ?? 10_000,
    rssOnly: true,
    includeTopReply: false,
  });
  const existingIds = new Set(queue.items.map((item) => item.id));
  const readIds = new Set(queue.readIds);
  const seenIds = new Set(queue.seenIds);
  let added = 0;
  for (const item of dedupeResearchItems(fetched)) {
    seenIds.add(item.id);
    if (existingIds.has(item.id) || readIds.has(item.id)) continue;
    queue.items.push(item);
    existingIds.add(item.id);
    added += 1;
  }
  queue.items = dedupeResearchItems(queue.items).sort((a, b) => Date.parse(b.createdAt) - Date.parse(a.createdAt));
  queue.seenIds = [...seenIds].slice(-1000);
  queue.updatedAt = (options.now ?? new Date()).toISOString();
  await saveQueue(queuePath, queue);
  return {
    queue,
    fetched: fetched.length,
    added,
    unread: queue.items.length,
  };
}

export async function readUnreadRedditSources(options: RedditQueueReadOptions = {}): Promise<SourceItem[]> {
  const queuePath = options.queuePath ?? defaultRedditQueuePath();
  const queue = await loadQueue(queuePath);
  const unread = queue.items.slice(0, options.limit ?? 80);
  if (options.markRead !== false && unread.length) {
    const ids = new Set(unread.map((item) => item.id));
    queue.readIds = [...new Set([...queue.readIds, ...ids])].slice(-2000);
    queue.items = queue.items.filter((item) => !ids.has(item.id));
    queue.updatedAt = new Date().toISOString();
    await saveQueue(queuePath, queue);
  }
  return unread;
}
