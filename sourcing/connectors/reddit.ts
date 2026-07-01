import type { SourceItem, SourceMedia, TopReply } from "../types.ts";
import {
  fetchJson,
  mapLimit,
  normalizeWhitespace,
  numberValue,
  stableSourceItemId,
  toIsoDate,
} from "../utils.ts";

export const DEFAULT_REDDIT_SUBREDDITS = [
  "singularity",
  "LocalLLaMA",
  "StableDiffusion",
  "aivideo",
  "ChatGPT",
  "OpenAI",
  "artificial",
  "ai_agents",
  "ChatGPTCoding",
  "InternetIsBeautiful",
  "nextfuckinglevel",
  "Damnthatsinteresting",
];

const VIDEO_DOMAINS = [
  "v.redd.it",
  "youtube.com",
  "youtu.be",
  "tiktok.com",
  "instagram.com/reel",
  "streamable.com",
  "redgifs.com",
  "gfycat.com",
  "imgur.com",
  "vimeo.com",
  "x.com",
  "twitter.com",
];

export interface RedditListing {
  kind: "top" | "rising";
  time?: "day" | "week";
}

export interface RedditListingEvent {
  subreddit: string;
  listing: string;
  url: string;
  ok: boolean;
  count?: number;
  error?: string;
}

export interface RedditConnectorOptions {
  subreddits?: string[];
  limitPerListing?: number;
  maxItems?: number;
  concurrency?: number;
  rssDelayMs?: number;
  rssOnly?: boolean;
  includeTopReply?: boolean;
  topReplyConcurrency?: number;
  onListing?: (event: RedditListingEvent) => void;
}

const LISTINGS: RedditListing[] = [
  { kind: "top", time: "day" },
  { kind: "top", time: "week" },
  { kind: "rising" },
];

interface RedditRequestContext {
  oauth: boolean;
  headers: Record<string, string>;
}

let redditTokenCache: { token: string; expiresAt: number } | undefined;

export function redditListingUrl(
  subreddit: string,
  listing: RedditListing,
  limit: number,
  options: { oauth?: boolean } = {},
): string {
  const params = new URLSearchParams({ limit: String(limit), raw_json: "1" });
  if (listing.time) params.set("t", listing.time);
  const base = (options.oauth ? "https://oauth.reddit.com" : process.env.REDDIT_JSON_BASE_URL || "https://www.reddit.com").replace(/\/$/, "");
  const suffix = options.oauth ? "" : ".json";
  return `${base}/r/${encodeURIComponent(subreddit)}/${listing.kind}${suffix}?${params.toString()}`;
}

export function redditRssListingUrl(subreddit: string, listing: RedditListing, limit: number): string {
  const params = new URLSearchParams({ limit: String(limit) });
  if (listing.time) params.set("t", listing.time);
  const base = (process.env.REDDIT_RSS_BASE_URL || "https://www.reddit.com").replace(/\/$/, "");
  return `${base}/r/${encodeURIComponent(subreddit)}/${listing.kind}/.rss?${params.toString()}`;
}

function redditBaseHeaders(): Record<string, string> {
  const headers: Record<string, string> = {
    "User-Agent": process.env.REDDIT_USER_AGENT || "carousel-app-ai-news-bot/0.1",
    Accept: "application/json,text/json;q=0.9,*/*;q=0.5",
  };
  return headers;
}

function redditRssHeaders(): Record<string, string> {
  return {
    ...redditBaseHeaders(),
    Accept: "application/atom+xml,application/rss+xml,application/xml,text/xml;q=0.9,*/*;q=0.5",
  };
}

function redditPublicHeaders(): Record<string, string> {
  const headers = redditBaseHeaders();
  if (process.env.REDDIT_AUTHORIZATION) {
    headers.Authorization = process.env.REDDIT_AUTHORIZATION;
  }
  if (process.env.REDDIT_COOKIE) {
    headers.Cookie = process.env.REDDIT_COOKIE;
  }
  return headers;
}

