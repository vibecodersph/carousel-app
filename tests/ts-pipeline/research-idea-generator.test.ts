import assert from "node:assert/strict";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import type { SourceItem } from "../../sourcing/types.ts";
import { stableSourceItemId } from "../../sourcing/utils.ts";
import {
  clearRedditTokenCacheForTests,
  fetchRedditSourceItems,
  redditListingUrl,
  redditRssFeedToSourceItems,
  redditRssListingUrl,
} from "../../sourcing/connectors/reddit.ts";
import { clusterSourceItems } from "../../research_idea_generator/cluster.ts";
import { insightCardToCarouselBrief } from "../../research_idea_generator/carouselBriefs.ts";
import { parseArgs, main as researchCliMain } from "../../research_idea_generator/cli.ts";
import { githubRepoToSourceItem } from "../../research_idea_generator/sources/github.ts";
import { hackerNewsHitToSourceItem } from "../../research_idea_generator/sources/hackerNews.ts";
import { dedupeResearchItems } from "../../research_idea_generator/sources/index.ts";
import { buildResearchQueries } from "../../research_idea_generator/taxonomy.ts";
import { scoreResearchCluster } from "../../research_idea_generator/scoring.ts";
import { evidenceFromCluster, generateInsightCards, selectClustersForInsightCards } from "../../research_idea_generator/generator.ts";
import { assessHookRisk, generateHookVariants, optimizeHookText } from "../../research_idea_generator/hooks.ts";
import { filterSeenSourceItems, type ResearchMemory } from "../../research_idea_generator/memory.ts";
import { collectRedditSourceQueue, readUnreadRedditSources } from "../../research_idea_generator/redditQueue.ts";
import type { InsightCard, ResearchCluster, ScoredResearchCluster, TaxonomyConfig } from "../../research_idea_generator/types.ts";

function item(overrides: Partial<SourceItem> & { source: string; externalId: string; title: string }): SourceItem {
  return {
    id: stableSourceItemId(overrides.source, overrides.externalId),
    source: overrides.source,
    externalId: overrides.externalId,
    url: overrides.url ?? `https://example.com/${overrides.externalId}`,
    title: overrides.title,
    body: overrides.body ?? "",
    author: overrides.author ?? "tester",
    createdAt: overrides.createdAt ?? "2026-06-21T00:00:00.000Z",
    subreddit: overrides.subreddit,
    metrics: overrides.metrics ?? { upvotes: 100, comments: 10, score: 100 },
    media: overrides.media ?? { hasVideo: false },
    topReply: overrides.topReply,
    raw: overrides.raw,
  };
}

function scoredCluster(
  id: string,
  overall: number,
  options: {
    confidence?: ScoredResearchCluster["confidence"];
    crossSourceConfirmation?: number;
    itemCount?: number;
    sourceCount?: number;
  } = {},
): ScoredResearchCluster {
  const sourceNames = ["reddit", "github", "hacker_news"].slice(0, options.sourceCount ?? 1);
  const count = options.itemCount ?? sourceNames.length;
  return {
    id,
    label: `${id} builder pattern`,
    keywords: [id, "ai", "workflow"],
    items: Array.from({ length: count }, (_, index) => item({
      source: sourceNames[index % sourceNames.length],
      externalId: `${id}_${index}`,
      title: `${id} research item ${index}`,
      body: "AI builder workflow with practical implementation details.",
    })),
    scores: {
      overall,
      engagementVelocity: overall,
      sourceQuality: overall,
      novelty: overall,
      practicalUtility: overall,
      controversyOrTension: 0.2,
      crossSourceConfirmation: options.crossSourceConfirmation ?? 0.1,
      audienceFit: overall,
      hookability: overall,
    },
    confidence: options.confidence ?? "medium",
  };
}

test("GitHub and Hacker News fixtures map into SourceItems", () => {
  const repo = githubRepoToSourceItem({
    id: 123,
    full_name: "builder/llm-router",
    html_url: "https://github.com/builder/llm-router",
    description: "Route LLM requests by cost, latency, and fallback provider.",
    language: "TypeScript",
    stargazers_count: 1200,
    forks_count: 80,
    open_issues_count: 12,
    pushed_at: "2026-06-20T12:00:00Z",
    owner: { login: "builder" },
    topics: ["llm", "routing", "ai"],
  }, "llm router");
  assert.ok(repo);
  assert.equal(repo.source, "github");
  assert.equal(repo.externalId, "123");
  assert.equal(repo.metrics.upvotes, 1200);
  assert.equal(repo.metrics.score, 1360);
  assert.match(repo.body, /Route LLM requests/);

  const hn = hackerNewsHitToSourceItem({
    objectID: "456",
    title: "Ask HN: Are LLM routers worth it in production?",
    author: "hn_user",
    points: 88,
    num_comments: 41,
    created_at_i: 1782000000,
  }, "llm router");
  assert.ok(hn);
  assert.equal(hn.source, "hacker_news");
  assert.equal(hn.url, "https://news.ycombinator.com/item?id=456");
  assert.equal(hn.metrics.comments, 41);
});

test("taxonomy query generation expands AI-builder seeds with recency", () => {
  const taxonomy: TaxonomyConfig = {
    audience: "ai_builders",
    redditSubreddits: ["LocalLLaMA", "ChatGPTCoding", "LocalLLaMA"],
    hookStyles: ["list", "contrarian", "curiosity"],
    topics: [
      {
        id: "routing",
        label: "Model routing",
        keywords: ["llm router"],
        githubQueries: ["llm router pushed:>={sinceDate} fork:false"],
        hnQueries: ["llm router", "cheap llm api"],
      },
    ],
  };
  const queries = buildResearchQueries(taxonomy, {
    days: 7,
    now: new Date("2026-07-01T00:00:00.000Z"),
  });
  assert.deepEqual(queries.redditSubreddits, ["LocalLLaMA", "ChatGPTCoding"]);
  assert.equal(queries.github[0], "llm router pushed:>=2026-06-24 fork:false");
  assert.deepEqual(queries.hn, ["llm router", "cheap llm api"]);
});

