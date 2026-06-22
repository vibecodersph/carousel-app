import type { SourceItem } from "../types.ts";

export interface ProductHuntConnectorOptions {
  topic?: string;
  limit?: number;
}

export async function fetchProductHuntSourceItems(
  _options: ProductHuntConnectorOptions = {},
): Promise<SourceItem[]> {
  return [];
}
