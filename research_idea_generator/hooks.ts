import { normalizeWhitespace } from "../sourcing/utils.ts";
import type { EvidenceItem, HookRiskLevel, HookStyle, HookVariant } from "./types.ts";

const OVERCLAIM_PATTERN = /\b(cheapest|fastest|best|everyone|most used|most developers|abandoning|will replace|replacing|secretly)\b/i;
const ENGLISH_HOOK_WORD_LIMIT = 14;
const JAPANESE_HOOK_CHAR_LIMIT = 25;
export const APPROVED_HOOK_STYLES: HookStyle[] = ["list", "contrarian", "curiosity"];

function hasJapaneseText(value: string): boolean {
  return /[\u3040-\u30ff\u3400-\u9fff]/u.test(value);
}

function trimHookEnding(value: string): string {
  return value.replace(/[\s,;:.!?、。！？：]+$/u, "").trim();
}

function fitEnglishHook(value: string): string {
  const words = normalizeWhitespace(value).split(/\s+/).filter(Boolean);
  if (words.length <= ENGLISH_HOOK_WORD_LIMIT) return trimHookEnding(words.join(" "));
  return trimHookEnding(words.slice(0, ENGLISH_HOOK_WORD_LIMIT).join(" "));
}

function fitJapaneseHook(value: string): string {
  const normalized = normalizeWhitespace(value);
  const chars = Array.from(normalized);
  if (chars.length <= JAPANESE_HOOK_CHAR_LIMIT) return trimHookEnding(normalized);
  return trimHookEnding(chars.slice(0, JAPANESE_HOOK_CHAR_LIMIT).join(""));
}

export function optimizeHookText(value: string): string {
  const normalized = normalizeWhitespace(value);
  if (!normalized) return "";
  return hasJapaneseText(normalized) ? fitJapaneseHook(normalized) : fitEnglishHook(normalized);
}

function hasFactCheckEvidence(hook: string, evidence: EvidenceItem[]): boolean {
  if (!OVERCLAIM_PATTERN.test(hook)) return true;
  if (/^(which|what|where|when|why|how|is|are|can|should|could)\b.+[?？]/i.test(normalizeWhitespace(hook))) {
    return true;
  }
  const evidenceText = evidence.map((item) => `${item.title} ${item.excerpt} ${item.url}`).join(" ").toLowerCase();
  if (/\b(cheapest|pricing|cost)\b/i.test(hook)) {
    return /\b(official|docs|pricing|benchmark)\b/.test(evidenceText);
  }
  if (/\b(fastest|best)\b/i.test(hook)) {
    return /\b(benchmark|leaderboard|official|docs)\b/.test(evidenceText);
  }
  return false;
}

export function assessHookRisk(hook: string, evidence: EvidenceItem[]): { riskLevel: HookRiskLevel; needsFactCheck: boolean } {
  const overclaims = OVERCLAIM_PATTERN.test(hook);
  const supported = hasFactCheckEvidence(hook, evidence);
  const needsFactCheck = overclaims && !supported;
  return {
    riskLevel: needsFactCheck ? "high" : overclaims ? "medium" : "low",
    needsFactCheck,
  };
}

function topicFromTitle(title: string): string {
  const normalized = normalizeWhitespace(title).replace(/^[^\p{L}\p{N}]+/u, "").trim();
  const normalizedLower = normalized.toLowerCase();
  if (/\b(agent stack|local memory|mcp)\b/.test(normalizedLower)) return "the agent stack";
  if (/\bgateway|routing\b/.test(normalizedLower)) return "AI gateways";
  if (/\bopenmontage\b/.test(normalizedLower)) return "OpenMontage";
  if (/\bmulti-agent|praisonai\b/.test(normalizedLower)) return "multi-agent frameworks";
  if (/\bsandbox|security\b/.test(normalizedLower)) return "agent sandboxing";
  const withoutPrefix = normalized
    .replace(/^AI builders are converging around\s+/i, "")
    .replace(/^The rise of\s+/i, "")
    .replace(/^Deconstructing\s+/i, "")
    .replace(/^AI builders (?:lean into|focus on)\s+/i, "")
    .split(":")[0]
    .replace(/\blike\s+\S+$/i, "")
    .trim();
  if (hasJapaneseText(normalized)) return fitJapaneseHook(normalized);
  const words = withoutPrefix.split(/\s+/).slice(0, 4);
  while (words.length && /^(and|or|of|the|a|an)$/i.test(words[words.length - 1])) words.pop();
  return words.join(" ");
}

function combinedContext(options: { title: string; claim: string; evidence: EvidenceItem[] }): string {
  return [
    options.title,
    options.claim,
    ...options.evidence.map((item) => `${item.title} ${item.excerpt}`),
  ].join(" ").toLowerCase();
}

