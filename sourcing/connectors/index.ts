import type { SourceItem } from "../types.ts";
import { fetchHuggingFaceSpacesSourceItems } from "./huggingFaceSpaces.ts";
import { fetchProductHuntSourceItems } from "./productHunt.ts";
import { fetchRedditSourceItems, type RedditConnectorOptions } from "./reddit.ts";
import { fetchXSourceItems, type XConnectorOptions } from "./x.ts";
import { fetchYouTubeShortsSourceItems, type YouTubeShortsConnectorOptions } from "./youtubeShorts.ts";

export interface FetchAllSourceOptions {
  reddit?: false | RedditConnectorOptions;
  x?: false | XConnectorOptions;
  youtube?: false | YouTubeShortsConnectorOptions;
  includeStubs?: boolean;
}

export async function fetchAllSourceItems(options: FetchAllSourceOptions = {}): Promise<SourceItem[]> {
  const batches: SourceItem[][] = [];
  if (options.reddit !== false) {
    batches.push(await fetchRedditSourceItems(typeof options.reddit === "object" ? options.reddit : {}));
  }
  if (options.x && options.x !== false) {
    batches.push(await fetchXSourceItems(options.x));
  }
  if (options.youtube !== false) {
    batches.push(await fetchYouTubeShortsSourceItems(typeof options.youtube === "object" ? options.youtube : {}));
  }
  if (options.includeStubs) {
    batches.push(
      await fetchProductHuntSourceItems(),
      await fetchHuggingFaceSpacesSourceItems(),
    );
  }
  return batches.flat();
}
