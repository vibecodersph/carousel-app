import { normalizeWhitespace, sha256 } from "../sourcing/utils.ts";
import { APPROVED_HOOK_STYLES, optimizeHookText } from "./hooks.ts";
import type {
  CarouselBrief,
  CarouselBriefOutput,
  CarouselSlideImage,
  CarouselBriefSlide,
  EvidenceItem,
  HookVariant,
  InsightCard,
  InsightCardOutput,
} from "./types.ts";

const APPROVED_HOOK_STYLE_SET = new Set(APPROVED_HOOK_STYLES);
type CarouselBriefSlideDraft = Omit<CarouselBriefSlide, "image">;

interface SourceImageAsset {
  imageUrl: string;
  sourcePageUrl: string;
  sourceItemId: string;
  sourceName: string;
  sourceTitle: string;
  provider: string;
}

function splitSentences(value: string): string[] {
  return normalizeWhitespace(value)
    .split(/(?<=[.!?])\s+/)
    .map((line) => normalizeWhitespace(line))
    .filter(Boolean);
}

function line(value: string, limit = 88): string {
  const text = normalizeWhitespace(value).replace(/(?:\.\.\.|…)/g, "");
  if (text.length <= limit) return cleanLineEnding(text);
  const sentence = splitSentences(text).find((candidate) => candidate.length <= limit);
  if (sentence) return cleanLineEnding(sentence);
  const clipped = text.slice(0, limit + 1);
  const wordBoundary = clipped.lastIndexOf(" ");
  const cut = wordBoundary >= Math.floor(limit * 0.65) ? wordBoundary : limit;
  return cleanLineEnding(clipped.slice(0, cut));
}

function cleanLineEnding(value: string): string {
  return normalizeWhitespace(value)
    .replace(/(?:\.\.\.|…)$/u, "")
    .replace(/[\s,;:]+$/u, "")
    .trim();
}

function sourceLabel(evidence: EvidenceItem): string {
  return evidence.source || evidence.sourceName || "Source";
}

function altFor(card: InsightCard, slide: string): string {
  return line(`${slide} slide for ${card.workingTitle}.`, 160);
}

