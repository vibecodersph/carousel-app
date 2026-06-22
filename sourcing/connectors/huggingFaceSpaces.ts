import type { SourceItem } from "../types.ts";

export interface HuggingFaceSpacesConnectorOptions {
  sort?: "likes" | "trending" | "updated";
  limit?: number;
}

export async function fetchHuggingFaceSpacesSourceItems(
  _options: HuggingFaceSpacesConnectorOptions = {},
): Promise<SourceItem[]> {
  return [];
}
