import { dirname } from "node:path";
import { mkdir, writeFile } from "node:fs/promises";
import type { SourceItem } from "../sourcing/types.ts";
import { normalizeWhitespace, readJsonFile, sha256, writeJsonFile } from "../sourcing/utils.ts";
import { dedupeResearchItems } from "./sources/index.ts";
import { extractTheBatchNextData, fetchTheBatchSourceItems } from "./sources/theBatch.ts";
import type {
  CarouselBrief,
  CarouselBriefOutput,
  CarouselBriefSlide,
  CarouselSlideImage,
  HookStyle,
  ResearchGeneratorOptions,
} from "./types.ts";

type CoverTemplateId = "stop-signal" | "pattern-break" | "metric-snap" | "split-switch" | "loom-reveal";
type StoryKind = "story" | "letter";

interface StoryDraft {
  sourceItem: SourceItem;
  sourceItemId: string;
  kind: StoryKind;
  titleEn: string;
  titleJa: string;
  summaryJa: string;
  keyPointJa: string;
  builderAngleJa: string;
  caveatJa: string;
  hook: string;
  hookStyle: HookStyle;
  coverTemplate: CoverTemplateId;
  label: string;
  style: string;
  kicker: string;
  swipe: string;
  why: string;
  rank: number;
  score: number;
}

interface GeminiStoryDraft {
  sourceItemId?: string;
  titleJa?: string;
  summaryJa?: string;
  keyPointJa?: string;
  builderAngleJa?: string;
  caveatJa?: string;
  hook?: string;
  hookStyle?: HookStyle;
  coverTemplate?: CoverTemplateId;
  label?: string;
  style?: string;
  kicker?: string;
  swipe?: string;
  why?: string;
  rank?: number;
}

interface CoverCandidateOutput {
  issue: {
    title: string;
    issue?: string;
    generatedAt: string;
    issueUrl?: string;
    issueTagUrl?: string;
  };
  stories: Array<Record<string, unknown>>;
  candidates: Array<Record<string, unknown>>;
}

const COVER_TEMPLATES = new Set<CoverTemplateId>([
  "stop-signal",
  "pattern-break",
  "metric-snap",
  "split-switch",
  "loom-reveal",
]);
const DEFAULT_USER_AGENT = "carousel-app-the-batch-issue-briefs/0.1";
const DEFAULT_FETCH_TIMEOUT_MS = 30_000;
const DEFAULT_GEMINI_TIMEOUT_MS = 45_000;

function envTimeoutMs(name: string, fallback: number): number {
  const value = Number(process.env[name]);
  return Number.isFinite(value) && value > 0 ? value : fallback;
}

async function fetchWithTimeout(
  url: string,
  init: RequestInit,
  timeoutMs: number,
  label: string,
): Promise<Response> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...init, signal: controller.signal });
  } catch (error) {
    if ((error as { name?: string }).name === "AbortError") {
      throw new Error(`${label} timed out after ${timeoutMs}ms`);
    }
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}

function stringValue(value: unknown): string {
  return String(value ?? "").trim();
}

function compact(value: unknown, limit: number): string {
  const text = normalizeWhitespace(value);
  if (text.length <= limit) return text;
  const clipped = text.slice(0, limit + 1);
  const boundary = Math.max(clipped.lastIndexOf("。"), clipped.lastIndexOf("、"), clipped.lastIndexOf(" "));
  const cut = boundary >= Math.floor(limit * 0.55) ? boundary : limit;
  return normalizeWhitespace(clipped.slice(0, cut));
}

function visibleJapaneseChars(value: string): number {
  return Array.from(normalizeWhitespace(value).replace(/\s+/g, "")).length;
}

function fitJapaneseHook(value: string): string {
  const text = normalizeWhitespace(value).replace(/[。！？!?、：:]+$/u, "");
  if (visibleJapaneseChars(text) <= 25) return text;
  return Array.from(text).slice(0, 25).join("").replace(/[。！？!?、：:]+$/u, "");
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? value as Record<string, unknown> : {};
}

function rawStory(item: SourceItem): Record<string, unknown> {
  return record(record(item.raw).fullStory);
}

function sourceTags(item: SourceItem): string[] {
  const rawTags = record(item.raw).tags;
  return Array.isArray(rawTags) ? rawTags.map((tag) => normalizeWhitespace(tag)).filter(Boolean) : [];
}

function articleText(item: SourceItem): string {
  return stringValue(rawStory(item).articleText) || item.body || "";
}

