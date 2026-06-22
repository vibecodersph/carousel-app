import type { SourceItem } from "../sourcing/types.ts";

export const SOURCE_WEIGHTS: Record<string, Record<string, number>> = {
  reddit: {
    LocalLLaMA: 0.88,
    StableDiffusion: 0.82,
    aivideo: 0.82,
    ChatGPTCoding: 0.78,
    OpenAI: 0.74,
    ChatGPT: 0.7,
    singularity: 0.66,
    ai_agents: 0.64,
    artificial: 0.6,
    InternetIsBeautiful: 0.52,
    nextfuckinglevel: 0.48,
    Damnthatsinteresting: 0.46
  },
  x: {
    default: 0.72
  },
  youtube_shorts: {
    default: 0.58
  },
  product_hunt: {
    default: 0.56
  },
  huggingface_spaces: {
    default: 0.64
  }
};

export function authorityFor(item: SourceItem): number {
  const bySource = SOURCE_WEIGHTS[item.source] ?? {};
  const key = item.subreddit ?? "default";
  return bySource[key] ?? bySource.default ?? 0.5;
}
