import type { SourceItem } from "../types.ts";

export interface YouTubeShortsConnectorOptions {
  query?: string;
  limit?: number;
}

export async function fetchYouTubeShortsSourceItems(
  _options: YouTubeShortsConnectorOptions = {},
): Promise<SourceItem[]> {
  return [];
}
