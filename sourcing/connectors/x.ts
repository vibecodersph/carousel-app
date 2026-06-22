import { spawn } from "node:child_process";
import type { SourceItem, TopReply } from "../types.ts";
import {
  normalizeWhitespace,
  numberValue,
  readJsonFile,
  stableSourceItemId,
  toIsoDate,
  writeJsonFile,
} from "../utils.ts";

export interface XConnectorOptions {
  urls?: string[];
  queuePath?: string;
  includeTopReply?: boolean;
  topReplyCachePath?: string;
}

function runCommand(command: string, args: string[], timeoutMs: number): Promise<string> {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { stdio: ["ignore", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    const timeout = setTimeout(() => {
      child.kill("SIGTERM");
      reject(new Error(`${command} ${args.join(" ")} timed out after ${timeoutMs}ms`));
    }, timeoutMs);
    child.stdout.on("data", (chunk) => {
      stdout += chunk.toString();
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk.toString();
    });
    child.on("error", (error) => {
      clearTimeout(timeout);
      reject(error);
    });
    child.on("close", (code) => {
      clearTimeout(timeout);
      if (code === 0) resolve(stdout);
      else reject(new Error(`${command} exited ${code}: ${stderr || stdout}`));
    });
  });
}

function xUrlFromPost(post: Record<string, unknown>): string {
  const direct = normalizeWhitespace(post.url);
  if (direct) return direct.replace("twitter.com", "x.com");
  const handle = normalizeWhitespace(post.handle).replace(/^@/, "");
  const id = normalizeWhitespace(post.id);
  return handle && id ? `https://x.com/${handle}/status/${id}` : "";
}

export function xPostToSourceItem(post: Record<string, unknown>, raw: unknown = post): SourceItem | null {
  const externalId = normalizeWhitespace(post.id);
  const title = normalizeWhitespace(post.text ?? post.full_text);
  const url = xUrlFromPost(post);
  if (!externalId || !title || !url) return null;
  return {
    id: stableSourceItemId("x", externalId),
    source: "x",
    externalId,
    url,
    title,
    body: normalizeWhitespace(post.why),
    author: normalizeWhitespace(post.author || post.author_name || post.handle),
    createdAt: toIsoDate(post.date),
    metrics: {
      likes: numberValue(post.likes),
      retweets: numberValue(post.retweets),
      replies: numberValue(post.replies),
      views: numberValue(post.views),
      comments: numberValue(post.replies),
      upvotes: numberValue(post.likes),
      score: numberValue(post.likes) + numberValue(post.retweets) * 2 + numberValue(post.replies),
    },
    media: {
      hasVideo: Boolean(post.has_video),
      provider: Boolean(post.has_video) ? "x_video" : undefined,
    },
    raw,
  };
}

async function fetchTweetViaExistingConnector(url: string): Promise<SourceItem | null> {
  const output = await runCommand("uv", ["run", "python", "fetch_tweet_data.py", url], 90_000);
  const jsonStart = output.indexOf("{");
  const jsonEnd = output.lastIndexOf("}");
  if (jsonStart < 0 || jsonEnd <= jsonStart) {
    throw new Error(`fetch_tweet_data.py did not return a JSON object for ${url}`);
  }
  const post = JSON.parse(output.slice(jsonStart, jsonEnd + 1)) as Record<string, unknown>;
  return xPostToSourceItem(post, post);
}

function extractXItemsFromQueue(queue: unknown): SourceItem[] {
  const candidates = (queue as { candidates?: unknown[] }).candidates ?? [];
  const items: SourceItem[] = [];
  for (const candidate of candidates) {
    const record = candidate as Record<string, unknown>;
    const post = record.post as Record<string, unknown> | undefined;
    if (!post) continue;
    const item = xPostToSourceItem(post, candidate);
    if (item) items.push(item);
  }
  return items;
}

async function xaiTopReplyOrQuote(item: SourceItem): Promise<TopReply | undefined> {
  const token = process.env.XAI_API_KEY;
  if (!token) return undefined;
  const model = process.env.XAI_TWEET_MODEL || "grok-4.3";
  const prompt = [
    `Look up public replies and quote tweets for this X post: ${item.url}`,
    "Choose the highest-signal reply or quote tweet that a curious developer would care about.",
    "Exclude spam, bots, giveaways, and replies shorter than 8 chars or longer than 200 chars.",
    "Return ONLY strict JSON with this shape:",
    "{\"author\":\"display or handle\",\"body\":\"reply or quote text\",\"score\":number,\"source\":\"reply or quote\"}",
    "If none exists, return {\"author\":\"\",\"body\":\"\",\"score\":0,\"source\":\"reply\"}.",
  ].join("\n");
  const response = await fetch("https://api.x.ai/v1/responses", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      model,
      input: prompt,
      tools: [{ type: "x_search" }],
    }),
  });
  if (!response.ok) return undefined;
  const data = await response.json() as { output?: Array<{ type?: string; content?: Array<{ type?: string; text?: string }> }> };
  const text = data.output
    ?.flatMap((part) => part.content ?? [])
    .filter((part) => part.type === "output_text")
    .map((part) => part.text ?? "")
    .join("") ?? "";
  const start = text.indexOf("{");
  const end = text.lastIndexOf("}");
  if (start < 0 || end <= start) return undefined;
  const parsed = JSON.parse(text.slice(start, end + 1)) as TopReply;
  if (!normalizeWhitespace(parsed.body)) return undefined;
  return {
    author: normalizeWhitespace(parsed.author),
    body: normalizeWhitespace(parsed.body),
    score: numberValue(parsed.score),
    source: normalizeWhitespace(parsed.source) || "reply",
  };
}

async function withCachedTopReply(item: SourceItem, cachePath: string): Promise<SourceItem> {
  const cache = await readJsonFile<Record<string, TopReply | null>>(cachePath, {});
  if (Object.hasOwn(cache, item.id)) {
    return cache[item.id] ? { ...item, topReply: cache[item.id] ?? undefined } : item;
  }
  const topReply = await xaiTopReplyOrQuote(item);
  cache[item.id] = topReply ?? null;
  await writeJsonFile(cachePath, cache);
  return topReply ? { ...item, topReply } : item;
}

export async function fetchXSourceItems(options: XConnectorOptions = {}): Promise<SourceItem[]> {
  const items: SourceItem[] = [];
  if (options.queuePath) {
    const queue = await readJsonFile<unknown>(options.queuePath, { candidates: [] });
    items.push(...extractXItemsFromQueue(queue));
  }
  for (const url of options.urls ?? []) {
    const item = await fetchTweetViaExistingConnector(url);
    if (item) items.push(item);
  }
  const byId = new Map(items.map((item) => [item.id, item]));
  const unique = [...byId.values()];
  if (options.includeTopReply === false) return unique;
  const cachePath = options.topReplyCachePath ?? "out/automation/sourcing/x-top-replies.json";
  return Promise.all(unique.map((item) => withCachedTopReply(item, cachePath)));
}