function englishSummary(item: SourceItem): string {
  return stringValue(rawStory(item).summary) || item.body || item.title;
}

function titleLead(title: string): string {
  return normalizeWhitespace(title).split(":")[0] || title;
}

function hasAny(text: string, patterns: RegExp[]): boolean {
  return patterns.some((pattern) => pattern.test(text));
}

function storyKind(item: SourceItem): StoryKind {
  const tags = sourceTags(item).join(" ").toLowerCase();
  const text = `${item.title} ${item.url} ${tags}`.toLowerCase();
  return /\bletters?\b|andrew ng|how-we-decide-what-courses|courses-to-teach/.test(text) ? "letter" : "story";
}

function storyKey(item: SourceItem): string {
  return `${item.title} ${item.body} ${articleText(item)} ${item.url}`.toLowerCase();
}

function storyMetaKey(item: SourceItem): string {
  return `${item.title} ${item.url} ${sourceTags(item).join(" ")} ${englishSummary(item)}`.toLowerCase();
}

function subjectToken(item: SourceItem): string {
  const title = item.title;
  const candidates = [
    ...title.matchAll(/\b(?:GPT-\d+(?:\.\d+)*|MAI-[A-Za-z0-9.-]+|RoboReward|Fugu(?:-Ultra)?|Claude|Gemini|OpenAI|Microsoft|Sakana)\b/g),
  ].map((match) => match[0]);
  if (candidates[0]) return candidates[0];
  return titleLead(title).split(/\s+/).slice(0, 3).join(" ");
}

function localTitleJa(item: SourceItem, kind: StoryKind): string {
  const text = storyMetaKey(item);
  if (/roboreward|robot|reward model/.test(text)) return "ロボット報酬モデルの前進";
  if (/microsoft|mai-thinking|distillation/.test(text)) return "Microsoft、自前AIへ";
  if (/fugu|sakana|orchestrat|spawn/.test(text)) return "Fugu、AIを使い分ける";
  if (/gpt-\d|openai/.test(text)) return "GPT-5.6、限定プレビュー";
  if (kind === "letter") return "AIのノイズから学ぶ順番を選ぶ";
  return `${subjectToken(item)}の要点`;
}

function localHook(item: SourceItem, kind: StoryKind): string {
  const text = storyMetaKey(item);
  if (/roboreward|robot|reward model/.test(text)) return "ロボット報酬、手作業に迫る";
  if (/microsoft|mai-thinking/.test(text)) return "Microsoft、自前AIへ";
  if (/fugu|sakana|orchestrat|spawn/.test(text)) return "AIがAIを使い分ける";
  if (/gpt-\d|openai/.test(text) && /government|selected|limited|preview|limbo/.test(text)) return "GPT-5.6、まだ届かない";
  if (/government|selected|limited/.test(text)) return "政府先行のAIプレビュー";
  if (kind === "letter" || /hype|sales pitch|noisy|courses/.test(text)) return "AIのノイズ、どう削る？";
  return fitJapaneseHook(`${subjectToken(item)}、何が違う？`);
}

function localCoverTemplate(item: SourceItem, hook: string, kind: StoryKind): CoverTemplateId {
  const text = `${storyMetaKey(item)} ${hook}`.toLowerCase();
  if (kind === "letter" || /hype|noise|wrong|risk|limited|government|届かない|狭い/.test(text)) return "stop-signal";
  if (/microsoft|own|自前|versus|instead|from scratch|distillation/.test(text)) return "split-switch";
  if (/fugu|orchestrat|spawn|route|使い分け|束ねる/.test(text)) return "split-switch";
  if (/reward|benchmark|score|performance|報酬|迫る/.test(text)) return "metric-snap";
  if (/\d|models?|family|roundup/.test(text)) return "pattern-break";
  return "loom-reveal";
}

function localHookStyle(item: SourceItem, kind: StoryKind): HookStyle {
  const text = storyMetaKey(item);
  if (kind === "letter" || /wrong|noise|hype|not /.test(text)) return "contrarian";
  if (/\b\d+\b|family|models|ways/.test(text)) return "list";
  return "curiosity";
}