function isDirectImageUrl(value: string): boolean {
  return /\.(png|jpe?g|webp|gif)(?:[?#].*)?$/i.test(value);
}

function githubRepoNameFromUrl(value: string): string | undefined {
  try {
    const url = new URL(value);
    if (url.hostname !== "github.com" && url.hostname !== "www.github.com") return undefined;
    const [owner, repo] = url.pathname.split("/").filter(Boolean);
    if (!owner || !repo) return undefined;
    return `${owner}/${repo.replace(/\.git$/i, "")}`;
  } catch {
    return undefined;
  }
}

function githubOpenGraphImageUrl(fullName: string): string {
  return `https://opengraph.githubassets.com/1/${fullName}`;
}

function sourceImageUrlForEvidence(evidence: EvidenceItem): { imageUrl: string; provider: string } | undefined {
  if (evidence.media?.imageUrl) {
    return { imageUrl: evidence.media.imageUrl, provider: evidence.media.provider || "source_media" };
  }
  if (evidence.media?.localPath) {
    return { imageUrl: evidence.media.localPath, provider: evidence.media.provider || "local_source_media" };
  }
  if (isDirectImageUrl(evidence.url)) {
    return { imageUrl: evidence.url, provider: `${evidence.sourceName || evidence.source}_direct_image` };
  }
  const githubName = githubRepoNameFromUrl(evidence.url);
  if (githubName) {
    return { imageUrl: githubOpenGraphImageUrl(githubName), provider: "github_open_graph" };
  }
  return undefined;
}

function sourceImageAssets(card: InsightCard): SourceImageAsset[] {
  const assets: SourceImageAsset[] = [];
  const seen = new Set<string>();
  for (const evidence of card.evidence) {
    const sourceImage = sourceImageUrlForEvidence(evidence);
    if (!sourceImage || seen.has(sourceImage.imageUrl)) continue;
    seen.add(sourceImage.imageUrl);
    assets.push({
      imageUrl: sourceImage.imageUrl,
      sourcePageUrl: evidence.url,
      sourceItemId: evidence.sourceItemId || evidence.sourceExternalId || "",
      sourceName: evidence.sourceName || evidence.source,
      sourceTitle: evidence.title,
      provider: sourceImage.provider,
    });
  }
  return assets;
}

function promptSentence(label: string, value: string): string {
  const text = cleanLineEnding(value).replace(/[.!?]+$/u, "");
  return text ? `${label}: ${text}.` : "";
}

function imagePrompt(card: InsightCard, slide: CarouselBriefSlideDraft, assets: SourceImageAsset[]): string {
  const evidenceTitles = card.evidence
    .slice(0, 4)
    .map((evidence) => evidence.title)
    .filter(Boolean)
    .join("; ");
  const sourceContext = assets.length
    ? `Use these source images as visual references, not as text overlays: ${assets.map((asset) => asset.sourceTitle).join("; ")}.`
    : "No source image is available, so create an original editorial visual grounded in the evidence.";
  return [
    `Create a 4:5 Instagram carousel ${slide.type === "cover" ? "cover background" : "supporting slide image"} for AI builders.`,
    promptSentence("Topic", card.workingTitle),
    promptSentence("Slide headline", slide.headline),
    card.claim ? promptSentence("Core claim", card.claim) : "",
    evidenceTitles ? `Evidence cues: ${evidenceTitles}.` : "",
    sourceContext,
    "Style: polished technical editorial, concrete product/repo/workflow cues, high contrast, no embedded text, no fake UI labels, leave clear negative space for the slide typography.",
  ].filter(Boolean).join(" ");
}

function imageFromAsset(
  card: InsightCard,
  slide: CarouselBriefSlideDraft,
  asset: SourceImageAsset,
  role: CarouselSlideImage["role"],
): CarouselSlideImage {
  return {
    kind: "source_image",
    role,
    altText: line(`${slide.headline} using source image from ${asset.sourceTitle}.`, 160),
    rationale: role === "cover"
      ? "Single clear source image found; use it as the cover visual anchor."
      : "Slide maps directly to a source with a usable image.",
    sourceImageUrl: asset.imageUrl,
    sourceImageUrls: [asset.imageUrl],
    sourcePageUrls: [asset.sourcePageUrl],
    sourceItemIds: asset.sourceItemId ? [asset.sourceItemId] : [],
    sourceNames: [asset.sourceName],
    sourceTitles: [asset.sourceTitle],
  };
}

function generatedImagePlan(
  card: InsightCard,
  slide: CarouselBriefSlideDraft,
  assets: SourceImageAsset[],
  role: CarouselSlideImage["role"],
  rationale: string,
): CarouselSlideImage {
  return {
    kind: "generated_prompt",
    role,
    altText: line(`${slide.headline} generated image prompt for ${card.workingTitle}.`, 160),
    rationale,
    promptBase: imagePrompt(card, slide, assets),
    sourceImageUrls: assets.map((asset) => asset.imageUrl),
    sourcePageUrls: assets.map((asset) => asset.sourcePageUrl),
    sourceItemIds: assets.map((asset) => asset.sourceItemId).filter(Boolean),
    sourceNames: Array.from(new Set(assets.map((asset) => asset.sourceName))),
    sourceTitles: assets.map((asset) => asset.sourceTitle),
  };
}

function imageForSlide(card: InsightCard, slide: CarouselBriefSlideDraft, assets: SourceImageAsset[]): CarouselSlideImage {
  const role: CarouselSlideImage["role"] = slide.type === "cover" ? "cover" : "supporting";
  if (role === "cover" && assets.length === 1) {
    return imageFromAsset(card, slide, assets[0], role);
  }
  if (role === "cover" && assets.length > 1) {
    return generatedImagePlan(
      card,
      slide,
      assets.slice(0, 6),
      role,
      "Multiple source images found; generate a cover that merges the source visual assets into one coherent visual.",
    );
  }
  return generatedImagePlan(
    card,
    slide,
    assets.slice(0, 4),
    role,
    assets.length
      ? "Supporting slides should visualize the slide-specific idea while using source images as references."
      : "No source image found; generate a slide-specific visual from the hook, claim, and evidence.",
  );
}

function hashtagsFor(card: InsightCard): string {
  const text = `${card.workingTitle} ${card.claim} ${card.whyItMatters}`.toLowerCase();
  const tags = ["#AIbuilders", "#AIAgents", "#LLMOps"];
  if (/mcp|model context protocol/.test(text)) tags.push("#MCP");
  if (/open.?source|github|repo/.test(text)) tags.push("#OpenSourceAI");
  if (/local|on-device|edge/.test(text)) tags.push("#LocalAI");
  if (/security|sandbox|auth|permission/.test(text)) tags.push("#AISecurity");
  if (/workflow|orchestrat|agent graph/.test(text)) tags.push("#AgentWorkflows");
  return Array.from(new Set(tags)).slice(0, 6).join(" ");
}

function evidenceSummary(card: InsightCard): string {
  if (!card.evidence.length) return "Evidence base: no source evidence attached yet.";
  const sources = card.evidence
    .slice(0, 3)
    .map((evidence) => `${sourceLabel(evidence)}: ${evidence.title}`)
    .map((value) => line(value, 120));
  const count = `${card.evidence.length} source${card.evidence.length === 1 ? "" : "s"}`;
  return `Evidence base: ${count}, including ${sources.join("; ")}.`;
}

function instagramDescriptionFor(card: InsightCard, hook: string): string {
  const parts = [
    hook,
    line(card.claim, 320),
    line(card.whyItMatters, 260),
    evidenceSummary(card),
    card.contentAngles[0] ? `Content angle: ${line(card.contentAngles[0], 180)}` : "",
    card.risks[0] ? `Publish note: ${line(card.risks[0], 180)}` : "Publish note: treat this as a research-backed pattern, not a universal claim.",
    hashtagsFor(card),
  ].filter(Boolean);
  const accepted: string[] = [];
  for (const part of parts) {
    const next = [...accepted, part].join("\n\n");
    if (next.length > 1800) break;
    accepted.push(part);
  }
  return accepted.join("\n\n");
}

function fallbackHook(card: InsightCard): HookVariant {
  const hook = optimizeHookText(card.workingTitle);
  return {
    hook,
    lines: [hook],
    style: "curiosity",
    riskLevel: "low",
    whyItWorks: "Uses the strongest available card title when no approved hook exists.",
    needsFactCheck: false,
    bestPlatform: "X",
  };
}

function cardContext(card: InsightCard): string {
  return [
    card.workingTitle,
    card.claim,
    card.whyItMatters,
    ...card.contentAngles,
    ...card.evidence.map((evidence) => `${evidence.title} ${evidence.excerpt}`),
  ].join(" ").toLowerCase();
}

function listItemOverlap(hook: HookVariant, context: string): number {
  return hook.lines
    .slice(1)
    .map(listItemFromLine)
    .filter((item): item is { number: number; label: string } => Boolean(item))
    .filter((item) =>
      item.label
        .toLowerCase()
        .split(/[^a-z0-9]+/)
        .filter((word) => word.length >= 5)
        .some((word) => context.includes(word))
    ).length;
}

function hookScore(card: InsightCard, hook: HookVariant, usedHooks: Set<string>): number {
  const context = cardContext(card);
  let score = 0;
  if (usedHooks.has(hook.hook.toLowerCase())) score -= 3;
  if (hook.needsFactCheck) score -= 5;
  if (hook.riskLevel === "high") score -= 2;
  if (hook.riskLevel === "medium") score -= 0.5;
  if (hook.riskLevel === "low") score += 0.4;

  if (hook.style === "curiosity") {
    score += 1.7;
    if (/not a new model/i.test(hook.hook) && !/\b(model|llm|api|provider)\b/.test(context)) score -= 1.2;
  }
  if (hook.style === "contrarian") {
    score += 1.5;
    if (/\b(cost|cheap|pricing|token|security|sandbox|fragile|risk|wrong)\b/.test(context)) score += 0.5;
  }
  if (hook.style === "list") {
    const overlap = listItemOverlap(hook, context);
    score += 0.4 + overlap * 0.4;
    if (/^5 AI workflows people are actually using right now/i.test(hook.hook)) score -= 1.4;
    if (overlap < 2) score -= 0.8;
  }
  return score;
}

function selectHook(card: InsightCard, usedHooks = new Set<string>()): HookVariant {
  const approved = card.hooks
    .filter((hook) => APPROVED_HOOK_STYLE_SET.has(hook.style))
    .map((hook) => ({
      ...hook,
      hook: optimizeHookText(hook.hook),
      lines: [optimizeHookText(hook.lines[0] ?? hook.hook), ...hook.lines.slice(1).map((value) => line(value, 88))],
    }));
  return approved
    .filter((hook) => !/AI builders are converging around/i.test(hook.hook))
    .sort((a, b) => hookScore(card, b, usedHooks) - hookScore(card, a, usedHooks))[0]
    ?? approved[0]
    ?? fallbackHook(card);
}

function listItemFromLine(value: string): { number: number; label: string } | undefined {
  const match = normalizeWhitespace(value).match(/^([0-9]+)[.)]\s+(.+)$/);
  if (!match) return undefined;
  return { number: Number(match[1]), label: cleanLineEnding(match[2]) };
}

function titleCase(value: string): string {
  return value.replace(/\b[a-z]/g, (match) => match.toUpperCase());
}

function isTheBatchEvidence(evidence: EvidenceItem): boolean {
  return evidence.sourceName === "the_batch"
    || evidence.source === "The Batch (DeepLearning.AI)"
    || evidence.url.includes("deeplearning.ai/the-batch");
}

function primaryTheBatchEvidence(card: InsightCard): EvidenceItem | undefined {
  return card.evidence.find(isTheBatchEvidence);
}

function cleanEvidenceParagraph(value: string): string {
  const text = normalizeWhitespace(value)
    .replace(/\s+Tags:\s*.+$/i, "")
    .replace(/^-+\s*/, "");
  if (/^(?:article|tags):?$/i.test(text)) return "";
  if (/^tags:/i.test(text)) return "";
  return text.replace(/^(?:title|summary|article):\s*/i, "").trim();
}

function evidenceParagraphs(evidence: EvidenceItem): string[] {
  const seen = new Set<string>();
  const paragraphs: string[] = [];
  for (const raw of `${evidence.title}\n${evidence.excerpt}`.split(/\n+/)) {
    const text = cleanEvidenceParagraph(raw);
    if (!text) continue;
    const units = text.length > 260 && !/^[A-Z][A-Za-z0-9 /+().-]{2,52}:\s+/.test(text)
      ? splitSentences(text)
      : [text];
    for (const unit of units) {
      const paragraph = cleanEvidenceParagraph(unit);
      const key = paragraph.toLowerCase();
      if (!paragraph || seen.has(key)) continue;
      seen.add(key);
      paragraphs.push(paragraph);
    }
  }
  return paragraphs;
}

function sourceUrlsFor(evidence: EvidenceItem): string[] {
  return evidence.url ? [evidence.url] : [];
}

function sourceItemIdsFor(evidence: EvidenceItem): string[] | undefined {
  return evidence.sourceItemId ? [evidence.sourceItemId] : undefined;
}

function hasConcreteEvidenceToken(value: string): boolean {
  return /\b[A-Z]{2,}(?:[-.][A-Z0-9]+)*\b/.test(value)
    || /\b[A-Z]\.ai\b/.test(value)
    || /\b[A-Za-z]+(?:[-.]\d+|\d)[A-Za-z0-9.-]*\b/.test(value)
    || /\b\d[\d,.]*\s*(?:%|x|tokens?|parameters?|billion|million|thousand|seconds?|cents?|dollars?|API calls?)\b/i.test(value)
    || /\b[A-Z][A-Za-z]+(?:\s+[a-z][A-Za-z-]+){0,4}\s+loop\b/.test(value);
}

function versionedSubject(value: string): string | undefined {
  const candidates = [
    ...value.matchAll(/\b[A-Za-z]+(?:[-.]\d+(?:\.\d+)*|\d)[A-Za-z0-9.-]*\b/g),
    ...value.matchAll(/\b[A-Z][A-Za-z]+(?:\s+[A-Z]?[A-Za-z]*\d(?:\.\d+)*)\b/g),
    ...value.matchAll(/\b[A-Z]\.ai\b/g),
  ]
    .map((match) => cleanLineEnding(match[0]))
    .filter((candidate) => !/^(?:AI|API|LLM|GPU|CPU)$/i.test(candidate));
  return candidates[0];
}

function articleSubject(evidence: EvidenceItem, paragraphs: string[]): string {
  const context = `${evidence.title} ${paragraphs.slice(0, 4).join(" ")}`;
  const versioned = versionedSubject(context);
  if (versioned) return versioned;
  if (/three key loops/i.test(evidence.title)) return line(evidence.title, 74);
  return line(evidence.title, 74);
}

function firstArticleSummary(evidence: EvidenceItem, paragraphs: string[]): string {
  const titleKey = normalizeWhitespace(evidence.title).toLowerCase();
  return paragraphs
    .filter((paragraph) => paragraph.toLowerCase() !== titleKey)
    .find((paragraph) => paragraph.length >= 24 && !/^tags:/i.test(paragraph))
    ?? "";
}

function storySlide(
  evidence: EvidenceItem,
  headline: string,
  lines: string[],
  altText: string,
): CarouselBriefSlideDraft {
  const cleanLines = Array.from(new Set(lines.map(storyLine).filter(hasUsefulStoryLine))).slice(0, 2);
  return {
    slideNumber: 0,
    type: "hook_detail",
    headline: line(headline, 76),
    lines: cleanLines,
    altText,
    sourceUrls: sourceUrlsFor(evidence),
    sourceItemIds: sourceItemIdsFor(evidence),
  };
}

function storyLine(value: string): string {
  const text = line(
    normalizeWhitespace(value)
      .replace(/\s+\u2014\s+[^\u2014-]{8,110}\s+\u2014\s+/g, " ")
      .replace(/\s+\([^)]{8,120}\)/g, ""),
    156,
  );
  return cleanLineEnding(text.replace(/\b(?:of|in|on|for|to|from|with|by|as|is|are|was|were|a|an|the|its|their|this|that|not)\s*$/i, ""));
}