test("research dedupe and clustering merge related Reddit, GitHub, and HN items", async () => {
  const reddit = item({
    source: "reddit",
    externalId: "t3_router",
    title: "Developers are using LLM routers to cut AI API costs",
    body: "People are routing cheap tasks to cheaper models and using fallbacks for reliability.",
    subreddit: "LocalLLaMA",
  });
  const duplicate = item({
    source: "reddit",
    externalId: "t3_router_copy",
    title: "Developers are using LLM routers to cut AI API costs",
    url: reddit.url,
    body: "Duplicate URL should not survive.",
    subreddit: "LocalLLaMA",
    metrics: { upvotes: 500, comments: 40, score: 500 },
  });
  const github = item({
    source: "github",
    externalId: "repo_router",
    title: "builder/llm-router",
    body: "Open source model routing proxy for LLM APIs, fallback providers, and cost controls.",
    metrics: { upvotes: 900, comments: 22, score: 940 },
  });
  const hn = item({
    source: "hacker_news",
    externalId: "hn_router",
    title: "Ask HN: model routing to reduce LLM API cost",
    body: "Teams are comparing OpenRouter, LiteLLM, and provider fallbacks.",
    metrics: { upvotes: 150, comments: 65, score: 150 },
  });

  const deduped = dedupeResearchItems([reddit, duplicate, github, hn]);
  assert.equal(deduped.length, 3);
  assert.ok(deduped.some((entry) => entry.externalId === "t3_router_copy"));
  assert.ok(!deduped.some((entry) => entry.externalId === "t3_router"));
  const clusters = await clusterSourceItems(deduped, { similarityThreshold: 0.24 });
  const routerCluster = clusters.find((cluster) => cluster.items.length === 3);
  assert.ok(routerCluster);
  assert.deepEqual(new Set(routerCluster.items.map((entry) => entry.source)), new Set(["reddit", "github", "hacker_news"]));
});

test("multi-source clusters receive stronger source spread and confidence", () => {
  const now = new Date("2026-06-21T02:00:00.000Z");
  const single: ResearchCluster = {
    id: "single",
    label: "LLM routing cost control",
    keywords: ["llm", "routing", "cost", "api"],
    items: [
      item({
        source: "reddit",
        externalId: "single",
        title: "LLM routing cost control workflow",
        body: "A practical workflow for API fallback cost control in production.",
        metrics: { upvotes: 100, comments: 8, score: 100 },
      }),
    ],
  };
  const multi: ResearchCluster = {
    id: "multi",
    label: "LLM routing cost control",
    keywords: ["llm", "routing", "cost", "api"],
    items: [
      ...single.items,
      item({
        source: "github",
        externalId: "multi_repo",
        title: "builder/llm-router",
        body: "A production API routing proxy with fallback models.",
        metrics: { upvotes: 110, comments: 5, score: 120 },
      }),
      item({
        source: "hacker_news",
        externalId: "multi_hn",
        title: "Model routing for cheaper LLM APIs",
        body: "HN discusses cost control, latency, pricing, and reliability tradeoffs.",
        metrics: { upvotes: 90, comments: 30, score: 90 },
      }),
    ],
  };
  const singleScore = scoreResearchCluster(single, now);
  const multiScore = scoreResearchCluster(multi, now);
  assert.ok(multiScore.scores.crossSourceConfirmation > singleScore.scores.crossSourceConfirmation);
  assert.ok(multiScore.scores.overall > singleScore.scores.overall);
  assert.equal(multiScore.confidence, "high");
});

test("insight card selection is score-driven unless cards is explicitly requested", () => {
  const clusters = [
    scoredCluster("top", 0.74, {
      confidence: "high",
      crossSourceConfirmation: 1,
      itemCount: 3,
      sourceCount: 3,
    }),
    scoredCluster("supported", 0.58, {
      confidence: "medium-high",
      crossSourceConfirmation: 0.65,
      itemCount: 2,
      sourceCount: 2,
    }),
    scoredCluster("weak", 0.36, {
      confidence: "low",
      crossSourceConfirmation: 0.1,
      itemCount: 1,
      sourceCount: 1,
    }),
    scoredCluster("weaker", 0.28, {
      confidence: "low",
      crossSourceConfirmation: 0,
      itemCount: 1,
      sourceCount: 1,
    }),
  ];

  assert.deepEqual(selectClustersForInsightCards(clusters).map((cluster) => cluster.id), ["top", "supported"]);
  assert.deepEqual(selectClustersForInsightCards(clusters, 3).map((cluster) => cluster.id), ["top", "supported", "weak"]);
  assert.equal(selectClustersForInsightCards(clusters, 0).length, 0);
  assert.deepEqual(selectClustersForInsightCards([scoredCluster("watch_item", 0.3, {
    confidence: "low",
  })]).map((cluster) => cluster.id), ["watch_item"]);
});

