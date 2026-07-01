import type { SourceItem } from "../../sourcing/types.ts";
import { fetchJson, normalizeWhitespace, numberValue, stableSourceItemId } from "../../sourcing/utils.ts";

export interface GitHubRepository {
  id?: number;
  full_name?: string;
  name?: string;
  html_url?: string;
  description?: string | null;
  language?: string | null;
  stargazers_count?: number;
  forks_count?: number;
  open_issues_count?: number;
  watchers_count?: number;
  created_at?: string;
  updated_at?: string;
  pushed_at?: string;
  topics?: string[];
  owner?: {
    login?: string;
  };
}

export interface GitHubCollectorOptions {
  queries: string[];
  perQueryLimit?: number;
  maxItems?: number;
}

function githubHeaders(): Record<string, string> {
  const headers: Record<string, string> = {
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2026-03-10",
    "User-Agent": "carousel-app-research-idea-generator/0.1",
  };
  if (process.env.GITHUB_TOKEN) {
    headers.Authorization = `Bearer ${process.env.GITHUB_TOKEN}`;
  }
  return headers;
}

export function githubRepoToSourceItem(repo: GitHubRepository, query = ""): SourceItem | null {
  const externalId = normalizeWhitespace(repo.id ?? repo.full_name);
  const fullName = normalizeWhitespace(repo.full_name ?? repo.name);
  const url = normalizeWhitespace(repo.html_url);
  if (!externalId || !fullName || !url) return null;

  const stars = numberValue(repo.stargazers_count);
  const forks = numberValue(repo.forks_count);
  const openIssues = numberValue(repo.open_issues_count);
  const pushedAt = normalizeWhitespace(repo.pushed_at ?? repo.updated_at ?? repo.created_at);
  const topics = Array.isArray(repo.topics) ? repo.topics.filter(Boolean) : [];
  const description = normalizeWhitespace(repo.description);
  const language = normalizeWhitespace(repo.language);
  const body = normalizeWhitespace([
    description,
    language ? `Language: ${language}` : "",
    topics.length ? `Topics: ${topics.join(", ")}` : "",
  ].filter(Boolean).join("\n"));

  return {
    id: stableSourceItemId("github", externalId),
    source: "github",
    externalId,
    url,
    title: fullName,
    body,
    author: normalizeWhitespace(repo.owner?.login),
    createdAt: pushedAt || new Date(0).toISOString(),
    metrics: {
      upvotes: stars,
      score: stars + forks * 2,
      comments: openIssues,
    },
    media: { hasVideo: false },
    raw: {
      query,
      stars,
      forks,
      openIssues,
      watchers: numberValue(repo.watchers_count),
      pushedAt,
      repository: repo,
    },
  };
}

export async function fetchGitHubSourceItems(options: GitHubCollectorOptions): Promise<SourceItem[]> {
  const perQueryLimit = Math.min(Math.max(1, options.perQueryLimit ?? 12), 100);
  const byId = new Map<string, SourceItem>();
  for (const query of options.queries) {
    const params = new URLSearchParams({
      q: query,
      sort: "updated",
      order: "desc",
      per_page: String(perQueryLimit),
    });
    const json = await fetchJson(`https://api.github.com/search/repositories?${params}`, {
      timeoutMs: 25_000,
      headers: githubHeaders(),
    }) as { items?: GitHubRepository[] };
    for (const repo of json.items ?? []) {
      const item = githubRepoToSourceItem(repo, query);
      if (!item) continue;
      const previous = byId.get(item.id);
      if (!previous || (item.metrics.score ?? 0) > (previous.metrics.score ?? 0)) {
        byId.set(item.id, item);
      }
    }
  }
  return [...byId.values()]
    .sort((a, b) => (b.metrics.score ?? 0) - (a.metrics.score ?? 0))
    .slice(0, options.maxItems ?? 80);
}