function hasUsefulStoryLine(value: string): boolean {
  return value.length >= 24 || /[0-9;,]|\b(?:API|MIT|MSA|MoE|DNA|RNA|VRAM)\b/.test(value);
}

function articlePointSlide(evidence: EvidenceItem, paragraphs: string[], subject: string): CarouselBriefSlideDraft {
  const headline = subject && !/^three key loops/i.test(subject)
    ? `${subject}: The Article's Point`
    : subject || "The Article's Point";
  const summary = firstArticleSummary(evidence, paragraphs);
  const title = line(evidence.title, 104);
  const lines = summary ? [summary] : [title];
  return storySlide(
    evidence,
    headline,
    lines.filter(Boolean),
    line(`Article point slide for ${evidence.title}.`, 160),
  );
}

function isMetaFactLabel(label: string): boolean {
  return /^(?:title|summary|article|tags|source|sources)$/i.test(normalizeWhitespace(label));
}

function colonFactSlide(evidence: EvidenceItem, paragraph: string): CarouselBriefSlideDraft | undefined {
  if (paragraph.toLowerCase() === normalizeWhitespace(evidence.title).toLowerCase()) return undefined;
  const match = paragraph.match(/^([A-Z][A-Za-z0-9 /+().-]{2,52}):\s+(.{12,})$/);
  if (!match || isMetaFactLabel(match[1])) return undefined;
  const detail = match[2];
  if (!hasConcreteEvidenceToken(paragraph) && !/\b(loop|agent|coding|cost|training|architecture|benchmark|performance)\b/i.test(paragraph)) {
    return undefined;
  }
  return storySlide(
    evidence,
    titleCase(cleanLineEnding(match[1])),
    splitSentences(detail).length ? splitSentences(detail) : [detail],
    line(`Article fact slide about ${match[1]} from ${evidence.title}.`, 160),
  );
}