test("hook generation flags unsupported overclaims", () => {
  const evidence = [{
    source: "Reddit",
    title: "People are discussing cheap LLM APIs",
    url: "https://example.com/reddit",
    excerpt: "Anecdotal discussion about cost concerns.",
    author: "tester",
    metrics: { score: 10 },
    timestamp: "2026-06-21T00:00:00.000Z",
  }];
  const risky = assessHookRisk("The cheapest LLM API for everyone", evidence);
  assert.equal(risky.riskLevel, "high");
  assert.equal(risky.needsFactCheck, true);

  const safe = assessHookRisk("A practical pattern for routing cheaper LLM tasks", evidence);
  assert.equal(safe.riskLevel, "low");
  assert.equal(safe.needsFactCheck, false);

  const hooks = generateHookVariants({
    title: "AI builders are converging around llm, routing, cost",
    claim: "A pattern is emerging.",
    evidence,
    count: 4,
  });
  assert.equal(hooks.length, 3);
  assert.deepEqual(hooks.map((hook) => hook.style), ["list", "contrarian", "curiosity"]);
  assert.ok(hooks.every((hook) => hook.hook && hook.whyItWorks));
  assert.ok(hooks.every((hook) => Array.isArray(hook.lines) && hook.lines.length >= 2));
  assert.ok(hooks.every((hook) => hook.hook.split(/\s+/).filter(Boolean).length <= 14));
  assert.doesNotMatch(hooks.find((hook) => hook.style === "list")?.hook ?? "", /^5 AI workflows people/i);
  assert.match(hooks.find((hook) => hook.style === "list")?.lines.join("\n") ?? "", /1\. model routing/);
  assert.match(hooks.find((hook) => hook.style === "contrarian")?.lines.join("\n") ?? "", /The better question:/);

  const longEnglish = optimizeHookText("If your AI product depends on local memory, graph workflows, model routing, sandboxing, and MCP, check the evidence before launch");
  assert.ok(longEnglish.split(/\s+/).filter(Boolean).length <= 14);

  const longJapanese = optimizeHookText("AIエージェント開発者が今すぐ確認すべきローカルメモリとMCPの実装パターン");
  assert.ok(Array.from(longJapanese).length <= 25);
});

test("carousel brief uses the selected hook as slide-ready source of truth", () => {
  const card: InsightCard = {
    id: "card_list",
    workingTitle: "AI builders are testing model routing",
    claim: "Builders are routing routine work to cheaper models and saving premium models for harder tasks.",
    evidence: [{
      source: "GitHub",
      title: "builder/llm-router",
      url: "https://github.com/builder/llm-router",
      excerpt: "Route LLM requests by cost and latency.",
      author: "builder",
      metrics: { score: 100 },
      timestamp: "2026-06-21T00:00:00.000Z",
    }],
    whyItMatters: "Routing changes the economics of AI apps before the model choice changes.",
    contentAngles: ["Model routing as a practical cost-control workflow."],
    hooks: [{
      hook: "5 AI workflows people are actually using right now",
      lines: [
        "5 AI workflows people are actually using right now",
        "1. model routing",
        "2. AI code review",
      ],
      style: "list",
      riskLevel: "low",
      whyItWorks: "Promises a compact scan of evidence.",
      needsFactCheck: false,
      bestPlatform: "X",
    }],
    scores: {
      overall: 0.8,
      engagementVelocity: 0.7,
      sourceQuality: 0.8,
      novelty: 0.5,
      practicalUtility: 0.9,
      controversyOrTension: 0.3,
      crossSourceConfirmation: 0.4,
      audienceFit: 0.9,
      hookability: 0.8,
    },
    confidence: "medium",
    risks: ["Do not claim universal adoption from one repo."],
    suggestedFormats: ["Instagram carousel"],
  };
  const brief = insightCardToCarouselBrief(card);
  assert.equal(Object.hasOwn(brief, "hookVariants"), false);
  assert.equal(Object.hasOwn(brief, "cta"), false);
  assert.equal(brief.slides[0].type, "cover");
  assert.equal(brief.slides[0].headline, brief.hook);
  assert.equal(brief.slides[1].type, "list_item");
  assert.equal(brief.slides[1].headline, "1. Model Routing");
  assert.ok(brief.slides[1].lines.length <= 2);
  assert.ok(brief.slides.every((slide) => ["cover", "list_item", "hook_detail"].includes(slide.type)));
  assert.match(brief.instagramDescription, /Evidence base:/);
  assert.match(brief.instagramDescription, /Publish note:/);
});

test("contrarian carousel details get their own slides after the cover", () => {
  const card: InsightCard = {
    id: "card_contrarian",
    workingTitle: "AI builders are testing model routing",
    claim: "Builders are routing routine work to cheaper models and saving premium models for harder tasks.",
    evidence: [{
      source: "GitHub",
      title: "builder/llm-router",
      url: "https://github.com/builder/llm-router",
      excerpt: "Route LLM requests by cost and latency.",
      author: "builder",
      metrics: { score: 100 },
      timestamp: "2026-06-21T00:00:00.000Z",
    }],
    whyItMatters: "Routing changes the economics of AI apps before the model choice changes.",
    contentAngles: ["Model routing as a practical cost-control workflow."],
    hooks: [{
      hook: "Which AI API is cheapest is the wrong question",
      lines: [
        "Which AI API is cheapest is the wrong question",
        '"Cheapest AI API" is the wrong question.',
        "The better question:",
        "which tasks deserve expensive tokens?",
      ],
      style: "contrarian",
      riskLevel: "low",
      whyItWorks: "Reframes the cost question around task routing.",
      needsFactCheck: false,
      bestPlatform: "X",
    }],
    scores: {
      overall: 0.8,
      engagementVelocity: 0.7,
      sourceQuality: 0.8,
      novelty: 0.5,
      practicalUtility: 0.9,
      controversyOrTension: 0.3,
      crossSourceConfirmation: 0.4,
      audienceFit: 0.9,
      hookability: 0.8,
    },
    confidence: "medium",
    risks: ["Do not claim universal adoption from one repo."],
    suggestedFormats: ["Instagram carousel"],
  };
  const brief = insightCardToCarouselBrief(card);
  assert.equal(brief.slideCount, 3);
  assert.equal(brief.slides[0].type, "cover");
  assert.equal(brief.slides[1].headline, '"Cheapest AI API" is the wrong question.');
  assert.equal(brief.slides[2].headline, "The better question: which tasks deserve expensive tokens?");
  assert.ok(brief.slides.slice(1).every((slide) => slide.type === "hook_detail" && slide.lines.length === 0));
});

