import type { SourceItem } from "../sourcing/types.ts";
import { embedText } from "../sourcing/embeddings.ts";
import { clamp, cosineSimilarity, normalizeWhitespace, sha256 } from "../sourcing/utils.ts";
import type { ResearchCluster } from "./types.ts";

const STOPWORDS = new Set([
  "about",
  "after",
  "again",
  "also",
  "because",
  "before",
  "being",
  "built",
  "could",
  "from",
  "have",
  "into",
  "just",
  "like",
  "more",
  "much",
  "over",
  "that",
  "their",
  "there",
  "these",
  "they",
  "this",
  "using",
  "what",
  "when",
  "where",
  "with",
  "would",
]);

export interface ClusterOptions {
  similarityThreshold?: number;
  maxClusters?: number;
}

interface MutableCluster {
  items: SourceItem[];
  embeddings: number[][];
  keywords: Set<string>;
}

function itemText(item: SourceItem): string {
  return normalizeWhitespace([
    item.title,
    item.body,
    item.topReply?.body,
  ].filter(Boolean).join(" "));
}

function tokens(value: string): string[] {
  return normalizeWhitespace(value)
    .toLowerCase()
    .match(/[a-z0-9][a-z0-9+.-]{2,}/g)
    ?.filter((token) => !STOPWORDS.has(token))
    .slice(0, 80) ?? [];
}

function keywordSet(item: SourceItem): Set<string> {
  return new Set(tokens(itemText(item)));
}

function jaccard(a: Set<string>, b: Set<string>): number {
  if (!a.size || !b.size) return 0;
  let overlap = 0;
  for (const value of a) {
    if (b.has(value)) overlap += 1;
  }
  return overlap / Math.max(1, a.size + b.size - overlap);
}

function phraseOverlap(a: Set<string>, b: Set<string>): number {
  const important = ["agent", "agents", "router", "routing", "llm", "model", "api", "cost", "local", "rag", "eval", "evals", "mcp", "workflow"];
  const overlap = important.filter((token) => a.has(token) && b.has(token)).length;
  return clamp(overlap / 3);
}

function averageEmbedding(cluster: MutableCluster): number[] {
  const dimensions = cluster.embeddings[0]?.length ?? 0;
  if (!dimensions) return [];
  const vector = new Array<number>(dimensions).fill(0);
  for (const embedding of cluster.embeddings) {
    for (let i = 0; i < dimensions; i += 1) {
      vector[i] += embedding[i] ?? 0;
    }
  }
  const magnitude = Math.sqrt(vector.reduce((sum, value) => sum + value * value, 0)) || 1;
  return vector.map((value) => value / magnitude);
}

function clusterSimilarity(itemEmbedding: number[], itemKeywords: Set<string>, cluster: MutableCluster): number {
  const semantic = cosineSimilarity(itemEmbedding, averageEmbedding(cluster));
  const lexical = jaccard(itemKeywords, cluster.keywords);
  const phrase = phraseOverlap(itemKeywords, cluster.keywords);
  return clamp(semantic * 0.55 + lexical * 0.25 + phrase * 0.2);
}

function bestKeywords(cluster: MutableCluster): string[] {
  const counts = new Map<string, number>();
  for (const item of cluster.items) {
    for (const token of keywordSet(item)) {
      counts.set(token, (counts.get(token) ?? 0) + 1);
    }
  }
  return [...counts.entries()]
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .slice(0, 8)
    .map(([token]) => token);
}

function clusterLabel(cluster: MutableCluster): string {
  const best = [...cluster.items].sort((a, b) => (b.metrics.score ?? 0) - (a.metrics.score ?? 0))[0];
  return normalizeWhitespace(best?.title).slice(0, 120) || "Untitled research cluster";
}

export async function clusterSourceItems(
  items: SourceItem[],
  options: ClusterOptions = {},
): Promise<ResearchCluster[]> {
  const threshold = options.similarityThreshold ?? 0.32;
  const ordered = [...items].sort((a, b) => {
    const scoreDelta = (b.metrics.score ?? 0) - (a.metrics.score ?? 0);
    if (scoreDelta) return scoreDelta;
    return Date.parse(b.createdAt) - Date.parse(a.createdAt);
  });
  const clusters: MutableCluster[] = [];

  for (const item of ordered) {
    const text = itemText(item);
    const embedding = await embedText(text);
    const itemKeywords = keywordSet(item);
    let bestIndex = -1;
    let bestScore = 0;
    for (let index = 0; index < clusters.length; index += 1) {
      const score = clusterSimilarity(embedding, itemKeywords, clusters[index]);
      if (score > bestScore) {
        bestScore = score;
        bestIndex = index;
      }
    }
    if (bestIndex >= 0 && bestScore >= threshold) {
      const cluster = clusters[bestIndex];
      cluster.items.push(item);
      cluster.embeddings.push(embedding);
      for (const token of itemKeywords) cluster.keywords.add(token);
    } else {
      clusters.push({
        items: [item],
        embeddings: [embedding],
        keywords: new Set(itemKeywords),
      });
    }
  }

  return clusters
    .map((cluster) => ({
      id: sha256(cluster.items.map((item) => item.id).sort().join(":")).slice(0, 16),
      label: clusterLabel(cluster),
      items: cluster.items,
      keywords: bestKeywords(cluster),
    }))
    .sort((a, b) => b.items.length - a.items.length || (b.items[0]?.metrics.score ?? 0) - (a.items[0]?.metrics.score ?? 0))
    .slice(0, options.maxClusters ?? 30);
}
