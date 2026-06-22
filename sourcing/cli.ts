#!/usr/bin/env node
import { mkdir } from "node:fs/promises";
import { access } from "node:fs/promises";
import { dirname } from "node:path";
import { dedupeSourceItems, rememberSourceItems } from "./dedup.ts";
import { loadDotEnv } from "./env.ts";
import { fetchMediaForItems } from "./fetchMedia.ts";
import { mergeRankedItemsIntoQueue } from "./queue.ts";
import { summarizeSourceRun, type SourceRunIssue } from "./report.ts";
import type { SourceItem } from "./types.ts";
import { fetchAllSourceItems } from "./connectors/index.ts";
import type { RedditListingEvent } from "./connectors/reddit.ts";
import { rankSourceItems } from "../ranking/ranker.ts";
import { readJsonFile, writeJsonFile } from "./utils.ts";

interface CliOptions {
  command: string;
  output?: string;
  input?: string;
  queue?: string;
  top?: number;
  minItems?: number;
  redditOnly?: boolean;
  noReddit?: boolean;
  noRemember?: boolean;
  noMedia?: boolean;
  allowMissingMedia?: boolean;
  maxDurationSeconds?: number;
  maxHeight?: number;
  dedupState?: string;
  xQueue?: string;
  noXQueue?: boolean;
  noTopReplies?: boolean;
  xUrl?: string[];
  noYoutube?: boolean;
  youtubeQuery?: string[];
  youtubeLimit?: number;
  youtubeSearch?: boolean;
  youtubeWindowDays?: number;
  xLive?: boolean;
  recencyDays?: number;
}

function parseArgs(argv: string[]): CliOptions {
  const [command = "run", ...rest] = argv;
  const options: CliOptions = { command, xUrl: [], youtubeQuery: [] };
  for (let i = 0; i < rest.length; i += 1) {
    const arg = rest[i];
    if (arg === "--out" || arg === "--output") options.output = rest[++i];
    else if (arg === "--input") options.input = rest[++i];
    else if (arg === "--queue") options.queue = rest[++i];
    else if (arg === "--top") options.top = Number(rest[++i]);
    else if (arg === "--min-items") options.minItems = Number(rest[++i]);
    else if (arg === "--reddit-only") options.redditOnly = true;
    else if (arg === "--no-reddit") options.noReddit = true;
    else if (arg === "--no-remember") options.noRemember = true;
    else if (arg === "--no-media") options.noMedia = true;
    else if (arg === "--allow-missing-media") options.allowMissingMedia = true;
    else if (arg === "--max-duration-seconds") options.maxDurationSeconds = Number(rest[++i]);
    else if (arg === "--max-height") options.maxHeight = Number(rest[++i]);
    else if (arg === "--dedup-state") options.dedupState = rest[++i];
    else if (arg === "--x-queue") options.xQueue = rest[++i];
    else if (arg === "--no-x-queue") options.noXQueue = true;
    else if (arg === "--no-top-replies") options.noTopReplies = true;
    else if (arg === "--x-url") options.xUrl?.push(rest[++i]);
    else if (arg === "--no-youtube") options.noYoutube = true;
    else if (arg === "--youtube-query") options.youtubeQuery?.push(rest[++i]);
    else if (arg === "--youtube-limit") options.youtubeLimit = Number(rest[++i]);
    else if (arg === "--youtube-search") options.youtubeSearch = true;
    else if (arg === "--youtube-window-days") options.youtubeWindowDays = Number(rest[++i]);
    else if (arg === "--x-live") options.xLive = true;
    else if (arg === "--recency-days") options.recencyDays = Number(rest[++i]);
    else throw new Error(`Unknown argument: ${arg}`);
  }
  return options;
}

async function existingPath(path: string): Promise<string | undefined> {
  try {
    await access(path);
    return path;
  } catch {
    return undefined;
  }
}

async function writeOutput(path: string | undefined, data: unknown): Promise<void> {
  if (!path) {
    console.log(JSON.stringify(data, null, 2));
    return;
  }
  await mkdir(dirname(path), { recursive: true });
  await writeJsonFile(path, data);
}

