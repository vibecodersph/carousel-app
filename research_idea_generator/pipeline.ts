import { mkdir, writeFile } from "node:fs/promises";
import { dirname } from "node:path";
import type { SourceItem } from "../sourcing/types.ts";
import { normalizeWhitespace, readJsonFile, sha256, writeJsonFile } from "../sourcing/utils.ts";
import { clusterSourceItems } from "./cluster.ts";
import { generateCarouselBriefs } from "./carouselBriefs.ts";
import { generateInsightCards } from "./generator.ts";
import { judgeHookWorthiness } from "./hookJudge.ts";
import {
  applyInsightMemoryPenalty,
  defaultMemoryPath,
  filterSeenSourceItems,
  loadResearchMemory,
  rememberInsightOutput,
} from "./memory.ts";
import { renderInsightReport } from "./report.ts";
import { readUnreadRedditSources } from "./redditQueue.ts";
import { scoreResearchClusters } from "./scoring.ts";
import { collectLiveSourceItems, dedupeResearchItems } from "./sources/index.ts";
import { buildResearchQueries, loadTaxonomy } from "./taxonomy.ts";
import type {
  CarouselBriefOutput,
  InsightCardOutput,
  ResearchCluster,
  ResearchGeneratorOptions,
  ResearchSourceName,
  ScoredResearchCluster,
} from "./types.ts";

const DEFAULT_SOURCES: ResearchSourceName[] = ["reddit", "github", "hacker_news"];
const HOOK_JUDGED_SOURCES = new Set<string>(["the_batch"]);
const BATCH_CLUSTER_STOPWORDS = new Set([
  "article",
  "batch",
  "deep",
  "learning",
  "summary",
  "tags",
  "this",
  "that",
  "with",
  "from",
  "into",
  "using",
]);

async function readInputItems(path: string): Promise<SourceItem[]> {
  const payload = await readJsonFile<{ items?: SourceItem[] } | SourceItem[]>(path, []);
  return Array.isArray(payload) ? payload : payload.items ?? [];
}

function defaultOutPath(): string {
  return "out/research_idea_generator/insight_cards.json";
}

function defaultReportPath(): string {
  return "out/research_idea_generator/report.md";
}

function defaultCarouselOutPath(): string {
  return "out/research_idea_generator/carousel_briefs.json";
}

function defaultRunsDir(): string {
  return "out/research_idea_generator/runs";
}

function batchStoryKeywords(item: SourceItem): string[] {
  return normalizeWhitespace(`${item.title} ${item.body}`)
    .toLowerCase()
    .match(/[a-z0-9][a-z0-9+.-]{2,}/g)
    ?.filter((token) => !BATCH_CLUSTER_STOPWORDS.has(token))
    .slice(0, 80)
    .reduce<string[]>((keywords, token) => {
      if (!keywords.includes(token)) keywords.push(token);
      return keywords;
    }, [])
    .slice(0, 8) ?? [];
}

function shouldClusterBatchStoriesSeparately(sources: ResearchSourceName[], items: SourceItem[]): boolean {
  return sources.length === 1
    && sources[0] === "the_batch"
    && items.some((item) => item.source === "the_batch");
}

function batchStoryClusters(items: SourceItem[]): ResearchCluster[] {
  return items.map((item) => ({
    id: sha256(`the_batch_story:${item.id}`).slice(0, 16),
    label: item.title,
    items: [item],
    keywords: batchStoryKeywords(item),
  }));
}

function runSlug(isoDate: string): string {
  return isoDate.replace(/[:.]/g, "-");
}

async function writeTextFile(path: string, text: string): Promise<void> {
  await mkdir(dirname(path), { recursive: true });
  await writeFile(path, text, "utf8");
}

function archivedCluster(cluster: ScoredResearchCluster): Record<string, unknown> {
  return {
    id: cluster.id,
    label: cluster.label,
    keywords: cluster.keywords,
    scores: cluster.scores,
    confidence: cluster.confidence,
    sourceItemIds: cluster.items.map((item) => item.id),
    sourceItems: cluster.items.map((item) => ({
      id: item.id,
      source: item.source,
      externalId: item.externalId,
      title: item.title,
      url: item.url,
      author: item.author,
      createdAt: item.createdAt,
      subreddit: item.subreddit,
      metrics: item.metrics,
      media: item.media,
    })),
  };
}

async function filterHookWorthySourceItems(
  items: SourceItem[],
): Promise<{
  items: SourceItem[];
  skipped: Array<Record<string, unknown>>;
}> {
  const judgedItems = items.filter((item) => HOOK_JUDGED_SOURCES.has(item.source));
  if (!judgedItems.length) return { items, skipped: [] };

  const result = await judgeHookWorthiness(judgedItems);
  const keptById = new Map(result.kept.map((item) => [item.id, item]));
  const filtered = items
    .map((item) => {
      if (!HOOK_JUDGED_SOURCES.has(item.source)) return item;
      return keptById.get(item.id);
    })
    .filter((item): item is SourceItem => Boolean(item));

  return {
    items: filtered,
    skipped: result.dropped.map(({ item, judgment }) => ({
      reason: "hook_judge_rejected",
      judgment,
      item,
    })),
  };
}

