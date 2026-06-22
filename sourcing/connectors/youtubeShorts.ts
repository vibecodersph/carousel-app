import { spawn } from "node:child_process";
import type { SourceItem } from "../types.ts";
import { mapLimit, normalizeWhitespace, numberValue, stableSourceItemId, toIsoDate } from "../utils.ts";

// Credible AI channels (handles) whose recent uploads are fresh, on-topic, and from
// accounts with real audiences. Confirmed reachable + posting in-window from this
// machine. Override with YOUTUBE_CHANNELS (comma separated handles, no @).
const DEFAULT_CHANNELS = [
  "OpenAI",
  "GoogleDeepMind",
  "twominutepapers",
  "mreflow", // Matt Wolfe
  "TheAIGRID",
  "matthew_berman",
  "WesRoth",
  "bycloudAI",
  "aiexplained-official",
  "theAIsearch",
];

// Optional search supplement (behind --youtube-search). Plain `ytsearch` — this
// yt-dlp build does NOT support the `ytsearchdate` prefix. Override with
// YOUTUBE_SOURCE_QUERIES (comma separated).
const DEFAULT_QUERIES = ["open source AI model", "new AI tool", "AI agent", "AI robot"];

// Engagement-bait / off-topic title patterns to drop (open search is noisy).
const TITLE_DENYLIST = /\b(giveaway|deleting (this|in)|sub for sub|free leads|dm me|link in bio)\b|comment ['"]?(send|yes|leads|go)\b/i;

export interface YouTubeShortsConnectorOptions {
  channels?: string[];
  queries?: string[];
  useSearch?: boolean;
  perChannel?: number;
  perQuery?: number;
  maxItems?: number;
  windowDays?: number;
  minViews?: number;
  minSubscribers?: number;
  timeoutMs?: number;
}

interface YouTubeEntry {
  id?: string;
  url?: string;
  webpage_url?: string;
  title?: string;
  uploader?: string;
  channel?: string;
  channel_id?: string;
  uploader_id?: string;
  channel_follower_count?: number;
  duration?: number;
  view_count?: number;
  like_count?: number;
  comment_count?: number;
  timestamp?: number;
  release_timestamp?: number;
  upload_date?: string; // YYYYMMDD
  language?: string;
  description?: string;
}

function resolveList(explicit: string[] | undefined, env: string | undefined, fallback: string[]): string[] {
  if (explicit?.length) return explicit;
  const fromEnv = (env || "").split(",").map((v) => v.trim()).filter(Boolean);
  return fromEnv.length ? fromEnv : fallback;
}

function youtubeUrl(entry: YouTubeEntry): string {
  const direct = normalizeWhitespace(entry.webpage_url || entry.url);
  if (direct.startsWith("http")) return direct;
  const id = normalizeWhitespace(entry.id);
  return id ? `https://www.youtube.com/watch?v=${id}` : "";
}

/** Convert a yt-dlp entry's date fields to ISO, preferring the epoch timestamp. */
function youtubeIsoDate(entry: YouTubeEntry): string {
  if (typeof entry.timestamp === "number") return toIsoDate(entry.timestamp);
  if (typeof entry.release_timestamp === "number") return toIsoDate(entry.release_timestamp);
  const ud = normalizeWhitespace(entry.upload_date);
  if (/^\d{8}$/.test(ud)) return `${ud.slice(0, 4)}-${ud.slice(4, 6)}-${ud.slice(6, 8)}T00:00:00.000Z`;
  return new Date(0).toISOString();
}

/** Pure mapper from a yt-dlp entry to a SourceItem (unit-testable, no network). */
export function youtubeEntryToSourceItem(entry: YouTubeEntry): SourceItem | null {
  const externalId = normalizeWhitespace(entry.id);
  const title = normalizeWhitespace(entry.title);
  const url = youtubeUrl(entry);
  if (!externalId || !title || !url) return null;
  if (/^\[(private|deleted) video\]$/i.test(title)) return null;

  const subscribers = numberValue(entry.channel_follower_count) || undefined;
  return {
    id: stableSourceItemId("youtube_shorts", externalId),
    source: "youtube_shorts",
    externalId,
    url,
    title,
    body: normalizeWhitespace(entry.description),
    author: normalizeWhitespace(entry.uploader ?? entry.channel ?? entry.uploader_id),
    createdAt: youtubeIsoDate(entry),
    metrics: {
      views: numberValue(entry.view_count),
      likes: numberValue(entry.like_count) || undefined,
      comments: numberValue(entry.comment_count) || undefined,
      score: numberValue(entry.view_count),
    },
    media: {
      hasVideo: true,
      videoUrl: url,
      durationSeconds: numberValue(entry.duration) || undefined,
      provider: "youtube",
    },
    raw: { ...entry, channel_follower_count: subscribers },
  };
}

function yyyymmddDaysAgo(days: number): string {
  const date = new Date(Date.now() - days * 86_400_000);
  return `${date.getUTCFullYear()}${String(date.getUTCMonth() + 1).padStart(2, "0")}${String(date.getUTCDate()).padStart(2, "0")}`;
}

function runYtDlpJson(target: string, extraArgs: string[], timeoutMs: number): Promise<YouTubeEntry[]> {
  return new Promise((resolve) => {
    const child = spawn(
      "uv",
      ["run", "yt-dlp", "--dump-json", "--no-warnings", "--ignore-errors", "--socket-timeout", "20", ...extraArgs, target],
      { stdio: ["ignore", "pipe", "pipe"] },
    );
    let stdout = "";
    let stderr = "";
    const timeout = setTimeout(() => {
      child.kill("SIGTERM");
      resolve(parseEntries(stdout));
    }, timeoutMs);
    child.stdout.on("data", (chunk) => (stdout += chunk.toString()));
    child.stderr.on("data", (chunk) => (stderr += chunk.toString()));
    child.on("error", () => {
      clearTimeout(timeout);
      console.warn(`[youtube] yt-dlp failed for ${target}`);
      resolve([]);
    });
    child.on("close", () => {
      clearTimeout(timeout);
      if (!stdout && stderr) console.warn(`[youtube] ${target}: ${normalizeWhitespace(stderr).slice(0, 140)}`);
      resolve(parseEntries(stdout));
    });
  });
}

function parseEntries(stdout: string): YouTubeEntry[] {
  const entries: YouTubeEntry[] = [];
  for (const line of stdout.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed.startsWith("{")) continue;
    try {
      entries.push(JSON.parse(trimmed) as YouTubeEntry);
    } catch {
      // skip malformed line
    }
  }
  return entries;
}