test("Gemini provider generates evidence-grounded hooks with local guardrails", async () => {
  const cluster = scoreResearchCluster({
    id: "gemini_hooks",
    label: "model routing for AI API cost control",
    keywords: ["model", "routing", "cost"],
    items: [
      item({
        source: "github",
        externalId: "gemini_repo",
        title: "llm-router routes prompts by price and latency",
        body: "Model routing gateway for cheaper routine tasks and premium fallback calls.",
        metrics: { upvotes: 200, comments: 8, score: 200 },
      }),
      item({
        source: "hacker_news",
        externalId: "gemini_hn",
        title: "Ask HN: How do you route LLM calls by cost?",
        body: "Builders discuss sending summarization to cheaper models and reserving frontier models.",
        metrics: { upvotes: 90, comments: 42, score: 90 },
      }),
    ],
  }, new Date("2026-07-01T00:00:00.000Z"));
  const originalFetch = globalThis.fetch;
  const originalKey = process.env.GEMINI_API_KEY;
  process.env.GEMINI_API_KEY = "test-key";
  globalThis.fetch = (async () => new Response(JSON.stringify({
    candidates: [{
      content: {
        parts: [{
          text: JSON.stringify({
            workingTitle: "Model routers are becoming cost controls",
            claim: "Builders are routing cheap tasks away from premium models.",
            whyItMatters: "Routing gives teams a cost lever before they change product scope.",
            contentAngles: ["How to split tasks by model cost and failure risk."],
            hooks: [
              {
                style: "list",
                hook: "3 routing decisions that lower AI bills",
                lines: [
                  "3 routing decisions that lower AI bills",
                  "1. send summaries to cheaper models",
                  "2. reserve premium models for hard calls",
                  "3. track fallback failures",
                ],
                bestPlatform: "X",
                whyItWorks: "Turns the evidence into concrete routing choices.",
              },
              {
                style: "contrarian",
                hook: "The model is not your cost strategy",
                lines: [
                  "The model is not your cost strategy",
                  "The workflow around it is.",
                ],
                bestPlatform: "LinkedIn",
                whyItWorks: "Reframes model choice as routing design.",
              },
              {
                style: "curiosity",
                hook: "The quiet AI cost lever is routing",
                lines: [
                  "The quiet AI cost lever is routing",
                  "The same app can spend differently by task.",
                ],
                bestPlatform: "X",
                whyItWorks: "Creates curiosity around a less obvious control point.",
              },
            ],
          }),
        }],
      },
    }],
  }), { status: 200, headers: { "Content-Type": "application/json" } })) as typeof fetch;
  try {
    const cards = await generateInsightCards([cluster], {
      cards: 1,
      hooksPerCard: 3,
      provider: "gemini",
      taxonomy: {
        audience: "ai_builders",
        redditSubreddits: [],
        hookStyles: ["list", "contrarian", "curiosity"],
        topics: [],
      },
    });
    assert.equal(cards.length, 1);
    assert.equal(cards[0].workingTitle, "Model routers are becoming cost controls");
    assert.deepEqual(cards[0].hooks.map((hook) => hook.style), ["list", "contrarian", "curiosity"]);
    assert.equal(cards[0].hooks[0].hook, "3 routing decisions that lower AI bills");
    assert.match(cards[0].hooks[0].lines.join("\n"), /1\. send summaries/);
    assert.ok(cards[0].hooks.every((hook) => hook.hook.split(/\s+/).filter(Boolean).length <= 14));
    assert.ok(cards[0].hooks.every((hook) => hook.needsFactCheck === false));
  } finally {
    globalThis.fetch = originalFetch;
    if (originalKey === undefined) delete process.env.GEMINI_API_KEY;
    else process.env.GEMINI_API_KEY = originalKey;
  }
});

