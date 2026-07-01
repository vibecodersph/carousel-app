import type { SourceItem } from "../../sourcing/types.ts";
import {
  fetchJson,
  normalizeWhitespace,
  numberValue,
  stableSourceItemId,
  toIsoDate,
} from "../../sourcing/utils.ts";

export interface HackerNewsHit {
  objectID?: string;
  story_id?: number;
  title?: string | null;
  story_title?: string | null;
  url?: string | null;
  story_url?: string | null;
  author?: string | null;
  points?: number | null;
  num_comments?: number | null;
  created_at?: string;
  created_at_i?: number;
  comment_text?: string | null;
  _tags?: string[];
}

export interface HackerNewsCollectorOptions {
  queries: string[];
  days?: number;
  now?: Date;
  perQueryLimit?: number;
  maxItems?: number;
}

function stripHtml(value: string): string {
  return normalizeWhitespace(value.replace(/<[^>]*>/g, " "));
}

function hnItemUrl(storyId: string): string {
  return `https://news.ycombinator.com/item?id=${encodeURIComponent(storyId)}`;
}

export function hackerNewsHitToSourceItem(hit: HackerNewsHit, query = ""): SourceItem | null {
  const storyId = normalizeWhitespace(hit.story_id ?? hit.objectID);
  const title = normalizeWhitespace(hit.title ?? hit.story_title);
  if (!storyId || !title) return null;

  const url = normalizeWhitespace(hit.url ?? hit.story_url) || hnItemUrl(storyId);
  const createdAt = hit.created_at
    ? toIsoDate(hit.created_at)
    : toIsoDate(numberValue(hit.created_at_i, 0));

  return {
    id: stableSourceItemId("hacker_news", storyId),
    source: "hacker_news",
    externalId: storyId,
    url,
    title,
    body: stripHtml(normalizeWhitespace(hit.comment_text)),
    author: normalizeWhitespace(hit.author),
    createdAt,
    metrics: {
      upvotes: numberValue(hit.points),
      score: numberValue(hit.points),
      comments: numberValue(hit.num_comments),
    },
    media: { hasVideo: false },
    raw: {
      query,
      hnUrl: hnItemUrl(storyId),
      hit,
    },
  };
}

export async function fetchHackerNewsSourceItems(options: HackerNewsCollectorOptions): Promise<SourceItem[]> {
  const perQueryLimit = Math.min(Math.max(1, options.perQueryLimit ?? 12), 100);
  const now = options.now ?? new Date();
  const since = Math.floor((now.getTime() - Math.max(0, options.days ?? 7) * 86_400_000) / 1000);
  const byId = new Map<string, SourceItem>();
  for (const query of options.queries) {
    const params = new URLSearchParams({
      query,
      tags: "story",
      numericFilters: `created_at_i>${since}`,
      hitsPerPage: String(perQueryLimit),
    });
    const json = await fetchJson(`https://hn.algolia.com/api/v1/search_by_date?${params}`, {
      timeoutMs: 25_000,
      headers: {
        "User-Agent": "carousel-app-research-idea-generator/0.1",
        Accept: "application/json",
      },
    }) as { hits?: HackerNewsHit[] };
    for (const hit of json.hits ?? []) {
      const item = hackerNewsHitToSourceItem(hit, query);
      if (!item) continue;
      const previous = byId.get(item.id);
      if (!previous || (item.metrics.score ?? 0) > (previous.metrics.score ?? 0)) {
        byId.set(item.id, item);
      }
    }
  }
  return [...byId.values()]
    .sort((a, b) => (b.metrics.score ?? 0) - (a.metrics.score ?? 0))
    .slice(0, options.maxItems ?? 80);
}
