import type { SourceItem } from "../sourcing/types.ts";
import { embedText } from "../sourcing/embeddings.ts";
import { clamp, cosineSimilarity, normalizeWhitespace, readJsonFile, writeJsonFile } from "../sourcing/utils.ts";
import type { InsightCardOutput, ScoredResearchCluster } from "./types.ts";

export interface MemorySourceItem {
  id: string;
  source: string;
  externalId: string;
  url: string;
  title: string;
  firstSeenAt: string;
  lastSeenAt: string;
  lastUsedAt: string;
  usedCount: number;
  lastScore: number;
  cardIds: string[];
}

export interface MemoryInsightCard {
  id: string;
  title: string;
  claim: string;
  createdAt: string;
  evidenceUrls: string[];
  evidenceSourceItemIds: string[];
  embedding: number[];
}

export interface ResearchMemory {
  version: 1;
  updatedAt: string;
  sourceItems: Record<string, MemorySourceItem>;
  urls: Record<string, string>;
  insightCards: Record<string, MemoryInsightCard>;
}

export interface MemoryFilterResult {
  items: SourceItem[];
  skipped: Array<{ item: SourceItem; reason: string; previous?: MemorySourceItem }>;
}

const SOURCE_COOLDOWN_DAYS: Record<string, number> = {
  reddit: 14,
  hacker_news: 45,
  github: 90,
};

const GITHUB_REUSE_GROWTH_MULTIPLIER = 1.35;
const CARD_SIMILARITY_THRESHOLD = 0.88;

export function defaultMemoryPath(): string {
  return "out/research_idea_generator/memory.json";
}

function emptyMemory(): ResearchMemory {
  return {
    version: 1,
    updatedAt: new Date(0).toISOString(),
    sourceItems: {},
    urls: {},
    insightCards: {},
  };
}

