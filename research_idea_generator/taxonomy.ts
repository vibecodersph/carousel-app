import { readJsonFile } from "../sourcing/utils.ts";
import type { ResearchQueries, TaxonomyConfig } from "./types.ts";

export function defaultTaxonomyPath(): string {
  return new URL("./config/ai_builder_taxonomy.json", import.meta.url).pathname;
}

export async function loadTaxonomy(path = defaultTaxonomyPath()): Promise<TaxonomyConfig> {
  return readJsonFile<TaxonomyConfig>(path, {
    audience: "ai_builders",
    redditSubreddits: [],
    hookStyles: ["list", "contrarian", "curiosity"],
    topics: [],
  });
}

export function isoDateDaysAgo(days: number, now = new Date()): string {
  const date = new Date(now);
  date.setUTCDate(date.getUTCDate() - Math.max(0, days));
  return date.toISOString().slice(0, 10);
}

function unique(values: string[]): string[] {
  return [...new Set(values.map((value) => value.trim()).filter(Boolean))];
}

export function buildResearchQueries(
  taxonomy: TaxonomyConfig,
  options: { days?: number; now?: Date } = {},
): ResearchQueries {
  const sinceDate = isoDateDaysAgo(options.days ?? 7, options.now);
  return {
    redditSubreddits: unique(taxonomy.redditSubreddits),
    github: unique(
      taxonomy.topics.flatMap((topic) =>
        topic.githubQueries.map((query) => query.replaceAll("{sinceDate}", sinceDate))
      ),
    ),
    hn: unique(taxonomy.topics.flatMap((topic) => topic.hnQueries)),
  };
}

export function taxonomyKeywords(taxonomy: TaxonomyConfig): string[] {
  return unique(taxonomy.topics.flatMap((topic) => [topic.label, ...topic.keywords]));
}
