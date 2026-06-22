import type { Route, SourceItem } from "./types.ts";
import { compactText, normalizeWhitespace } from "./utils.ts";

export interface StoryObject {
  id: string;
  stableId: string;
  sourceType: string;
  source: string;
  sourceUrl: string;
  sourceExternalId: string;
  title: string;
  body: string;
  summary: string;
  author: string;
  publishedAt: string;
  community?: string;
  metrics: SourceItem["metrics"];
  media: SourceItem["media"];
  topReply?: SourceItem["topReply"];
  routes: Route[];
  language: "ja";
  approval: {
    required: true;
    irreversibleAfterApproval: true;
  };
}

export function sourceItemToStoryObject(item: SourceItem, routes: Route[]): StoryObject {
  const topReply = item.topReply?.body ? `Top reply: ${item.topReply.body}` : "";
  const body = normalizeWhitespace([item.body, topReply].filter(Boolean).join("\n\n"));
  return {
    id: item.id,
    stableId: item.id,
    sourceType: item.source,
    source: item.subreddit ? `r/${item.subreddit}` : item.source,
    sourceUrl: item.url,
    sourceExternalId: item.externalId,
    title: item.title,
    body,
    summary: compactText(body || item.title, 280),
    author: item.author,
    publishedAt: item.createdAt,
    community: item.subreddit,
    metrics: item.metrics,
    media: item.media,
    topReply: item.topReply,
    routes,
    language: "ja",
    approval: {
      required: true,
      irreversibleAfterApproval: true,
    },
  };
}
