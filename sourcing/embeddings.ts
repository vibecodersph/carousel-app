import { sha256 } from "./utils.ts";

export interface EmbeddingOptions {
  provider?: "local" | "openai";
  dimensions?: number;
  model?: string;
}

function tokensForEmbedding(text: string): string[] {
  const lowered = text.toLowerCase();
  const words = lowered.match(/[\p{L}\p{N}]+/gu) ?? [];
  const compact = lowered.replace(/\s+/g, "");
  const grams: string[] = [];
  for (let i = 0; i < compact.length - 2; i += 1) {
    grams.push(compact.slice(i, i + 3));
  }
  return [...words, ...grams];
}

export function localEmbedding(text: string, dimensions = 384): number[] {
  const vector = new Array<number>(dimensions).fill(0);
  const tokens = tokensForEmbedding(text);
  if (!tokens.length) return vector;
  for (const token of tokens) {
    const digest = sha256(token);
    const index = Number.parseInt(digest.slice(0, 8), 16) % dimensions;
    const sign = Number.parseInt(digest.slice(8, 10), 16) % 2 === 0 ? 1 : -1;
    vector[index] += sign;
  }
  const magnitude = Math.sqrt(vector.reduce((sum, value) => sum + value * value, 0)) || 1;
  return vector.map((value) => Number((value / magnitude).toFixed(6)));
}

async function openAiEmbeddings(texts: string[], options: EmbeddingOptions): Promise<number[][]> {
  const apiKey = process.env.OPENAI_API_KEY;
  if (!apiKey) {
    throw new Error("OPENAI_API_KEY is required for EMBEDDING_PROVIDER=openai");
  }
  const response = await fetch("https://api.openai.com/v1/embeddings", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${apiKey}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      model: options.model ?? process.env.OPENAI_EMBEDDING_MODEL ?? "text-embedding-3-small",
      input: texts,
    }),
  });
  if (!response.ok) {
    throw new Error(`OpenAI embeddings returned HTTP ${response.status}: ${await response.text()}`);
  }
  const data = await response.json() as { data?: Array<{ embedding?: number[] }> };
  const embeddings = data.data?.map((item) => item.embedding ?? []) ?? [];
  if (embeddings.length !== texts.length) {
    throw new Error(`OpenAI embeddings returned ${embeddings.length} vectors for ${texts.length} inputs`);
  }
  return embeddings;
}

export async function embedTexts(texts: string[], options: EmbeddingOptions = {}): Promise<number[][]> {
  const provider = options.provider ?? (process.env.EMBEDDING_PROVIDER === "openai" ? "openai" : "local");
  if (provider === "openai") return openAiEmbeddings(texts, options);
  return texts.map((text) => localEmbedding(text, options.dimensions));
}

export async function embedText(text: string, options: EmbeddingOptions = {}): Promise<number[]> {
  return (await embedTexts([text], options))[0];
}
