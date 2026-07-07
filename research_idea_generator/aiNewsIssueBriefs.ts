import { dirname } from "node:path";
import { mkdir, writeFile } from "node:fs/promises";
import type { SourceItem } from "../sourcing/types.ts";
import { clamp, normalizeWhitespace, readJsonFile, sha256, writeJsonFile } from "../sourcing/utils.ts";
import { dedupeResearchItems } from "./sources/index.ts";
import { fetchAiNewsSourceItems } from "./sources/aiNews.ts";
import type {
  CarouselBrief,
  CarouselBriefOutput,
  CarouselBriefSlide,
  CarouselSlideImage,
  HookStyle,
  ResearchGeneratorOptions,
} from "./types.ts";

type CoverTemplateId = "stop-signal" | "pattern-break" | "metric-snap" | "split-switch" | "loom-reveal";
type StudyTemplateId =
  | "gpt_gate"
  | "gpt_typerain"
  | "fugu_call"
  | "fugu_router"
  | "ms_split"
  | "robo_enso"
  | "noise_filter"
  | "issue_wave";
type StoryKind = "twitter_recap" | "reddit_recap" | "discord_recap" | "issue_recap";
type ChecklistKey =
  | "underOneSecond"
  | "dominantFocalPoint"
  | "valueContrast"
  | "humanCue"
  | "curiosityGap"
  | "phoneReadable"
  | "eyeGuide"
  | "unexpected"
  | "payoff"
  | "delivers";

type HookChecklist = Record<ChecklistKey, number>;

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
  studyTemplate: StudyTemplateId;
  label: string;
  style: string;
  kicker: string;
  swipe: string;
  why: string;
  stakes: string;
  gap: string;
  checklist: HookChecklist;
  hookScore: number;
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
  stakes?: string;
  gap?: string;
  checklist?: Partial<HookChecklist>;
  hookScore?: number;
  rank?: number;
}