async function redditOAuthToken(): Promise<string | undefined> {
  if (process.env.REDDIT_BEARER_TOKEN) return process.env.REDDIT_BEARER_TOKEN;
  if (redditTokenCache && redditTokenCache.expiresAt > Date.now() + 60_000) {
    return redditTokenCache.token;
  }
  const clientId = process.env.REDDIT_CLIENT_ID;
  if (!clientId) return undefined;
  const clientSecret = process.env.REDDIT_CLIENT_SECRET ?? "";
  const params = new URLSearchParams();
  if (clientSecret) {
    params.set("grant_type", "client_credentials");
  } else {
    params.set("grant_type", "https://oauth.reddit.com/grants/installed_client");
    params.set("device_id", process.env.REDDIT_DEVICE_ID || "DO_NOT_TRACK_THIS_DEVICE");
  }

  const response = await fetch("https://www.reddit.com/api/v1/access_token", {
    method: "POST",
    headers: {
      ...redditBaseHeaders(),
      Authorization: `Basic ${Buffer.from(`${clientId}:${clientSecret}`).toString("base64")}`,
      "Content-Type": "application/x-www-form-urlencoded",
    },
    body: params.toString(),
  });
  if (!response.ok) {
    throw new Error(`Reddit OAuth token returned HTTP ${response.status}: ${await response.text()}`);
  }
  const data = await response.json() as { access_token?: string; expires_in?: number };
  if (!data.access_token) throw new Error("Reddit OAuth token response did not include access_token");
  redditTokenCache = {
    token: data.access_token,
    expiresAt: Date.now() + Math.max(60, Number(data.expires_in ?? 3600) - 60) * 1000,
  };
  return redditTokenCache.token;
}

async function redditRequestContext(): Promise<RedditRequestContext> {
  if (process.env.REDDIT_AUTHORIZATION?.toLowerCase().startsWith("bearer ")) {
    return {
      oauth: true,
      headers: {
        ...redditBaseHeaders(),
        Authorization: process.env.REDDIT_AUTHORIZATION,
      },
    };
  }
  const token = await redditOAuthToken();
  if (token) {
    return {
      oauth: true,
      headers: {
        ...redditBaseHeaders(),
        Authorization: `Bearer ${token}`,
      },
    };
  }
  return {
    oauth: false,
    headers: redditPublicHeaders(),
  };
}

export function clearRedditTokenCacheForTests(): void {
  redditTokenCache = undefined;
}

