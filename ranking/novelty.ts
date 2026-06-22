import type { SourceItem } from "../sourcing/types.ts";
import { embedText, type EmbeddingOptions } from "../sourcing/embeddings.ts";
import { cosineSimilarity, normalizeWhitespace, readJsonFile } from "../sourcing/utils.ts";

export interface PublishedTitleEmbedding {
  title: string;
  embedding: number[];
  publishedAt?: string;
  sourceId?: string;
}

export interface NoveltyOptions {
  queuePath?: string;
  publishedTitlesPath?: string;
  limit?: number;
  embedding?: EmbeddingOptions;
}

function candidateTitle(candidate: Record<string, unknown>): string {
  const sourceItem = candidate.source_item as Record<string, unknown> | undefined;
  const article = candidate.article as Record<string, unknown> | undefined;
  const post = candidate.post as Record<string, unknown> | undefined;
  return normalizeWhitespace(
    sourceItem?.title
      ?? article?.title
      ?? post?.text
      ?? candidate.title
      ?? "",
  );
}

export async function loadPublishedTitleEmbeddings(options: NoveltyOptions = {}): Promise<PublishedTitleEmbedding[]> {
  const limit = options.limit ?? 200;
  const explicit = options.publishedTitlesPath
    ? await readJsonFile<Array<{ title: string; embedding?: number[]; publishedAt?: string; sourceId?: string }>>(options.publishedTitlesPath, [])
    : [];
  const explicitEmbeddings: PublishedTitleEmbedding[] = [];
  for (const item of explicit.slice(-limit)) {
    const title = normalizeWhitespace(item.title);
    if (!title) continue;
    explicitEmbeddings.push({
      title,
      embedding: item.embedding?.length ? item.embedding : await embedText(title, options.embedding),
      publishedAt: item.publishedAt,
      sourceId: item.sourceId,
    });
  }
  if (explicitEmbeddings.length) return explicitEmbeddings.slice(-limit);

  const queuePath = options.queuePath ?? "out/automation/candidates.json";
  const queue = await readJsonFile<{ candidates?: Array<Record<string, unknown>> }>(queuePath, { candidates: [] });
  const published = (queue.candidates ?? [])
    .filter((candidate) => candidate.status === "published")
    .sort((a, b) => normalizeWhitespace(a.updated_at).localeCompare(normalizeWhitespace(b.updated_at)))
    .slice(-limit);
  const result: PublishedTitleEmbedding[] = [];
  for (const candidate of published) {
    const title = candidateTitle(candidate);
    if (!title) continue;
    result.push({
      title,
      embedding: await embedText(title, options.embedding),
      publishedAt: normalizeWhitespace(candidate.updated_at),
      sourceId: normalizeWhitespace(candidate.id),
    });
  }
  return result;
}

export async function scoreNovelty(
  item: SourceItem,
  published: PublishedTitleEmbedding[],
  options: NoveltyOptions = {},
): Promise<{ novelty: number; similarity: number }> {
  if (!published.length) return { novelty: 1, similarity: 0 };
  const embedding = await embedText(item.title, options.embedding);
  let maxSimilarity = 0;
  for (const previous of published) {
    maxSimilarity = Math.max(maxSimilarity, cosineSimilarity(embedding, previous.embedding));
  }
  return {
    novelty: Number(Math.max(0, 1 - maxSimilarity).toFixed(4)),
    similarity: Number(maxSimilarity.toFixed(4)),
  };
}