function localSummaryJa(item: SourceItem, kind: StoryKind): string {
  const text = storyMetaKey(item);
  if (/roboreward|robot|reward model/.test(text)) {
    return "RoboRewardは視覚言語モデルを使った報酬モデル群です。ロボット強化学習で、手作り報酬関数との差を縮めます。";
  }
  if (/microsoft|mai-thinking/.test(text)) {
    return "MicrosoftはMAI-Thinking-1を明らかにしました。蒸留ではなく一から作った推論モデルという点が焦点です。";
  }
  if (/fugu|sakana|orchestrat|spawn/.test(text)) {
    return "Sakana AIのFuguは、タスクごとにClaude、Gemini、GPT系エージェントを呼び分けるモデル・オーケストレーターです。";
  }
  if (/gpt-\d|openai/.test(text)) {
    return "OpenAIはGPT-5.6ファミリーをプレビューしました。ただし現時点では、選ばれた米政府ユーザー向けの限定提供です。";
  }
  if (kind === "letter" || /hype|sales pitch|noisy|courses/.test(text)) {
    return "AI界隈のノイズや売り込みから距離を取り、どのベンダーにも応用できる重要な技術に絞るという話です。";
  }
  return compact(englishSummary(item), 88);
}

function localKeyPointJa(item: SourceItem, kind: StoryKind): string {
  const text = storyMetaKey(item);
  if (/roboreward|robot|reward model/.test(text)) return "報酬関数を毎回手で作る負担を、視覚言語モデルでどこまで置き換えられるかが論点です。";
  if (/microsoft|mai-thinking/.test(text)) return "OpenAIとの関係が強いMicrosoftが、自前の推論モデルを持つ意味が大きいです。";
  if (/fugu|sakana|orchestrat|spawn/.test(text)) return "単体モデルを選ぶ発想から、複数モデルをタスク単位で束ねる発想に寄っています。";
  if (/gpt-\d|openai/.test(text)) return "話題はモデル性能だけでなく、誰が先に使えるのかというアクセス設計にもあります。";
  if (kind === "letter") return "ハイプの量ではなく、長く使える概念と実装力に学習時間を寄せる姿勢です。";
  return "ニュースの見出しより、現場で何が変わるかを確認したい話です。";
}

function localBuilderAngleJa(item: SourceItem, kind: StoryKind): string {
  const text = storyMetaKey(item);
  if (/fugu|orchestrat|spawn|route/.test(text)) return "AIプロダクトでは、モデル選定を固定設定ではなくルーティングの問題として見る流れが強まりそうです。";
  if (/microsoft|mai-thinking/.test(text)) return "大手プラットフォームのモデル内製化は、開発者の選択肢と依存関係を変える可能性があります。";
  if (/roboreward|robot|reward model/.test(text)) return "ロボット学習の改善は派手に見えませんが、現実環境での試行回数と設計コストに効きます。";
  if (/gpt-\d|openai/.test(text)) return "新モデルの発表を見る時は、性能表だけでなく提供範囲、API、価格、規制面をセットで見たいところです。";
  if (kind === "letter") return "学ぶ側も作る側も、売り文句より再利用できる原理を優先するほうが迷いにくくなります。";
  return "まずは自分のワークフローで、何が速くなるのか、何が安全になるのかに分解して見るのがよさそうです。";
}

function localCaveatJa(item: SourceItem, kind: StoryKind): string {
  const text = storyMetaKey(item);
  if (/roboreward|robot|reward model/.test(text)) return "研究成果なので、現場導入にはタスク差、環境差、安全性の検証が残ります。";
  if (/microsoft|mai-thinking/.test(text)) return "発表時点のモデル評価だけでなく、提供形態、価格、既存エコシステムとの接続が重要です。";
  if (/fugu|sakana|orchestrat|spawn/.test(text)) return "オーケストレーションは便利ですが、失敗時の責任範囲、コスト、遅延は別途見積もる必要があります。";
  if (/gpt-\d|openai/.test(text)) return "広い提供前の情報なので、実際の開発者体験は公開範囲とAPI条件を見てから判断です。";
  if (kind === "letter") return "何を学ばないかを決めるのは有効ですが、判断基準そのものも定期的に更新する必要があります。";
  return "初期発表だけで判断せず、一次情報と利用条件を確認したいところです。";
}

function labelFor(item: SourceItem, kind: StoryKind): string {
  const text = storyMetaKey(item);
  if (/roboreward|robot|reward/.test(text)) return "Robot reward metric";
  if (/microsoft|mai-thinking/.test(text)) return "Microsoft split-switch";
  if (/fugu|sakana|orchestrat|spawn/.test(text)) return "Fugu model router";
  if (/gpt-\d|openai/.test(text)) return "GPT access gate";
  if (kind === "letter") return "Hype noise filter";
  return `${subjectToken(item)} reveal`;
}