async function fetchText(url: string, options: { timeoutMs?: number; headers?: Record<string, string> } = {}): Promise<string> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), options.timeoutMs ?? 25_000);
  try {
    const response = await fetch(url, {
      signal: controller.signal,
      headers: {
        "User-Agent": "carousel-app/1.0 ai-news-source-pipeline",
        Accept: "text/plain,*/*",
        ...options.headers,
      },
    });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status} for ${url}`);
    }
    return await response.text();
  } finally {
    clearTimeout(timeout);
  }
}

function decodeXmlEntities(value: string): string {
  return value
    .replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g, "$1")
    .replace(/&#x([0-9a-fA-F]+);/g, (_match, hex) => String.fromCodePoint(Number.parseInt(hex, 16)))
    .replace(/&#([0-9]+);/g, (_match, decimal) => String.fromCodePoint(Number.parseInt(decimal, 10)))
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, "\"")
    .replace(/&apos;/g, "'");
}

function stripXmlHtml(value: string): string {
  return normalizeWhitespace(
    decodeXmlEntities(value)
      .replace(/<br\s*\/?>/gi, "\n")
      .replace(/<\/p>/gi, "\n")
      .replace(/<[^>]*>/g, " "),
  );
}

function xmlTag(block: string, tag: string): string {
  const match = block.match(new RegExp(`<${tag}\\b[^>]*>([\\s\\S]*?)<\\/${tag}>`, "i"));
  return match ? decodeXmlEntities(match[1]) : "";
}

function xmlTagText(block: string, tag: string): string {
  return stripXmlHtml(xmlTag(block, tag));
}

function xmlLink(block: string): string {
  const atom = block.match(/<link\b[^>]*\bhref=(["'])(.*?)\1[^>]*\/?>/i);
  if (atom) return decodeXmlEntities(atom[2]);
  return xmlTagText(block, "link");
}

function redditIdFromUrl(url: string): string {
  const match = url.match(/\/comments\/([a-z0-9]+)/i);
  return match ? `t3_${match[1]}` : "";
}

function rssAuthor(block: string): string {
  const authorBlock = xmlTag(block, "author");
  const atomName = authorBlock ? xmlTagText(authorBlock, "name") : "";
  return normalizeWhitespace(atomName || xmlTagText(block, "dc:creator") || xmlTagText(block, "author")).replace(/^\/?u\//i, "");
}

export function redditRssEntryToSourceItem(
  block: string,
  options: { subreddit: string; listing: RedditListing; feedUrl: string },
): SourceItem | null {
  const title = xmlTagText(block, "title");
  const url = xmlLink(block);
  const externalId = normalizeWhitespace(xmlTagText(block, "id") || xmlTagText(block, "guid") || redditIdFromUrl(url));
  if (!title || !url || !externalId) return null;
  const body = normalizeWhitespace(
    stripXmlHtml(xmlTag(block, "content") || xmlTag(block, "description") || xmlTag(block, "summary")),
  );
  return {
    id: stableSourceItemId("reddit", externalId),
    source: "reddit",
    externalId,
    url,
    title,
    body,
    author: rssAuthor(block),
    createdAt: toIsoDate(xmlTagText(block, "published") || xmlTagText(block, "updated") || xmlTagText(block, "pubDate")),
    subreddit: options.subreddit,
    metrics: {
      upvotes: 1,
      score: 1,
      comments: 0,
    },
    media: detectRedditMedia({ url }),
    raw: {
      connector: "rss",
      feedUrl: options.feedUrl,
      listing: options.listing,
    },
  };
}

export function redditRssFeedToSourceItems(
  xml: string,
  options: { subreddit: string; listing: RedditListing; feedUrl: string },
): SourceItem[] {
  const blocks = xml.match(/<entry\b[\s\S]*?<\/entry>/gi)
    ?? xml.match(/<item\b[\s\S]*?<\/item>/gi)
    ?? [];
  return blocks
    .map((block) => redditRssEntryToSourceItem(block, options))
    .filter((item): item is SourceItem => Boolean(item));
}

function nestedValue(data: Record<string, unknown>, path: string[]): unknown {
  let current: unknown = data;
  for (const part of path) {
    if (!current || typeof current !== "object") return undefined;
    current = (current as Record<string, unknown>)[part];
  }
  return current;
}

export function detectRedditMedia(post: Record<string, unknown>): SourceMedia {
  const directUrl = normalizeWhitespace(post.url_overridden_by_dest ?? post.url);
  const fallbackUrl = normalizeWhitespace(
    nestedValue(post, ["media", "reddit_video", "fallback_url"])
      ?? nestedValue(post, ["secure_media", "reddit_video", "fallback_url"])
      ?? nestedValue(post, ["preview", "reddit_video_preview", "fallback_url"]),
  );
  const mediaUrl = fallbackUrl || directUrl;
  const urlLower = mediaUrl.toLowerCase();
  const isKnownVideo = VIDEO_DOMAINS.some((domain) => urlLower.includes(domain));
  const isGifv = /\.(gifv|mp4)(?:\?|$)/i.test(mediaUrl);
  const hasVideo = Boolean(post.is_video) || Boolean(fallbackUrl) || isKnownVideo || isGifv;
  const imageUrl = /\.(png|jpe?g|webp)(?:\?|$)/i.test(directUrl) ? directUrl : "";
  const durationSeconds = numberValue(
    nestedValue(post, ["media", "reddit_video", "duration"])
      ?? nestedValue(post, ["secure_media", "reddit_video", "duration"]),
    0,
  );
  return {
    hasVideo,
    hasImage: Boolean(imageUrl),
    videoUrl: hasVideo ? mediaUrl : undefined,
    imageUrl: imageUrl || undefined,
    durationSeconds: durationSeconds || undefined,
    provider: hasVideo ? (urlLower.includes("v.redd.it") ? "reddit_video" : "external_video") : undefined,
    raw: {
      is_video: post.is_video,
      media: post.media,
      secure_media: post.secure_media,
      url: directUrl,
    },
  };
}

export function redditPostToSourceItem(post: Record<string, unknown>): SourceItem | null {
  const externalId = normalizeWhitespace(post.name ?? post.id);
  const shortId = normalizeWhitespace(post.id);
  const subreddit = normalizeWhitespace(post.subreddit);
  const permalink = normalizeWhitespace(post.permalink);
  const title = normalizeWhitespace(post.title);
  if (!externalId || !shortId || !subreddit || !title) return null;

  return {
    id: stableSourceItemId("reddit", externalId),
    source: "reddit",
    externalId,
    url: permalink.startsWith("http") ? permalink : `https://www.reddit.com${permalink}`,
    title,
    body: normalizeWhitespace(post.selftext),
    author: normalizeWhitespace(post.author),
    createdAt: toIsoDate(numberValue(post.created_utc, 0)),
    subreddit,
    metrics: {
      upvotes: numberValue(post.ups),
      score: numberValue(post.score),
      comments: numberValue(post.num_comments),
      awards: numberValue(post.total_awards_received),
      upvoteRatio: numberValue(post.upvote_ratio),
    },
    media: detectRedditMedia(post),
    raw: post,
  };
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function fetchRssListing(subreddit: string, listing: RedditListing, limit: number): Promise<SourceItem[]> {
  const feedUrl = redditRssListingUrl(subreddit, listing, limit);
  const xml = await fetchText(feedUrl, {
    timeoutMs: 25_000,
    headers: redditRssHeaders(),
  });
  const items = redditRssFeedToSourceItems(xml, { subreddit, listing, feedUrl });
  if (items.length) return items;
  throw new Error(`RSS feed returned no parseable entries for ${feedUrl}`);
}