function namedLoopFromParagraph(paragraph: string): string | undefined {
  for (const match of paragraph.matchAll(/\b([A-Z][A-Za-z]+(?:\s+(?:[a-z][A-Za-z-]+|[A-Z][A-Za-z-]+)){0,4}\s+loop)\b/g)) {
    const loopName = cleanLineEnding(match[1]);
    if (!/^(?:this|the|a|an|each|key)\s/i.test(loopName)) return loopName;
  }
  return undefined;
}

function loopFactSlides(evidence: EvidenceItem, paragraphs: string[]): CarouselBriefSlideDraft[] {
  const slides: CarouselBriefSlideDraft[] = [];
  const seen = new Set<string>();
  for (let index = 0; index < paragraphs.length; index += 1) {
    const paragraph = paragraphs[index];
    const loopName = namedLoopFromParagraph(paragraph);
    if (!loopName || seen.has(loopName.toLowerCase())) continue;
    const rest = cleanLineEnding(paragraph.slice(paragraph.indexOf(loopName) + loopName.length).replace(/^[-:.\s]+/, ""));
    const detail = rest.length >= 24 ? rest : paragraphs[index + 1] ?? "";
    seen.add(loopName.toLowerCase());
    slides.push(storySlide(
      evidence,
      titleCase(loopName),
      detail ? splitSentences(detail) : [],
      line(`Named loop slide ${loopName} from ${evidence.title}.`, 160),
    ));
  }
  return slides;
}