test("offline CLI writes valid insight-card JSON and Markdown report", async () => {
  const dir = await mkdtemp(join(tmpdir(), "research-ideas-"));
  const inputPath = join(dir, "source-items.json");
  const outPath = join(dir, "insight-cards.json");
  const reportPath = join(dir, "report.md");
  const carouselPath = join(dir, "carousel-briefs.json");
  const runsDir = join(dir, "runs");
  const memoryPath = join(dir, "memory.json");
  const items = [
    item({
      source: "reddit",
      externalId: "cli_reddit",
      title: "Builders are wiring MCP servers into coding agents",
      body: "A practical workflow is emerging around agent tools, code review, and local automation.",
      subreddit: "ChatGPTCoding",
    }),
    item({
      source: "github",
      externalId: "cli_repo",
      title: "builder/mcp-workflow",
      body: "Open-source MCP workflow server for coding agents and developer automation.",
      metrics: { upvotes: 300, comments: 10, score: 320 },
    }),
    item({
      source: "hacker_news",
      externalId: "cli_hn",
      title: "MCP servers for agent workflows",
      body: "HN thread about practical coding-agent workflows and tool calling.",
      metrics: { upvotes: 70, comments: 22, score: 70 },
    }),
  ];
  await writeFile(inputPath, JSON.stringify({ items }), "utf8");

  const parsed = parseArgs([
    "run",
    "--input",
    inputPath,
    "--out",
    outPath,
    "--report",
    reportPath,
    "--carousel-out",
    carouselPath,
    "--runs-dir",
    runsDir,
    "--memory",
    memoryPath,
    "--cards",
    "1",
  ]);
  assert.equal(parsed.command, "run");
  assert.equal(parsed.options.input, inputPath);
  assert.equal(parsed.options.carouselOut, carouselPath);
  assert.equal(parsed.options.runsDir, runsDir);
  assert.equal(await researchCliMain([
    "run",
    "--input",
    inputPath,
    "--out",
    outPath,
    "--report",
    reportPath,
    "--carousel-out",
    carouselPath,
    "--runs-dir",
    runsDir,
    "--memory",
    memoryPath,
    "--cards",
    "1",
  ]), 0);

  const output = JSON.parse(await readFile(outPath, "utf8")) as { generatedAt: string; cards?: Array<Record<string, unknown>> };
  assert.equal(output.cards?.length, 1);
  const card = output.cards[0];
  assert.ok(card.workingTitle);
  assert.ok(Array.isArray(card.evidence));
  assert.ok(Array.isArray(card.hooks));
  assert.deepEqual((card.hooks as Array<{ style?: string }>).map((hook) => hook.style), ["list", "contrarian", "curiosity"]);
  assert.ok((card.hooks as Array<{ lines?: string[] }>).every((hook) => Array.isArray(hook.lines) && hook.lines.length));
  assert.ok(card.scores);
  const report = await readFile(reportPath, "utf8");
  assert.match(report, /Research Idea Generator Report/);
  assert.match(report, /Hooks/);
  const carousel = JSON.parse(await readFile(carouselPath, "utf8")) as {
    carouselCount?: number;
    carousels?: Array<{
      hook?: string;
      hookStyle?: string;
      instagramDescription?: string;
      slides?: Array<{ headline?: string; lines?: string[]; altText?: string; type?: string }>;
      evidenceSourceItemIds?: string[];
      evidenceUrls?: string[];
    }>;
  };
  assert.equal(carousel.carouselCount, 1);
  assert.ok(carousel.carousels?.[0].hook);
  assert.match(carousel.carousels?.[0].hookStyle ?? "", /^(list|contrarian|curiosity)$/);
  assert.equal(Object.hasOwn(carousel.carousels?.[0] ?? {}, "hookVariants"), false);
  assert.equal(Object.hasOwn(carousel.carousels?.[0] ?? {}, "cta"), false);
  assert.ok((carousel.carousels?.[0].hook ?? "").split(/\s+/).filter(Boolean).length <= 14);
  assert.equal(carousel.carousels?.[0].slides?.[0].type, "cover");
  assert.equal(carousel.carousels?.[0].slides?.[0].headline, carousel.carousels?.[0].hook);
  assert.ok(carousel.carousels?.[0].slides?.every((slide) => slide.type === "cover" || slide.type === "list_item" || slide.type === "hook_detail"));
  if (carousel.carousels?.[0].hookStyle === "list") {
    assert.equal(carousel.carousels?.[0].slides?.[1].type, "list_item");
    assert.match(carousel.carousels?.[0].slides?.[1].headline ?? "", /^1\. /);
    assert.ok((carousel.carousels?.[0].slides?.[1].lines?.length ?? 0) <= 2);
  }
  assert.match(carousel.carousels?.[0].instagramDescription ?? "", /Evidence base:/);
  assert.match(carousel.carousels?.[0].instagramDescription ?? "", /Publish note:/);
  assert.match(carousel.carousels?.[0].instagramDescription ?? "", /#AIbuilders/);
  assert.ok(carousel.carousels?.[0].evidenceSourceItemIds?.length);
  assert.ok(carousel.carousels?.[0].evidenceUrls?.length);
  assert.ok(carousel.carousels?.[0].slides?.every((slide) => Array.isArray(slide.lines) && slide.altText));
  for (const slide of carousel.carousels?.[0].slides ?? []) {
    for (const value of [slide.headline, ...(slide.lines ?? []), slide.altText]) {
      assert.doesNotMatch(value ?? "", /(?:\.\.\.|…)$/);
    }
  }
  const regeneratedCarouselPath = join(dir, "regenerated-carousel-briefs.json");
  const briefsParsed = parseArgs([
    "briefs",
    "--input",
    outPath,
    "--out",
    regeneratedCarouselPath,
  ]);
  assert.equal(briefsParsed.command, "briefs");
  assert.equal(briefsParsed.options.input, outPath);
  assert.equal(briefsParsed.options.out, regeneratedCarouselPath);
  assert.equal(await researchCliMain([
    "briefs",
    "--input",
    outPath,
    "--out",
    regeneratedCarouselPath,
  ]), 0);
  const regeneratedCarousel = JSON.parse(await readFile(regeneratedCarouselPath, "utf8")) as {
    carouselCount?: number;
    carousels?: Array<{
      hook?: string;
      hookStyle?: string;
      instagramDescription?: string;
      slides?: Array<{ headline?: string; lines?: string[]; altText?: string; type?: string }>;
    }>;
  };
  assert.equal(regeneratedCarousel.carouselCount, 1);
  assert.ok(regeneratedCarousel.carousels?.[0].hook);
  assert.match(regeneratedCarousel.carousels?.[0].hookStyle ?? "", /^(list|contrarian|curiosity)$/);
  assert.equal(Object.hasOwn(regeneratedCarousel.carousels?.[0] ?? {}, "hookVariants"), false);
  assert.equal(Object.hasOwn(regeneratedCarousel.carousels?.[0] ?? {}, "cta"), false);
  assert.ok(regeneratedCarousel.carousels?.[0].slides?.every((slide) => slide.type === "cover" || slide.type === "list_item" || slide.type === "hook_detail"));
  if (regeneratedCarousel.carousels?.[0].hookStyle === "list") {
    assert.equal(regeneratedCarousel.carousels?.[0].slides?.[1].type, "list_item");
    assert.match(regeneratedCarousel.carousels?.[0].slides?.[1].headline ?? "", /^1\. /);
    assert.ok((regeneratedCarousel.carousels?.[0].slides?.[1].lines?.length ?? 0) <= 2);
  }
  assert.ok((regeneratedCarousel.carousels?.[0].hook ?? "").split(/\s+/).filter(Boolean).length <= 14);
  assert.match(regeneratedCarousel.carousels?.[0].instagramDescription ?? "", /Evidence base:/);
  assert.match(regeneratedCarousel.carousels?.[0].instagramDescription ?? "", /Publish note:/);
  assert.ok(regeneratedCarousel.carousels?.[0].slides?.every((slide) => Array.isArray(slide.lines) && slide.altText));
  for (const slide of regeneratedCarousel.carousels?.[0].slides ?? []) {
    for (const value of [slide.headline, ...(slide.lines ?? []), slide.altText]) {
      assert.doesNotMatch(value ?? "", /(?:\.\.\.|…)$/);
    }
  }
  const rehookedOutPath = join(dir, "rehooked-insight-cards.json");
  const rehookedReportPath = join(dir, "rehooked-report.md");
  const rehookedCarouselPath = join(dir, "rehooked-carousel-briefs.json");
  assert.equal(await researchCliMain([
    "hooks",
    "--input",
    outPath,
    "--out",
    rehookedOutPath,
    "--report",
    rehookedReportPath,
    "--carousel-out",
    rehookedCarouselPath,
  ]), 0);
  const rehooked = JSON.parse(await readFile(rehookedOutPath, "utf8")) as {
    cards?: Array<{ hooks?: Array<{ hook?: string; lines?: string[] }> }>;
  };
  assert.ok(rehooked.cards?.[0].hooks?.every((hook) => hook.hook && hook.lines?.length));
  assert.equal(rehooked.cards?.[0].hooks?.length, 3);
  assert.ok(rehooked.cards?.[0].hooks?.every((hook) => (hook.hook ?? "").split(/\s+/).filter(Boolean).length <= 14));
  const rehookedReport = await readFile(rehookedReportPath, "utf8");
  assert.match(rehookedReport, /The better question:/);
  const runDir = join(runsDir, output.generatedAt.replace(/[:.]/g, "-"));
  const manifest = JSON.parse(await readFile(join(runDir, "manifest.json"), "utf8")) as { files?: string[]; counts?: Record<string, number> };
  assert.ok(manifest.files?.includes("insight_cards.json"));
  assert.ok(manifest.files?.includes("carousel_briefs.json"));
  assert.ok(manifest.files?.includes("source_items.json"));
  assert.equal(manifest.counts?.insightCards, 1);
  assert.equal(manifest.counts?.carouselBriefs, 1);
  const archivedSources = JSON.parse(await readFile(join(runDir, "source_items.json"), "utf8")) as { count?: number };
  assert.equal(archivedSources.count, 3);
  const archivedClusters = JSON.parse(await readFile(join(runDir, "clusters.json"), "utf8")) as { count?: number };
  assert.equal(archivedClusters.count, 1);

  const secondOutPath = join(dir, "second-insight-cards.json");
  const secondReportPath = join(dir, "second-report.md");
  const secondCarouselPath = join(dir, "second-carousel-briefs.json");
  assert.equal(await researchCliMain([
    "run",
    "--input",
    inputPath,
    "--out",
    secondOutPath,
    "--report",
    secondReportPath,
    "--carousel-out",
    secondCarouselPath,
    "--runs-dir",
    runsDir,
    "--memory",
    memoryPath,
    "--cards",
    "1",
  ]), 0);
  const secondOutput = JSON.parse(await readFile(secondOutPath, "utf8")) as { cards?: Array<Record<string, unknown>>; sourceCount?: number };
  assert.equal(secondOutput.sourceCount, 0);
  assert.equal(secondOutput.cards?.length, 0);

  const oldEvidence = evidenceFromCluster(scoreResearchCluster({
    id: "check",
    label: "separate old engine check",
    keywords: ["check"],
    items: items.slice(0, 1),
  }));
  assert.equal(oldEvidence[0].source, "Reddit r/ChatGPTCoding");
});

test("source-aware memory cools down exact evidence by source", () => {
  const now = new Date("2026-07-01T00:00:00.000Z");
  const github = item({
    source: "github",
    externalId: "repo_memory",
    title: "builder/agent-router",
    url: "https://github.com/builder/agent-router",
    metrics: { upvotes: 120, comments: 5, score: 120 },
  });
  const reddit = item({
    source: "reddit",
    externalId: "t3_memory",
    title: "Agent routing discussion",
    url: "https://www.reddit.com/r/LocalLLaMA/comments/memory",
    subreddit: "LocalLLaMA",
    metrics: { upvotes: 50, comments: 10, score: 50 },
  });
  const memory: ResearchMemory = {
    version: 1,
    updatedAt: "2026-06-30T00:00:00.000Z",
    sourceItems: {
      [github.id]: {
        id: github.id,
        source: "github",
        externalId: github.externalId,
        url: github.url,
        title: github.title,
        firstSeenAt: "2026-06-30T00:00:00.000Z",
        lastSeenAt: "2026-06-30T00:00:00.000Z",
        lastUsedAt: "2026-06-30T00:00:00.000Z",
        usedCount: 1,
        lastScore: 100,
        cardIds: ["card-1"],
      },
      [reddit.id]: {
        id: reddit.id,
        source: "reddit",
        externalId: reddit.externalId,
        url: reddit.url,
        title: reddit.title,
        firstSeenAt: "2026-06-30T00:00:00.000Z",
        lastSeenAt: "2026-06-30T00:00:00.000Z",
        lastUsedAt: "2026-06-30T00:00:00.000Z",
        usedCount: 1,
        lastScore: 50,
        cardIds: ["card-1"],
      },
    },
    urls: {
      [github.url]: github.id,
      [reddit.url]: reddit.id,
    },
    insightCards: {},
  };

  const filtered = filterSeenSourceItems([github, reddit], memory, now);
  assert.equal(filtered.items.length, 0);
  assert.equal(filtered.skipped.length, 2);

  const grownGithub = { ...github, metrics: { ...github.metrics, score: 140, upvotes: 140 } };
  const withGrowth = filterSeenSourceItems([grownGithub, reddit], memory, now);
  assert.deepEqual(withGrowth.items.map((entry) => entry.id), [github.id]);
  assert.equal(withGrowth.skipped[0].item.id, reddit.id);
});

test("reddit OAuth mode requests tokens and uses oauth.reddit.com listings", async () => {
  const originalFetch = globalThis.fetch;
  const originalEnv = {
    REDDIT_CLIENT_ID: process.env.REDDIT_CLIENT_ID,
    REDDIT_CLIENT_SECRET: process.env.REDDIT_CLIENT_SECRET,
    REDDIT_BEARER_TOKEN: process.env.REDDIT_BEARER_TOKEN,
    REDDIT_AUTHORIZATION: process.env.REDDIT_AUTHORIZATION,
  };
  clearRedditTokenCacheForTests();
  process.env.REDDIT_CLIENT_ID = "client";
  process.env.REDDIT_CLIENT_SECRET = "secret";
  delete process.env.REDDIT_BEARER_TOKEN;
  delete process.env.REDDIT_AUTHORIZATION;
  const requested: string[] = [];
  globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit): Promise<Response> => {
    const url = String(input);
    requested.push(url);
    if (url === "https://www.reddit.com/api/v1/access_token") {
      assert.equal(init?.method, "POST");
      assert.match(String((init?.headers as Record<string, string>)?.Authorization), /^Basic /);
      return Response.json({ access_token: "token", expires_in: 3600 });
    }
    assert.match(url, /^https:\/\/oauth\.reddit\.com\/r\/LocalLLaMA\/(top|rising)\?/);
    assert.equal((init?.headers as Record<string, string>)?.Authorization, "Bearer token");
    return Response.json({
      data: {
        children: [
          {
            data: {
              name: "t3_oauth",
              id: "oauth",
              subreddit: "LocalLLaMA",
              permalink: "/r/LocalLLaMA/comments/oauth/test/",
              title: "OAuth Reddit source item",
              author: "tester",
              created_utc: 1782000000,
              ups: 10,
              score: 10,
              num_comments: 2,
            },
          },
        ],
      },
    });
  }) as typeof fetch;
  try {
    assert.equal(
      redditListingUrl("LocalLLaMA", { kind: "top", time: "day" }, 1, { oauth: true }),
      "https://oauth.reddit.com/r/LocalLLaMA/top?limit=1&raw_json=1&t=day",
    );
    const items = await fetchRedditSourceItems({
      subreddits: ["LocalLLaMA"],
      limitPerListing: 1,
      maxItems: 1,
      concurrency: 1,
      includeTopReply: false,
    });
    assert.equal(items.length, 1);
    assert.equal(items[0].source, "reddit");
    assert.ok(requested.includes("https://www.reddit.com/api/v1/access_token"));
    assert.ok(requested.some((url) => url.startsWith("https://oauth.reddit.com/r/LocalLLaMA/top?")));
  } finally {
    globalThis.fetch = originalFetch;
    clearRedditTokenCacheForTests();
    for (const [key, value] of Object.entries(originalEnv)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  }
});