export async function fetchYouTubeShortsSourceItems(
  options: YouTubeShortsConnectorOptions = {},
): Promise<SourceItem[]> {
  const channels = resolveList(options.channels, process.env.YOUTUBE_CHANNELS, DEFAULT_CHANNELS);
  const queries = resolveList(options.queries, process.env.YOUTUBE_SOURCE_QUERIES, DEFAULT_QUERIES);
  const perChannel = options.perChannel ?? 4;
  const perQuery = options.perQuery ?? 25;
  const maxItems = options.maxItems ?? 40;
  const windowDays = options.windowDays ?? 7;
  const minViews = options.minViews ?? 5_000;
  const minSubscribers = options.minSubscribers ?? 50_000;
  const timeoutMs = options.timeoutMs ?? 90_000;
  const dateAfter = yyyymmddDaysAgo(windowDays);
  const matchFilter = `view_count >= ${minViews}`;

  // Primary: recent uploads from credible channels (credibility guaranteed by the
  // allowlist, freshness by --dateafter, demand by the view floor).
  const channelEntries = (
    await mapLimit(channels, 4, (handle) =>
      runYtDlpJson(
        `https://www.youtube.com/@${handle}/videos`,
        ["--playlist-end", String(perChannel), "--dateafter", dateAfter, "--match-filter", matchFilter],
        timeoutMs,
      ),
    )
  ).flat();

  // Optional supplement: filtered search (noisier; opt-in via useSearch).
  const searchEntries: YouTubeEntry[] = [];
  if (options.useSearch) {
    for (const query of queries) {
      searchEntries.push(
        ...(await runYtDlpJson(
          `ytsearch${perQuery}:${query}`,
          ["--dateafter", dateAfter, "--match-filter", `view_count >= ${minViews} & channel_follower_count >= ${minSubscribers}`],
          timeoutMs,
        )),
      );
    }
  }

  const cutoff = Date.now() - windowDays * 86_400_000;
  const byId = new Map<string, SourceItem>();
  for (const entry of [...channelEntries, ...searchEntries]) {
    const item = youtubeEntryToSourceItem(entry);
    if (!item) continue;
    if (TITLE_DENYLIST.test(item.title)) continue;
    if (entry.language && !entry.language.toLowerCase().startsWith("en")) continue;
    const subscribers = numberValue((entry as YouTubeEntry).channel_follower_count);
    if (subscribers && subscribers < minSubscribers) continue;
    if ((item.metrics.views ?? 0) < minViews) continue;
    const created = Date.parse(item.createdAt);
    if (Number.isFinite(created) && created < cutoff) continue; // enforce freshness
    if (!byId.has(item.id)) byId.set(item.id, item);
  }

  return [...byId.values()]
    .sort((a, b) => (b.metrics.views ?? 0) - (a.metrics.views ?? 0))
    .slice(0, maxItems);
}
