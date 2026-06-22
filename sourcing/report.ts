import type { SourceItem } from "./types.ts";

export interface SourceRunIssue {
  source: string;
  code: string;
  message: string;
}

export interface SourceRunSummaryInput {
  rawItems: SourceItem[];
  outputItems: SourceItem[];
  droppedCount: number;
  mediaFailureCount: number;
  minItems: number;
  issues?: SourceRunIssue[];
}

export interface SourceRunSummary {
  rawCount: number;
  outputCount: number;
  droppedCount: number;
  mediaFailureCount: number;
  bySource: Record<string, number>;
  videos: number;
  withMetrics: number;
  acceptance: {
    minItems: number;
    ok: boolean;
    reasons: string[];
  };
  issues: SourceRunIssue[];
}

function hasMetrics(item: SourceItem): boolean {
  return Object.values(item.metrics).some((value) => typeof value === "number" && Number.isFinite(value));
}

export function summarizeSourceRun(input: SourceRunSummaryInput): SourceRunSummary {
  const bySource: Record<string, number> = {};
  for (const item of input.outputItems) {
    bySource[item.source] = (bySource[item.source] ?? 0) + 1;
  }
  const videos = input.outputItems.filter((item) => item.media.hasVideo).length;
  const withMetrics = input.outputItems.filter(hasMetrics).length;
  const reasons: string[] = [];
  if (input.outputItems.length < input.minItems) {
    reasons.push(`only ${input.outputItems.length} item(s), below minimum ${input.minItems}`);
  }
  if (withMetrics !== input.outputItems.length) {
    reasons.push(`${input.outputItems.length - withMetrics} item(s) missing numeric metrics`);
  }
  const videoWithoutLocalPath = input.outputItems.filter((item) => item.media.hasVideo && !item.media.localPath).length;
  if (videoWithoutLocalPath && input.mediaFailureCount) {
    reasons.push(`${videoWithoutLocalPath} video item(s) missing localPath`);
  }
  const blockingIssues = (input.issues ?? []).filter((issue) => issue.code === "source_unavailable");
  if (blockingIssues.length) {
    reasons.push(`${blockingIssues.length} source listing(s) unavailable`);
  }
  return {
    rawCount: input.rawItems.length,
    outputCount: input.outputItems.length,
    droppedCount: input.droppedCount,
    mediaFailureCount: input.mediaFailureCount,
    bySource,
    videos,
    withMetrics,
    acceptance: {
      minItems: input.minItems,
      ok: reasons.length === 0,
      reasons,
    },
    issues: input.issues ?? [],
  };
}
