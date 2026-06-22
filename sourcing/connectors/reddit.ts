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

interface RedditListing {
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
  includeTopReply?: boolean;
  topReplyConcurrency?: number;
  onListing?: (event: RedditListingEvent) => void;
}

const LISTINGS: RedditListing[] = [
  { kind: "top", time: "day" },
  { kind: "top", time: "week" },
  { kind: "rising" },
];

function redditListingUrl(subreddit: string, listing: RedditListing, limit: number): string {
  const params = new URLSearchParams({ limit: String(limit), raw_json: "1" });
  if (listing.time) params.set("t", listing.time);
  const base = (process.env.REDDIT_JSON_BASE_URL || "https://www.reddit.com").replace(/\/$/, "");
  return `${base}/r/${encodeURIComponent(subreddit)}/${listing.kind}.json?${params.toString()}`;
}

function redditHeaders(): Record<string, string> {
  const headers: Record<string, string> = {
    "User-Agent": process.env.REDDIT_USER_AGENT || "carousel-app-ai-news-bot/0.1",
    Accept: "application/json,text/json;q=0.9,*/*;q=0.5",
  };
  if (process.env.REDDIT_AUTHORIZATION) {
    headers.Authorization = process.env.REDDIT_AUTHORIZATION;
  }
  if (process.env.REDDIT_COOKIE) {
    headers.Cookie = process.env.REDDIT_COOKIE;
  }
  return headers;
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

async function fetchListing(subreddit: string, listing: RedditListing, limit: number): Promise<SourceItem[]> {
  const url = redditListingUrl(subreddit, listing, limit);
  const json = await fetchJson(url, {
    timeoutMs: 25_000,
    headers: redditHeaders(),
  });
  const children = (json as { data?: { children?: Array<{ data?: Record<string, unknown> }> } }).data?.children ?? [];
  return children
    .map((child) => child.data ? redditPostToSourceItem(child.data) : null)
    .filter((item): item is SourceItem => Boolean(item));
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
  const url = `https://www.reddit.com/r/${encodeURIComponent(item.subreddit)}/comments/${encodeURIComponent(redditId)}.json?${params}`;
  const json = await fetchJson(url, {
    timeoutMs: 25_000,
    headers: redditHeaders(),
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
  const batches = await mapLimit(tasks, options.concurrency ?? 4, async (task) => {
    const url = redditListingUrl(task.subreddit, task.listing, limit);
    const listingLabel = `${task.listing.kind}${task.listing.time ? `/${task.listing.time}` : ""}`;
    try {
      const items = await fetchListing(task.subreddit, task.listing, limit);
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