function isHeadingParagraph(paragraph: string): boolean {
  return paragraph.length >= 8
    && paragraph.length <= 88
    && !/[.!?]$/u.test(paragraph)
    && /\b(loop|architecture|training|performance|availability|input|output|benchmark|cost|why it matters|how it works)\b/i.test(paragraph);
}

function headingFactSlides(evidence: EvidenceItem, paragraphs: string[]): CarouselBriefSlideDraft[] {
  const slides: CarouselBriefSlideDraft[] = [];
  for (let index = 0; index < paragraphs.length - 1; index += 1) {
    const heading = paragraphs[index];
    if (!isHeadingParagraph(heading)) continue;
    const detail = paragraphs[index + 1];
    if (!detail || isHeadingParagraph(detail)) continue;
    slides.push(storySlide(
      evidence,
      titleCase(heading),
      splitSentences(detail).length ? splitSentences(detail) : [detail],
      line(`Article section slide ${heading} from ${evidence.title}.`, 160),
    ));
  }
  return slides;
}

function factHeadline(sentence: string, subject: string): string {
  const prefix = subject && !/^three key loops/i.test(subject) ? `${subject}: ` : "";
  if (/\b(input|output|context|token)\b/i.test(sentence)) return `${prefix}Input And Output`;
  if (/\b(architecture|parameter|mixture|expert|transformer)\b/i.test(sentence)) return `${prefix}Architecture`;
  if (/\b(benchmark|score|leaderboard|performance|outperform|rival)\b/i.test(sentence)) return `${prefix}Performance`;
  if (/\b(cost|price|pricing|cheap|low cost)\b/i.test(sentence)) return `${prefix}Cost`;
  if (/\b(function calling|structured output|context caching|reasoning)\b/i.test(sentence)) return `${prefix}Builder Features`;
  if (/\b(train|training|data)\b/i.test(sentence)) return `${prefix}Training`;
  return line(sentence, 76);
}