function visualStyleFor(template: CoverTemplateId, kind: StoryKind): string {
  if (kind === "letter") return "type noise + stop-signal";
  if (template === "stop-signal") return "restricted gate + warning line";
  if (template === "split-switch") return "split-switch comparison";
  if (template === "metric-snap") return "metric snap + reward signal";
  if (template === "pattern-break") return "pattern-break grid";
  return "loom reveal";
}

function sourceImage(item: SourceItem): string {
  return item.media.imageUrl || item.media.localPath || "";
}

async function fetchText(url: string): Promise<string> {
  const response = await fetchWithTimeout(
    url,
    {
      headers: {
        "User-Agent": DEFAULT_USER_AGENT,
        Accept: "text/html,application/xhtml+xml",
      },
    },
    envTimeoutMs("THE_BATCH_FETCH_TIMEOUT_MS", DEFAULT_FETCH_TIMEOUT_MS),
    `The Batch fetch ${url}`,
  );
  if (!response.ok) throw new Error(`HTTP ${response.status} for ${url}`);
  return response.text();
}

function isTheBatchIssueUrl(value: string): boolean {
  return /deeplearning\.ai\/the-batch\/issue-\d+\/?$/i.test(value);
}

function isDateTagSlug(value: string): boolean {
  return /^[a-z]{3}-\d{1,2}-\d{4}$/i.test(value);
}

function issueDateTagUrlFromHtml(html: string, issueUrl: string): string | undefined {
  const data = extractTheBatchNextData(html);
  const post = data?.props?.pageProps?.post;
  const tags = Array.isArray(post?.tags) ? post.tags : [];
  const dateTag = tags
    .map((tag) => record(tag))
    .find((tag) => isDateTagSlug(normalizeWhitespace(tag.slug)));
  const slug = normalizeWhitespace(dateTag?.slug);
  return slug ? new URL(`/the-batch/tag/${slug}`, issueUrl).toString() : undefined;
}

async function resolveIssueTagUrl(value: string | undefined): Promise<string | undefined> {
  const url = normalizeWhitespace(value);
  if (!url || !isTheBatchIssueUrl(url)) return url || undefined;
  return issueDateTagUrlFromHtml(await fetchText(url), url) ?? url;
}

function slideImage(
  item: SourceItem,
  role: CarouselSlideImage["role"],
  headline: string,
  options: { useSourceImage?: boolean } = {},
): CarouselSlideImage {
  const imageUrl = sourceImage(item);
  const sourceTitle = item.title;
  const promptBase = [
    `Editorial visual for ${sourceTitle}.`,
    `Slide focus: ${headline}.`,
    "Abstract AI/news illustration, cream paper, dark ink, terracotta accent, clean negative space for Japanese carousel typography.",
    "No text, no logos, no UI, no charts, no readable marks.",
  ].join(" ");
  if (imageUrl && options.useSourceImage) {
    return {
      kind: "source_image",
      role,
      altText: compact(`${headline} using source image from ${sourceTitle}.`, 160),
      rationale: role === "cover"
        ? "Use the source feature image as the visual anchor while the kinetic cover carries the hook."
        : "Use the raw source feature image so the body slide stays tied to the article.",
      sourceImageUrl: imageUrl,
      sourceImageUrls: [imageUrl],
      sourcePageUrls: [item.url],
      sourceItemIds: [item.id],
      sourceNames: [item.source],
      sourceTitles: [sourceTitle],
    };
  }
  return {
    kind: "generated_prompt",
    role,
    altText: compact(`${headline} generated visual plan for ${sourceTitle}.`, 160),
    rationale: imageUrl
      ? "Generate a fresh supporting visual; keep the source image URL only for provenance and raw-image fallback."
      : "No source image was available; generate a quiet editorial visual only if image generation is enabled.",
    promptBase,
    sourceImageUrls: imageUrl ? [imageUrl] : [],
    sourcePageUrls: [item.url],
    sourceItemIds: [item.id],
    sourceNames: [item.source],
    sourceTitles: [sourceTitle],
  };
}

