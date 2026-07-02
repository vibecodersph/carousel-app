import type { SourceItem } from "../sourcing/types.ts";
import { clamp, compactText, normalizeWhitespace } from "../sourcing/utils.ts";
import type { HookJudgment } from "./types.ts";

const JUDGE_BATCH_SIZE = 10;
const DEFAULT_THRESHOLD = 0.55;

export interface HookJudgeResult {
  kept: SourceItem[];
  dropped: Array<{ item: SourceItem; judgment: HookJudgment }>;
  judgments: HookJudgment[];
}

function withJudgment(item: SourceItem, judgment: HookJudgment): SourceItem {
  const baseRaw = item.raw && typeof item.raw === "object" ? item.raw as Record<string, unknown> : {};
  return { ...item, raw: { ...baseRaw, hookJudgment: judgment } };
}

function judgePrompt(items: SourceItem[]): string {
  const stories = items.map((item, index) => ({
    index,
    title: item.title,
    excerpt: compactText(normalizeWhitespace(item.body), 900),
  }));
  return [
    "You are a content strategist for an Instagram page aimed at AI builders.",
    "Judge each story below for carousel hook potential: can it support a compelling, evidence-grounded hook",
    "(curiosity gap, contrarian angle, or concrete list) that is more nuanced than plain news repetition?",
    "Score 0 to 1. Scores >= 0.55 mean worth turning into a carousel.",
    "Favor stories with concrete numbers, tension or stakes, practical builder relevance, or a surprising insight.",
    "Penalize incremental funding/corporate news, vague announcements, and stories needing heavy context to land.",
    'Return strict JSON: an array of objects {"index": number, "score": number, "worthCarousel": boolean, "reason": string, "bestAngle": string}.',
    "Include every index exactly once.",
    JSON.stringify(stories),
  ].join("\n");
}

function geminiApiKey(): string {
  const apiKey = process.env.GEMINI_API_KEY || process.env.GOOGLE_API_KEY;
  if (!apiKey) {
    throw new Error("LLM hook judge requires GEMINI_API_KEY or GOOGLE_API_KEY.");
  }
  return apiKey;
}

async function judgeBatchWithGemini(items: SourceItem[]): Promise<HookJudgment[]> {
  const apiKey = geminiApiKey();
  const model = process.env.GEMINI_TEXT_MODEL || "gemini-3.5-flash";
  const apiVersion = process.env.GEMINI_TEXT_API_VERSION || "v1beta";
  const response = await fetch(`https://generativelanguage.googleapis.com/${apiVersion}/models/${model}:generateContent`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "x-goog-api-key": apiKey,
    },
    body: JSON.stringify({
      contents: [{ role: "user", parts: [{ text: judgePrompt(items) }] }],
      generationConfig: { temperature: 0.1, responseMimeType: "application/json" },
    }),
  });
  if (!response.ok) throw new Error(`Gemini hook judge returned HTTP ${response.status}: ${await response.text()}`);
  const data = await response.json() as { candidates?: Array<{ content?: { parts?: Array<{ text?: string }> } }> };
  const text = data.candidates?.[0]?.content?.parts?.map((part) => part.text ?? "").join("") ?? "";
  const parsed = JSON.parse(text) as Array<{ index?: number; score?: number; worthCarousel?: boolean; reason?: string; bestAngle?: string }>;
  if (!Array.isArray(parsed)) throw new Error("Gemini hook judge did not return a JSON array.");
  const byIndex = new Map<number, HookJudgment>();
  for (const entry of parsed) {
    if (typeof entry?.index !== "number" || typeof entry.score !== "number") continue;
    byIndex.set(entry.index, {
      score: Number(clamp(entry.score).toFixed(4)),
      worthCarousel: entry.worthCarousel ?? entry.score >= DEFAULT_THRESHOLD,
      reason: normalizeWhitespace(entry.reason ?? "") || "No reason given.",
      bestAngle: normalizeWhitespace(entry.bestAngle ?? "") || undefined,
      judgedBy: "gemini",
    });
  }
  return items.map((_item, index) => {
    const judgment = byIndex.get(index);
    if (!judgment) throw new Error(`Gemini hook judge response omitted story index ${index}.`);
    return judgment;
  });
}

export async function judgeHookWorthiness(
  items: SourceItem[],
  options: { threshold?: number } = {},
): Promise<HookJudgeResult> {
  const threshold = options.threshold ?? DEFAULT_THRESHOLD;
  const judgments: HookJudgment[] = [];
  for (let start = 0; start < items.length; start += JUDGE_BATCH_SIZE) {
    const batch = items.slice(start, start + JUDGE_BATCH_SIZE);
    judgments.push(...await judgeBatchWithGemini(batch));
  }
  const kept: SourceItem[] = [];
  const dropped: HookJudgeResult["dropped"] = [];
  items.forEach((item, index) => {
    const judgment = judgments[index];
    if (judgment.score >= threshold) kept.push(withJudgment(item, judgment));
    else dropped.push({ item, judgment });
  });
  return { kept, dropped, judgments };
}
