import { spawn } from "node:child_process";
import { mkdir, readdir } from "node:fs/promises";
import { join } from "node:path";
import type { SourceItem } from "./types.ts";
import { mapLimit } from "./utils.ts";

export interface FetchMediaOptions {
  outDir?: string;
  maxDurationSeconds?: number;
  maxHeight?: number;
  concurrency?: number;
  cookiesFromBrowser?: string;
}

function safeFileStem(value: string): string {
  return value.replace(/[^a-zA-Z0-9_-]+/g, "_").slice(0, 80);
}

function runYtDlp(args: string[], timeoutMs: number): Promise<string> {
  return new Promise((resolve, reject) => {
    const child = spawn("uv", ["run", "yt-dlp", ...args], { stdio: ["ignore", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    const timeout = setTimeout(() => {
      child.kill("SIGTERM");
      reject(new Error(`yt-dlp timed out after ${timeoutMs}ms`));
    }, timeoutMs);
    child.stdout.on("data", (chunk) => {
      stdout += chunk.toString();
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk.toString();
    });
    child.on("error", (error) => {
      clearTimeout(timeout);
      reject(error);
    });
    child.on("close", (code) => {
      clearTimeout(timeout);
      if (code === 0) resolve(stdout);
      else reject(new Error(`yt-dlp exited ${code}: ${stderr || stdout}`));
    });
  });
}

async function newestDownloadedPath(outDir: string, stem: string): Promise<string> {
  const entries = await readdir(outDir, { withFileTypes: true });
  const matches = entries
    .filter((entry) => entry.isFile() && entry.name.startsWith(stem) && /\.(mp4|mov|m4v)$/i.test(entry.name))
    .map((entry) => join(outDir, entry.name))
    .sort();
  return matches.at(-1) ?? "";
}

export async function fetchMediaForItem(item: SourceItem, options: FetchMediaOptions = {}): Promise<SourceItem> {
  if (!item.media.hasVideo) return item;

  const outDir = options.outDir ?? "out/automation/source_media";
  await mkdir(outDir, { recursive: true });
  const stem = safeFileStem(item.id.slice(0, 24));
  const outputTemplate = join(outDir, `${stem}.%(ext)s`);
  const maxHeight = options.maxHeight ?? 720;
  const maxDuration = options.maxDurationSeconds ?? 90;
  const format = [
    `bestvideo[ext=mp4][height<=${maxHeight}]+bestaudio[ext=m4a]`,
    `bestvideo[height<=${maxHeight}]+bestaudio`,
    `best[ext=mp4][height<=${maxHeight}]`,
    `best[height<=${maxHeight}]`,
  ].join("/");

  const args = [
    "--no-playlist",
    "--merge-output-format",
    "mp4",
    "--remux-video",
    "mp4",
    "--match-filter",
    `duration <= ${maxDuration}`,
    "-f",
    format,
    "-o",
    outputTemplate,
    "--print",
    "after_move:filepath",
  ];
  if (options.cookiesFromBrowser) {
    args.push("--cookies-from-browser", options.cookiesFromBrowser);
  }
  args.push(item.url);

  const stdout = await runYtDlp(args, Math.max(120_000, maxDuration * 4_000));
  const printedPath = stdout
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .at(-1);
  const localPath = printedPath || await newestDownloadedPath(outDir, stem);
  if (!localPath) {
    throw new Error(`yt-dlp finished but no local media path was found for ${item.url}`);
  }
  return {
    ...item,
    media: {
      ...item.media,
      localPath,
    },
  };
}

export async function fetchMediaForItems(items: SourceItem[], options: FetchMediaOptions = {}): Promise<SourceItem[]> {
  return mapLimit(items, options.concurrency ?? 2, async (item) => {
    if (!item.media.hasVideo) return item;
    try {
      return await fetchMediaForItem(item, options);
    } catch (error) {
      return {
        ...item,
        media: {
          ...item.media,
          raw: {
            ...(typeof item.media.raw === "object" && item.media.raw ? item.media.raw as Record<string, unknown> : {}),
            downloadError: (error as Error).message,
          },
        },
      };
    }
  });
}