async function source(options: CliOptions): Promise<SourceItem[]> {
  const redditEvents: RedditListingEvent[] = [];
  const xQueuePath = options.noXQueue
    ? undefined
    : options.xQueue ?? await existingPath("out/automation/candidates.json");
  const items = await fetchAllSourceItems({
    reddit: options.noReddit ? false : {
      includeTopReply: !options.noTopReplies,
      onListing: (event) => redditEvents.push(event),
    },
    x: options.redditOnly ? false : {
      urls: options.xUrl,
      queuePath: xQueuePath,
      includeTopReply: !options.noTopReplies,
      live: options.xLive,
      recencyDays: options.recencyDays,
    },
    youtube: options.redditOnly || options.noYoutube ? false : {
      queries: options.youtubeQuery?.length ? options.youtubeQuery : undefined,
      maxItems: options.youtubeLimit,
      useSearch: options.youtubeSearch,
      windowDays: options.youtubeWindowDays,
    },
    includeStubs: true,
  });
  const deduped = await dedupeSourceItems(items, {
    remember: false,
    statePath: options.dedupState,
  });
  const withMedia = options.noMedia
    ? deduped.items
    : await fetchMediaForItems(deduped.items, {
      maxDurationSeconds: options.maxDurationSeconds,
      maxHeight: options.maxHeight,
    });
  const mediaFailures = options.noMedia
    ? []
    : withMedia.filter((item) => item.media.hasVideo && !item.media.localPath);
  const outputItems = options.noMedia || options.allowMissingMedia
    ? withMedia
    : withMedia.filter((item) => !item.media.hasVideo || Boolean(item.media.localPath));
  if (!options.noRemember) {
    await rememberSourceItems(outputItems, { statePath: options.dedupState });
  }
  const issues: SourceRunIssue[] = redditEvents
    .filter((event) => !event.ok)
    .map((event) => ({
      source: "reddit",
      code: "source_unavailable",
      message: `r/${event.subreddit} ${event.listing}: ${event.error ?? "unknown error"}`,
    }));
  const report = summarizeSourceRun({
    rawItems: items,
    outputItems,
    droppedCount: deduped.dropped.length,
    mediaFailureCount: mediaFailures.length,
    minItems: options.minItems ?? 50,
    issues,
  });
  await writeOutput(options.output ?? "out/automation/sourcing/source_items.json", {
    createdAt: new Date().toISOString(),
    count: outputItems.length,
    report,
    sourceEvents: {
      reddit: redditEvents,
    },
    dropped: deduped.dropped.map((drop) => ({
      id: drop.item.id,
      title: drop.item.title,
      reason: drop.reason,
      duplicateOf: drop.duplicateOf,
      similarity: drop.similarity,
    })),
    mediaFailures: mediaFailures.map((item) => ({
      id: item.id,
      url: item.url,
      title: item.title,
      error: typeof item.media.raw === "object" && item.media.raw
        ? (item.media.raw as Record<string, unknown>).downloadError
        : undefined,
    })),
    items: outputItems,
  });
  return outputItems;
}

async function rank(options: CliOptions): Promise<void> {
  const inputPath = options.input ?? "out/automation/sourcing/source_items.json";
  const input = await readJsonFile<{ items?: SourceItem[] } | SourceItem[]>(inputPath, []);
  const items = Array.isArray(input) ? input : input.items ?? [];
  const ranked = await rankSourceItems(items, { topN: options.top });
  await writeOutput(options.output ?? "out/automation/ranking/ranked_items.json", {
    createdAt: new Date().toISOString(),
    count: ranked.length,
    items: ranked,
  });
}

async function run(options: CliOptions): Promise<void> {
  const sourceOutputPath = options.output ?? "out/automation/sourcing/source_items.json";
  const items = await source({
    ...options,
    output: sourceOutputPath,
  });
  const ranked = await rankSourceItems(items, { topN: options.top ?? 30 });
  await writeJsonFile("out/automation/ranking/ranked_items.json", {
    createdAt: new Date().toISOString(),
    count: ranked.length,
    items: ranked,
  });
  const queueResult = await mergeRankedItemsIntoQueue(ranked, {
    queuePath: options.queue,
    limit: options.top ?? 30,
  });
  const sourceOutput = await readJsonFile<{ report?: unknown }>(sourceOutputPath, {});
  console.log(JSON.stringify({
    sourced: items.length,
    ranked: ranked.length,
    queued: queueResult.queued,
    queuePath: options.queue ?? "out/automation/candidates.json",
    sourceReport: sourceOutput.report,
  }, null, 2));
}

async function main(): Promise<void> {
  await loadDotEnv();
  const options = parseArgs(process.argv.slice(2));
  if (options.command === "source") await source(options);
  else if (options.command === "rank") await rank(options);
  else if (options.command === "run") await run(options);
  else throw new Error(`Unknown command: ${options.command}`);
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
});