async function writeRunArchive(options: {
  runsDir: string;
  output: InsightCardOutput;
  report: string;
  carouselBriefs: CarouselBriefOutput;
  collectedSourceItems: SourceItem[];
  dedupedSourceItems: SourceItem[];
  eligibleSourceItems: SourceItem[];
  skippedItems: Array<Record<string, unknown>>;
  clusters: ScoredResearchCluster[];
}): Promise<string> {
  const runDir = `${options.runsDir.replace(/\/$/, "")}/${runSlug(options.output.generatedAt)}`;
  await writeJsonFile(`${runDir}/insight_cards.json`, options.output);
  await writeTextFile(`${runDir}/report.md`, options.report);
  await writeJsonFile(`${runDir}/carousel_briefs.json`, options.carouselBriefs);
  await writeJsonFile(`${runDir}/source_items.json`, {
    generatedAt: options.output.generatedAt,
    count: options.collectedSourceItems.length,
    items: options.collectedSourceItems,
  });
  await writeJsonFile(`${runDir}/deduped_source_items.json`, {
    generatedAt: options.output.generatedAt,
    count: options.dedupedSourceItems.length,
    items: options.dedupedSourceItems,
  });
  await writeJsonFile(`${runDir}/eligible_source_items.json`, {
    generatedAt: options.output.generatedAt,
    count: options.eligibleSourceItems.length,
    items: options.eligibleSourceItems,
  });
  await writeJsonFile(`${runDir}/skipped_items.json`, {
    generatedAt: options.output.generatedAt,
    count: options.skippedItems.length,
    items: options.skippedItems,
  });
  await writeJsonFile(`${runDir}/clusters.json`, {
    generatedAt: options.output.generatedAt,
    count: options.clusters.length,
    clusters: options.clusters.map(archivedCluster),
  });
  await writeJsonFile(`${runDir}/manifest.json`, {
    generatedAt: options.output.generatedAt,
    files: [
      "insight_cards.json",
      "report.md",
      "carousel_briefs.json",
      "source_items.json",
      "deduped_source_items.json",
      "eligible_source_items.json",
      "skipped_items.json",
      "clusters.json",
    ],
    counts: {
      collectedSourceItems: options.collectedSourceItems.length,
      dedupedSourceItems: options.dedupedSourceItems.length,
      eligibleSourceItems: options.eligibleSourceItems.length,
      skippedItems: options.skippedItems.length,
      clusters: options.clusters.length,
      insightCards: options.output.cardCount,
      carouselBriefs: options.carouselBriefs.carouselCount,
    },
  });
  return runDir;
}

export async function runResearchIdeaGenerator(options: ResearchGeneratorOptions = {}): Promise<InsightCardOutput> {
  const now = options.now ?? new Date();
  const days = options.days ?? 7;
  const cards = options.cards;
  const provider = options.provider ?? "local";
  const taxonomy = await loadTaxonomy(options.taxonomyPath);
  const hooksPerCard = options.hooksPerCard ?? taxonomy.hookStyles.length;
  const sources = options.sources?.length ? options.sources : DEFAULT_SOURCES;
  const queries = buildResearchQueries(taxonomy, { days, now });
  const queuedRedditItems = !options.input && sources.includes("reddit")
    ? await readUnreadRedditSources({
      queuePath: options.redditQueue,
      limit: options.maxItemsPerSource ?? 80,
      markRead: false,
    })
    : [];
  const sourceItems = options.input
    ? await readInputItems(options.input)
    : await collectLiveSourceItems({
      sources,
      queries,
      days,
      now,
      maxItemsPerSource: options.maxItemsPerSource,
      redditItems: queuedRedditItems,
      redditLive: options.redditLive,
      theBatchLive: options.theBatchLive,
      theBatchIssueUrl: options.theBatchIssueUrl,
    });
  const memoryPath = options.memory ?? defaultMemoryPath();
  const memory = options.noMemory ? undefined : await loadResearchMemory(memoryPath);
  const deduped = dedupeResearchItems(sourceItems);
  const hookFilter = await filterHookWorthySourceItems(deduped);
  const hookFiltered = hookFilter.items;
  const memoryFilter = memory ? filterSeenSourceItems(hookFiltered, memory, now) : { items: hookFiltered, skipped: [] };
  const memoryFiltered = memoryFilter.items;
  const clusters = shouldClusterBatchStoriesSeparately(sources, memoryFiltered)
    ? batchStoryClusters(memoryFiltered)
    : await clusterSourceItems(memoryFiltered);
  const scored = memory
    ? await applyInsightMemoryPenalty(scoreResearchClusters(clusters, now), memory, now)
    : scoreResearchClusters(clusters, now);
  const insightCards = await generateInsightCards(scored, {
    cards,
    hooksPerCard,
    provider,
    taxonomy,
  });
  const output: InsightCardOutput = {
    generatedAt: now.toISOString(),
    audience: taxonomy.audience,
    sourceCount: memoryFiltered.length,
    clusterCount: clusters.length,
    cardCount: insightCards.length,
    cards: insightCards,
  };
  const report = renderInsightReport(output);
  const carouselBriefs = generateCarouselBriefs(output);
  await writeJsonFile(options.out ?? defaultOutPath(), output);
  await writeTextFile(options.report ?? defaultReportPath(), report);
  await writeJsonFile(options.carouselOut ?? defaultCarouselOutPath(), carouselBriefs);
  if (!options.noArchive) {
    await writeRunArchive({
      runsDir: options.runsDir ?? defaultRunsDir(),
      output,
      report,
      carouselBriefs,
      collectedSourceItems: sourceItems,
      dedupedSourceItems: deduped,
      eligibleSourceItems: memoryFiltered,
      skippedItems: [
        ...hookFilter.skipped,
        ...memoryFilter.skipped.map((skipped) => ({
          reason: skipped.reason,
          item: skipped.item,
          previous: skipped.previous,
        })),
      ],
      clusters: scored,
    });
  }
  if (memory) {
    await rememberInsightOutput(memoryPath, memory, output);
  }
  if (queuedRedditItems.length && insightCards.length) {
    await readUnreadRedditSources({
      queuePath: options.redditQueue,
      limit: queuedRedditItems.length,
      markRead: true,
    });
  }
  return output;
}
