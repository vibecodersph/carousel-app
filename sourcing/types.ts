export type SourceName =
  | "reddit"
  | "x"
  | "youtube_shorts"
  | "product_hunt"
  | "huggingface_spaces"
  | string;

export type Route = "reel" | "carousel";

export interface SourceMetrics {
  upvotes?: number;
  downvotes?: number;
  score?: number;
  comments?: number;
  views?: number;
  likes?: number;
  retweets?: number;
  replies?: number;
  shares?: number;
  quotes?: number;
  awards?: number;
  upvoteRatio?: number;
}

export interface SourceMedia {
  hasVideo: boolean;
  hasImage?: boolean;
  videoUrl?: string;
  imageUrl?: string;
  localPath?: string;
  durationSeconds?: number;
  width?: number;
  height?: number;
  provider?: string;
  raw?: unknown;
}

export interface TopReply {
  author: string;
  body: string;
  score?: number;
  url?: string;
  createdAt?: string;
  source?: "reply" | "quote" | "comment" | string;
  raw?: unknown;
}

export interface SourceItem {
  id: string;
  source: SourceName;
  externalId: string;
  url: string;
  title: string;
  body: string;
  author: string;
  createdAt: string;
  subreddit?: string;
  metrics: SourceMetrics;
  media: SourceMedia;
  topReply?: TopReply;
  raw?: unknown;
}

export interface RankingWeights {
  velocity: number;
  engagement: number;
  recency: number;
  authority: number;
  hasVideo: number;
  spectacle: number;
  novelty: number;
}

export interface ScoreComponents {
  velocity: number;
  engagement: number;
  recency: number;
  authority: number;
  hasVideo: number;
  spectacle: number;
  novelty: number;
}

export interface RankedItem {
  id: string;
  sourceItem: SourceItem;
  score: number;
  components: ScoreComponents;
  spectacleReason: string;
  noveltySimilarity: number;
  routing: {
    primary: Route;
    routes: Route[];
    reelEligible: boolean;
    carouselEligible: boolean;
  };
  ranking: {
    weights: RankingWeights;
    scoredAt: string;
  };
}

export interface PipelineRunSummary {
  sourced: number;
  deduped: number;
  ranked: number;
  queued: number;
  outputPath?: string;
  queuePath?: string;
}
