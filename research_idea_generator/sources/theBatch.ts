import type { SourceItem } from "../../sourcing/types.ts";
import { compactText, mapLimit, normalizeWhitespace, stableSourceItemId, toIsoDate } from "../../sourcing/utils.ts";

export const THE_BATCH_INDEX_URL = "https://www.deeplearning.ai/the-batch";
const THE_BATCH_BASE_URL = "https://www.deeplearning.ai/the-batch";
const DEFAULT_USER_AGENT = "carousel-app-research-idea-generator/0.1";

interface TheBatchTag {
  name?: string | null;
  slug?: string | null;
}

export interface TheBatchPost {
  id?: string | null;
  title?: string | null;
  slug?: string | null;
  url?: string | null;
  feature_image?: string | null;
  custom_excerpt?: string | null;
  excerpt?: string | null;
  published_at?: string | null;
  tags?: TheBatchTag[];
  primary_tag?: TheBatchTag | null;
}

export interface TheBatchSourceLink {
  text: string;
  url: string;
}

interface TheBatchNextData {
  props?: {
    pageProps?: {
      posts?: TheBatchPost[];
      post?: TheBatchPost & {
        html?: string | null;
        reading_time?: number | null;
        feature_image_alt?: string | null;
        feature_image_caption?: string | null;
      };
      tag?: TheBatchTag & { url?: string | null };
    };
  };
}

export interface TheBatchCollectorOptions {
  indexUrl?: string;
  issueTagUrl?: string;
  days?: number;
  now?: Date;
  maxItems?: number;
}