interface CoverCandidateOutput {
  issue: {
    title: string;
    generatedAt: string;
    issueUrl?: string;
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
const CHECKLIST_KEYS: ChecklistKey[] = [
  "underOneSecond",
  "dominantFocalPoint",
  "valueContrast",
  "humanCue",
  "curiosityGap",
  "phoneReadable",
  "eyeGuide",
  "unexpected",
  "payoff",
  "delivers",
];
const DEFAULT_USER_AGENT = "carousel-app-ai-news-issue-briefs/0.1";
const DEFAULT_GEMINI_TIMEOUT_MS = 90_000;
const DEFAULT_LLM_CANDIDATE_LIMIT = 8;

function envTimeoutMs(name: string, fallback: number): number {
  const value = Number(process.env[name]);
  return Number.isFinite(value) && value > 0 ? value : fallback;
}

function envPositiveInteger(name: string, fallback: number): number {
  const value = Number(process.env[name]);
  return Number.isFinite(value) && value > 0 ? Math.floor(value) : fallback;
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

function normalizeHookText(value: string): string {
  return normalizeWhitespace(value)
    .replace(/[\u3131-\u318e\uac00-\ud7af]+/g, "")
    .replace(/([0-9０-９]+)分([0-9０-９]+)(?=に|へ|まで|以下|削減|低下|$)/g, "$1分の$2");
}

function fitJapaneseHook(value: string): string {
  const text = normalizeHookText(value).replace(/[。！!、：:]+$/u, "");
  if (visibleJapaneseChars(text) <= 25) return text;
  return Array.from(text).slice(0, 25).join("").replace(/[。！!、：:]+$/u, "");
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? value as Record<string, unknown> : {};
}

function rawStory(item: SourceItem): Record<string, unknown> {
  return record(record(item.raw).fullStory);
}

function rawSourceLinks(item: SourceItem): Array<{ text?: string; url?: string }> {
  const links = rawStory(item).sourceLinks;
  return Array.isArray(links)
    ? links.map((entry) => record(entry)).map((entry) => ({
      text: normalizeWhitespace(entry.text),
      url: normalizeWhitespace(entry.url),
    }))
    : [];
}

function weakEvidenceUrl(value: string): boolean {
  const url = normalizeWhitespace(value).toLowerCase();
  if (!url) return true;
  return /(?:^|\/\/)(?:pastebin\.com\/raw|rentry\.co)/.test(url);
}

function strongEvidenceUrls(item: SourceItem): string[] {
  const urls = [
    item.url,
    ...rawSourceLinks(item).map((link) => link.url ?? ""),
  ].map(normalizeWhitespace).filter(Boolean);
  return urls.filter((url) => !weakEvidenceUrl(url));
}

function hasPublishableEvidence(item: SourceItem): boolean {
  return strongEvidenceUrls(item).length > 0;
}

function primaryEvidenceUrl(item: SourceItem): string {
  return strongEvidenceUrls(item)[0] ?? item.url;
}

function sourceSection(item: SourceItem): string {
  return normalizeWhitespace(record(item.raw).section);
}

function sourceTopic(item: SourceItem): string {
  return normalizeWhitespace(record(item.raw).topic);
}

function articleText(item: SourceItem): string {
  return stringValue(rawStory(item).articleText) || item.body || "";
}

function englishSummary(item: SourceItem): string {
  return stringValue(rawStory(item).summary) || item.body || item.title;
}

function storyKind(item: SourceItem): StoryKind {
  const rawKind = normalizeWhitespace(record(item.raw).storyKind);
  if (rawKind === "twitter_recap" || rawKind === "reddit_recap" || rawKind === "discord_recap") return rawKind;
  const section = sourceSection(item).toLowerCase();
  if (/twitter/.test(section)) return "twitter_recap";
  if (/reddit/.test(section)) return "reddit_recap";
  if (/discord/.test(section)) return "discord_recap";
  return "issue_recap";
}

function storyMetaKey(item: SourceItem): string {
  return `${item.title} ${sourceSection(item)} ${sourceTopic(item)} ${englishSummary(item)} ${articleText(item)}`.toLowerCase();
}

function subjectToken(item: SourceItem): string {
  const text = `${item.title} ${articleText(item)}`;
  const candidates = [
    ...text.matchAll(/\b(?:GPT-\d+(?:\.\d+)*|Claude(?:\s+Code)?|Gemini|GLM-\d+(?:\.\d+)*|Qwen\d*(?:\.\d+)?|Kimi|DeepSeek|vLLM|LangSmith|LangChain|LlamaIndex|OpenWiki|Cursor|Devin|Codex|OpenAI|Anthropic|Google|Microsoft|Meta|NVIDIA|Hugging Face|Cloudflare|Sakana|Z\.ai)\b/g),
  ].map((match) => match[0]);
  if (candidates[0]) return candidates[0];
  return normalizeWhitespace(item.title).split(/\s+/).slice(0, 4).join(" ");
}

function localTitleJa(item: SourceItem): string {
  const text = storyMetaKey(item);
  if (/coordination|observability|routing|memory|workflow|developer|agent|coding|devin|cursor|codex/.test(text)) return "AI開発ワークフローの変化";
  if (/speculative decoding|prefill|throughput|inference|tok\/s|token\/s/.test(text)) return "AI推論高速化の論点";
  if (/open-model economics|open.?model|open weights|open-weight/.test(text)) return "オープンAIの経済性";
  if (/cost|price|pricing|token|cheap|cheaper/.test(text)) return "AIコスト設計の変化";
  if (/benchmark|eval|leaderboard|score|arena/.test(text)) return "AI評価の見方が変わる";
  if (/security|risk|trust|permission|access/.test(text)) return "AI運用リスクの論点";
  return `${subjectToken(item)}の論点`;
}

function localHook(item: SourceItem): string {
  const text = storyMetaKey(item);
  if (/coordination|observability|routing|memory|workflow|developer|agent|coding|devin|cursor|codex/.test(text)) return "AI開発、調整で詰まる？";
  if (/speculative decoding|prefill|throughput|inference|tok\/s|token\/s/.test(text)) return "推論高速化、何が効く？";
  if (/open-model economics|open.?model|open weights|open-weight/.test(text)) return "オープンAI、安さだけ？";
  if (/cost|price|pricing|token|cheap|cheaper/.test(text)) return "AIコスト、どこで差がつく？";
  if (/benchmark|eval|leaderboard|score|arena/.test(text)) return "そのAI評価、実戦向き？";
  if (/security|risk|trust|permission|access/.test(text)) return "AI運用の穴、見えた？";
  return fitJapaneseHook(`${subjectToken(item)}、何が変わる？`);
}

function localCoverTemplate(item: SourceItem, hook: string): CoverTemplateId {
  const text = `${storyMetaKey(item)} ${hook}`.toLowerCase();
  if (/risk|wrong|trust|security|permission|hole|穴|not |avoid/.test(text)) return "stop-signal";
  if (/cost|price|pricing|token|benchmark|score|%|x|倍|差/.test(text)) return "metric-snap";
  if (/versus|vs\.?|instead|rather|from .+ to |shift|代わる|比較/.test(text)) return "split-switch";
  if (/\b\d+\b|top tweets|three|3|4|5/.test(text)) return "pattern-break";
  return "loom-reveal";
}

function localHookStyle(item: SourceItem): HookStyle {
  const text = storyMetaKey(item);
  if (/\b\d+\b|top tweets|three|3|4|5/.test(text)) return "list";
  if (/wrong|risk|not |instead|avoid|trust|security/.test(text)) return "contrarian";
  return "curiosity";
}

function localSummaryJa(item: SourceItem): string {
  const subject = subjectToken(item);
  return compact(`${subject}について、AINewsが複数のコミュニティ投稿から動きを拾っています。${englishSummary(item)}`, 110);
}

function localKeyPointJa(item: SourceItem): string {
  const text = storyMetaKey(item);
  if (/cost|price|pricing|token/.test(text)) return "重要なのは性能だけでなく、どの作業に高いトークンを使うかという配分です。";
  if (/benchmark|eval|leaderboard|score/.test(text)) return "ランキングの数字より、評価が現場の作業をどこまで再現しているかが焦点です。";
  if (/agent|workflow|coding|developer/.test(text)) return "モデル単体より、周辺の観測、ルーティング、記憶、レビューの設計が効いてきます。";
  if (/security|risk|trust|permission/.test(text)) return "AIツールが仕事に深く入るほど、権限と責任範囲の設計がボトルネックになります。";
  return "見出しよりも、現場で何が変わるかに分解して見る価値があります。";
}

function localBuilderAngleJa(item: SourceItem): string {
  const text = storyMetaKey(item);
  if (/agent|workflow|coding|developer/.test(text)) return "自分の開発フローでは、モデル選びより先に失敗検知とやり直し方を決めるのが実用的です。";
  if (/cost|price|pricing|token/.test(text)) return "高性能モデルを全部に使うのではなく、安いモデル、キャッシュ、ルーティングを組み合わせたい話です。";
  if (/benchmark|eval|leaderboard|score/.test(text)) return "評価を見る時は、タスク、制約、コスト、再現性をセットで確認したいところです。";
  return "ニュースをそのまま追うより、自分のチームで試すなら何が変わるかに落とすと使えます。";
}

function localCaveatJa(item: SourceItem): string {
  const links = rawSourceLinks(item);
  if (links.length) return "AINewsは複数投稿の要約なので、採用前に一次リンクと公式情報で条件を確認したいところです。";
  return "コミュニティ由来のまとめなので、数字や提供条件は一次情報で確認してから判断です。";
}

function localStakes(item: SourceItem): string {
  const text = storyMetaKey(item);
  if (/coordination|observability|routing|memory|workflow|coding|developer|agent/.test(text)) return "become better";
  if (/benchmark|eval|leaderboard|score/.test(text)) return "look smarter";
  if (/cost|price|pricing|token|cheap/.test(text)) return "save money";
  if (/security|risk|trust/.test(text)) return "avoid embarrassment";
  return "avoid missing out";
}

function localGap(item: SourceItem): string {
  const text = storyMetaKey(item);
  if (/coordination|observability|routing|memory|workflow|coding|developer|agent/.test(text)) return "How did they do that?";
  if (/benchmark|eval|leaderboard|score/.test(text)) return "Which option wins?";
  if (/cost|price|pricing|token/.test(text)) return "Where is the hidden cost?";
  if (/wrong|risk|security|trust/.test(text)) return "What mistake am I making?";
  return "What changed?";
}

function localChecklist(hook: string, item: SourceItem): HookChecklist {
  const readable = visibleJapaneseChars(hook) <= 25 ? 4 : 2;
  const hasQuestion = /[？?]/.test(hook);
  const text = storyMetaKey(item);
  const hasHumanCue = /developer|builder|team|user|community|reddit|twitter|engineer|開発|運用/.test(text) ? 4 : 3;
  const unexpected = /wrong|hidden|surprising|unexpected|穴|詰まり|差/.test(`${text} ${hook}`) ? 4 : 3;
  return {
    underOneSecond: 4,
    dominantFocalPoint: subjectToken(item) ? 4 : 3,
    valueContrast: /cost|price|benchmark|score|risk|vs|差/.test(text) ? 4 : 3,
    humanCue: hasHumanCue,
    curiosityGap: hasQuestion ? 4 : 3,
    phoneReadable: readable,
    eyeGuide: 4,
    unexpected,
    payoff: 4,
    delivers: 4,
  };
}

function checklistAverage(checklist: HookChecklist): number {
  const total = CHECKLIST_KEYS.reduce((sum, key) => sum + checklist[key], 0);
  return total / CHECKLIST_KEYS.length;
}

function checklistMinimum(checklist: HookChecklist): number {
  return Math.min(...CHECKLIST_KEYS.map((key) => checklist[key]));
}

function labelFor(item: SourceItem): string {
  const kind = storyKind(item);
  if (kind === "twitter_recap") return "AI Twitter signal";
  if (kind === "reddit_recap") return "AI Reddit proof";
  if (kind === "discord_recap") return "AI Discord pulse";
  return "AINews signal";
}

function visualStyleFor(template: CoverTemplateId): string {
  if (template === "stop-signal") return "warning gate + social tension";
  if (template === "split-switch") return "before-after split-switch";
  if (template === "metric-snap") return "metric snap + cost contrast";
  if (template === "pattern-break") return "pattern-break grid with one odd tile";
  return "loom reveal with one focal object";
}

function localStudyTemplate(item: SourceItem, hook: string, coverTemplate: CoverTemplateId): StudyTemplateId {
  const text = `${storyMetaKey(item)} ${hook}`.toLowerCase();
  if (/noise|hype|rumor|panic|wrong|mistake|trust|routing|fallback|degrad|劣化|噂|誤解|真犯人|ルーティング|フォールバック/.test(text)) {
    return "noise_filter";
  }
  if (/router|routing|orchestration|model selection|使い分け|ルーティング|オーケスト/.test(text)) return "fugu_router";
  if (/microsoft|partner|自前|own model|split|vs\.?|versus|比較/.test(text)) return "ms_split";
  if (/robot|reward|rl|強化学習|報酬/.test(text)) return "robo_enso";
  if (/access|limited|gate|restricted|制限|届かない|非公開/.test(text)) return "gpt_gate";
  if (coverTemplate === "pattern-break") return "fugu_call";
  if (coverTemplate === "metric-snap") return "gpt_typerain";
  if (coverTemplate === "split-switch") return "ms_split";
  if (coverTemplate === "stop-signal") return "gpt_gate";
  return "issue_wave";
}

function sourceImage(item: SourceItem): string {
  return item.media.imageUrl || item.media.localPath || "";
}

function localDraft(item: SourceItem, index: number): StoryDraft {
  const kind = storyKind(item);
  const hook = localHook(item);
  const coverTemplate = localCoverTemplate(item, hook);
  const studyTemplate = localStudyTemplate(item, hook, coverTemplate);
  const checklist = localChecklist(hook, item);
  const score = Math.max(1, Number(item.metrics.score ?? item.metrics.upvotes ?? 50));
  return {
    sourceItem: item,
    sourceItemId: item.id,
    kind,
    titleEn: item.title,
    titleJa: localTitleJa(item),
    summaryJa: localSummaryJa(item),
    keyPointJa: localKeyPointJa(item),
    builderAngleJa: localBuilderAngleJa(item),
    caveatJa: localCaveatJa(item),
    hook,
    hookStyle: localHookStyle(item),
    coverTemplate,
    studyTemplate,
    label: labelFor(item),
    style: visualStyleFor(coverTemplate),
    kicker: "AINEWS",
    swipe: "要点を見る",
    why: `${hook} creates a short curiosity gap tied to ${localStakes(item)} and maps to ${coverTemplate}.`,
    stakes: localStakes(item),
    gap: localGap(item),
    checklist,
    hookScore: checklistAverage(checklist),
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

function normalizeChecklist(value: unknown, fallback: HookChecklist): HookChecklist {
  const raw = record(value);
  return CHECKLIST_KEYS.reduce((checklist, key) => {
    const numeric = Number(raw[key]);
    checklist[key] = Math.round(clamp(Number.isFinite(numeric) ? numeric : fallback[key], 1, 5));
    return checklist;
  }, {} as HookChecklist);
}

function applyGeminiDrafts(localDrafts: StoryDraft[], geminiDrafts: GeminiStoryDraft[]): StoryDraft[] {
  const byId = new Map(geminiDrafts.map((draft) => [normalizeWhitespace(draft.sourceItemId), draft]));
  return localDrafts.map((draft) => {
    const gemini = byId.get(draft.sourceItemId);
    if (!gemini) return draft;
    const hook = fitJapaneseHook(normalizeWhitespace(gemini.hook) || draft.hook);
    const coverTemplate = normalizeTemplate(gemini.coverTemplate, draft.coverTemplate);
    const checklist = normalizeChecklist(gemini.checklist, draft.checklist);
    return {
      ...draft,
      titleJa: normalizeWhitespace(gemini.titleJa) || draft.titleJa,
      summaryJa: normalizeWhitespace(gemini.summaryJa) || draft.summaryJa,
      keyPointJa: normalizeWhitespace(gemini.keyPointJa) || draft.keyPointJa,
      builderAngleJa: normalizeWhitespace(gemini.builderAngleJa) || draft.builderAngleJa,
      caveatJa: normalizeWhitespace(gemini.caveatJa) || draft.caveatJa,
      hook,
      hookStyle: normalizeHookStyle(gemini.hookStyle, draft.hookStyle),
      coverTemplate,
      studyTemplate: localStudyTemplate(draft.sourceItem, hook, coverTemplate),
      label: normalizeWhitespace(gemini.label) || draft.label,
      style: normalizeWhitespace(gemini.style) || visualStyleFor(coverTemplate),
      kicker: normalizeWhitespace(gemini.kicker) || draft.kicker,
      swipe: normalizeWhitespace(gemini.swipe) || draft.swipe,
      why: normalizeWhitespace(gemini.why) || draft.why,
      stakes: normalizeWhitespace(gemini.stakes) || draft.stakes,
      gap: normalizeWhitespace(gemini.gap) || draft.gap,
      checklist,
      hookScore: Number.isFinite(gemini.hookScore) ? Number(gemini.hookScore) : checklistAverage(checklist),
      rank: Number.isFinite(gemini.rank) ? Number(gemini.rank) : draft.rank,
    };
  });
}

async function geminiIssueDrafts(localDrafts: StoryDraft[]): Promise<GeminiStoryDraft[] | undefined> {
  const apiKey = process.env.GEMINI_API_KEY || process.env.GOOGLE_API_KEY;
  if (!apiKey) throw new Error("AINews hook generation requires GEMINI_API_KEY or GOOGLE_API_KEY.");
  const model = process.env.GEMINI_TEXT_MODEL || "gemini-3.5-flash";
  const apiVersion = process.env.GEMINI_TEXT_API_VERSION || "v1beta";
  const prompt = [
    "You are creating Japanese Instagram carousel title-page hooks for AI Brief JP from AINews by smol.ai.",
    "Audience: Japanese AI builders, AI-curious operators, and people who want to look smart fast without hype.",
    "Voice: restrained working engineer, clear, concrete, no emoji, no markdown, no bracket markup.",
    "Use only the facts in the source items. Do not invent numbers, availability, capabilities, timelines, or conclusions.",
    "Preserve exact model, company, product, benchmark, repo, and metric names in their standard spelling.",
    "For each source item, return one candidate. Include every sourceItemId exactly once.",
    "The hook is the title page headline: standalone, no subtitle, no CTA, no swipe/navigation copy.",
    "Japanese hook limit: <=25 visible characters. Make it readable at phone size.",
    "No stakes means no stop. Choose one stakes phrase such as save money, avoid embarrassment, become better, learn faster, look smarter, see something shocking, witness transformation, confirm identity, join trend, avoid missing out.",
    "Choose one gap phrase such as What happened next?, How did they do that?, Why is this wrong?, Which option wins?, What is hidden?, What is the result?, What mistake am I making?",
    "Score the title-page hook checklist from 1 to 5: underOneSecond, dominantFocalPoint, valueContrast, humanCue, curiosityGap, phoneReadable, eyeGuide, unexpected, payoff, delivers.",
    "Every returned candidate must score 4 or 5 on every checklist field. If any field would score 3 or lower, rewrite the hook and cover strategy before returning it.",
    "Make the hook or label carry a human, emotional, or social cue when the source supports it: developers, users, teams, community reaction, SNS panic, public proof, regret, trust, or status.",
    "Make the content deliver on the hook: summaryJa, keyPointJa, builderAngleJa, and caveatJa must answer the curiosity gap.",
    "Pick coverTemplate from exactly: stop-signal, pattern-break, metric-snap, split-switch, loom-reveal.",
    "Template fit: list -> pattern-break; metrics/cost/benchmark -> metric-snap; warning/anti-default -> stop-signal; comparison/shift -> split-switch; launch/tool/reveal -> loom-reveal.",
    "Return JSON only: {\"candidates\":[{sourceItemId,titleJa,summaryJa,keyPointJa,builderAngleJa,caveatJa,hook,hookStyle,coverTemplate,label,style,kicker,swipe,why,stakes,gap,checklist:{underOneSecond,dominantFocalPoint,valueContrast,humanCue,curiosityGap,phoneReadable,eyeGuide,unexpected,payoff,delivers},hookScore,rank}]}",
    JSON.stringify({
      localDrafts: localDrafts.map((draft) => ({
        sourceItemId: draft.sourceItemId,
        kind: draft.kind,
        titleEn: draft.titleEn,
        sourceUrl: draft.sourceItem.url,
        section: sourceSection(draft.sourceItem),
        topic: sourceTopic(draft.sourceItem),
        summaryEn: compact(englishSummary(draft.sourceItem), 450),
        articleExcerpt: compact(articleText(draft.sourceItem), 700),
        sourceLinks: rawSourceLinks(draft.sourceItem).slice(0, 5),
        localHook: draft.hook,
        localStakes: draft.stakes,
        localGap: draft.gap,
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
        "User-Agent": DEFAULT_USER_AGENT,
      },
      body: JSON.stringify({
        contents: [{ role: "user", parts: [{ text: prompt }] }],
        generationConfig: { temperature: 0.25, responseMimeType: "application/json" },
      }),
    },
    envTimeoutMs("GEMINI_TEXT_TIMEOUT_MS", DEFAULT_GEMINI_TIMEOUT_MS),
    "Gemini AINews issue generation",
  );
  if (!response.ok) throw new Error(`Gemini AINews issue generation returned HTTP ${response.status}: ${await response.text()}`);
  const data = await response.json() as { candidates?: Array<{ content?: { parts?: Array<{ text?: string }> } }> };
  const text = data.candidates?.[0]?.content?.parts?.map((part) => part.text ?? "").join("") ?? "";
  const parsed = JSON.parse(text) as { candidates?: GeminiStoryDraft[] };
  return Array.isArray(parsed.candidates) ? parsed.candidates : undefined;
}

async function maybeEnhanceWithGemini(drafts: StoryDraft[], provider: string): Promise<StoryDraft[]> {
  if (provider !== "gemini") return drafts;
  const geminiDrafts = await geminiIssueDrafts(drafts);
  if (!geminiDrafts?.length) throw new Error("AINews Gemini hook generation returned no candidates.");
  return applyGeminiDrafts(drafts, geminiDrafts);
}

function sortDrafts(drafts: StoryDraft[]): StoryDraft[] {
  function editorialPriority(draft: StoryDraft): number {
    const text = `${draft.hook} ${draft.titleJa} ${draft.titleEn} ${draft.stakes}`.toLowerCase();
    if (/cost|price|pricing|token|コスト|money/.test(text)) return 50;
    if (/agent|coding|workflow|developer|開発/.test(text)) return 45;
    if (/benchmark|eval|score|評価|look smarter/.test(text)) return 40;
    if (/security|risk|trust|穴|avoid embarrassment/.test(text)) return 38;
    if (draft.kind === "reddit_recap") return 30;
    return 20;
  }
  return [...drafts]
    .sort((a, b) =>
      checklistMinimum(b.checklist) - checklistMinimum(a.checklist)
      || b.hookScore - a.hookScore
      || editorialPriority(b) - editorialPriority(a)
      || b.score - a.score
      || a.titleEn.localeCompare(b.titleEn)
    )
    .map((draft, index) => ({ ...draft, rank: index + 1 }));
}

function selectDraftsForHookGeneration(drafts: StoryDraft[], maxCards: number): StoryDraft[] {
  const requestedCards = Math.max(1, maxCards || drafts.length);
  const defaultLimit = Math.max(requestedCards, Math.min(DEFAULT_LLM_CANDIDATE_LIMIT, requestedCards * 3));
  const limit = envPositiveInteger("AI_NEWS_LLM_CANDIDATE_LIMIT", defaultLimit);
  return sortDrafts(drafts).slice(0, Math.min(drafts.length, limit));
}

function alternateHooks(draft: StoryDraft): string[] {
  const text = storyMetaKey(draft.sourceItem);
  const subject = subjectToken(draft.sourceItem);
  const hooks = [
    draft.hook,
    localHook(draft.sourceItem),
  ];
  if (/coordination|observability|routing|memory|workflow|coding|developer|agent/.test(text)) {
    hooks.push("AI開発、調整で詰まる？", "AI開発、見えないと詰む？");
  }
  if (/speculative decoding|prefill|throughput|inference|tok\/s|token\/s/.test(text)) {
    hooks.push("推論高速化、何が効く？", "速いAI、裏側は何？");
  }
  if (/open-model economics|open.?model|open weights|open-weight/.test(text)) {
    hooks.push("オープンAI、安さだけ？", "AIモデル選び、何が得？");
  }
  if (/cost|price|pricing|token|cheap/.test(text)) {
    hooks.push("AIコスト、どこで差がつく？", "安いAI、どこで効く？");
  }
  if (/benchmark|eval|leaderboard|score|arena/.test(text)) {
    hooks.push("そのAI評価、実戦向き？", "AI評価、何を見落とす？");
  }
  hooks.push(
    fitJapaneseHook(`${subject}、何が違う？`),
    fitJapaneseHook(`${draft.titleJa}、何を見る？`),
  );
  return hooks.map(fitJapaneseHook).filter(Boolean);
}

function ensureUniqueHooks(drafts: StoryDraft[]): StoryDraft[] {
  const seen = new Set<string>();
  return drafts.map((draft) => {
    const selected = alternateHooks(draft).find((hook) => !seen.has(hook.toLowerCase())) ?? draft.hook;
    seen.add(selected.toLowerCase());
    if (selected === draft.hook) return draft;
    const coverTemplate = localCoverTemplate(draft.sourceItem, selected);
    const studyTemplate = localStudyTemplate(draft.sourceItem, selected, coverTemplate);
    return {
      ...draft,
      hook: selected,
      coverTemplate,
      studyTemplate,
      style: visualStyleFor(coverTemplate),
      why: `${selected} keeps the title page unique while preserving the ${draft.stakes} stakes and ${draft.gap} gap.`,
    };
  });
}

function candidateId(draft: StoryDraft): string {
  return sha256(`ai-news-candidate:${draft.sourceItemId}:${draft.hook}:${draft.coverTemplate}`).slice(0, 16);
}

function briefId(draft: StoryDraft): string {
  return sha256(`ai-news-issue-brief:${draft.sourceItemId}:${draft.hook}:${draft.coverTemplate}`).slice(0, 16);
}

function slideImage(
  draft: StoryDraft,
  role: CarouselSlideImage["role"],
  headline: string,
): CarouselSlideImage {
  const imageUrl = sourceImage(draft.sourceItem);
  return {
    kind: "generated_prompt",
    role,
    altText: compact(`${headline} generated visual plan for ${draft.titleJa}.`, 160),
    rationale: "AINews has no stable story image; generate a high-contrast editorial visual anchored to one subject and one human/social cue.",
    promptBase: [
      `Editorial AI news visual for ${draft.titleEn}.`,
      `Slide focus: ${headline}.`,
      `Hook stakes: ${draft.stakes}. Curiosity gap: ${draft.gap}.`,
      "One dominant focal point, strong value contrast, subtle human or social cue, clean negative space for Japanese carousel typography.",
      "No embedded text, no logos, no fake UI labels, no charts with readable marks.",
    ].join(" "),
    sourceImageUrls: imageUrl ? [imageUrl] : [],
    sourcePageUrls: [primaryEvidenceUrl(draft.sourceItem)],
    sourceItemIds: [draft.sourceItemId],
    sourceNames: [draft.sourceItem.source],
    sourceTitles: [draft.titleEn],
  };
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
    image: slideImage(draft, slideNumber === 1 ? "cover" : "supporting", headline),
    sourceUrls: slideNumber === 1 ? undefined : [primaryEvidenceUrl(draft.sourceItem)],
    sourceItemIds: slideNumber === 1 ? undefined : [draft.sourceItemId],
  };
}

function slidesForDraft(draft: StoryDraft): CarouselBriefSlide[] {
  return [
    slide(draft, 1, "cover", draft.hook, []),
    slide(draft, 2, "hook_detail", "何が起きた？", [draft.summaryJa]),
    slide(draft, 3, "hook_detail", "なぜ重要？", [draft.keyPointJa]),
    slide(draft, 4, "hook_detail", "現場で見るなら", [draft.builderAngleJa]),
    slide(draft, 5, "hook_detail", "落とし穴", [draft.caveatJa]),
  ];
}

function instagramDescription(draft: StoryDraft): string {
  const sourceLinks = rawSourceLinks(draft.sourceItem)
    .slice(0, 3)
    .map((link) => `- ${link.text}: ${link.url}`);
  return [
    draft.hook,
    draft.summaryJa,
    draft.builderAngleJa,
    `Source: AINews by smol.ai ${primaryEvidenceUrl(draft.sourceItem)}`,
    sourceLinks.length ? `Primary links:\n${sourceLinks.join("\n")}` : "",
    "#AIニュース #生成AI #AI開発 #AIブリーフ",
  ].filter(Boolean).join("\n\n");
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
    confidence: draft.hookScore >= 4 ? "medium-high" : "medium",
    score: Math.max(0.01, Math.min(1, Math.max(draft.score / 100, draft.hookScore / 5))),
    suggestedFormat: "instagram_carousel",
    coverTemplate: draft.coverTemplate,
    studyTemplate: draft.studyTemplate,
    coverStrategy: {
      label: draft.label,
      style: draft.style,
      why: draft.why,
      kicker: draft.kicker,
      swipe: draft.swipe,
      stakes: draft.stakes,
      gap: draft.gap,
      checklist: draft.checklist,
      hookScore: draft.hookScore,
      studyTemplate: draft.studyTemplate,
    },
    sourceKind: draft.kind,
    slideCount: slides.length,
    slides,
    instagramDescription: instagramDescription(draft),
    evidenceSourceItemIds: [draft.sourceItemId],
    evidenceUrls: [primaryEvidenceUrl(draft.sourceItem)],
  };
}

function issueUrlFromItems(items: SourceItem[]): string | undefined {
  for (const item of items) {
    const url = normalizeWhitespace(record(item.raw).issueUrl);
    if (url) return url;
  }
  return undefined;
}

function issueTitleFromItems(items: SourceItem[]): string | undefined {
  for (const item of items) {
    const title = normalizeWhitespace(record(item.raw).issueTitle);
    if (title) return title;
  }
  return undefined;
}

function candidateOutput(options: {
  generatedAt: string;
  issueTitle: string;
  issueUrl?: string;
  drafts: StoryDraft[];
}): CoverCandidateOutput {
  return {
    issue: {
      title: options.issueTitle,
      generatedAt: options.generatedAt,
      issueUrl: options.issueUrl,
    },
    stories: options.drafts.map((draft) => ({
      id: draft.sourceItemId,
      kind: draft.kind,
      source_url: draft.sourceItem.url,
      title_en: draft.titleEn,
      title_ja: draft.titleJa,
      summary_ja: draft.summaryJa,
      key_point_ja: draft.keyPointJa,
      builder_angle_ja: draft.builderAngleJa,
      caveat_ja: draft.caveatJa,
      section: sourceSection(draft.sourceItem),
      topic: sourceTopic(draft.sourceItem),
      source_links: rawSourceLinks(draft.sourceItem),
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
      studyTemplate: draft.studyTemplate,
      kicker: draft.kicker,
      swipe: draft.swipe,
      why: draft.why,
      stakes: draft.stakes,
      gap: draft.gap,
      checklist: draft.checklist,
      hookScore: draft.hookScore,
      story: {
        id: draft.sourceItemId,
        source_url: draft.sourceItem.url,
        title_en: draft.titleEn,
        title_ja: draft.titleJa,
        summary_ja: draft.summaryJa,
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
  return fetchAiNewsSourceItems({
    issueUrl: options.aiNewsIssueUrl,
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
    "# AINews Issue Carousel Briefs",
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
      `  - studyTemplate: ${candidate.studyTemplate}`,
      `  - stakes: ${candidate.stakes}`,
      `  - gap: ${candidate.gap}`,
      `  - hookScore: ${candidate.hookScore}`,
      `  - story: ${record(candidate.story).title_ja}`,
    );
  }
  return `${lines.join("\n")}\n`;
}

export async function generateAiNewsIssuePackage(
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
    .filter((item) => item.source === "ai_news");
  const localDrafts = sourceItems.filter(hasPublishableEvidence).map(localDraft);
  const candidateDrafts = selectDraftsForHookGeneration(localDrafts, maxCards);
  const enhanced = await maybeEnhanceWithGemini(candidateDrafts, options.provider ?? "gemini");
  const drafts = ensureUniqueHooks(sortDrafts(enhanced)).slice(0, maxCards || enhanced.length);
  const issueTitle = issueTitleFromItems(sourceItems) || (
    drafts.length
      ? `AINews: ${drafts.map((draft) => draft.titleJa).slice(0, 3).join(" / ")}`
      : "AINews issue"
  );
  const issueUrl = options.aiNewsIssueUrl || issueUrlFromItems(sourceItems);
  const coverCandidates = candidateOutput({
    generatedAt,
    issueTitle,
    issueUrl,
    drafts,
  });
  const carouselBriefs = carouselOutput(generatedAt, drafts);

  await writeJsonFile(options.coverCandidatesOut ?? options.out ?? "out/research_idea_generator/ai_news/issue_cover_candidates.json", coverCandidates);
  await writeJsonFile(options.carouselOut ?? "out/research_idea_generator/ai_news/issue_carousel_briefs.json", carouselBriefs);
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
    runDir = `${(options.runsDir ?? "out/research_idea_generator/runs/ai_news_issue").replace(/\/$/, "")}/${runSlug(generatedAt)}`;
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
        twitterRecaps: drafts.filter((draft) => draft.kind === "twitter_recap").length,
        redditRecaps: drafts.filter((draft) => draft.kind === "reddit_recap").length,
        discordRecaps: drafts.filter((draft) => draft.kind === "discord_recap").length,
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
