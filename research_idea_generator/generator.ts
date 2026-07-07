import { compactText, normalizeWhitespace, sha256 } from "../sourcing/utils.ts";
import type {
  EvidenceItem,
  EvidenceMedia,
  EvidenceSourceLink,
  HookStyle,
  HookVariant,
  InsightCard,
  ScoredResearchCluster,
  TaxonomyConfig,
} from "./types.ts";
import { APPROVED_HOOK_STYLES, assessHookRisk, generateHookVariants, optimizeHookText } from "./hooks.ts";

function sourceLabel(source: string): string {
  if (source === "hacker_news") return "Hacker News";
  if (source === "github") return "GitHub";
  if (source === "reddit") return "Reddit";
  if (source === "the_batch") return "The Batch (DeepLearning.AI)";
  if (source === "ai_news") return "AINews (smol.ai)";
  return source;
}

function compactEvidenceText(value: string, limit: number, options: { preserveLines?: boolean } = {}): string {
  const text = options.preserveLines
    ? value
      .split(/\n+/)
      .map((line) => normalizeWhitespace(line))
      .filter(Boolean)
      .join("\n")
    : normalizeWhitespace(value);
  if (text.length <= limit) return text;
  return `${text.slice(0, Math.max(0, limit - 3)).trimEnd()}...`;
}

function rawRecord(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === "object" ? value as Record<string, unknown> : undefined;
}

function rawFullStoryRecord(item: ScoredResearchCluster["items"][number]): Record<string, unknown> | undefined {
  return rawRecord(rawRecord(item.raw)?.fullStory);
}

function evidenceExcerpt(item: ScoredResearchCluster["items"][number]): string {
  if (item.source === "the_batch" || item.source === "ai_news") {
    const fullStory = rawFullStoryRecord(item);
    const articleText = typeof fullStory?.articleText === "string" ? fullStory.articleText : "";
    const summary = typeof fullStory?.summary === "string" ? fullStory.summary : "";
    const body = [
      item.title ? `Title: ${item.title}` : "",
      summary ? `Summary: ${summary}` : "",
      articleText || item.body || item.topReply?.body || item.title,
    ].filter(Boolean).join("\n");
    return compactEvidenceText(body, 4_000, { preserveLines: true });
  }
  return compactText(normalizeWhitespace(item.body || item.topReply?.body || item.title), 240);
}

function evidenceMedia(item: ScoredResearchCluster["items"][number]): EvidenceMedia | undefined {
  const media = item.media;
  if (!media?.hasImage && !media?.imageUrl && !media?.localPath) return undefined;
  return {
    hasImage: media.hasImage,
    hasVideo: media.hasVideo,
    imageUrl: media.imageUrl,
    localPath: media.localPath,
    provider: media.provider,
    width: media.width,
    height: media.height,
  };
}

function evidenceSourceLinks(item: ScoredResearchCluster["items"][number]): EvidenceSourceLink[] | undefined {
  const fullStory = rawFullStoryRecord(item);
  const rawLinks = fullStory?.sourceLinks;
  if (!Array.isArray(rawLinks)) return undefined;
  const links = rawLinks
    .map((entry) => rawRecord(entry))
    .map((entry) => ({
      text: normalizeWhitespace(entry?.text),
      url: normalizeWhitespace(entry?.url),
    }))
    .filter((entry) => entry.text && entry.url);
  return links.length ? links.slice(0, 12) : undefined;
}

function compareEvidenceItems(
  a: ScoredResearchCluster["items"][number],
  b: ScoredResearchCluster["items"][number],
): number {
  return (b.metrics.score ?? 0) - (a.metrics.score ?? 0)
    || (b.metrics.upvotes ?? 0) - (a.metrics.upvotes ?? 0)
    || a.title.localeCompare(b.title);
}

function sourceDiverseEvidenceItems(cluster: ScoredResearchCluster, limit: number): ScoredResearchCluster["items"] {
  const sorted = [...cluster.items].sort(compareEvidenceItems);
  const bySource = new Map<string, ScoredResearchCluster["items"]>();
  for (const item of sorted) {
    const source = item.source;
    const sourceItems = bySource.get(source) ?? [];
    sourceItems.push(item);
    bySource.set(source, sourceItems);
  }

  const selected: ScoredResearchCluster["items"] = [];
  const usedIds = new Set<string>();
  for (const sourceItems of bySource.values()) {
    const item = sourceItems[0];
    if (!item || usedIds.has(item.id)) continue;
    selected.push(item);
    usedIds.add(item.id);
    if (selected.length >= limit) return selected;
  }
  for (const item of sorted) {
    if (usedIds.has(item.id)) continue;
    selected.push(item);
    usedIds.add(item.id);
    if (selected.length >= limit) break;
  }
  return selected;
}