test("reddit RSS parser maps Atom entries into SourceItems", () => {
  const feedUrl = "https://www.reddit.com/r/LocalLLaMA/top/.rss?limit=1&t=day";
  const items = redditRssFeedToSourceItems(`
    <?xml version="1.0" encoding="UTF-8"?>
    <feed>
      <entry>
        <id>t3_rssdemo</id>
        <title>Builders are routing local LLM tasks to cheaper models</title>
        <author><name>/u/rss_builder</name></author>
        <published>2026-07-01T01:02:03+00:00</published>
        <updated>2026-07-01T01:03:03+00:00</updated>
        <link href="https://www.reddit.com/r/LocalLLaMA/comments/rssdemo/builders_are_routing/" />
        <content type="html">&lt;p&gt;A practical RSS body about LiteLLM and fallback routing.&lt;/p&gt;</content>
      </entry>
    </feed>
  `, {
    subreddit: "LocalLLaMA",
    listing: { kind: "top", time: "day" },
    feedUrl,
  });

  assert.equal(items.length, 1);
  assert.equal(items[0].source, "reddit");
  assert.equal(items[0].externalId, "t3_rssdemo");
  assert.equal(items[0].author, "rss_builder");
  assert.equal(items[0].subreddit, "LocalLLaMA");
  assert.match(items[0].body, /LiteLLM/);
  assert.equal(items[0].metrics.score, 1);
});