function localDraft(item: SourceItem, index: number): StoryDraft {
  const kind = storyKind(item);
  const hook = localHook(item, kind);
  const coverTemplate = localCoverTemplate(item, hook, kind);
  const score = Math.max(1, Number(item.metrics.score ?? item.metrics.upvotes ?? 50));
  return {
    sourceItem: item,
    sourceItemId: item.id,
    kind,
    titleEn: item.title,
    titleJa: localTitleJa(item, kind),
    summaryJa: localSummaryJa(item, kind),
    keyPointJa: localKeyPointJa(item, kind),
    builderAngleJa: localBuilderAngleJa(item, kind),
    caveatJa: localCaveatJa(item, kind),
    hook,
    hookStyle: localHookStyle(item, kind),
    coverTemplate,
    label: labelFor(item, kind),
    style: visualStyleFor(coverTemplate, kind),
    kicker: kind === "letter" ? "ANDREW NG LETTER" : "THE BATCH",
    swipe: kind === "letter" ? "学ぶ順番の話" : "スワイプで要点",
    why: `${hook} is short, concrete, and maps to the ${coverTemplate} kinetic cover template.`,
    rank: index + 1,
    score,
  };
}

function normalizeTemplate(value: unknown, fallback: CoverTemplateId): CoverTemplateId {
  const text = normalizeWhitespace(value).replace(/_/g, "-") as CoverTemplateId;
  return COVER_TEMPLATES.has(text) ? text : fallback;
}

function normalizeHookStyle(value: unknown, fallback: HookStyle): HookStyle {
  return value === "list" || value === "contrarian" || value === "curiosity" ? value : fallback;
}

function applyGeminiDrafts(localDrafts: StoryDraft[], geminiDrafts: GeminiStoryDraft[]): StoryDraft[] {
  const byId = new Map(geminiDrafts.map((draft) => [normalizeWhitespace(draft.sourceItemId), draft]));
  return localDrafts.map((draft) => {
    const gemini = byId.get(draft.sourceItemId);
    if (!gemini) return draft;
    const hook = fitJapaneseHook(normalizeWhitespace(gemini.hook) || draft.hook);
    return {
      ...draft,
      titleJa: normalizeWhitespace(gemini.titleJa) || draft.titleJa,
      summaryJa: normalizeWhitespace(gemini.summaryJa) || draft.summaryJa,
      keyPointJa: normalizeWhitespace(gemini.keyPointJa) || draft.keyPointJa,
      builderAngleJa: normalizeWhitespace(gemini.builderAngleJa) || draft.builderAngleJa,
      caveatJa: normalizeWhitespace(gemini.caveatJa) || draft.caveatJa,
      hook,
      hookStyle: normalizeHookStyle(gemini.hookStyle, draft.hookStyle),
      coverTemplate: normalizeTemplate(gemini.coverTemplate, draft.coverTemplate),
      label: normalizeWhitespace(gemini.label) || draft.label,
      style: normalizeWhitespace(gemini.style) || draft.style,
      kicker: normalizeWhitespace(gemini.kicker) || draft.kicker,
      swipe: normalizeWhitespace(gemini.swipe) || draft.swipe,
      why: normalizeWhitespace(gemini.why) || draft.why,
      rank: Number.isFinite(gemini.rank) ? Number(gemini.rank) : draft.rank,
    };
  });
}

async function geminiIssueDrafts(localDrafts: StoryDraft[]): Promise<GeminiStoryDraft[] | undefined> {
  const apiKey = process.env.GEMINI_API_KEY || process.env.GOOGLE_API_KEY;
  if (!apiKey) return undefined;
  const model = process.env.GEMINI_TEXT_MODEL || "gemini-3.5-flash";
  const apiVersion = process.env.GEMINI_TEXT_API_VERSION || "v1beta";
  const prompt = [
    "You are improving Japanese Instagram carousel hooks for AI Brief JP.",
    "Audience: Japanese AI builders and curious non-engineers. Voice: working Japanese engineer, restrained, clear, no hype.",
    "Use only the facts in the source items. Do not invent numbers or claims.",
    "For each source item, return one candidate with a hook <=25 visible Japanese characters.",
    "Pick coverTemplate from exactly: stop-signal, pattern-break, metric-snap, split-switch, loom-reveal.",
    "Use the letter item as content too; do not drop it just because it is not a news story.",
    "Return JSON only: {\"candidates\":[{sourceItemId,titleJa,summaryJa,keyPointJa,builderAngleJa,caveatJa,hook,hookStyle,coverTemplate,label,style,kicker,swipe,why,rank}]}",
    JSON.stringify({
      localDrafts: localDrafts.map((draft) => ({
        sourceItemId: draft.sourceItemId,
        kind: draft.kind,
        titleEn: draft.titleEn,
        titleJa: draft.titleJa,
        sourceUrl: draft.sourceItem.url,
        summaryEn: englishSummary(draft.sourceItem),
        articleExcerpt: compact(articleText(draft.sourceItem), 900),
        localHook: draft.hook,
        localCoverTemplate: draft.coverTemplate,
      })),
    }),
  ].join("\n");
  const response = await fetchWithTimeout(
    `https://generativelanguage.googleapis.com/${apiVersion}/models/${model}:generateContent`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "x-goog-api-key": apiKey,
      },
      body: JSON.stringify({
        contents: [{ role: "user", parts: [{ text: prompt }] }],
        generationConfig: { temperature: 0.25, responseMimeType: "application/json" },
      }),
    },
    envTimeoutMs("GEMINI_TEXT_TIMEOUT_MS", DEFAULT_GEMINI_TIMEOUT_MS),
    "Gemini issue generation",
  );
  if (!response.ok) throw new Error(`Gemini issue generation returned HTTP ${response.status}: ${await response.text()}`);
  const data = await response.json() as { candidates?: Array<{ content?: { parts?: Array<{ text?: string }> } }> };
  const text = data.candidates?.[0]?.content?.parts?.map((part) => part.text ?? "").join("") ?? "";
  const parsed = JSON.parse(text) as { candidates?: GeminiStoryDraft[] };
  return Array.isArray(parsed.candidates) ? parsed.candidates : undefined;
}

