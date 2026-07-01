import type { SourceItem } from "../sourcing/types.ts";

export type ResearchSourceName = "reddit" | "github" | "hacker_news";
export type ResearchProvider = "local" | "gemini";
export type HookStyle = "curiosity" | "contrarian" | "list";
export type HookRiskLevel = "low" | "medium" | "high";
export type ConfidenceLevel = "low" | "medium" | "medium-high" | "high";

export interface ResearchGeneratorOptions {
  sources?: ResearchSourceName[];
  days?: number;
  cards?: number;
  hooksPerCard?: number;
  provider?: ResearchProvider;
  input?: string;
  out?: string;
  report?: string;
  carouselOut?: string;
  runsDir?: string;
  noArchive?: boolean;
  memory?: string;
  noMemory?: boolean;
  redditQueue?: string;
  redditLive?: boolean;
  taxonomyPath?: string;
  now?: Date;
  maxItemsPerSource?: number;
}

export interface TaxonomyTopic {
  id: string;
  label: string;
  keywords: string[];
  githubQueries: string[];
  hnQueries: string[];
  angles?: string[];
}

export interface TaxonomyConfig {
  audience: "ai_builders" | string;
  redditSubreddits: string[];
  hookStyles: HookStyle[];
  topics: TaxonomyTopic[];
}

export interface ResearchQueries {
  redditSubreddits: string[];
  github: string[];
  hn: string[];
}

export interface ResearchCluster {
  id: string;
  label: string;
  items: SourceItem[];
  keywords: string[];
}

export interface ResearchScores {
  overall: number;
  engagementVelocity: number;
  sourceQuality: number;
  novelty: number;
  practicalUtility: number;
  controversyOrTension: number;
  crossSourceConfirmation: number;
  audienceFit: number;
  hookability: number;
}

export interface ScoredResearchCluster extends ResearchCluster {
  scores: ResearchScores;
  confidence: ConfidenceLevel;
}

export interface EvidenceItem {
  source: string;
  sourceName?: string;
  sourceItemId?: string;
  sourceExternalId?: string;
  title: string;
  url: string;
  excerpt: string;
  author: string;
  metrics: Record<string, number | undefined>;
  timestamp: string;
}

export interface HookVariant {
  hook: string;
  lines: string[];
  style: HookStyle;
  riskLevel: HookRiskLevel;
  whyItWorks: string;
  needsFactCheck: boolean;
  bestPlatform: "X" | "LinkedIn" | "newsletter" | "YouTube";
}

export interface InsightCard {
  id: string;
  workingTitle: string;
  claim: string;
  evidence: EvidenceItem[];
  whyItMatters: string;
  contentAngles: string[];
  hooks: HookVariant[];
  scores: ResearchScores;
  confidence: ConfidenceLevel;
  risks: string[];
  suggestedFormats: string[];
}

export interface InsightCardOutput {
  generatedAt: string;
  audience: string;
  sourceCount: number;
  clusterCount: number;
  cardCount: number;
  cards: InsightCard[];
}

export type CarouselSlideType = "cover" | "hook_detail" | "list_item";

export interface CarouselBriefSlide {
  slideNumber: number;
  type: CarouselSlideType;
  headline: string;
  lines: string[];
  altText: string;
  sourceUrls?: string[];
}

export interface CarouselBrief {
  id: string;
  sourceInsightCardId: string;
  workingTitle: string;
  hook: string;
  hookStyle: HookStyle;
  hookRiskLevel: HookRiskLevel;
  hookNeedsFactCheck: boolean;
  hookBestPlatform: HookVariant["bestPlatform"];
  confidence: ConfidenceLevel;
  score: number;
  suggestedFormat: "instagram_carousel";
  slideCount: number;
  slides: CarouselBriefSlide[];
  instagramDescription: string;
  evidenceSourceItemIds: string[];
  evidenceUrls: string[];
}

export interface CarouselBriefOutput {
  generatedAt: string;
  audience: string;
  sourceInsightGeneratedAt: string;
  carouselCount: number;
  carousels: CarouselBrief[];
}
