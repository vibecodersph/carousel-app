import type { SourceItem } from "../sourcing/types.ts";
import { clamp, hoursSince, normalizeWhitespace } from "../sourcing/utils.ts";
import type { ConfidenceLevel, ResearchCluster, ResearchScores, ScoredResearchCluster } from "./types.ts";

const SOURCE_QUALITY: Record<string, number> = {
  github: 0.82,
  hacker_news: 0.72,
  reddit: 0.68,
};

const BUILDER_TERMS = [
  "api",
  "agent",
  "agents",
  "automation",
  "benchmark",
  "code",
  "coding",
  "deploy",
  "developer",
  "github",
  "inference",
  "local",
  "llm",
  "model",
  "open-source",
  "production",
  "rag",
  "repo",
  "routing",
  "sdk",
  "server",
  "tool",
  "workflow",
];

const PRACTICAL_TERMS = [
  "cost",
  "cheap",
  "cheaper",
  "example",
  "guide",
  "how",
  "latency",
  "open-source",
  "pricing",
  "production",
  "save",
  "shipping",
  "stack",
  "tutorial",
  "workflow",
];

const TENSION_TERMS = [
  "abandon",
  "bad",
  "broken",
  "but",
  "complaint",
  "expensive",
  "fail",
  "hard",
  "instead",
  "problem",
  "replace",
  "switch",
  "tradeoff",
  "versus",
  "vs",
];

const HOOK_TERMS = [
  "built",
  "cut",
  "cost",
  "demo",
  "fast",
  "free",
  "nobody",
  "open-source",
  "pattern",
  "replacing",
  "secret",
  "switching",
  "weird",
  "workflow",
];

function itemText(item: SourceItem): string {
  return normalizeWhitespace([item.title, item.body, item.topReply?.body].filter(Boolean).join(" ")).toLowerCase();
}

function textScore(text: string, terms: string[]): number {
  const hits = terms.filter((term) => text.includes(term)).length;
  return clamp(hits / Math.min(6, terms.length));
}

function engagementValue(item: SourceItem): number {
  return item.metrics.score ?? item.metrics.upvotes ?? item.metrics.likes ?? item.metrics.views ?? 0;
}

function sourceQuality(item: SourceItem): number {
  if (item.source === "reddit" && item.subreddit) {
    const subreddit = item.subreddit.toLowerCase();
    if (["localllama", "chatgptcoding", "ai_agents", "openai"].includes(subreddit)) return 0.78;
  }
  return SOURCE_QUALITY[item.source] ?? 0.5;
}

function confidenceFor(scores: ResearchScores, cluster: ResearchCluster): ConfidenceLevel {
  const sourceCount = new Set(cluster.items.map((item) => item.source)).size;
  if (sourceCount >= 3 && cluster.items.length >= 3 && scores.overall >= 0.68) return "high";
  if (sourceCount >= 2 && cluster.items.length >= 3 && scores.overall >= 0.58) return "medium-high";
  if (scores.overall >= 0.42) return "medium";
  return "low";
}

export function scoreResearchCluster(cluster: ResearchCluster, now = new Date()): ScoredResearchCluster {
  const sourceCount = new Set(cluster.items.map((item) => item.source)).size;
  const maxEngagement = Math.max(1, ...cluster.items.map(engagementValue));
  const engagementVelocity = clamp(
    cluster.items.reduce((sum, item) => {
      const age = Math.max(1, hoursSince(item.createdAt, now));
      return sum + (engagementValue(item) / maxEngagement) * Math.exp(-age / 96);
    }, 0) / Math.max(1, Math.min(4, cluster.items.length)),
  );
  const sourceQualityScore = clamp(
    cluster.items.reduce((sum, item) => sum + sourceQuality(item), 0) / Math.max(1, cluster.items.length),
  );
  const combinedText = cluster.items.map(itemText).join(" ");
  const practicalUtility = textScore(combinedText, PRACTICAL_TERMS);
  const controversyOrTension = textScore(combinedText, TENSION_TERMS);
  const crossSourceConfirmation = clamp((sourceCount - 1) / 2 * 0.7 + Math.min(cluster.items.length, 4) / 4 * 0.3);
  const audienceFit = textScore(combinedText, BUILDER_TERMS);
  const hookability = clamp(textScore(combinedText, HOOK_TERMS) * 0.75 + (cluster.items.length >= 3 ? 0.15 : 0));
  const novelty = clamp(0.62 + Math.min(cluster.keywords.length, 8) * 0.035 - Math.max(0, cluster.items.length - sourceCount) * 0.015);
  const scores: ResearchScores = {
    engagementVelocity: Number(engagementVelocity.toFixed(4)),
    sourceQuality: Number(sourceQualityScore.toFixed(4)),
    novelty: Number(novelty.toFixed(4)),
    practicalUtility: Number(practicalUtility.toFixed(4)),
    controversyOrTension: Number(controversyOrTension.toFixed(4)),
    crossSourceConfirmation: Number(crossSourceConfirmation.toFixed(4)),
    audienceFit: Number(audienceFit.toFixed(4)),
    hookability: Number(hookability.toFixed(4)),
    overall: 0,
  };
  scores.overall = Number((
    0.20 * scores.engagementVelocity
    + 0.15 * scores.sourceQuality
    + 0.15 * scores.novelty
    + 0.15 * scores.practicalUtility
    + 0.10 * scores.controversyOrTension
    + 0.10 * scores.crossSourceConfirmation
    + 0.10 * scores.audienceFit
    + 0.05 * scores.hookability
  ).toFixed(4));

  return {
    ...cluster,
    scores,
    confidence: confidenceFor(scores, cluster),
  };
}

export function scoreResearchClusters(clusters: ResearchCluster[], now = new Date()): ScoredResearchCluster[] {
  return clusters
    .map((cluster) => scoreResearchCluster(cluster, now))
    .sort((a, b) => b.scores.overall - a.scores.overall || b.items.length - a.items.length);
}