function decodeHtmlEntityFallback(value: string): string {
  return value
    .replace(/&quot;/g, "\"")
    .replace(/&#x27;/g, "'")
    .replace(/&#39;/g, "'")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">");
}

export function extractTheBatchNextData(html: string): TheBatchNextData | undefined {
  const match = html.match(/<script[^>]+\bid=(["'])__NEXT_DATA__\1[^>]*>([\s\S]*?)<\/script>/i);
  if (!match) return undefined;
  try {
    return JSON.parse(match[2]) as TheBatchNextData;
  } catch {
    return JSON.parse(decodeHtmlEntityFallback(match[2])) as TheBatchNextData;
  }
}

function tagSlug(tag: TheBatchTag): string {
  return normalizeWhitespace(tag.slug).toLowerCase();
}

function tagName(tag: TheBatchTag): string {
  return normalizeWhitespace(tag.name).toLowerCase();
}

function isIssueTag(tag: TheBatchTag): boolean {
  const slug = tagSlug(tag);
  return slug.startsWith("issue-") || slug === "the-batch";
}

function isDateTag(tag: TheBatchTag): boolean {
  const slug = tagSlug(tag);
  const name = tagName(tag);
  return /^[a-z]{3}-\d{1,2}-\d{4}$/.test(slug) || /^[a-z]{3,9}\s+\d{1,2},\s+\d{4}$/.test(name);
}

function latestIssuePost(posts: TheBatchPost[]): TheBatchPost | undefined {
  return posts.find((post) => (post.tags ?? []).some((tag) => tagSlug(tag) === "the-batch"))
    ?? posts.find((post) => normalizeWhitespace(post.slug).startsWith("issue-"))
    ?? posts[0];
}

export function latestTheBatchIssueTagUrlFromIndexHtml(html: string, indexUrl = THE_BATCH_INDEX_URL): string | undefined {
  const data = extractTheBatchNextData(html);
  const posts = data?.props?.pageProps?.posts ?? [];
  const issue = latestIssuePost(posts);
  const dateTag = issue?.tags?.find((tag) => isDateTag(tag) && !isIssueTag(tag));
  if (!dateTag?.slug) return undefined;
  return new URL(`/the-batch/tag/${dateTag.slug}`, indexUrl).toString();
}

function absoluteUrl(value: string | null | undefined, base = THE_BATCH_BASE_URL): string {
  const text = normalizeWhitespace(value);
  if (!text) return "";
  try {
    return new URL(text, base).toString().replace(/\/$/, "");
  } catch {
    return text;
  }
}

function storyUrlForPost(post: TheBatchPost): string {
  const slug = normalizeWhitespace(post.slug);
  const candidate = absoluteUrl(post.url);
  if (candidate.includes("deeplearning.ai/the-batch/") && !candidate.endsWith("/the-batch")) {
    return candidate;
  }
  return slug ? `${THE_BATCH_BASE_URL}/${encodeURIComponent(slug)}` : candidate;
}

function isNewsletterIssuePost(post: TheBatchPost): boolean {
  const slug = normalizeWhitespace(post.slug).toLowerCase();
  const tags = post.tags ?? [];
  return slug.startsWith("issue-")
    || tags.some((tag) => tagSlug(tag) === "the-batch" || tagName(tag) === "the batch newsletter");
}

function postTags(post: TheBatchPost): string[] {
  return (post.tags ?? [])
    .map((tag) => normalizeWhitespace(tag.name || tag.slug))
    .filter(Boolean);
}

function postBody(post: TheBatchPost): string {
  const tags = postTags(post).filter((tag) => !/^issue-/i.test(tag) && !/^\w{3,9}\s+\d{1,2},\s+\d{4}$/i.test(tag));
  return normalizeWhitespace([
    post.custom_excerpt ?? post.excerpt ?? "",
    tags.length ? `Tags: ${tags.join(", ")}` : "",
  ].filter(Boolean).join("\n"));
}

function decodeHtmlEntities(value: string): string {
  return value
    .replace(/&#x([0-9a-fA-F]+);/g, (_match, hex) => String.fromCodePoint(Number.parseInt(hex, 16)))
    .replace(/&#([0-9]+);/g, (_match, decimal) => String.fromCodePoint(Number.parseInt(decimal, 10)))
    .replace(/&nbsp;/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, "\"")
    .replace(/&apos;|&#x27;|&#39;/g, "'");
}

function compactPreservingLines(value: string, limit: number): string {
  const text = value
    .split(/\n+/)
    .map((line) => normalizeWhitespace(line))
    .filter(Boolean)
    .join("\n");
  if (text.length <= limit) return text;
  return `${text.slice(0, Math.max(0, limit - 3)).trimEnd()}...`;
}

export function theBatchHtmlToText(html: string): string {
  const text = decodeHtmlEntities(html)
    .replace(/<!--kg-card-begin: html-->[\s\S]*?<!--kg-card-end: html-->/g, "\n")
    .replace(/<script\b[\s\S]*?<\/script>/gi, "\n")
    .replace(/<style\b[\s\S]*?<\/style>/gi, "\n")
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/<\/(?:p|li|h[1-6]|blockquote)>/gi, "\n")
    .replace(/<li\b[^>]*>/gi, "\n- ")
    .replace(/<[^>]*>/g, " ")
    .split(/\n+/)
    .map((line) => normalizeWhitespace(line))
    .filter(Boolean)
    .join("\n");
  return text;
}

export function theBatchHtmlLinks(html: string, baseUrl = THE_BATCH_BASE_URL): TheBatchSourceLink[] {
  const links: TheBatchSourceLink[] = [];
  const seen = new Set<string>();
  const linkPattern = /<a\b[^>]*\bhref=(["'])(.*?)\1[^>]*>([\s\S]*?)<\/a>/gi;
  for (const match of html.matchAll(linkPattern)) {
    const href = decodeHtmlEntities(match[2]);
    const url = absoluteUrl(href, baseUrl);
    if (!url || seen.has(url)) continue;
    const text = compactText(theBatchHtmlToText(match[3]), 120);
    if (!text) continue;
    seen.add(url);
    links.push({ text, url });
  }
  return links.slice(0, 20);
}

function fullStoryBody(post: TheBatchPost, articleText: string, tags: string[]): string {
  return compactPreservingLines([
    normalizeWhitespace(post.title) ? `Title: ${normalizeWhitespace(post.title)}` : "",
    normalizeWhitespace(post.custom_excerpt ?? post.excerpt) ? `Summary: ${normalizeWhitespace(post.custom_excerpt ?? post.excerpt)}` : "",
    articleText ? `Article:\n${articleText}` : "",
    tags.length ? `Tags: ${tags.join(", ")}` : "",
  ].filter(Boolean).join("\n"), 6_000);
}

export function theBatchPostToSourceItem(
  post: TheBatchPost,
  options: { issueTagSlug?: string; issueTagUrl?: string; index?: number } = {},
): SourceItem | null {
  const slug = normalizeWhitespace(post.slug);
  const title = normalizeWhitespace(post.title);
  if (!slug || !title || isNewsletterIssuePost(post)) return null;

  const imageUrl = absoluteUrl(post.feature_image);
  const publishedAt = toIsoDate(post.published_at);
  const score = Math.max(1, 100 - (options.index ?? 0) * 5);
  const url = storyUrlForPost(post);
  return {
    id: stableSourceItemId("the_batch", slug),
    source: "the_batch",
    externalId: slug,
    url,
    title,
    body: postBody(post),
    author: "DeepLearning.AI",
    createdAt: publishedAt,
    metrics: {
      score,
      upvotes: score,
    },
    media: {
      hasVideo: false,
      hasImage: Boolean(imageUrl),
      imageUrl: imageUrl || undefined,
      provider: imageUrl ? "the_batch_feature_image" : undefined,
    },
    raw: {
      issueTagSlug: options.issueTagSlug,
      issueTagUrl: options.issueTagUrl,
      tags: postTags(post),
      post,
    },
  };
}

export function enrichTheBatchSourceItemFromHtml(item: SourceItem, html: string): SourceItem {
  const data = extractTheBatchNextData(html);
  const post = data?.props?.pageProps?.post;
  if (!post) return item;
  const articleText = theBatchHtmlToText(post.html ?? "");
  const sourceLinks = theBatchHtmlLinks(post.html ?? "", item.url);
  const tags = postTags(post);
  const body = fullStoryBody(post, articleText, tags);
  const imageUrl = absoluteUrl(post.feature_image) || item.media.imageUrl;
  const baseRaw = item.raw && typeof item.raw === "object" ? item.raw as Record<string, unknown> : {};
  const mediaRaw = item.media.raw && typeof item.media.raw === "object"
    ? item.media.raw as Record<string, unknown>
    : {};
  return {
    ...item,
    title: normalizeWhitespace(post.title) || item.title,
    body: body || item.body,
    url: storyUrlForPost(post) || item.url,
    createdAt: toIsoDate(post.published_at) || item.createdAt,
    media: {
      ...item.media,
      hasImage: Boolean(imageUrl),
      imageUrl,
      provider: imageUrl ? "the_batch_feature_image" : item.media.provider,
      raw: {
        ...mediaRaw,
        featureImageAlt: normalizeWhitespace(post.feature_image_alt) || undefined,
        featureImageCaption: normalizeWhitespace(post.feature_image_caption) || undefined,
      },
    },
    raw: {
      ...baseRaw,
      tags,
      fullPost: {
        id: post.id,
        slug: post.slug,
        readingTime: post.reading_time,
        primaryTag: post.primary_tag?.name,
      },
      fullStory: {
        title: normalizeWhitespace(post.title) || item.title,
        summary: normalizeWhitespace(post.custom_excerpt ?? post.excerpt ?? ""),
        articleText,
        tags,
        sourceLinks,
        featureImageAlt: normalizeWhitespace(post.feature_image_alt) || undefined,
        featureImageCaption: normalizeWhitespace(post.feature_image_caption) || undefined,
      },
    },
  };
}

async function enrichTheBatchSourceItem(item: SourceItem): Promise<SourceItem> {
  try {
    return enrichTheBatchSourceItemFromHtml(item, await fetchText(item.url));
  } catch (error) {
    console.warn(`[the_batch] could not fetch full story ${item.url}; using tag excerpt: ${(error as Error).message}`);
    return item;
  }
}

export function theBatchTagPageToSourceItems(
  html: string,
  options: { issueTagUrl?: string; maxItems?: number } = {},
): SourceItem[] {
  const data = extractTheBatchNextData(html);
  const posts = data?.props?.pageProps?.posts ?? [];
  const issueTagSlug = data?.props?.pageProps?.tag?.slug ?? undefined;
  return posts
    .map((post, index) => theBatchPostToSourceItem(post, {
      issueTagSlug: normalizeWhitespace(issueTagSlug) || undefined,
      issueTagUrl: options.issueTagUrl,
      index,
    }))
    .filter((item): item is SourceItem => Boolean(item))
    .slice(0, options.maxItems ?? 80);
}

function isRecent(item: SourceItem, days: number | undefined, now = new Date()): boolean {
  if (days === undefined) return true;
  const created = Date.parse(item.createdAt);
  if (!Number.isFinite(created)) return false;
  return now.getTime() - created <= Math.max(1, days) * 86_400_000;
}

async function fetchText(url: string, options: { timeoutMs?: number } = {}): Promise<string> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), options.timeoutMs ?? 25_000);
  try {
    const response = await fetch(url, {
      signal: controller.signal,
      headers: {
        "User-Agent": DEFAULT_USER_AGENT,
        Accept: "text/html,application/xhtml+xml",
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

export async function fetchTheBatchSourceItems(options: TheBatchCollectorOptions = {}): Promise<SourceItem[]> {
  const indexUrl = options.indexUrl ?? THE_BATCH_INDEX_URL;
  const issueTagUrl = options.issueTagUrl
    ?? latestTheBatchIssueTagUrlFromIndexHtml(await fetchText(indexUrl), indexUrl);
  if (!issueTagUrl) return [];
  const html = await fetchText(issueTagUrl);
  const items = theBatchTagPageToSourceItems(html, {
    issueTagUrl,
    maxItems: options.maxItems,
  }).filter((item) => isRecent(item, options.days, options.now));
  return mapLimit(items, 3, enrichTheBatchSourceItem);
}