function workflowList(context: string): string[] {
  const workflows: string[] = [];
  const add = (value: string) => {
    if (!workflows.includes(value)) workflows.push(value);
  };
  if (/\b(router|routing|gateway|cost|pricing|token|api)\b/.test(context)) add("model routing");
  if (/\b(code|coding|github|repo|pull request|review)\b/.test(context)) add("AI code review");
  if (/\b(meeting|sales|crm)\b/.test(context)) add("meeting-to-CRM automation");
  if (/\b(local|document|docs|rag|memory|search)\b/.test(context)) add("local document search");
  if (/\b(prompt|internal tool|prototype|workflow)\b/.test(context)) add("prompt-to-internal-tool prototypes");
  if (/\b(agent|agents|mcp|graph|orchestrat)\b/.test(context)) add("agent workflow orchestration");
  if (/\b(security|sandbox|permission|auth)\b/.test(context)) add("sandboxed agent runs");
  for (const fallback of [
    "model routing",
    "AI code review",
    "meeting-to-CRM automation",
    "local document search",
    "prompt-to-internal-tool prototypes",
  ]) {
    add(fallback);
    if (workflows.length >= 5) break;
  }
  return workflows.slice(0, 5);
}

function listHookHeadline(topic: string): string {
  const normalized = normalizeWhitespace(topic).replace(/^the\s+/i, "");
  if (/gateway/i.test(normalized)) return "5 AI gateway workflows builders are testing now:";
  if (/agent stack/i.test(normalized)) return "5 agent-stack workflows builders are testing now:";
  if (/openmontage/i.test(normalized)) return "5 OpenMontage workflows builders can inspect:";
  if (/multi-agent/i.test(normalized)) return "5 multi-agent workflows builders are testing now:";
  if (/sandbox/i.test(normalized)) return "5 agent sandboxing workflows builders should inspect:";
  if (/model|api|routing|cost/i.test(normalized)) return "5 model-routing workflows builders are testing now:";
  return `5 ${normalized} workflows builders are testing now:`;
}

function hookFormulaFor(
  style: HookStyle,
  options: { title: string; claim: string; evidence: EvidenceItem[] },
): string[] {
  const topic = topicFromTitle(options.title);
  const context = combinedContext(options);
  const isCost = /\b(cost|cheap|cheaper|cheapest|pricing|token|api|unit economics)\b/.test(context);
  const isModel = /\b(model|llm|api|token|provider)\b/.test(context);
  if (style === "curiosity") {
    return isModel && topic === "the model"
      ? [
        "The trend is not the model.",
        "It is what developers are building around it.",
      ]
      : [
        `The trend is not ${topic}.`,
        "It is the workflow developers are building around it.",
      ];
  }
  if (style === "contrarian") {
    return isCost
      ? [
        '"Cheapest AI API" is the wrong question.',
        "The better question:",
        "which tasks deserve expensive tokens?",
      ]
      : [
        `"Should we use ${topic}?" is the wrong question.`,
        "The better question:",
        "which workflow is this supposed to improve?",
      ];
  }
  if (style === "list") {
    const workflows = workflowList(context);
    return [
      listHookHeadline(topic),
      ...workflows.map((workflow, index) => `${index + 1}. ${workflow}`),
    ];
  }
  return [options.claim];
}

function whyItWorks(style: HookStyle): string {
  if (style === "curiosity") return "Creates a gap between the obvious story and the deeper behavior pattern.";
  if (style === "contrarian") return "Challenges the default framing without making an unsupported factual claim.";
  if (style === "list") return "Promises a compact scan of evidence and reduces review friction.";
  return "Frames the evidence as a concrete builder-facing hook.";
}

function bestPlatform(style: HookStyle): HookVariant["bestPlatform"] {
  if (style === "list" || style === "contrarian") return "X";
  return "X";
}

export function generateHookVariants(
  options: {
    title: string;
    claim: string;
    evidence: EvidenceItem[];
    count: number;
    styles?: HookStyle[];
  },
): HookVariant[] {
  const styles = options.styles?.length
    ? options.styles
    : APPROVED_HOOK_STYLES;
  const variants: HookVariant[] = [];
  const seen = new Set<string>();
  for (const style of styles) {
    const formulaLines = hookFormulaFor(style, options)
      .map((line) => normalizeWhitespace(line))
      .filter(Boolean);
    const hook = optimizeHookText(formulaLines[0] ?? options.claim);
    const lines = [hook, ...formulaLines.slice(1)];
    const signature = `${style}:${lines.join("\n")}`;
    if (seen.has(signature)) continue;
    seen.add(signature);
    const risk = assessHookRisk(lines.join("\n"), options.evidence);
    variants.push({
      hook,
      lines,
      style,
      riskLevel: risk.riskLevel,
      whyItWorks: whyItWorks(style),
      needsFactCheck: risk.needsFactCheck,
      bestPlatform: bestPlatform(style),
    });
    if (variants.length >= options.count) break;
  }
  return variants;
}
