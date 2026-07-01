#!/usr/bin/env node
import { loadDotEnv } from "../sourcing/env.ts";
import { collectRedditSourceQueue } from "./redditQueue.ts";

interface CliOptions {
  queue?: string;
  maxItems?: number;
  limitPerListing?: number;
  maxSubreddits?: number;
  rssDelayMs?: number;
}

function parseArgs(argv: string[]): { command: string; options: CliOptions } {
  const [command = "collect", ...rest] = argv;
  const options: CliOptions = {};
  for (let i = 0; i < rest.length; i += 1) {
    const arg = rest[i];
    if (arg === "--queue") options.queue = rest[++i];
    else if (arg === "--max-items") options.maxItems = Number(rest[++i]);
    else if (arg === "--limit-per-listing") options.limitPerListing = Number(rest[++i]);
    else if (arg === "--max-subreddits") options.maxSubreddits = Number(rest[++i]);
    else if (arg === "--rss-delay-ms") options.rssDelayMs = Number(rest[++i]);
    else throw new Error(`Unknown argument: ${arg}`);
  }
  return { command, options };
}

export async function main(argv = process.argv.slice(2)): Promise<number> {
  await loadDotEnv();
  const { command, options } = parseArgs(argv);
  if (command !== "collect") throw new Error(`Unknown command: ${command}`);
  const result = await collectRedditSourceQueue({
    queuePath: options.queue,
    maxItems: options.maxItems,
    limitPerListing: options.limitPerListing,
    maxSubreddits: options.maxSubreddits,
    rssDelayMs: options.rssDelayMs,
  });
  console.log(JSON.stringify({
    updatedAt: result.queue.updatedAt,
    fetched: result.fetched,
    added: result.added,
    unread: result.unread,
  }, null, 2));
  return 0;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((error) => {
    console.error(error instanceof Error ? error.message : String(error));
    process.exitCode = 1;
  });
}
