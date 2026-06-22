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
  /** Pull fresh posts live via xAI x_search instead of relying only on the queue. */
  live?: boolean;
  liveLimit?: number;
  /** Drop items whose post date is older than this many days (0 = no filter). */
  recencyDays?: number;
}

// Engagement-bait patterns the live model tends to surface; demote/drop these.
const X_BAIT = /\b(deleting (this|in)|comment ['"]?(send|yes|leads|go)\b|drop a ['"]|rt (to|for)|giveaway|like \+ rt|follow \+ rt)\b/i;

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

function isoDaysAgo(days: number): string {
  return new Date(Date.now() - days * 86_400_000).toISOString().slice(0, 10);
}

function liveXPostToSourceItem(post: Record<string, unknown>): SourceItem | null {
  const url = normalizeWhitespace(post.url).replace("twitter.com", "x.com");
  const statusId = url.match(/status\/(\d+)/)?.[1] ?? "";
  if (!statusId) return null;
  return xPostToSourceItem(
    {
      id: statusId,
      text: post.text,
      handle: post.handle,
      author: post.author,
      date: post.date,
      url,
      likes: post.likes,
      retweets: post.reposts ?? post.retweets,
      replies: post.replies,
      views: post.views,
      has_video: post.has_video,
    },
    post,
  );
}

/** Fetch fresh, high-engagement AI posts live via xAI x_search. */
async function fetchXLivePosts(limit: number, recencyDays: number): Promise<SourceItem[]> {
  const token = process.env.XAI_API_KEY;
  if (!token) return [];
  const model = process.env.XAI_TWEET_MODEL || "grok-4.3";
  const days = Math.max(1, recencyDays || 3);
  const prompt = [
    `Using x_search, find ${limit} of the highest-engagement AI / AI-tooling posts on X`,
    `published between ${isoDaysAgo(days)} and ${isoDaysAgo(0)} (the last ~${days} days).`,
    "Prefer credible/verified accounts (labs, founders, researchers) showing real demand",
    "(high likes/reposts/views), ideally with video. Return STRICT JSON ONLY: an array of",
    "objects with keys handle, author, date, url, likes, reposts, replies, views, has_video, text. No prose.",
  ].join(" ");
  let response: Response;
  try {
    response = await fetch("https://api.x.ai/v1/responses", {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ model, input: prompt, tools: [{ type: "x_search" }] }),
    });
  } catch (error) {
    console.warn(`[x] live search failed: ${(error as Error).message}`);
    return [];
  }
  if (!response.ok) {
    console.warn(`[x] live search HTTP ${response.status}`);
    return [];
  }
  const data = await response.json() as { output?: Array<{ content?: Array<{ type?: string; text?: string }> }> };
  const text = data.output
    ?.flatMap((part) => part.content ?? [])
    .filter((part) => part.type === "output_text")
    .map((part) => part.text ?? "")
    .join("") ?? "";
  const start = text.indexOf("[");
  const end = text.lastIndexOf("]");
  if (start < 0 || end <= start) return [];
  let posts: Array<Record<string, unknown>>;
  try {
    posts = JSON.parse(text.slice(start, end + 1)) as Array<Record<string, unknown>>;
  } catch {
    return [];
  }
  return posts.map(liveXPostToSourceItem).filter((item): item is SourceItem => Boolean(item));
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
  if (options.live) {
    items.push(...await fetchXLivePosts(options.liveLimit ?? 15, options.recencyDays ?? 3));
  }

  // Drop engagement-bait and (when requested) stale posts so nothing old surfaces.
  const recencyDays = options.recencyDays ?? 0;
  const cutoff = recencyDays > 0 ? Date.now() - recencyDays * 86_400_000 : 0;
  const filtered = items.filter((item) => {
    if (X_BAIT.test(item.title)) return false;
    if (cutoff) {
      const created = Date.parse(item.createdAt);
      if (Number.isFinite(created) && created < cutoff) return false;
    }
    return true;
  });

  const byId = new Map(filtered.map((item) => [item.id, item]));
  const unique = [...byId.values()];
  if (options.includeTopReply === false) return unique;
  const cachePath = options.topReplyCachePath ?? "out/automation/sourcing/x-top-replies.json";
  return Promise.all(unique.map((item) => withCachedTopReply(item, cachePath)));
}
