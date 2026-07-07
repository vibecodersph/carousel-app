import type { SourceItem } from "../../sourcing/types.ts";
import { compactText, normalizeWhitespace, stableSourceItemId, toIsoDate } from "../../sourcing/utils.ts";

export const AI_NEWS_INDEX_URL = "https://news.smol.ai/";
export const AI_NEWS_RSS_URL = "https://news.smol.ai/rss.xml";
export const AI_NEWS_RAW_ISSUES_BASE_URL = "https://raw.githubusercontent.com/smol-ai/ainews-web-2025/main/src/content/issues";
const AI_NEWS_BASE_URL = "https://news.smol.ai/";
const DEFAULT_USER_AGENT = "carousel-app-ai-news-source/0.1";

export interface AiNewsSourceLink {
  text: string;
  url: string;
}

export interface AiNewsCollectorOptions {
  indexUrl?: string;
  rssUrl?: string;
  issueUrl?: string;
  days?: number;
  now?: Date;
  maxItems?: number;
}

interface AiNewsRssIssue {
  title: string;
  url: string;
  guid: string;
  description: string;
  pubDate: string;
  categories: string[];
}

interface StoryBlock {
  section: string;
  topic: string;
  title: string;
  text: string;
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

function absoluteUrl(value: string | null | undefined, base = AI_NEWS_BASE_URL): string {
  const text = normalizeWhitespace(value);
  if (!text) return "";
  try {
    return new URL(text, base).toString().replace(/\/$/, "");
  } catch {
    return text;
  }
}

function slugify(value: string): string {
  return normalizeWhitespace(value)
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 80);
}

function issueSlugFromUrl(value: string): string {
  try {
    const parts = new URL(value, AI_NEWS_BASE_URL).pathname.split("/").filter(Boolean);
    return parts[parts.length - 1] || "latest";
  } catch {
    return slugify(value) || "latest";
  }
}

function rawMarkdownUrlForIssue(issueUrl: string): string {
  return `${AI_NEWS_RAW_ISSUES_BASE_URL}/${encodeURIComponent(issueSlugFromUrl(issueUrl))}.md`;
}

function issueDateFromSlug(value: string): string | undefined {
  const slug = issueSlugFromUrl(value);
  const match = slug.match(/^(\d{2})-(\d{2})-(\d{2})\b/);
  if (!match) return undefined;
  const yy = Number(match[1]);
  const year = yy >= 70 ? 1900 + yy : 2000 + yy;
  const month = Number(match[2]);
  const day = Number(match[3]);
  if (!Number.isFinite(year) || !Number.isFinite(month) || !Number.isFinite(day)) return undefined;
  return new Date(Date.UTC(year, month - 1, day, 12, 0, 0)).toISOString();
}