export function evidenceFromCluster(cluster: ScoredResearchCluster): EvidenceItem[] {
  return sourceDiverseEvidenceItems(cluster, 8)
    .map((item) => ({
      source: item.subreddit ? `Reddit r/${item.subreddit}` : sourceLabel(item.source),
      sourceName: item.source,
      sourceItemId: item.id,
      sourceExternalId: item.externalId,
      title: item.title,
      url: item.url,
      excerpt: evidenceExcerpt(item),
      author: item.author,
      metrics: item.metrics,
      timestamp: item.createdAt,
      media: evidenceMedia(item),
      sourceLinks: evidenceSourceLinks(item),
    }));
}

function titleFor(cluster: ScoredResearchCluster): string {
  const keywords = cluster.keywords.slice(0, 3).join(", ");
  if (keywords) return `AI builders are converging around ${keywords}`;
  return cluster.label;
}

function claimFor(cluster: ScoredResearchCluster): string {
  const sources = new Set(cluster.items.map((item) => item.source));
  const sourcePhrase = sources.size > 1
    ? `${sources.size} source types`
    : sourceLabel(cluster.items[0]?.source ?? "sources");
  return `A pattern is emerging across ${sourcePhrase}: ${cluster.label}`;
}

function whyItMattersFor(cluster: ScoredResearchCluster): string {
  const hasCost = cluster.keywords.some((keyword) => ["cost", "cheap", "cheaper", "pricing", "api"].includes(keyword));
  const hasAgent = cluster.keywords.some((keyword) => ["agent", "agents", "mcp", "workflow"].includes(keyword));
  if (hasCost) return "AI builders can use this to spot cost, routing, and reliability tradeoffs before they become production problems.";
  if (hasAgent) return "This points to the practical agent workflows builders are testing beyond demos and launch hype.";
  return "This is useful because it turns scattered discussion into a concrete builder-facing angle worth reviewing.";
}

function contentAnglesFor(cluster: ScoredResearchCluster): string[] {
  const angles = [
    `Practical: what builders can try from ${cluster.keywords[0] ?? "this pattern"}`,
    "Evidence-led: what Reddit, GitHub, and HN are each showing",
    "Risk check: where the hook could outrun the proof",
    "Tactical: how to turn the pattern into a small workflow experiment",
  ];
  return angles.slice(0, 4);
}

function risksFor(cluster: ScoredResearchCluster): string[] {
  const risks = [
    "Avoid claiming broad adoption unless the evidence set grows beyond these communities.",
  ];
  if (cluster.scores.crossSourceConfirmation < 0.5) {
    risks.push("Needs another source type before publishing as a trend claim.");
  }
  if (/\b(cheap|cheapest|pricing|fastest|best)\b/i.test(`${cluster.label} ${cluster.keywords.join(" ")}`)) {
    risks.push("Pricing, speed, and best-in-class claims need current official docs or benchmark checks.");
  }
  if (cluster.items.length < 3) {
    risks.push("Evidence pack is thin; treat this as a watch item, not a finished claim.");
  }
  return risks;
}

function suggestedFormats(cluster: ScoredResearchCluster): string[] {
  if (cluster.scores.practicalUtility > 0.65) return ["X thread", "LinkedIn carousel", "newsletter blurb"];
  return ["newsletter blurb", "X post", "research backlog item"];
}

const MIN_PUBLISHABLE_SCORE = 0.52;
const MIN_CLOSE_TO_TOP_SCORE = 0.45;
const CLOSE_TO_TOP_RATIO = 0.82;

export function selectClustersForInsightCards(
  clusters: ScoredResearchCluster[],
  requestedCards?: number,
): ScoredResearchCluster[] {
  if (requestedCards !== undefined) {
    const limit = Math.max(0, Math.floor(requestedCards));
    return clusters.slice(0, limit);
  }
  if (!clusters.length) return [];

  const topScore = clusters[0].scores.overall;
  const selected = clusters.filter((cluster, index) => {
    const score = cluster.scores.overall;
    if (index === 0) return score >= MIN_CLOSE_TO_TOP_SCORE;

    const hasSourceSupport = cluster.items.length >= 2 || cluster.scores.crossSourceConfirmation >= 0.35;
    const hasUsableConfidence = cluster.confidence !== "low";
    const strongEnough = score >= MIN_PUBLISHABLE_SCORE && (hasSourceSupport || hasUsableConfidence);
    const closeToBest = score >= MIN_CLOSE_TO_TOP_SCORE && score >= topScore * CLOSE_TO_TOP_RATIO;
    return strongEnough || closeToBest;
  });

  return selected.length ? selected : clusters.slice(0, 1);
}

