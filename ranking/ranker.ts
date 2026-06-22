import type { RankedItem, RankingWeights, Route, ScoreComponents, SourceItem } from "../sourcing/types.ts";
import { clamp, hoursSince, readJsonFile } from "../sourcing/utils.ts";
import { authorityFor } from "./sourceWeights.ts";
import { loadPublishedTitleEmbeddings, scoreNovelty, type NoveltyOptions } from "./novelty.ts";
import { scoreSpectacle, type SpectacleOptions } from "./spectacle.ts";

export interface RankOptions {
  weightsPath?: string;
  now?: Date;
  topN?: number;
  spectacle?: SpectacleOptions;
  novelty?: NoveltyOptions;
}

const DEFAULT_WEIGHTS: RankingWeights = {
  velocity: 0.22,
  engagement: 0.12,
  recency: 0.12,
  authority: 0.12,
  hasVideo: 0.15,
  spectacle: 0.2,
  novelty: 0.07,
};

export async function loadRankingWeights(path = "ranking/weights.json"): Promise<RankingWeights> {
  const raw = await readJsonFile<Partial<RankingWeights>>(path, DEFAULT_WEIGHTS);
  return {
    velocity: Number(raw.velocity ?? DEFAULT_WEIGHTS.velocity),
    engagement: Number(raw.engagement ?? DEFAULT_WEIGHTS.engagement),
    recency: Number(raw.recency ?? DEFAULT_WEIGHTS.recency),
    authority: Number(raw.authority ?? DEFAULT_WEIGHTS.authority),
    hasVideo: Number(raw.hasVideo ?? DEFAULT_WEIGHTS.hasVideo),
    spectacle: Number(raw.spectacle ?? DEFAULT_WEIGHTS.spectacle),
    novelty: Number(raw.novelty ?? DEFAULT_WEIGHTS.novelty),
  };
}

function rawVelocity(item: SourceItem, now: Date): number {
  const upvotes = item.metrics.upvotes ?? item.metrics.likes ?? item.metrics.score ?? 0;
  return upvotes / Math.max(hoursSince(item.createdAt, now), 1);
}

function engagement(item: SourceItem): number {
  const comments = item.metrics.comments ?? item.metrics.replies ?? 0;
  const upvotes = item.metrics.upvotes ?? item.metrics.likes ?? item.metrics.score ?? 1;
  return clamp(comments / Math.max(upvotes, 1));
}

function routingFor(item: SourceItem): RankedItem["routing"] {
  const routes: Route[] = item.media.hasVideo ? ["reel", "carousel"] : ["carousel"];
  return {
    primary: item.media.hasVideo ? "reel" : "carousel",
    routes,
    reelEligible: item.media.hasVideo,
    carouselEligible: true,
  };
}

function weightedScore(components: ScoreComponents, weights: RankingWeights): number {
  return (
    weights.velocity * components.velocity
    + weights.engagement * components.engagement
    + weights.recency * components.recency
    + weights.authority * components.authority
    + weights.hasVideo * components.hasVideo
    + weights.spectacle * components.spectacle
    + weights.novelty * components.novelty
  );
}

export async function rankSourceItems(items: SourceItem[], options: RankOptions = {}): Promise<RankedItem[]> {
  const now = options.now ?? new Date();
  const weights = await loadRankingWeights(options.weightsPath);
  const velocities = items.map((item) => rawVelocity(item, now));
  const maxVelocity = Math.max(1, ...velocities);
  const publishedTitles = await loadPublishedTitleEmbeddings(options.novelty);
  const scored: RankedItem[] = [];

  for (let index = 0; index < items.length; index += 1) {
    const item = items[index];
    const spectacle = await scoreSpectacle(item, options.spectacle);
    const novelty = await scoreNovelty(item, publishedTitles, options.novelty);
    const ageHours = hoursSince(item.createdAt, now);
    const components: ScoreComponents = {
      velocity: clamp(velocities[index] / maxVelocity),
      engagement: engagement(item),
      recency: Number(Math.exp(-ageHours / 18).toFixed(4)),
      authority: clamp(authorityFor(item)),
      hasVideo: item.media.hasVideo ? 1 : 0,
      spectacle: clamp(spectacle.score),
      novelty: novelty.novelty,
    };
    scored.push({
      id: item.id,
      sourceItem: item,
      score: Number(weightedScore(components, weights).toFixed(6)),
      components,
      spectacleReason: spectacle.reason,
      noveltySimilarity: novelty.similarity,
      routing: routingFor(item),
      ranking: {
        weights,
        scoredAt: now.toISOString(),
      },
    });
  }

  scored.sort((a, b) => {
    if (b.score !== a.score) return b.score - a.score;
    const createdDelta = Date.parse(b.sourceItem.createdAt) - Date.parse(a.sourceItem.createdAt);
    if (createdDelta) return createdDelta;
    return a.id.localeCompare(b.id);
  });
  return options.topN ? scored.slice(0, options.topN) : scored;
}