export function latestAiNewsIssueUrlFromIndexHtml(html: string, indexUrl = AI_NEWS_INDEX_URL): string | undefined {
  const seen = new Set<string>();
  const links: string[] = [];
  const linkPattern = /<a\b[^>]*\bhref=(["'])(.*?)\1[^>]*>([\s\S]*?)<\/a>/gi;
  for (const match of html.matchAll(linkPattern)) {
    const href = decodeHtmlEntities(match[2]);
    const text = normalizeWhitespace(aiNewsHtmlToText(match[3]));
    const url = absoluteUrl(href, indexUrl);
    if (!/\/issues\/[^/?#]+/i.test(url)) continue;
    if (/see all issues|back to issues/i.test(text)) continue;
    if (seen.has(url)) continue;
    seen.add(url);
    links.push(url);
  }
  if (links[0]) return links[0];

  const rawMatch = html.match(/href=(["'])(\/issues\/[^"']+)\1/i);
  return rawMatch ? absoluteUrl(rawMatch[2], indexUrl) : undefined;
}

function xmlTagText(block: string, tag: string): string {
  const match = block.match(new RegExp(`<${tag}[^>]*>([\\s\\S]*?)<\\/${tag}>`, "i"));
  const text = match?.[1] ?? "";
  return decodeHtmlEntities(text.replace(/^<!\[CDATA\[|\]\]>$/g, ""));
}

export function aiNewsRssXmlToIssues(xml: string): AiNewsRssIssue[] {
  return [...xml.matchAll(/<item\b[\s\S]*?<\/item>/gi)]
    .map((match) => match[0])
    .map((block) => {
      const url = absoluteUrl(xmlTagText(block, "link"));
      const categories = [...block.matchAll(/<category[^>]*>([\s\S]*?)<\/category>/gi)]
        .map((category) => decodeHtmlEntities(category[1].replace(/^<!\[CDATA\[|\]\]>$/g, "")))
        .map((category) => normalizeWhitespace(category))
        .filter(Boolean);
      return {
        title: normalizeWhitespace(xmlTagText(block, "title")),
        url,
        guid: normalizeWhitespace(xmlTagText(block, "guid")) || url,
        description: normalizeWhitespace(xmlTagText(block, "description")),
        pubDate: normalizeWhitespace(xmlTagText(block, "pubDate")),
        categories,
      };
    })
    .filter((item) => /\/issues\/[^/?#]+/i.test(item.url));
}

export function latestAiNewsIssueFromRssXml(xml: string): AiNewsRssIssue | undefined {
  return aiNewsRssXmlToIssues(xml)[0];
}

export function aiNewsHtmlToText(html: string): string {
  return decodeHtmlEntities(html)
    .replace(/<script\b[\s\S]*?<\/script>/gi, "\n")
    .replace(/<style\b[\s\S]*?<\/style>/gi, "\n")
    .replace(/<svg\b[\s\S]*?<\/svg>/gi, "\n")
    .replace(/<hr\b[^>]*>/gi, "\n* * *\n")
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/<h1\b[^>]*>/gi, "\n# ")
    .replace(/<h2\b[^>]*>/gi, "\n## ")
    .replace(/<h3\b[^>]*>/gi, "\n### ")
    .replace(/<h4\b[^>]*>/gi, "\n#### ")
    .replace(/<\/h[1-4]>/gi, "\n")
    .replace(/<li\b[^>]*>/gi, "\n- ")
    .replace(/<\/(?:p|li|blockquote|div|section|article)>/gi, "\n")
    .replace(/<[^>]*>/g, " ")
    .split(/\n+/)
    .map((line) => normalizeWhitespace(line))
    .filter(Boolean)
    .join("\n");
}

export function aiNewsHtmlLinks(html: string, baseUrl = AI_NEWS_BASE_URL): AiNewsSourceLink[] {
  const links: AiNewsSourceLink[] = [];
  const seen = new Set<string>();
  const linkPattern = /<a\b[^>]*\bhref=(["'])(.*?)\1[^>]*>([\s\S]*?)<\/a>/gi;
  for (const match of html.matchAll(linkPattern)) {
    const href = decodeHtmlEntities(match[2]);
    const url = absoluteUrl(href, baseUrl);
    if (!url || seen.has(url)) continue;
    const text = compactText(aiNewsHtmlToText(match[3]), 120);
    if (!text || /^(back to issues|skip to main|ainews|subscribe|issues|tags)$/i.test(text)) continue;
    seen.add(url);
    links.push({ text, url });
  }
  return links.slice(0, 80);
}

function markdownBody(markdown: string): string {
  return markdown.replace(/^---\n[\s\S]*?\n---\n?/, "");
}

function frontmatter(markdown: string): Record<string, string> {
  const match = markdown.match(/^---\n([\s\S]*?)\n---/);
  if (!match) return {};
  const values: Record<string, string> = {};
  for (const rawLine of match[1].split(/\n+/)) {
    const line = rawLine.trim();
    const field = line.match(/^([A-Za-z][A-Za-z0-9_-]*):\s*(.*)$/);
    if (!field) continue;
    values[field[1]] = field[2].replace(/^["']|["']$/g, "").trim();
  }
  return values;
}

export function aiNewsMarkdownToText(markdown: string): string {
  return markdownBody(markdown)
    .replace(/^\s{4,}[-*]\s+/gm, "  ")
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, "$1")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/__([^_]+)__/g, "$1")
    .replace(/\*\*/g, "")
    .replace(/<!--[\s\S]*?-->/g, "\n")
    .split(/\n+/)
    .map((line) => normalizeWhitespace(line))
    .filter(Boolean)
    .join("\n");
}

export function aiNewsMarkdownLinks(markdown: string, baseUrl = AI_NEWS_BASE_URL): AiNewsSourceLink[] {
  const links: AiNewsSourceLink[] = [];
  const seen = new Set<string>();
  for (const match of markdown.matchAll(/\[([^\]]+)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g)) {
    const text = normalizeWhitespace(match[1]);
    const url = absoluteUrl(match[2], baseUrl);
    if (!text || !url || seen.has(url)) continue;
    seen.add(url);
    links.push({ text: compactText(text, 120), url });
  }
  return links.slice(0, 120);
}

function cleanLine(value: string): string {
  return normalizeWhitespace(value)
    .replace(/^[-*]\s+/, "")
    .replace(/^#+\s*/, "")
    .replace(/\*\*/g, "")
    .replace(/`([^`]+)`/g, "$1")
    .trim();
}

function issueTitleFromLines(lines: string[], issueUrl: string): string {
  const dateIndex = lines.findIndex((line) => /^(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\s+\d{1,2}(?:,\s*\d{4})?$/i.test(cleanLine(line)));
  if (dateIndex >= 0) {
    const title = cleanLine(lines[dateIndex + 1] ?? "");
    if (title && !/^show\/hide tags$/i.test(title)) return title;
  }
  const heading = lines
    .map(cleanLine)
    .find((line) => line && !/^(ainews|subscribe|issues|tags|back to issues|skip to main)$/i.test(line));
  return heading || issueSlugFromUrl(issueUrl);
}

function contentLines(lines: string[]): string[] {
  const start = lines.findIndex((line) => /^#+\s+AI (?:Twitter Recap|Reddit Recap|Discords)\b/i.test(line));
  const usable = start >= 0 ? lines.slice(start) : lines;
  const end = usable.findIndex((line) => /^#+\s+Let's Connect\b|^Back to top$|^©\s+\d{4}/i.test(line));
  return (end >= 0 ? usable.slice(0, end) : usable)
    .filter((line) => !/^Table of Contents$/i.test(cleanLine(line)))
    .filter((line) => !/^show\/hide tags$/i.test(cleanLine(line)));
}

function isMajorHeading(line: string): boolean {
  return /^#+\s+AI (?:Twitter Recap|Reddit Recap|Discords)\b/i.test(line);
}

function isSubHeading(line: string): boolean {
  return /^#{2,4}\s+/.test(line) && !isMajorHeading(line);
}

function isBullet(line: string): boolean {
  return /^[-*]\s+/.test(line);
}

function isTopicLine(line: string): boolean {
  const text = cleanLine(line);
  if (!text || /^#+\s*/.test(line)) return false;
  if (isBullet(line)) return false;
  if (text.length > 140) return false;
  if (/[.!?]$/u.test(text) && text.length > 80) return false;
  return /[A-Za-z]/.test(text);
}

function storyTitleFromText(text: string, topic: string): string {
  const first = cleanLine(text.split(/\n+/)[0] ?? "");
  const activity = first.match(/^(.+?)\s+\(Activity:\s*\d+\):/i);
  if (activity) return compactText(activity[1], 116);
  const colon = first.indexOf(":");
  if (colon >= 16 && colon <= 120) return compactText(first.slice(0, colon), 116);
  const dash = first.search(/\s[-–]\s/);
  if (dash >= 16 && dash <= 120) return compactText(first.slice(0, dash), 116);
  const sentence = first.split(/(?<=[.!?])\s+/)[0] ?? "";
  const title = compactText(sentence || topic || first, 116);
  return title || "AINews story";
}

function parseStoryBlocks(lines: string[]): StoryBlock[] {
  const blocks: StoryBlock[] = [];
  let section = "AINews";
  let topic = "";
  let current: { lines: string[]; topic: string; section: string } | undefined;

  const flush = () => {
    if (!current?.lines.length) return;
    const text = current.lines.join("\n");
    blocks.push({
      section: current.section,
      topic: current.topic,
      title: storyTitleFromText(text, current.topic),
      text,
    });
    current = undefined;
  };

  for (const rawLine of lines) {
    const line = normalizeWhitespace(rawLine);
    if (!line) continue;
    if (isMajorHeading(line)) {
      flush();
      section = cleanLine(line);
      topic = "";
      continue;
    }
    if (isSubHeading(line)) {
      flush();
      topic = cleanLine(line);
      continue;
    }
    if (isBullet(line)) {
      flush();
      current = { section, topic, lines: [cleanLine(line)] };
      continue;
    }
    if (!current && isTopicLine(line)) {
      topic = cleanLine(line);
      continue;
    }
    if (current) {
      current.lines.push(cleanLine(line));
    }
  }
  flush();

  return blocks
    .filter((block) => block.text.length >= 80)
    .filter((block) => !/^AI News for /i.test(block.text))
    .slice(0, 80);
}

function blockLinks(blockText: string, links: AiNewsSourceLink[]): AiNewsSourceLink[] {
  const haystack = normalizeWhitespace(blockText).toLowerCase();
  const selected = links.filter((link) => {
    const text = normalizeWhitespace(link.text).toLowerCase();
    return text.length >= 3 && haystack.includes(text);
  });
  return selected.slice(0, 10);
}

function storyKind(section: string): string {
  if (/twitter/i.test(section)) return "twitter_recap";
  if (/reddit/i.test(section)) return "reddit_recap";
  if (/discord/i.test(section)) return "discord_recap";
  return "issue_recap";
}

function sourceItemForBlock(
  block: StoryBlock,
  options: {
    issueUrl: string;
    issueSlug: string;
    issueTitle: string;
    createdAt: string;
    index: number;
    links: AiNewsSourceLink[];
  },
): SourceItem {
  const links = blockLinks(block.text, options.links);
  const externalId = [
    options.issueSlug,
    slugify(block.section),
    slugify(block.topic || block.title),
    String(options.index + 1).padStart(2, "0"),
  ].filter(Boolean).join("-");
  const score = Math.max(1, 100 - options.index * 3);
  const itemUrl = links[0]?.url || options.issueUrl;
  const body = [
    `Issue: ${options.issueTitle}`,
    `Section: ${block.section}`,
    block.topic ? `Topic: ${block.topic}` : "",
    block.text,
    links.length ? `Source links: ${links.map((link) => `${link.text} (${link.url})`).join("; ")}` : "",
  ].filter(Boolean).join("\n");
  return {
    id: stableSourceItemId("ai_news", externalId),
    source: "ai_news",
    externalId,
    url: itemUrl,
    title: block.title,
    body,
    author: "smol.ai",
    createdAt: options.createdAt,
    metrics: {
      score,
      upvotes: score,
    },
    media: {
      hasVideo: false,
      hasImage: false,
    },
    raw: {
      issueUrl: options.issueUrl,
      issueSlug: options.issueSlug,
      issueTitle: options.issueTitle,
      section: block.section,
      topic: block.topic,
      storyKind: storyKind(block.section),
      blockText: block.text,
      sourceLinks: links,
      fullStory: {
        title: block.title,
        summary: block.text.split(/(?<=[.!?])\s+/)[0] ?? block.text,
        articleText: block.text,
        sourceLinks: links,
      },
    },
  };
}

export function aiNewsIssueHtmlToSourceItems(
  html: string,
  options: { issueUrl?: string; maxItems?: number } = {},
): SourceItem[] {
  const issueUrl = absoluteUrl(options.issueUrl || AI_NEWS_INDEX_URL);
  const issueSlug = issueSlugFromUrl(issueUrl);
  const text = aiNewsHtmlToText(html);
  const lines = text.split(/\n+/).map((line) => normalizeWhitespace(line)).filter(Boolean);
  const issueTitle = issueTitleFromLines(lines, issueUrl);
  const createdAt = issueDateFromSlug(issueUrl) || toIsoDate(new Date());
  const links = aiNewsHtmlLinks(html, issueUrl);
  return parseStoryBlocks(contentLines(lines))
    .slice(0, options.maxItems ?? 80)
    .map((block, index) => sourceItemForBlock(block, {
      issueUrl,
      issueSlug,
      issueTitle,
      createdAt,
      index,
      links,
    }));
}

export function aiNewsIssueMarkdownToSourceItems(
  markdown: string,
  options: { issueUrl?: string; maxItems?: number; rssIssue?: Partial<AiNewsRssIssue> } = {},
): SourceItem[] {
  const issueUrl = absoluteUrl(options.issueUrl || options.rssIssue?.url || AI_NEWS_INDEX_URL);
  const issueSlug = issueSlugFromUrl(issueUrl);
  const meta = frontmatter(markdown);
  const text = aiNewsMarkdownToText(markdown);
  const lines = text.split(/\n+/).map((line) => normalizeWhitespace(line)).filter(Boolean);
  const issueTitle = normalizeWhitespace(meta.title || options.rssIssue?.title) || issueTitleFromLines(lines, issueUrl);
  const rawDate = normalizeWhitespace(meta.date || options.rssIssue?.pubDate);
  const createdAt = rawDate ? toIsoDate(rawDate) : issueDateFromSlug(issueUrl) || toIsoDate(new Date());
  const links = aiNewsMarkdownLinks(markdown, issueUrl);
  return parseStoryBlocks(contentLines(lines))
    .slice(0, options.maxItems ?? 80)
    .map((block, index) => sourceItemForBlock(block, {
      issueUrl,
      issueSlug,
      issueTitle,
      createdAt,
      index,
      links,
    }));
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
    if (!response.ok) throw new Error(`HTTP ${response.status} for ${url}`);
    return await response.text();
  } finally {
    clearTimeout(timeout);
  }
}

export async function fetchAiNewsSourceItems(options: AiNewsCollectorOptions = {}): Promise<SourceItem[]> {
  const indexUrl = options.indexUrl ?? AI_NEWS_INDEX_URL;
  let rssIssue: AiNewsRssIssue | undefined;
  const issueUrl = options.issueUrl
    ? absoluteUrl(options.issueUrl, indexUrl)
    : (rssIssue = latestAiNewsIssueFromRssXml(await fetchText(options.rssUrl ?? AI_NEWS_RSS_URL)))?.url
      ?? latestAiNewsIssueUrlFromIndexHtml(await fetchText(indexUrl), indexUrl);
  if (!issueUrl) return [];
  try {
    const markdown = await fetchText(rawMarkdownUrlForIssue(issueUrl));
    return aiNewsIssueMarkdownToSourceItems(markdown, {
      issueUrl,
      maxItems: options.maxItems,
      rssIssue,
    }).filter((item) => isRecent(item, options.days, options.now));
  } catch (error) {
    console.warn(`[ai_news] could not fetch raw markdown for ${issueUrl}; falling back to HTML: ${(error as Error).message}`);
    const html = await fetchText(issueUrl);
    return aiNewsIssueHtmlToSourceItems(html, {
      issueUrl,
      maxItems: options.maxItems,
    }).filter((item) => isRecent(item, options.days, options.now));
  }
}
