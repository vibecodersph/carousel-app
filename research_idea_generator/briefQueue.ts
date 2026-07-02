import { readdir } from "node:fs/promises";
import { dirname, join } from "node:path";
import { mkdir } from "node:fs/promises";
import { normalizeWhitespace, readJsonFile, sha256, writeJsonFile } from "../sourcing/utils.ts";
import type { CarouselBrief, CarouselBriefOutput } from "./types.ts";

export type CarouselBriefQueueStatus =
  | "new"
  | "rendered"
  | "scheduled"
  | "published"
  | "skipped"
  | "failed";

export interface QueuedCarouselBrief {
  id: string;
  briefId: string;
  briefPath: string;
  runDir: string;
  runGeneratedAt?: string;
  briefIndex: number;
  sourceInsightCardId: string;
  workingTitle: string;
  hook: string;
  hookStyle: string;
  score: number;
  confidence: string;
  evidenceUrls: string[];
  sourceImageUrls: string[];
  channelId?: string;
  status: CarouselBriefQueueStatus;
  firstSeenAt: string;
  lastSeenAt: string;
  renderedManifestPath?: string;
  scheduledAt?: string;
  publishedAt?: string;
  permalink?: string;
  lastError?: string;
}

export interface CarouselBriefQueue {
  version: 1;
  updatedAt: string;
  items: QueuedCarouselBrief[];
}

export interface CarouselBriefScanResult {
  queue: CarouselBriefQueue;
  queuePath: string;
  runsDir: string;
  scannedFiles: string[];
  briefCount: number;
  added: number;
  updated: number;
  unchanged: number;
  statusCounts: Record<CarouselBriefQueueStatus, number>;
}

export interface CarouselBriefScanOptions {
  runsDir?: string;
  queuePath?: string;
  now?: Date;
  channelId?: string;
  publishSlots?: PublishSlot[];
}

const TERMINAL_STATUSES = new Set<CarouselBriefQueueStatus>(["published", "skipped"]);
const DEFAULT_CHANNEL_ID = "aibrief_jp";
const DEFAULT_TIMEZONE_OFFSET_MINUTES = 9 * 60;
const SLOT_RESOLUTION_MS = 60_000;
export interface PublishSlot {
  hour: number;
  minute: number;
}

export const DEFAULT_CAROUSEL_BRIEF_PUBLISH_SLOTS: PublishSlot[] = [
  { hour: 9, minute: 0 },
  { hour: 12, minute: 0 },
  { hour: 18, minute: 0 },
  { hour: 21, minute: 0 },
];
const ALL_STATUSES: CarouselBriefQueueStatus[] = [
  "new",
  "rendered",
  "scheduled",
  "published",
  "skipped",
  "failed",
];

export function defaultCarouselBriefRunsDir(): string {
  return "out/research_idea_generator/runs";
}

export function defaultCarouselBriefQueuePath(): string {
  return "out/research_idea_generator/carousel_brief_queue.json";
}

function emptyQueue(): CarouselBriefQueue {
  return {
    version: 1,
    updatedAt: new Date(0).toISOString(),
    items: [],
  };
}

async function loadQueue(path = defaultCarouselBriefQueuePath()): Promise<CarouselBriefQueue> {
  const queue = await readJsonFile<CarouselBriefQueue>(path, emptyQueue());
  return {
    version: 1,
    updatedAt: queue.updatedAt || new Date(0).toISOString(),
    items: Array.isArray(queue.items) ? queue.items : [],
  };
}

async function saveQueue(path: string, queue: CarouselBriefQueue): Promise<void> {
  await mkdir(dirname(path), { recursive: true });
  await writeJsonFile(path, queue);
}