async function maybeEnhanceWithGemini(drafts: StoryDraft[], provider: string): Promise<StoryDraft[]> {
  if (provider !== "gemini") return drafts;
  try {
    const geminiDrafts = await geminiIssueDrafts(drafts);
    return geminiDrafts?.length ? applyGeminiDrafts(drafts, geminiDrafts) : drafts;
  } catch (error) {
    console.warn(`[the-batch-issue] Gemini enhancement failed; using local hooks (${(error as Error).message})`);
    return drafts;
  }
}

function sortDrafts(drafts: StoryDraft[]): StoryDraft[] {
  function editorialPriority(draft: StoryDraft): number {
    const text = `${draft.hook} ${draft.titleJa} ${draft.titleEn}`.toLowerCase();
    if (/fugu|使い分け|orchestrat|sakana/.test(text)) return 50;
    if (/gpt-5\.6|まだ届かない|government|限定/.test(text)) return 45;
    if (/ロボット報酬|roboreward|reward/.test(text)) return 40;
    if (/microsoft|mai-thinking|自前/.test(text)) return 35;
    if (draft.kind === "letter") return 30;
    return 10;
  }
  return [...drafts]
    .sort((a, b) => {
      const kindRank = (b.kind === "letter" ? 0 : 1) - (a.kind === "letter" ? 0 : 1);
      return kindRank || editorialPriority(b) - editorialPriority(a) || b.score - a.score || a.titleEn.localeCompare(b.titleEn);
    })
    .map((draft, index) => ({ ...draft, rank: index + 1 }));
}

function candidateId(draft: StoryDraft): string {
  return sha256(`the-batch-candidate:${draft.sourceItemId}:${draft.hook}:${draft.coverTemplate}`).slice(0, 16);
}

function briefId(draft: StoryDraft): string {
  return sha256(`the-batch-issue-brief:${draft.sourceItemId}:${draft.hook}:${draft.coverTemplate}`).slice(0, 16);
}

function slide(
  draft: StoryDraft,
  slideNumber: number,
  type: CarouselBriefSlide["type"],
  headline: string,
  lines: string[],
): CarouselBriefSlide {
  return {
    slideNumber,
    type,
    headline,
    lines: lines.map((value) => compact(value, 138)).filter(Boolean),
    altText: `${headline} slide for ${draft.titleJa}.`,
    image: slideImage(
      draft.sourceItem,
      slideNumber === 1 ? "cover" : "supporting",
      headline,
      { useSourceImage: slideNumber === 2 },
    ),
    sourceUrls: slideNumber === 1 ? undefined : [draft.sourceItem.url],
    sourceItemIds: slideNumber === 1 ? undefined : [draft.sourceItemId],
  };
}

function slidesForDraft(draft: StoryDraft): CarouselBriefSlide[] {
  return [
    slide(draft, 1, "cover", draft.hook, []),
    slide(draft, 2, "hook_detail", "何が起きた？", [draft.summaryJa]),
    slide(draft, 3, "hook_detail", "ここがポイント", [draft.keyPointJa]),
    slide(draft, 4, "hook_detail", draft.kind === "letter" ? "学ぶ順番で見る" : "現場で見るなら", [draft.builderAngleJa]),
    slide(draft, 5, "hook_detail", "気をつけたい点", [draft.caveatJa]),
  ];
}