async function fetchListing(
  subreddit: string,
  listing: RedditListing,
  limit: number,
  options: { rssOnly?: boolean } = {},
): Promise<SourceItem[]> {
  if (options.rssOnly) {
    return fetchRssListing(subreddit, listing, limit);
  }
  try {
    const context = await redditRequestContext();
    const url = redditListingUrl(subreddit, listing, limit, { oauth: context.oauth });
    const json = await fetchJson(url, {
      timeoutMs: 25_000,
      headers: context.headers,
    });
    const children = (json as { data?: { children?: Array<{ data?: Record<string, unknown> }> } }).data?.children ?? [];
    return children
      .map((child) => child.data ? redditPostToSourceItem(child.data) : null)
      .filter((item): item is SourceItem => Boolean(item));
  } catch (jsonError) {
    try {
      return await fetchRssListing(subreddit, listing, limit);
    } catch (rssError) {
      throw new Error(`json failed: ${(jsonError as Error).message}; rss failed: ${(rssError as Error).message}`);
    }
  }
}

function isHumanComment(comment: Record<string, unknown>): boolean {
  const body = normalizeWhitespace(comment.body);
  const author = normalizeWhitespace(comment.author).toLowerCase();
  if (body.length < 8 || body.length > 200) return false;
  if (!author || author === "automoderator" || author.endsWith("bot")) return false;
  if (body === "[deleted]" || body === "[removed]") return false;
  if (comment.stickied || comment.distinguished) return false;
  return true;
}

export async function fetchRedditTopReply(item: SourceItem): Promise<TopReply | undefined> {
  const redditId = item.externalId.replace(/^t3_/, "");
  if (!item.subreddit || !redditId) return undefined;
  const params = new URLSearchParams({ sort: "top", limit: "30", raw_json: "1" });
  const context = await redditRequestContext();
  const base = context.oauth ? "https://oauth.reddit.com" : "https://www.reddit.com";
  const suffix = context.oauth ? "" : ".json";
  const url = `${base}/r/${encodeURIComponent(item.subreddit)}/comments/${encodeURIComponent(redditId)}${suffix}?${params}`;
  const json = await fetchJson(url, {
    timeoutMs: 25_000,
    headers: context.headers,
  });
  const commentsListing = Array.isArray(json) ? json[1] : undefined;
  const children = (commentsListing as { data?: { children?: Array<{ kind?: string; data?: Record<string, unknown> }> } } | undefined)
    ?.data?.children ?? [];
  return redditCommentChildrenToTopReply(children);
}