function genericFactSlides(evidence: EvidenceItem, paragraphs: string[], subject: string): CarouselBriefSlideDraft[] {
  const sentences = paragraphs
    .flatMap((paragraph) => splitSentences(paragraph).length ? splitSentences(paragraph) : [paragraph])
    .filter((sentence) => sentence.length >= 32)
    .filter(hasConcreteEvidenceToken);
  return sentences.map((sentence) => storySlide(
    evidence,
    factHeadline(sentence, subject),
    [sentence],
    line(`Concrete fact slide from ${evidence.title}.`, 160),
  ));
}

function uniqueStorySlides(slides: CarouselBriefSlideDraft[]): CarouselBriefSlideDraft[] {
  const seen = new Set<string>();
  const unique: CarouselBriefSlideDraft[] = [];
  for (const slide of slides) {
    const signature = `${slide.headline}\n${slide.lines.join("\n")}`.toLowerCase();
    if (seen.has(signature)) continue;
    seen.add(signature);
    unique.push(slide);
  }
  return unique;
}

function theBatchStorySlides(card: InsightCard): CarouselBriefSlideDraft[] {
  const evidence = primaryTheBatchEvidence(card);
  if (!evidence) return [];
  const paragraphs = evidenceParagraphs(evidence);
  const subject = articleSubject(evidence, paragraphs);
  const slides = [articlePointSlide(evidence, paragraphs, subject)];
  const candidates = uniqueStorySlides([
    ...paragraphs.map((paragraph) => colonFactSlide(evidence, paragraph)).filter((slide): slide is CarouselBriefSlideDraft => Boolean(slide)),
    ...loopFactSlides(evidence, paragraphs),
    ...headingFactSlides(evidence, paragraphs),
    ...genericFactSlides(evidence, paragraphs, subject),
  ]);
  const usedHeadlines = new Set(slides.map((slide) => slide.headline.toLowerCase()));
  for (const candidate of candidates) {
    if (slides.length >= 5) break;
    if (usedHeadlines.has(candidate.headline.toLowerCase())) continue;
    if (!candidate.lines.length && !/\bloop\b/i.test(candidate.headline)) continue;
    if (candidate.lines.length === 1 && candidate.lines[0].toLowerCase() === candidate.headline.toLowerCase()) continue;
    usedHeadlines.add(candidate.headline.toLowerCase());
    slides.push(candidate);
  }
  return slides;
}