export function generateLocalInsightCard(
  cluster: ScoredResearchCluster,
  options: { hooksPerCard: number; hookStyles?: HookStyle[] } = { hooksPerCard: 4 },
): InsightCard {
  const evidence = evidenceFromCluster(cluster);
  const workingTitle = titleFor(cluster);
  const claim = claimFor(cluster);
  return {
    id: sha256(`${cluster.id}:${workingTitle}`).slice(0, 16),
    workingTitle,
    claim,
    evidence,
    whyItMatters: whyItMattersFor(cluster),
    contentAngles: contentAnglesFor(cluster),
    hooks: generateHookVariants({
      title: workingTitle,
      claim,
      evidence,
      count: options.hooksPerCard,
      styles: options.hookStyles,
    }),
    scores: cluster.scores,
    confidence: cluster.confidence,
    risks: risksFor(cluster),
    suggestedFormats: suggestedFormats(cluster),
  };
}

function isHookStyle(value: unknown): value is HookStyle {
  return typeof value === "string" && APPROVED_HOOK_STYLES.includes(value as HookStyle);
}

function bestPlatform(value: unknown, fallback: HookVariant["bestPlatform"]): HookVariant["bestPlatform"] {
  if (value === "X" || value === "LinkedIn" || value === "newsletter" || value === "YouTube") return value;
  return fallback;
}

function fallbackWhyItWorks(style: HookStyle): string {
  if (style === "list") return "Turns the evidence into a concrete scan of related builder workflows.";
  if (style === "contrarian") return "Challenges the obvious framing while keeping the claim tied to the evidence.";
  return "Creates a curiosity gap between the visible tool and the deeper workflow pattern.";
}

export function normalizeGeminiHookVariants(
  value: unknown,
  fallbackHooks: HookVariant[],
  evidence: EvidenceItem[],
  styles: HookStyle[] = APPROVED_HOOK_STYLES,
): HookVariant[] {
  const rawHooks = Array.isArray(value) ? value : [];
  const fallbackByStyle = new Map(fallbackHooks.map((hook) => [hook.style, hook]));
  const rawByStyle = new Map<HookStyle, Record<string, unknown>>();
  for (const raw of rawHooks) {
    if (!raw || typeof raw !== "object") continue;
    const hook = raw as Record<string, unknown>;
    if (isHookStyle(hook.style) && !rawByStyle.has(hook.style)) rawByStyle.set(hook.style, hook);
  }

  const normalized: HookVariant[] = [];
  for (const style of styles.filter((style) => APPROVED_HOOK_STYLES.includes(style))) {
    const raw = rawByStyle.get(style);
    const fallback = fallbackByStyle.get(style);
    if (!raw) {
      if (fallback) normalized.push(fallback);
      continue;
    }
    const rawLines = Array.isArray(raw.lines)
      ? raw.lines.map((entry) => normalizeWhitespace(entry)).filter(Boolean)
      : [];
    const rawHook = normalizeWhitespace(raw.hook) || rawLines[0] || fallback?.hook || "";
    const hook = optimizeHookText(rawHook);
    if (!hook) {
      if (fallback) normalized.push(fallback);
      continue;
    }
    const detailLines = rawLines.slice(1).map((entry) => normalizeWhitespace(entry)).filter(Boolean);
    const lines = [hook, ...detailLines];
    const risk = assessHookRisk(lines.join("\n"), evidence);
    normalized.push({
      hook,
      lines,
      style,
      riskLevel: risk.riskLevel,
      whyItWorks: normalizeWhitespace(raw.whyItWorks) || fallback?.whyItWorks || fallbackWhyItWorks(style),
      needsFactCheck: risk.needsFactCheck,
      bestPlatform: bestPlatform(raw.bestPlatform, fallback?.bestPlatform ?? "X"),
    });
  }
  return normalized.length ? normalized : fallbackHooks;
}