test("reddit listings fall back to RSS when JSON/OAuth fails", async () => {
  const originalFetch = globalThis.fetch;
  const originalEnv = {
    REDDIT_CLIENT_ID: process.env.REDDIT_CLIENT_ID,
    REDDIT_CLIENT_SECRET: process.env.REDDIT_CLIENT_SECRET,
    REDDIT_BEARER_TOKEN: process.env.REDDIT_BEARER_TOKEN,
    REDDIT_AUTHORIZATION: process.env.REDDIT_AUTHORIZATION,
  };
  clearRedditTokenCacheForTests();
  delete process.env.REDDIT_CLIENT_ID;
  delete process.env.REDDIT_CLIENT_SECRET;
  delete process.env.REDDIT_BEARER_TOKEN;
  delete process.env.REDDIT_AUTHORIZATION;
  const requested: string[] = [];
  globalThis.fetch = (async (input: string | URL | Request): Promise<Response> => {
    const url = String(input);
    requested.push(url);
    if (url.endsWith(".json?limit=1&raw_json=1&t=day")) {
      return new Response("blocked", { status: 403 });
    }
    if (url.endsWith(".json?limit=1&raw_json=1&t=week") || url.endsWith(".json?limit=1&raw_json=1")) {
      return Response.json({ data: { children: [] } });
    }
    if (url.includes("/top/.rss?")) {
      return new Response(`
        <feed>
          <entry>
            <id>t3_rssfallback</id>
            <title>RSS fallback found a Reddit AI workflow</title>
            <author><name>/u/rss_fallback</name></author>
            <published>2026-07-01T01:02:03+00:00</published>
            <link href="https://www.reddit.com/r/LocalLLaMA/comments/rssfallback/workflow/" />
            <content type="html">RSS body for fallback parsing.</content>
          </entry>
        </feed>
      `, {
        status: 200,
        headers: { "Content-Type": "application/atom+xml" },
      });
    }
    return new Response("", { status: 404 });
  }) as typeof fetch;
  try {
    assert.equal(
      redditRssListingUrl("LocalLLaMA", { kind: "top", time: "day" }, 1),
      "https://www.reddit.com/r/LocalLLaMA/top/.rss?limit=1&t=day",
    );
    const items = await fetchRedditSourceItems({
      subreddits: ["LocalLLaMA"],
      limitPerListing: 1,
      maxItems: 1,
      concurrency: 1,
      includeTopReply: false,
    });
    assert.equal(items.length, 1);
    assert.equal(items[0].externalId, "t3_rssfallback");
    assert.ok(requested.some((url) => url.includes("/top.json?")));
    assert.ok(requested.some((url) => url.includes("/top/.rss?limit=1&t=day")));
  } finally {
    globalThis.fetch = originalFetch;
    clearRedditTokenCacheForTests();
    for (const [key, value] of Object.entries(originalEnv)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  }
});

test("standalone Reddit queue collector stores unread RSS items and reader marks them read", async () => {
  const dir = await mkdtemp(join(tmpdir(), "reddit-queue-"));
  const queuePath = join(dir, "reddit.json");
  const taxonomyPath = join(dir, "taxonomy.json");
  await writeFile(taxonomyPath, JSON.stringify({
    audience: "ai_builders",
    redditSubreddits: ["LocalLLaMA"],
    hookStyles: ["list"],
    topics: [],
  }), "utf8");
  const originalFetch = globalThis.fetch;
  const originalEnv = {
    REDDIT_CLIENT_ID: process.env.REDDIT_CLIENT_ID,
    REDDIT_CLIENT_SECRET: process.env.REDDIT_CLIENT_SECRET,
    REDDIT_BEARER_TOKEN: process.env.REDDIT_BEARER_TOKEN,
    REDDIT_AUTHORIZATION: process.env.REDDIT_AUTHORIZATION,
  };
  clearRedditTokenCacheForTests();
  delete process.env.REDDIT_CLIENT_ID;
  delete process.env.REDDIT_CLIENT_SECRET;
  delete process.env.REDDIT_BEARER_TOKEN;
  delete process.env.REDDIT_AUTHORIZATION;
  globalThis.fetch = (async (input: string | URL | Request): Promise<Response> => {
    const url = String(input);
    if (url.includes("/top/.rss?") || url.includes("/rising/.rss?")) {
      return new Response(`
        <feed>
          <entry>
            <id>t3_${url.includes("rising") ? "queue_rising" : "queue_top"}</id>
            <title>${url.includes("rising") ? "Rising" : "Top"} Reddit AI builder workflow</title>
            <author><name>/u/queue_user</name></author>
            <published>2026-07-01T01:02:03+00:00</published>
            <link href="https://www.reddit.com/r/LocalLLaMA/comments/${url.includes("rising") ? "queue_rising" : "queue_top"}/workflow/" />
            <content type="html">Queue body about local LLM workflows.</content>
          </entry>
        </feed>
      `, { status: 200 });
    }
    return new Response("", { status: 404 });
  }) as typeof fetch;
  try {
    const collected = await collectRedditSourceQueue({
      queuePath,
      taxonomyPath,
      maxSubreddits: 1,
      limitPerListing: 1,
      maxItems: 10,
      concurrency: 1,
      rssDelayMs: 0,
      now: new Date("2026-07-01T02:00:00.000Z"),
    });
    assert.equal(collected.added, 2);
    assert.equal(collected.unread, 2);

    const peeked = await readUnreadRedditSources({ queuePath, limit: 1, markRead: false });
    assert.equal(peeked.length, 1);
    let queue = JSON.parse(await readFile(queuePath, "utf8")) as { items: unknown[]; readIds: unknown[] };
    assert.equal(queue.items.length, 2);
    assert.equal(queue.readIds.length, 0);

    const read = await readUnreadRedditSources({ queuePath, limit: 1 });
    assert.equal(read.length, 1);
    queue = JSON.parse(await readFile(queuePath, "utf8")) as { items: unknown[]; readIds: unknown[] };
    assert.equal(queue.items.length, 1);
    assert.equal(queue.readIds.length, 1);
  } finally {
    globalThis.fetch = originalFetch;
    clearRedditTokenCacheForTests();
    for (const [key, value] of Object.entries(originalEnv)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  }
});

test("idea generator consumes queued Reddit sources instead of live Reddit by default", async () => {
  const dir = await mkdtemp(join(tmpdir(), "reddit-queue-generator-"));
  const queuePath = join(dir, "reddit.json");
  const outPath = join(dir, "cards.json");
  const reportPath = join(dir, "report.md");
  const carouselPath = join(dir, "carousel.json");
  const runsDir = join(dir, "runs");
  const memoryPath = join(dir, "memory.json");
  await writeFile(queuePath, JSON.stringify({
    version: 1,
    updatedAt: "2026-07-01T00:00:00.000Z",
    items: [
      item({
        source: "reddit",
        externalId: "t3_queue_generator_1",
        title: "Reddit builders are sharing a local LLM routing workflow",
        body: "A practical workflow uses LiteLLM, local models, fallback routing, and cost control.",
        subreddit: "LocalLLaMA",
        createdAt: "2026-07-01T00:00:00.000Z",
      }),
      item({
        source: "reddit",
        externalId: "t3_queue_generator_2",
        title: "Another Reddit thread on local LLM routing workflows",
        body: "Builders compare latency, costs, local inference, and tool calling.",
        subreddit: "LocalLLaMA",
        createdAt: "2026-07-01T00:05:00.000Z",
      }),
    ],
    seenIds: [],
    readIds: [],
  }), "utf8");

  assert.equal(await researchCliMain([
    "run",
    "--sources",
    "reddit",
    "--reddit-queue",
    queuePath,
    "--out",
    outPath,
    "--report",
    reportPath,
    "--carousel-out",
    carouselPath,
    "--runs-dir",
    runsDir,
    "--memory",
    memoryPath,
    "--cards",
    "1",
    "--provider",
    "local",
  ]), 0);

  const output = JSON.parse(await readFile(outPath, "utf8")) as { sourceCount?: number; cards?: unknown[] };
  assert.equal(output.sourceCount, 2);
  assert.equal(output.cards?.length, 1);
  const queue = JSON.parse(await readFile(queuePath, "utf8")) as { items: unknown[]; readIds: unknown[] };
  assert.equal(queue.items.length, 0);
  assert.equal(queue.readIds.length, 2);
});