export function redditCommentChildrenToTopReply(
  children: Array<{ kind?: string; data?: Record<string, unknown> }>,
): TopReply | undefined {
  const comments = children
    .filter((child) => child.kind === "t1" && child.data && isHumanComment(child.data))
    .map((child) => child.data as Record<string, unknown>)
    .sort((a, b) => numberValue(b.score) - numberValue(a.score));
  const top = comments[0];
  if (!top) return undefined;
  return {
    author: normalizeWhitespace(top.author),
    body: normalizeWhitespace(top.body),
    score: numberValue(top.score),
    createdAt: toIsoDate(numberValue(top.created_utc, 0)),
    source: "comment",
    raw: top,
  };
}

function priorityKey(item: SourceItem): [number, number, number, string] {
  return [
    item.media.hasVideo ? 0 : 1,
    -(item.metrics.score ?? item.metrics.upvotes ?? 0),
    -(item.metrics.comments ?? 0),
    item.id,
  ];
}

function comparePriority(a: SourceItem, b: SourceItem): number {
  const left = priorityKey(a);
  const right = priorityKey(b);
  for (let i = 0; i < left.length; i += 1) {
    if (left[i] < right[i]) return -1;
    if (left[i] > right[i]) return 1;
  }
  return 0;
}

export async function fetchRedditSourceItems(options: RedditConnectorOptions = {}): Promise<SourceItem[]> {
  const subreddits = options.subreddits ?? DEFAULT_REDDIT_SUBREDDITS;
  const limit = options.limitPerListing ?? 35;
  const tasks = subreddits.flatMap((subreddit) => LISTINGS.map((listing) => ({ subreddit, listing })));
  const batches = await mapLimit(tasks, options.concurrency ?? 4, async (task, index) => {
    if (options.rssDelayMs && index > 0) {
      await sleep(options.rssDelayMs * index);
    }
    const url = redditListingUrl(task.subreddit, task.listing, limit, {
      oauth: Boolean(
        process.env.REDDIT_BEARER_TOKEN
          || process.env.REDDIT_CLIENT_ID
          || process.env.REDDIT_AUTHORIZATION?.toLowerCase().startsWith("bearer "),
      ),
    });
    const listingLabel = `${task.listing.kind}${task.listing.time ? `/${task.listing.time}` : ""}`;
    try {
      const items = await fetchListing(task.subreddit, task.listing, limit, { rssOnly: options.rssOnly });
      options.onListing?.({
        subreddit: task.subreddit,
        listing: listingLabel,
        url,
        ok: true,
        count: items.length,
      });
      return items;
    } catch (error) {
      const message = (error as Error).message;
      options.onListing?.({
        subreddit: task.subreddit,
        listing: listingLabel,
        url,
        ok: false,
        error: message,
      });
      console.warn(
        `[reddit] skipped r/${task.subreddit} ${listingLabel}: ${message}`,
      );
      return [];
    }
  });
  const byId = new Map<string, SourceItem>();
  for (const item of batches.flat()) {
    const previous = byId.get(item.id);
    if (!previous || (item.metrics.score ?? 0) > (previous.metrics.score ?? 0)) {
      byId.set(item.id, item);
    }
  }

  const prioritized = [...byId.values()].sort(comparePriority).slice(0, options.maxItems ?? 180);
  if (options.includeTopReply === false) return prioritized;

  return mapLimit(prioritized, options.topReplyConcurrency ?? 4, async (item) => {
    try {
      const topReply = await fetchRedditTopReply(item);
      return topReply ? { ...item, topReply } : item;
    } catch {
      return item;
    }
  });
}
