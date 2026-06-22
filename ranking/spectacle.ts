import type { SourceItem } from "../sourcing/types.ts";
import { clamp, compactText, normalizeWhitespace, readJsonFile, sha256, writeJsonFile } from "../sourcing/utils.ts";

export interface SpectacleScore {
  score: number;
  reason: string;
  provider?: string;
  cachedAt?: string;
}

export interface SpectacleOptions {
  cachePath?: string;
  provider?: "auto" | "gemini" | "local";
  requireLlm?: boolean;
}

const PROMPT = `Score 0.0-1.0 for how likely a curious 24-year-old developer would stop scrolling and send this to a friend. Reward surprise, "wait what", visible demos, absurdity, and "someone built X". Penalize incremental announcements, pricing/funding, vague claims, and jargon with nothing to show.

Return strict JSON only:
{"score": number, "reason": string}`;

function cacheKey(item: SourceItem): string {
  return sha256([
    item.id,
    item.title,
    item.body,
    item.topReply?.body ?? "",
    item.media.hasVideo ? "video" : "no-video",
  ].join("\n"));
}

function localSpectacle(item: SourceItem): SpectacleScore {
  const text = `${item.title} ${item.body} ${item.topReply?.body ?? ""}`.toLowerCase();
  const rewardPatterns = [
    /\bdemo\b/,
    /\bvideo\b/,
    /\bbuilt\b/,
    /\bcreated\b/,
    /\bmade\b/,
    /\bopen[-\s]?source\b/,
    /\brealtime\b/,
    /\brobot\b/,
    /\bagent\b/,
    /\bvoice\b/,
    /\b3d\b/,
    /\bwait\b/,
    /\binsane\b/,
    /\bwild\b/,
    /\babsurd\b/,
    /\bfrom scratch\b/,
    /\bgithub\b/,
  ];
  const penaltyPatterns = [
    /\bfunding\b/,
    /\braised\b/,
    /\bpricing\b/,
    /\bwebinar\b/,
    /\bnewsletter\b/,
    /\bpartnership\b/,
    /\bcoming soon\b/,
    /\bvague\b/,
  ];
  const rewards = rewardPatterns.filter((pattern) => pattern.test(text)).length;
  const penalties = penaltyPatterns.filter((pattern) => pattern.test(text)).length;
  const metricBoost = Math.min(0.18, Math.log10(Math.max(1, item.metrics.score ?? item.metrics.upvotes ?? 0)) / 18);
  const videoBoost = item.media.hasVideo ? 0.18 : 0;
  const score = clamp(0.28 + rewards * 0.055 + videoBoost + metricBoost - penalties * 0.08);
  return {
    score: Number(score.toFixed(4)),
    reason: rewards
      ? "Local heuristic found demo/build/surprise language and public demand signals."
      : "Local heuristic found limited spectacle signals.",
    provider: "local_heuristic",
  };
}

function parseStrictJson(text: string): SpectacleScore {
  const start = text.indexOf("{");
  const end = text.lastIndexOf("}");
  if (start < 0 || end <= start) {
    throw new Error(`No JSON object in LLM response: ${text.slice(0, 160)}`);
  }
  const parsed = JSON.parse(text.slice(start, end + 1)) as { score?: unknown; reason?: unknown };
  return {
    score: clamp(Number(parsed.score)),
    reason: normalizeWhitespace(parsed.reason).slice(0, 240),
  };
}

async function geminiSpectacle(item: SourceItem): Promise<SpectacleScore | undefined> {
  const apiKey = process.env.GEMINI_API_KEY || process.env.GOOGLE_API_KEY;
  if (!apiKey) return undefined;
  const model = process.env.GEMINI_TEXT_MODEL || "gemini-3.5-flash";
  const apiVersion = process.env.GEMINI_TEXT_API_VERSION || "v1beta";
  const payload = {
    contents: [
      {
        role: "user",
        parts: [
          {
            text: [
              PROMPT,
              "",
              "Candidate:",
              JSON.stringify({
                title: item.title,
                body: compactText(item.body, 600),
                source: item.source,
                subreddit: item.subreddit,
                metrics: item.metrics,
                hasVideo: item.media.hasVideo,
                topReply: item.topReply?.body,
              }),
            ].join("\n"),
          },
        ],
      },
    ],
    generationConfig: {
      temperature: 0.1,
      responseMimeType: "application/json",
    },
  };
  const response = await fetch(`https://generativelanguage.googleapis.com/${apiVersion}/models/${model}:generateContent`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "x-goog-api-key": apiKey,
    },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    throw new Error(`Gemini spectacle scoring returned HTTP ${response.status}: ${await response.text()}`);
  }
  const data = await response.json() as { candidates?: Array<{ content?: { parts?: Array<{ text?: string }> } }> };
  const text = data.candidates?.[0]?.content?.parts?.map((part) => part.text ?? "").join("") ?? "";
  const parsed = parseStrictJson(text);
  return { ...parsed, provider: "gemini" };
}

export async function scoreSpectacle(item: SourceItem, options: SpectacleOptions = {}): Promise<SpectacleScore> {
  const cachePath = options.cachePath ?? "out/automation/ranking/spectacle-cache.json";
  const key = cacheKey(item);
  const cache = await readJsonFile<Record<string, SpectacleScore>>(cachePath, {});
  if (cache[key]) return cache[key];

  const provider = options.provider ?? "auto";
  let score: SpectacleScore | undefined;
  if (provider === "gemini" || provider === "auto") {
    try {
      score = await geminiSpectacle(item);
    } catch (error) {
      if (provider === "gemini") throw error;
      console.warn(`[ranking] Gemini spectacle scoring failed for ${item.id}; using local fallback: ${(error as Error).message}`);
    }
  }
  if (!score && options.requireLlm) {
    throw new Error("No LLM provider is configured for spectacle scoring");
  }
  score = score ?? localSpectacle(item);
  const cached = { ...score, cachedAt: new Date().toISOString() };
  cache[key] = cached;
  await writeJsonFile(cachePath, cache);
  return cached;
}
