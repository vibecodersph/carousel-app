import type { SourceItem } from "./types.ts";
import { embedText, type EmbeddingOptions } from "./embeddings.ts";
import { cosineSimilarity, normalizeWhitespace, readJsonFile, writeJsonFile } from "./utils.ts";

export interface DedupState {
  version: 1;
  seenIds: Record<string, {
    firstSeenAt: string;
    source: string;
    externalId: string;
    title: string;
    url: string;
  }>;
  recentTitles: Array<{
    id: string;
    title: string;
    normalizedTitle: string;
    embedding: number[];
    seenAt: string;
    source: string;
    url: string;
  }>;
}

export interface DedupOptions {
  statePath?: string;
  remember?: boolean;
  similarityThreshold?: number;
  maxRecentTitles?: number;
  embedding?: EmbeddingOptions;
}

export interface DedupDrop {
  item: SourceItem;
  reason: "seen_id" | "batch_id" | "title_duplicate";
  duplicateOf?: string;
  similarity?: number;
}

export interface DedupResult {
  items: SourceItem[];
  dropped: DedupDrop[];
  state: DedupState;
}

function emptyDedupState(): DedupState {
  return {
    version: 1,
    seenIds: {},
    recentTitles: [],
  };
}

function normalizedTitle(title: string): string {
  return normalizeWhitespace(title)
    .toLowerCase()
    .replace(/https?:\/\/\S+/g, "")
    .replace(/[^\p{L}\p{N}\s]+/gu, " ")
    .replace(/\s+/g, " ")
    .trim();
}

async function titleRecordForItem(
  item: SourceItem,
  now: string,
  options: DedupOptions,
): Promise<DedupState["recentTitles"][number]> {
  const titleKey = normalizedTitle(item.title);
  return {
    id: item.id,
    title: item.title,
    normalizedTitle: titleKey,
    embedding: await embedText(titleKey, options.embedding),
    seenAt: now,
    source: item.source,
    url: item.url,
  };
}

async function loadDedupState(statePath: string): Promise<DedupState> {
  const state = await readJsonFile<DedupState | null>(statePath, null) ?? emptyDedupState();
  state.version = 1;
  state.seenIds = state.seenIds ?? {};
  state.recentTitles = Array.isArray(state.recentTitles) ? state.recentTitles : [];
  return state;
}

export async function dedupeSourceItems(items: SourceItem[], options: DedupOptions = {}): Promise<DedupResult> {
  const statePath = options.statePath ?? "out/automation/sourcing/dedup-state.json";
  const state = await loadDedupState(statePath);

  const remember = options.remember ?? true;
  const threshold = options.similarityThreshold ?? 0.88;
  const maxRecentTitles = options.maxRecentTitles ?? 1_000;
  const now = new Date().toISOString();
  const kept: SourceItem[] = [];
  const dropped: DedupDrop[] = [];
  const batchIds = new Set<string>();
  const batchTitles: DedupState["recentTitles"] = [];

  for (const item of items) {
    if (state.seenIds[item.id]) {
      dropped.push({ item, reason: "seen_id", duplicateOf: item.id });
      continue;
    }
    if (batchIds.has(item.id)) {
      dropped.push({ item, reason: "batch_id", duplicateOf: item.id });
      continue;
    }

    const titleKey = normalizedTitle(item.title);
    const embedding = await embedText(titleKey, options.embedding);
    let duplicate: { id: string; similarity: number } | undefined;
    for (const previous of [...state.recentTitles, ...batchTitles]) {
      const exactTitle = previous.normalizedTitle && previous.normalizedTitle === titleKey;
      const similarity = exactTitle ? 1 : cosineSimilarity(embedding, previous.embedding);
      if (similarity >= threshold) {
        duplicate = { id: previous.id, similarity };
        break;
      }
    }
    if (duplicate) {
      dropped.push({
        item,
        reason: "title_duplicate",
        duplicateOf: duplicate.id,
        similarity: Number(duplicate.similarity.toFixed(4)),
      });
      continue;
    }

    batchIds.add(item.id);
    kept.push(item);
    batchTitles.push({
      ...(await titleRecordForItem(item, now, options)),
      normalizedTitle: titleKey,
      embedding,
    });
  }

  if (remember) {
    for (const item of kept) {
      state.seenIds[item.id] = {
        firstSeenAt: state.seenIds[item.id]?.firstSeenAt ?? now,
        source: item.source,
        externalId: item.externalId,
        title: item.title,
        url: item.url,
      };
    }
    state.recentTitles = [...state.recentTitles, ...batchTitles].slice(-maxRecentTitles);
    await writeJsonFile(statePath, state);
  }

  return { items: kept, dropped, state };
}

export async function rememberSourceItems(
  items: SourceItem[],
  options: Omit<DedupOptions, "remember" | "similarityThreshold"> = {},
): Promise<DedupState> {
  const statePath = options.statePath ?? "out/automation/sourcing/dedup-state.json";
  const state = await loadDedupState(statePath);
  const maxRecentTitles = options.maxRecentTitles ?? 1_000;
  const now = new Date().toISOString();
  const existingTitleIds = new Set(state.recentTitles.map((entry) => entry.id));
  const newTitleRecords: DedupState["recentTitles"] = [];

  for (const item of items) {
    state.seenIds[item.id] = {
      firstSeenAt: state.seenIds[item.id]?.firstSeenAt ?? now,
      source: item.source,
      externalId: item.externalId,
      title: item.title,
      url: item.url,
    };
    if (!existingTitleIds.has(item.id)) {
      existingTitleIds.add(item.id);
      newTitleRecords.push(await titleRecordForItem(item, now, options));
    }
  }

  state.recentTitles = [...state.recentTitles, ...newTitleRecords].slice(-maxRecentTitles);
  await writeJsonFile(statePath, state);
  return state;
}