function hookDetailUnits(lines: string[]): string[] {
  const units: string[] = [];
  let pending = "";
  for (const raw of lines) {
    const text = normalizeWhitespace(raw);
    if (!text) continue;
    const joinsWithNext = /^(the\s+)?(?:better|real)\s+question$/i.test(text);
    pending = pending ? `${pending} ${text}` : text;
    if (joinsWithNext) pending = `${pending}:`;
    if (/[,:;]$/u.test(text) || joinsWithNext) continue;
    units.push(pending);
    pending = "";
  }
  if (pending) units.push(pending);
  return units.map((value) => line(value, 96)).filter(Boolean);
}

function hookDetailSlides(card: InsightCard, selectedHook: HookVariant): CarouselBriefSlideDraft[] {
  if (selectedHook.style === "list") {
    return selectedHook.lines
      .slice(1)
      .map(listItemFromLine)
      .filter((item): item is { number: number; label: string } => Boolean(item))
      .map((item) => ({
        slideNumber: 0,
        type: "list_item",
        headline: `${item.number}. ${titleCase(item.label)}`,
        lines: [],
        altText: line(`List item slide ${item.number} for ${card.workingTitle}: ${item.label}.`, 160),
      }));
  }
  return hookDetailUnits(selectedHook.lines.slice(1)).map((headline, index) => ({
    slideNumber: 0,
    type: "hook_detail",
    headline,
    lines: [],
    altText: line(`Hook detail slide ${index + 1} for ${card.workingTitle}.`, 160),
  }));
}

function renumberSlides(slides: CarouselBriefSlideDraft[]): CarouselBriefSlideDraft[] {
  return slides.map((slide, index) => ({ ...slide, slideNumber: index + 1 }));
}

function buildSlides(card: InsightCard, selectedHook: HookVariant): CarouselBriefSlide[] {
  const slides: CarouselBriefSlideDraft[] = [];
  const hook = selectedHook.hook;
  slides.push({
    slideNumber: 0,
    type: "cover",
    headline: hook,
    lines: [],
    altText: altFor(card, "Cover"),
  });
  const articleSlides = theBatchStorySlides(card);
  slides.push(...(articleSlides.length ? articleSlides : hookDetailSlides(card, selectedHook)));
  const assets = sourceImageAssets(card);
  return renumberSlides(slides).map((slide) => ({
    ...slide,
    image: imageForSlide(card, slide, assets),
  }));
}

export function insightCardToCarouselBrief(card: InsightCard, usedHooks = new Set<string>()): CarouselBrief {
  const selectedHook = selectHook(card, usedHooks);
  const hook = selectedHook.hook;
  usedHooks.add(hook.toLowerCase());
  const slides = buildSlides(card, selectedHook);
  const instagramDescription = instagramDescriptionFor(card, hook);
  return {
    id: sha256(`carousel-brief:${card.id}:${hook}`).slice(0, 16),
    sourceInsightCardId: card.id,
    workingTitle: card.workingTitle,
    hook,
    hookStyle: selectedHook.style,
    hookRiskLevel: selectedHook.riskLevel,
    hookNeedsFactCheck: selectedHook.needsFactCheck,
    hookBestPlatform: selectedHook.bestPlatform,
    confidence: card.confidence,
    score: card.scores.overall,
    suggestedFormat: "instagram_carousel",
    slideCount: slides.length,
    slides,
    instagramDescription,
    evidenceSourceItemIds: card.evidence
      .map((evidence) => evidence.sourceItemId)
      .filter((value): value is string => Boolean(value)),
    evidenceUrls: card.evidence.map((evidence) => evidence.url).filter(Boolean),
  };
}

export function generateCarouselBriefs(output: InsightCardOutput): CarouselBriefOutput {
  const usedHooks = new Set<string>();
  const carousels = output.cards.map((card) => insightCardToCarouselBrief(card, usedHooks));
  return {
    generatedAt: output.generatedAt,
    audience: output.audience,
    sourceInsightGeneratedAt: output.generatedAt,
    carouselCount: carousels.length,
    carousels,
  };
}