function instagramDescription(draft: StoryDraft): string {
  const tags = draft.kind === "letter"
    ? "#AIニュース #生成AI #AI学習 #AIブリーフ"
    : "#AIニュース #生成AI #AI開発 #エンジニア #AIブリーフ";
  return [
    draft.hook,
    draft.summaryJa,
    draft.builderAngleJa,
    `Source: The Batch / DeepLearning.AI ${draft.sourceItem.url}`,
    tags,
  ].join("\n\n");
}

function carouselBriefForDraft(draft: StoryDraft): CarouselBrief {
  const slides = slidesForDraft(draft);
  return {
    id: briefId(draft),
    sourceInsightCardId: draft.sourceItemId,
    workingTitle: draft.titleJa,
    hook: draft.hook,
    hookStyle: draft.hookStyle,
    hookRiskLevel: "low",
    hookNeedsFactCheck: false,
    hookBestPlatform: "X",
    confidence: "medium-high",
    score: Math.max(0.01, Math.min(1, draft.score / 100)),
    suggestedFormat: "instagram_carousel",
    coverTemplate: draft.coverTemplate,
    coverStrategy: {
      label: draft.label,
      style: draft.style,
      why: draft.why,
      kicker: draft.kicker,
      swipe: draft.swipe,
    },
    sourceKind: draft.kind,
    slideCount: slides.length,
    slides,
    instagramDescription: instagramDescription(draft),
    evidenceSourceItemIds: [draft.sourceItemId],
    evidenceUrls: [draft.sourceItem.url],
  };
}

function issueNumber(options: ResearchGeneratorOptions): string | undefined {
  const url = normalizeWhitespace(options.theBatchIssueUrl);
  return url.match(/issue-(\d+)/)?.[1];
}

function issueTagUrlFromItems(items: SourceItem[]): string | undefined {
  for (const item of items) {
    const raw = record(item.raw);
    const url = normalizeWhitespace(raw.issueTagUrl);
    if (url) return url;
  }
  return undefined;
}

function candidateOutput(options: {
  generatedAt: string;
  issueTitle: string;
  issue?: string;
  issueUrl?: string;
  issueTagUrl?: string;
  drafts: StoryDraft[];
}): CoverCandidateOutput {
  return {
    issue: {
      title: options.issueTitle,
      issue: options.issue,
      generatedAt: options.generatedAt,
      issueUrl: options.issueUrl,
      issueTagUrl: options.issueTagUrl,
    },
    stories: options.drafts.map((draft) => ({
      id: draft.sourceItemId,
      kind: draft.kind,
      source_url: draft.sourceItem.url,
      feature_image: sourceImage(draft.sourceItem),
      title_en: draft.titleEn,
      title_ja: draft.titleJa,
      summary_ja: draft.summaryJa,
      key_point_ja: draft.keyPointJa,
      builder_angle_ja: draft.builderAngleJa,
      caveat_ja: draft.caveatJa,
    })),
    candidates: options.drafts.map((draft) => ({
      id: candidateId(draft),
      rank: draft.rank,
      story_id: draft.sourceItemId,
      story_kind: draft.kind,
      label: draft.label,
      style: draft.style,
      hook: draft.hook,
      hookStyle: draft.hookStyle,
      coverTemplate: draft.coverTemplate,
      kicker: draft.kicker,
      swipe: draft.swipe,
      why: draft.why,
      story: {
        id: draft.sourceItemId,
        source_url: draft.sourceItem.url,
        title_en: draft.titleEn,
        title_ja: draft.titleJa,
        summary_ja: draft.summaryJa,
        feature_image: sourceImage(draft.sourceItem),
      },
    })),
  };
}

async function readInputSourceItems(path: string): Promise<SourceItem[]> {
  const payload = await readJsonFile<{ items?: SourceItem[] } | SourceItem[]>(path, []);
  return Array.isArray(payload) ? payload : payload.items ?? [];
}

async function collectIssueItems(options: ResearchGeneratorOptions): Promise<SourceItem[]> {
  if (options.input) return readInputSourceItems(options.input);
  const issueTagUrl = await resolveIssueTagUrl(options.theBatchIssueUrl);
  return fetchTheBatchSourceItems({
    issueTagUrl,
    days: options.days,
    now: options.now,
    maxItems: options.maxItemsPerSource,
  });
}

function carouselOutput(generatedAt: string, drafts: StoryDraft[]): CarouselBriefOutput {
  return {
    generatedAt,
    audience: "ai_builders",
    sourceInsightGeneratedAt: generatedAt,
    carouselCount: drafts.length,
    carousels: drafts.map(carouselBriefForDraft),
  };
}

