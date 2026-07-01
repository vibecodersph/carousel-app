import { mkdir, writeFile } from "node:fs/promises";
import { dirname } from "node:path";
import type { SourceItem } from "../sourcing/types.ts";
import { readJsonFile, writeJsonFile } from "../sourcing/utils.ts";
import { clusterSourceItems } from "./cluster.ts";
import { generateCarouselBriefs } from "./carouselBriefs.ts";
import { generateInsightCards } from "./generator.ts";
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
  ResearchGeneratorOptions,
  ResearchSourceName,
  ScoredResearchCluster,
} from "./types.ts";

const DEFAULT_SOURCES: ResearchSourceName[] = ["reddit", "github", "hacker_news"];

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
    });
  const memoryPath = options.memory ?? defaultMemoryPath();
  const memory = options.noMemory ? undefined : await loadResearchMemory(memoryPath);
  const deduped = dedupeResearchItems(sourceItems);
  const memoryFilter = memory ? filterSeenSourceItems(deduped, memory, now) : { items: deduped, skipped: [] };
  const memoryFiltered = memoryFilter.items;
  const clusters = await clusterSourceItems(memoryFiltered);
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
      skippedItems: memoryFilter.skipped.map((skipped) => ({
        reason: skipped.reason,
        item: skipped.item,
        previous: skipped.previous,
      })),
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