export async function enhanceInsightCardWithGemini(
  card: InsightCard,
  options: { hooksPerCard: number; hookStyles?: HookStyle[] },
): Promise<InsightCard | undefined> {
  const apiKey = process.env.GEMINI_API_KEY || process.env.GOOGLE_API_KEY;
  if (!apiKey) return undefined;
  const model = process.env.GEMINI_TEXT_MODEL || "gemini-3.5-flash";
  const apiVersion = process.env.GEMINI_TEXT_API_VERSION || "v1beta";
  const hookStyles = options.hookStyles?.length ? options.hookStyles : APPROVED_HOOK_STYLES;
  const prompt = [
    "Rewrite this research insight card and generate hooks for AI builders.",
    "Use the source evidence and metrics below. Do not use generic reusable hook templates.",
    "Do not add claims that are not supported by evidence.",
    "Preserve exact named evidence. If the source names a model, company, framework, benchmark, metric, or named workflow, use that exact name in the card or hook lines instead of broad labels.",
    "Return strict JSON with workingTitle, claim, whyItMatters, contentAngles, hooks.",
    `Generate exactly one hook for each style in this order: ${hookStyles.join(", ")}.`,
    "Hook preferences: list, contrarian, curiosity.",
    "Each hook object must include style, hook, lines, bestPlatform, and whyItWorks.",
    "The hook field must be the first line and must be max 14 English words or 25 Japanese characters.",
    "lines[0] must equal hook. Follow-up lines must be concise, complete phrases.",
    "For list hooks, pick a count from the evidence/context, usually 3-5. Do not always use 5. The headline number must match the number of list items.",
    "For contrarian hooks, challenge the obvious question for this topic, not a generic question.",
    "For curiosity hooks, point to the hidden workflow or behavior pattern behind the topic.",
    "Follow-up hook lines should expose the article's concrete facts, not just restate the hook.",
    "Avoid unsupported superlatives like cheapest, fastest, best, everyone, or most-used unless explicit evidence supports them.",
    JSON.stringify({
      workingTitle: card.workingTitle,
      claim: card.claim,
      whyItMatters: card.whyItMatters,
      contentAngles: card.contentAngles,
      localFallbackHooks: card.hooks,
      evidence: card.evidence,
      risks: card.risks,
      scores: card.scores,
      confidence: card.confidence,
    }),
  ].join("\n");
  const response = await fetch(`https://generativelanguage.googleapis.com/${apiVersion}/models/${model}:generateContent`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "x-goog-api-key": apiKey,
    },
    body: JSON.stringify({
      contents: [{ role: "user", parts: [{ text: prompt }] }],
      generationConfig: { temperature: 0.2, responseMimeType: "application/json" },
    }),
  });
  if (!response.ok) throw new Error(`Gemini card generation returned HTTP ${response.status}: ${await response.text()}`);
  const data = await response.json() as { candidates?: Array<{ content?: { parts?: Array<{ text?: string }> } }> };
  const text = data.candidates?.[0]?.content?.parts?.map((part) => part.text ?? "").join("") ?? "";
  const parsed = JSON.parse(text) as Partial<InsightCard> & { hooks?: unknown };
  return {
    ...card,
    workingTitle: normalizeWhitespace(parsed.workingTitle) || card.workingTitle,
    claim: normalizeWhitespace(parsed.claim) || card.claim,
    whyItMatters: normalizeWhitespace(parsed.whyItMatters) || card.whyItMatters,
    contentAngles: Array.isArray(parsed.contentAngles) && parsed.contentAngles.length
      ? parsed.contentAngles.map((angle) => normalizeWhitespace(angle)).filter(Boolean).slice(0, 4)
      : card.contentAngles,
    hooks: normalizeGeminiHookVariants(parsed.hooks, card.hooks, card.evidence, hookStyles).slice(0, options.hooksPerCard),
  };
}

export async function generateInsightCards(
  clusters: ScoredResearchCluster[],
  options: {
    cards?: number;
    hooksPerCard: number;
    provider: "local" | "gemini";
    taxonomy: TaxonomyConfig;
  },
): Promise<InsightCard[]> {
  const hookStyles = options.taxonomy.hookStyles;
  const localCards = selectClustersForInsightCards(clusters, options.cards)
    .map((cluster) => generateLocalInsightCard(cluster, {
      hooksPerCard: options.hooksPerCard,
      hookStyles,
    }));
  if (options.provider !== "gemini") return localCards;
  const enhanced: InsightCard[] = [];
  for (const card of localCards) {
    try {
      enhanced.push(await enhanceInsightCardWithGemini(card, {
        hooksPerCard: options.hooksPerCard,
        hookStyles,
      }) ?? card);
    } catch (error) {
      console.warn(`[research_idea_generator] Gemini enhancement failed for ${card.id}; using local card: ${(error as Error).message}`);
      enhanced.push(card);
    }
  }
  return enhanced;
}