function normalizeUrl(value: string): string {
  try {
    const url = new URL(value);
    url.hash = "";
    url.search = "";
    return url.toString().replace(/\/$/, "");
  } catch {
    return normalizeWhitespace(value).replace(/[?#].*$/, "").replace(/\/$/, "");
  }
}

export function carouselBriefQueueId(brief: Pick<CarouselBrief, "id" | "evidenceUrls">): string {
  const evidenceKey = (brief.evidenceUrls ?? []).map(normalizeUrl).filter(Boolean).sort().join("|");
  return sha256(`carousel-brief:${brief.id}:${evidenceKey}`).slice(0, 24);
}

async function findCarouselBriefFiles(root: string): Promise<string[]> {
  async function walk(dir: string): Promise<string[]> {
    let entries;
    try {
      entries = await readdir(dir, { withFileTypes: true });
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") return [];
      throw error;
    }
    const files: string[] = [];
    for (const entry of entries) {
      const path = join(dir, entry.name);
      if (entry.isDirectory()) {
        files.push(...await walk(path));
      } else if (entry.isFile() && entry.name === "carousel_briefs.json") {
        files.push(path);
      }
    }
    return files;
  }
  return (await walk(root)).sort();
}

function imageUrlsForBrief(brief: CarouselBrief): string[] {
  const urls = new Set<string>();
  for (const slide of brief.slides ?? []) {
    const image = slide.image;
    if (!image) continue;
    if (image.sourceImageUrl) urls.add(image.sourceImageUrl);
    for (const url of image.sourceImageUrls ?? []) {
      if (url) urls.add(url);
    }
  }
  return [...urls];
}

function queueItemFromBrief(options: {
  brief: CarouselBrief;
  briefPath: string;
  runGeneratedAt?: string;
  briefIndex: number;
  now: string;
  channelId: string;
}): QueuedCarouselBrief {
  const id = carouselBriefQueueId(options.brief);
  return {
    id,
    briefId: options.brief.id,
    briefPath: options.briefPath,
    runDir: dirname(options.briefPath),
    runGeneratedAt: options.runGeneratedAt,
    briefIndex: options.briefIndex,
    sourceInsightCardId: options.brief.sourceInsightCardId,
    workingTitle: options.brief.workingTitle,
    hook: options.brief.hook,
    hookStyle: options.brief.hookStyle,
    score: options.brief.score,
    confidence: options.brief.confidence,
    evidenceUrls: options.brief.evidenceUrls ?? [],
    sourceImageUrls: imageUrlsForBrief(options.brief),
    channelId: options.channelId,
    status: "new",
    firstSeenAt: options.now,
    lastSeenAt: options.now,
  };
}

function mergeQueueItem(existing: QueuedCarouselBrief, next: QueuedCarouselBrief): QueuedCarouselBrief {
  return {
    ...existing,
    briefId: next.briefId,
    briefPath: next.briefPath,
    runDir: next.runDir,
    runGeneratedAt: next.runGeneratedAt,
    briefIndex: next.briefIndex,
    sourceInsightCardId: next.sourceInsightCardId,
    workingTitle: next.workingTitle,
    hook: next.hook,
    hookStyle: next.hookStyle,
    score: next.score,
    confidence: next.confidence,
    evidenceUrls: next.evidenceUrls,
    sourceImageUrls: next.sourceImageUrls,
    channelId: existing.channelId || next.channelId,
    lastSeenAt: next.lastSeenAt,
  };
}

function pad2(value: number): string {
  return String(value).padStart(2, "0");
}

function zonedParts(date: Date): {
  year: number;
  month: number;
  day: number;
} {
  const shifted = new Date(date.getTime() + DEFAULT_TIMEZONE_OFFSET_MINUTES * SLOT_RESOLUTION_MS);
  return {
    year: shifted.getUTCFullYear(),
    month: shifted.getUTCMonth() + 1,
    day: shifted.getUTCDate(),
  };
}

function slotInstant(parts: { year: number; month: number; day: number }, slot: PublishSlot): {
  iso: string;
  ms: number;
} {
  const ms = Date.UTC(parts.year, parts.month - 1, parts.day, slot.hour, slot.minute)
    - DEFAULT_TIMEZONE_OFFSET_MINUTES * SLOT_RESOLUTION_MS;
  return {
    iso: `${parts.year}-${pad2(parts.month)}-${pad2(parts.day)}T${pad2(slot.hour)}:${pad2(slot.minute)}:00+09:00`,
    ms,
  };
}

function addDays(parts: { year: number; month: number; day: number }, days: number): {
  year: number;
  month: number;
  day: number;
} {
  const ms = Date.UTC(parts.year, parts.month - 1, parts.day + days, 0, 0);
  const date = new Date(ms);
  return {
    year: date.getUTCFullYear(),
    month: date.getUTCMonth() + 1,
    day: date.getUTCDate(),
  };
}

function normalizeSlots(slots: PublishSlot[]): PublishSlot[] {
  const unique = new Map<string, PublishSlot>();
  for (const slot of slots) {
    if (!Number.isInteger(slot.hour) || !Number.isInteger(slot.minute)) continue;
    if (slot.hour < 0 || slot.hour > 23 || slot.minute < 0 || slot.minute > 59) continue;
    unique.set(`${pad2(slot.hour)}:${pad2(slot.minute)}`, {
      hour: slot.hour,
      minute: slot.minute,
    });
  }
  return [...unique.values()].sort((a, b) => a.hour - b.hour || a.minute - b.minute);
}

function nextSlotAtOrAfter(cursor: Date, slots: PublishSlot[]): { iso: string; ms: number } {
  const normalized = normalizeSlots(slots);
  if (!normalized.length) {
    throw new Error("At least one publish slot is required to schedule carousel briefs");
  }
  const boundaryMs = Math.floor(cursor.getTime() / SLOT_RESOLUTION_MS) * SLOT_RESOLUTION_MS;
  const today = zonedParts(cursor);
  for (let dayOffset = 0; dayOffset < 370; dayOffset += 1) {
    const day = addDays(today, dayOffset);
    for (const slot of normalized) {
      const candidate = slotInstant(day, slot);
      if (candidate.ms >= boundaryMs) return candidate;
    }
  }
  throw new Error("Unable to find a publish slot");
}

function scheduleQueueItems(
  items: QueuedCarouselBrief[],
  options: {
    now: Date;
    channelId: string;
    publishSlots: PublishSlot[];
  },
): QueuedCarouselBrief[] {
  const scheduled = items.map((item) => ({ ...item }));
  const reserved = new Set<string>();
  let cursorMs = options.now.getTime();
  for (const item of scheduled) {
    item.channelId ||= options.channelId;
    if (item.scheduledAt && item.status === "new") {
      item.status = "scheduled";
    }
    if (item.scheduledAt) {
      reserved.add(item.scheduledAt);
    }
    if (!TERMINAL_STATUSES.has(item.status) && item.scheduledAt) {
      const parsed = Date.parse(item.scheduledAt);
      if (Number.isFinite(parsed)) {
        cursorMs = Math.max(cursorMs, parsed + SLOT_RESOLUTION_MS);
      }
    }
  }
  for (const item of scheduled) {
    if (TERMINAL_STATUSES.has(item.status) || item.scheduledAt) continue;
    item.channelId ||= options.channelId;
    let candidate = nextSlotAtOrAfter(new Date(cursorMs), options.publishSlots);
    while (reserved.has(candidate.iso)) {
      candidate = nextSlotAtOrAfter(new Date(candidate.ms + SLOT_RESOLUTION_MS), options.publishSlots);
    }
    item.scheduledAt = candidate.iso;
    reserved.add(candidate.iso);
    cursorMs = candidate.ms + SLOT_RESOLUTION_MS;
    if (item.status === "new") {
      item.status = "scheduled";
    }
  }
  return scheduled;
}

function statusCounts(items: QueuedCarouselBrief[]): Record<CarouselBriefQueueStatus, number> {
  const counts = Object.fromEntries(ALL_STATUSES.map((status) => [status, 0])) as Record<CarouselBriefQueueStatus, number>;
  for (const item of items) {
    counts[item.status] = (counts[item.status] ?? 0) + 1;
  }
  return counts;
}

export function unpublishedCarouselBriefs(queue: CarouselBriefQueue): QueuedCarouselBrief[] {
  return queue.items.filter((item) => !TERMINAL_STATUSES.has(item.status));
}

export async function scanCarouselBriefRunArchives(
  options: CarouselBriefScanOptions = {},
): Promise<CarouselBriefScanResult> {
  const nowDate = options.now ?? new Date();
  const now = nowDate.toISOString();
  const runsDir = options.runsDir ?? defaultCarouselBriefRunsDir();
  const queuePath = options.queuePath ?? defaultCarouselBriefQueuePath();
  const channelId = options.channelId ?? DEFAULT_CHANNEL_ID;
  const publishSlots = options.publishSlots ?? DEFAULT_CAROUSEL_BRIEF_PUBLISH_SLOTS;
  const queue = await loadQueue(queuePath);
  const byId = new Map(queue.items.map((item) => [item.id, item]));
  const files = await findCarouselBriefFiles(runsDir);
  let briefCount = 0;
  let added = 0;
  let updated = 0;
  let unchanged = 0;

  for (const file of files) {
    const output = await readJsonFile<CarouselBriefOutput>(file, {
      generatedAt: new Date(0).toISOString(),
      audience: "ai_builders",
      sourceInsightGeneratedAt: new Date(0).toISOString(),
      carouselCount: 0,
      carousels: [],
    });
    for (const [index, brief] of (output.carousels ?? []).entries()) {
      briefCount += 1;
      const next = queueItemFromBrief({
        brief,
        briefPath: file,
        runGeneratedAt: output.generatedAt || output.sourceInsightGeneratedAt,
        briefIndex: index,
        now,
        channelId,
      });
      const existing = byId.get(next.id);
      if (!existing) {
        byId.set(next.id, next);
        added += 1;
        continue;
      }
      const merged = mergeQueueItem(existing, next);
      if (JSON.stringify(merged) === JSON.stringify(existing)) {
        unchanged += 1;
      } else {
        byId.set(next.id, merged);
        updated += 1;
      }
    }
  }

  const items = scheduleQueueItems([...byId.values()].sort((a, b) =>
    (b.runGeneratedAt ?? "").localeCompare(a.runGeneratedAt ?? "")
    || b.score - a.score
    || a.workingTitle.localeCompare(b.workingTitle)
  ), { now: nowDate, channelId, publishSlots });
  const nextQueue: CarouselBriefQueue = {
    version: 1,
    updatedAt: now,
    items,
  };
  await saveQueue(queuePath, nextQueue);
  return {
    queue: nextQueue,
    queuePath,
    runsDir,
    scannedFiles: files,
    briefCount,
    added,
    updated,
    unchanged,
    statusCounts: statusCounts(items),
  };
}