function runSlug(isoDate: string): string {
  return isoDate.replace(/[:.]/g, "-");
}

async function writeTextFile(path: string, text: string): Promise<void> {
  await mkdir(dirname(path), { recursive: true });
  await writeFile(path, text, "utf8");
}

function reportMarkdown(candidates: CoverCandidateOutput, carouselBriefs: CarouselBriefOutput): string {
  const lines = [
    "# The Batch Issue Carousel Briefs",
    "",
    `Generated: ${candidates.issue.generatedAt}`,
    `Stories: ${candidates.stories.length}`,
    `Carousel briefs: ${carouselBriefs.carouselCount}`,
    "",
    "## Ranked Hooks",
    "",
  ];
  for (const candidate of candidates.candidates) {
    lines.push(
      `- #${candidate.rank} ${candidate.hook}`,
      `  - template: ${candidate.coverTemplate}`,
      `  - story: ${record(candidate.story).title_ja}`,
    );
  }
  return `${lines.join("\n")}\n`;
}

export async function generateTheBatchIssuePackage(
  options: ResearchGeneratorOptions = {},
): Promise<{
  generatedAt: string;
  sourceItems: SourceItem[];
  coverCandidates: CoverCandidateOutput;
  carouselBriefs: CarouselBriefOutput;
  runDir?: string;
}> {
  const generatedAt = (options.now ?? new Date()).toISOString();
  const maxCards = Math.max(0, Math.floor(options.cards ?? 5));
  const sourceItems = dedupeResearchItems(await collectIssueItems(options))
    .filter((item) => item.source === "the_batch")
    .filter((item) => !/\/issue-\d+$/i.test(item.url));
  const localDrafts = sourceItems.map(localDraft);
  const enhanced = await maybeEnhanceWithGemini(localDrafts, options.provider ?? "local");
  const drafts = sortDrafts(enhanced).slice(0, maxCards || enhanced.length);
  const issueTitle = drafts.length
    ? `The Batch issue${issueNumber(options) ? ` ${issueNumber(options)}` : ""}: ${drafts.map((draft) => draft.titleJa).slice(0, 3).join(" / ")}`
    : "The Batch issue";
  const coverCandidates = candidateOutput({
    generatedAt,
    issueTitle,
    issue: issueNumber(options),
    issueUrl: options.theBatchIssueUrl,
    issueTagUrl: issueTagUrlFromItems(sourceItems),
    drafts,
  });
  const carouselBriefs = carouselOutput(generatedAt, drafts);

  await writeJsonFile(options.coverCandidatesOut ?? options.out ?? "out/research_idea_generator/the_batch/issue_cover_candidates.json", coverCandidates);
  await writeJsonFile(options.carouselOut ?? "out/research_idea_generator/the_batch/issue_carousel_briefs.json", carouselBriefs);
  if (options.sourceItemsOut) {
    await writeJsonFile(options.sourceItemsOut, {
      generatedAt,
      count: sourceItems.length,
      items: sourceItems,
    });
  }
  if (options.report) {
    await writeTextFile(options.report, reportMarkdown(coverCandidates, carouselBriefs));
  }

  let runDir: string | undefined;
  if (!options.noArchive) {
    runDir = `${(options.runsDir ?? "out/research_idea_generator/runs/the_batch_issue").replace(/\/$/, "")}/${runSlug(generatedAt)}`;
    await writeJsonFile(`${runDir}/cover_candidates.json`, coverCandidates);
    await writeJsonFile(`${runDir}/carousel_briefs.json`, carouselBriefs);
    await writeJsonFile(`${runDir}/source_items.json`, {
      generatedAt,
      count: sourceItems.length,
      items: sourceItems,
    });
    await writeTextFile(`${runDir}/report.md`, reportMarkdown(coverCandidates, carouselBriefs));
    await writeJsonFile(`${runDir}/manifest.json`, {
      generatedAt,
      files: ["cover_candidates.json", "carousel_briefs.json", "source_items.json", "report.md"],
      counts: {
        sourceItems: sourceItems.length,
        coverCandidates: coverCandidates.candidates.length,
        carouselBriefs: carouselBriefs.carouselCount,
        letters: drafts.filter((draft) => draft.kind === "letter").length,
      },
    });
  }

  return {
    generatedAt,
    sourceItems,
    coverCandidates,
    carouselBriefs,
    runDir,
  };
}