function normalizeUrl(value: string): string {
  try {
    const url = new URL(value);
    url.hash = "";
    url.search = "";
    return url.toString().replace(/\/$/, "");
  } catch {
    return normalizeWhitespace(value).replace(/[?#].*$/, "").replace(/\/$/, "");
  }
}

function daysSince(isoDate: string, now: Date): number {
  const timestamp = Date.parse(isoDate);
  if (!Number.isFinite(timestamp)) return Number.POSITIVE_INFINITY;
  return Math.max(0, (now.getTime() - timestamp) / 86_400_000);
}

function memoryKeyForItem(item: SourceItem): string {
  return item.id;
}

function lookupMemoryItem(memory: ResearchMemory, item: SourceItem): MemorySourceItem | undefined {
  return memory.sourceItems[memoryKeyForItem(item)] ?? memory.sourceItems[memory.urls[normalizeUrl(item.url)] ?? ""];
}

export async function loadResearchMemory(path = defaultMemoryPath()): Promise<ResearchMemory> {
  const memory = await readJsonFile<ResearchMemory>(path, emptyMemory());
  return {
    version: 1,
    updatedAt: memory.updatedAt || new Date(0).toISOString(),
    sourceItems: memory.sourceItems ?? {},
    urls: memory.urls ?? {},
    insightCards: memory.insightCards ?? {},
  };
}

export function filterSeenSourceItems(
  items: SourceItem[],
  memory: ResearchMemory,
  now = new Date(),
): MemoryFilterResult {
  const kept: SourceItem[] = [];
  const skipped: MemoryFilterResult["skipped"] = [];
  for (const item of items) {
    const previous = lookupMemoryItem(memory, item);
    if (!previous) {
      kept.push(item);
      continue;
    }

    const cooldownDays = SOURCE_COOLDOWN_DAYS[item.source] ?? 45;
    const currentScore = item.metrics.score ?? item.metrics.upvotes ?? item.metrics.likes ?? 0;
    const previousScore = previous.lastScore || 0;
    const materiallyGrew = item.source === "github"
      && previousScore > 0
      && currentScore >= previousScore * GITHUB_REUSE_GROWTH_MULTIPLIER;
    if (daysSince(previous.lastUsedAt, now) >= cooldownDays || materiallyGrew) {
      kept.push(item);
      continue;
    }

    skipped.push({
      item,
      previous,
      reason: `${item.source} evidence used ${Math.round(daysSince(previous.lastUsedAt, now))} days ago`,
    });
  }
  return { items: kept, skipped };
}

function clusterText(cluster: ScoredResearchCluster): string {
  return normalizeWhitespace([
    cluster.label,
    cluster.keywords.join(" "),
    ...cluster.items.map((item) => `${item.title} ${item.body}`),
  ].join(" "));
}

export async function applyInsightMemoryPenalty(
  clusters: ScoredResearchCluster[],
  memory: ResearchMemory,
  now = new Date(),
): Promise<ScoredResearchCluster[]> {
  const cards = Object.values(memory.insightCards)
    .filter((card) => card.embedding.length && daysSince(card.createdAt, now) < 60);
  if (!cards.length) return clusters;

  const adjusted: ScoredResearchCluster[] = [];
  for (const cluster of clusters) {
    const embedding = await embedText(clusterText(cluster));
    let maxSimilarity = 0;
    for (const card of cards) {
      maxSimilarity = Math.max(maxSimilarity, cosineSimilarity(embedding, card.embedding));
    }
    if (maxSimilarity >= CARD_SIMILARITY_THRESHOLD) continue;
    const penalty = maxSimilarity >= 0.78 ? 0.25 : maxSimilarity >= 0.7 ? 0.12 : 0;
    if (!penalty) {
      adjusted.push(cluster);
      continue;
    }
    adjusted.push({
      ...cluster,
      scores: {
        ...cluster.scores,
        novelty: Number(clamp(cluster.scores.novelty * (1 - penalty)).toFixed(4)),
        overall: Number(clamp(cluster.scores.overall * (1 - penalty)).toFixed(4)),
      },
    });
  }
  return adjusted.sort((a, b) => b.scores.overall - a.scores.overall || b.items.length - a.items.length);
}

export async function rememberInsightOutput(
  path: string,
  memory: ResearchMemory,
  output: InsightCardOutput,
): Promise<ResearchMemory> {
  const next: ResearchMemory = {
    version: 1,
    updatedAt: output.generatedAt,
    sourceItems: { ...memory.sourceItems },
    urls: { ...memory.urls },
    insightCards: { ...memory.insightCards },
  };

  for (const card of output.cards) {
    const evidenceSourceItemIds = card.evidence
      .map((evidence) => evidence.sourceItemId)
      .filter((value): value is string => Boolean(value));
    const evidenceUrls = card.evidence.map((evidence) => normalizeUrl(evidence.url)).filter(Boolean);
    next.insightCards[card.id] = {
      id: card.id,
      title: card.workingTitle,
      claim: card.claim,
      createdAt: output.generatedAt,
      evidenceUrls,
      evidenceSourceItemIds,
      embedding: await embedText(`${card.workingTitle}\n${card.claim}`),
    };

    for (const evidence of card.evidence) {
      const sourceItemId = evidence.sourceItemId || normalizeUrl(evidence.url);
      if (!sourceItemId) continue;
      const url = normalizeUrl(evidence.url);
      const previous = next.sourceItems[sourceItemId] ?? next.sourceItems[next.urls[url] ?? ""];
      const current: MemorySourceItem = {
        id: sourceItemId,
        source: evidence.sourceName ?? evidence.source,
        externalId: evidence.sourceExternalId ?? "",
        url: evidence.url,
        title: evidence.title,
        firstSeenAt: previous?.firstSeenAt ?? output.generatedAt,
        lastSeenAt: output.generatedAt,
        lastUsedAt: output.generatedAt,
        usedCount: (previous?.usedCount ?? 0) + 1,
        lastScore: Number(evidence.metrics.score ?? evidence.metrics.upvotes ?? evidence.metrics.views ?? 0),
        cardIds: [...new Set([...(previous?.cardIds ?? []), card.id])],
      };
      next.sourceItems[sourceItemId] = current;
      if (url) next.urls[url] = sourceItemId;
    }
  }

  await writeJsonFile(path, next);
  return next;
}
