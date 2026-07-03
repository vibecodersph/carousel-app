import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, writeFile } from "node:fs/promises";
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
import {
  enrichTheBatchSourceItemFromHtml,
  latestTheBatchIssueTagUrlFromIndexHtml,
  theBatchHtmlToText,
  theBatchPostToSourceItem,
  theBatchTagPageToSourceItems,
} from "../../research_idea_generator/sources/theBatch.ts";
import {
  carouselBriefQueueId,
  scanCarouselBriefRunArchives,
  unpublishedCarouselBriefs,
} from "../../research_idea_generator/briefQueue.ts";
import { buildResearchQueries } from "../../research_idea_generator/taxonomy.ts";
import { scoreResearchCluster } from "../../research_idea_generator/scoring.ts";
import { evidenceFromCluster, generateInsightCards, selectClustersForInsightCards } from "../../research_idea_generator/generator.ts";
import { assessHookRisk, generateHookVariants, optimizeHookText } from "../../research_idea_generator/hooks.ts";
import { judgeHookWorthiness } from "../../research_idea_generator/hookJudge.ts";
import { filterSeenSourceItems, type ResearchMemory } from "../../research_idea_generator/memory.ts";
import { collectRedditSourceQueue, readUnreadRedditSources } from "../../research_idea_generator/redditQueue.ts";
import type { CarouselBriefOutput, InsightCard, ResearchCluster, ScoredResearchCluster, TaxonomyConfig } from "../../research_idea_generator/types.ts";

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

const THE_BATCH_INDEX_FIXTURE = `<!doctype html><html><body>
<script id="__NEXT_DATA__" type="application/json">{
  "props": {
    "pageProps": {
      "posts": [
        {
          "title": "Gemini Flash Gets Pricey, AI Act Delays, Agents Drive Online Traffic",
          "slug": "issue-355",
          "published_at": "2026-05-29T07:00:34.000-07:00",
          "tags": [
            { "name": "The Batch Newsletter", "slug": "the-batch" },
            { "name": "issue-355", "slug": "issue-355" },
            { "name": "May 29, 2026", "slug": "may-29-2026" }
          ]
        }
      ]
    }
  }
}</script>
</body></html>`;

const THE_BATCH_TAG_FIXTURE = `<!doctype html><html><body>
<script id="__NEXT_DATA__" type="application/json">{
  "props": {
    "pageProps": {
      "tag": { "name": "May 29, 2026", "slug": "may-29-2026" },
      "posts": [
        {
          "title": "Planning Generated Images In Stages: Meta improves image models by plotting and revising generations step-by-step",
          "slug": "planning-generated-images-in-stages",
          "url": "https://charonhub.deeplearning.ai/404",
          "feature_image": "https://charonhub.deeplearning.ai/content/images/2026/05/STROKES-1.webp",
          "custom_excerpt": "Text-to-image generators use staged plans to revise image layouts before final diffusion.",
          "published_at": "2026-05-29T08:15:59.000-07:00",
          "tags": [
            { "name": "Machine Learning Research", "slug": "research" },
            { "name": "Generative AI", "slug": "generative-ai" },
            { "name": "May 29, 2026", "slug": "may-29-2026" }
          ]
        },
        {
          "title": "Gemini 3.5 Flash Pairs Smarts With Speed: Google's updated Flash levels up, approaching top models but raising prices",
          "slug": "gemini-3-5-flash-pairs-smarts-with-speed",
          "feature_image": "https://charonhub.deeplearning.ai/content/images/2026/05/GEMINI3.5FLASH-1.webp",
          "custom_excerpt": "Google's faster model improves benchmark scores but raises token prices for builders.",
          "published_at": "2026-05-29T08:00:52.000-07:00",
          "tags": [
            { "name": "Machine Learning Research", "slug": "research" },
            { "name": "AI Agents", "slug": "ai-agents" },
            { "name": "May 29, 2026", "slug": "may-29-2026" }
          ]
        },
        {
          "title": "Gemini Flash Gets Pricey, AI Act Delays, Agents Drive Online Traffic",
          "slug": "issue-355",
          "feature_image": "https://charonhub.deeplearning.ai/content/images/2026/05/2026.05.29-LETTER-1.webp",
          "custom_excerpt": "The Batch AI News and Insights weekly issue wrapper.",
          "published_at": "2026-05-29T07:00:34.000-07:00",
          "tags": [
            { "name": "The Batch Newsletter", "slug": "the-batch" },
            { "name": "issue-355", "slug": "issue-355" },
            { "name": "May 29, 2026", "slug": "may-29-2026" }
          ]
        }
      ]
    }
  }
}</script>
</body></html>`;

const THE_BATCH_GLM_STORY_FIXTURE = `<!doctype html><html><body>
<script id="__NEXT_DATA__" type="application/json">${JSON.stringify({
  props: {
    pageProps: {
      post: {
        id: "post-glm",
        title: "Top Agentic Performance, Low Cost: GLM-5.2, designed for coding and long-running agentic jobs, now the top open model",
        slug: "top-agentic-performance-low-cost",
        url: "https://www.deeplearning.ai/the-batch/top-agentic-performance-low-cost",
        feature_image: "https://charonhub.deeplearning.ai/content/images/2026/06/GLM5.2-1.webp",
        feature_image_alt: "GLM-5.2 model illustration",
        custom_excerpt: "Z.ai released GLM-5.2 for coding and long-running agentic jobs.",
        published_at: "2026-07-01T08:00:00.000-07:00",
        reading_time: 4,
        primary_tag: { name: "Large Language Models (LLMs)", slug: "llms" },
        tags: [
          { name: "Large Language Models (LLMs)", slug: "llms" },
          { name: "AI Agents", slug: "ai-agents" },
          { name: "July 1, 2026", slug: "july-1-2026" },
        ],
        html: `
          <p>Z.ai released GLM-5.2, an open-weights model designed for coding and long-running agentic jobs.</p>
          <p>Input/output: GLM-5.2 accepts up to 1 million input tokens and up to 128,000 output tokens at 103 tokens per second.</p>
          <p>Architecture: GLM-5.2 is a mixture-of-experts transformer with 753 billion total parameters and 40 billion active parameters per token.</p>
          <p>Builder features: The model supports two reasoning levels, function calling, structured output, and context caching.</p>
          <p>Performance: Z.ai says GLM-5.2 rivals proprietary leaders on agentic coding benchmarks.</p>
          <p><a href="https://github.com/zai-org/GLM-5.2">GLM-5.2 repository</a></p>
        `,
      },
    },
  },
})}</script>
</body></html>`;

async function withMockGeminiHookJudge<T>(
  judgments: Array<{ index: number; score: number; worthCarousel: boolean; reason: string; bestAngle?: string }>,
  run: () => Promise<T>,
): Promise<T> {
  const originalFetch = globalThis.fetch;
  const originalKey = process.env.GEMINI_API_KEY;
  process.env.GEMINI_API_KEY = "test-key";
  globalThis.fetch = (async () => new Response(JSON.stringify({
    candidates: [{
      content: {
        parts: [{ text: JSON.stringify(judgments) }],
      },
    }],
  }), { status: 200, headers: { "Content-Type": "application/json" } })) as typeof fetch;
  try {
    return await run();
  } finally {
    globalThis.fetch = originalFetch;
    if (originalKey === undefined) delete process.env.GEMINI_API_KEY;
    else process.env.GEMINI_API_KEY = originalKey;
  }
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
    owner: { login: "builder", avatar_url: "https://avatars.githubusercontent.com/u/123?v=4" },
    topics: ["llm", "routing", "ai"],
  }, "llm router");
  assert.ok(repo);
  assert.equal(repo.source, "github");
  assert.equal(repo.externalId, "123");
  assert.equal(repo.metrics.upvotes, 1200);
  assert.equal(repo.metrics.score, 1360);
  assert.match(repo.body, /Route LLM requests/);
  assert.equal(repo.media.hasImage, true);
  assert.equal(repo.media.provider, "github_open_graph");
  assert.equal(repo.media.imageUrl, "https://opengraph.githubassets.com/1/builder/llm-router");

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

test("The Batch weekly fixture splits into image-backed story SourceItems", () => {
  const issueTagUrl = latestTheBatchIssueTagUrlFromIndexHtml(THE_BATCH_INDEX_FIXTURE);
  assert.equal(issueTagUrl, "https://www.deeplearning.ai/the-batch/tag/may-29-2026");

  const items = theBatchTagPageToSourceItems(THE_BATCH_TAG_FIXTURE, { issueTagUrl });
  assert.equal(items.length, 2);
  assert.deepEqual(items.map((entry) => entry.externalId), [
    "planning-generated-images-in-stages",
    "gemini-3-5-flash-pairs-smarts-with-speed",
  ]);
  assert.ok(!items.some((entry) => entry.externalId === "issue-355"));
  assert.equal(items[0].source, "the_batch");
  assert.equal(items[0].url, "https://www.deeplearning.ai/the-batch/planning-generated-images-in-stages");
  assert.equal(items[0].media.hasImage, true);
  assert.equal(items[0].media.hasVideo, false);
  assert.equal(items[0].media.provider, "the_batch_feature_image");
  assert.equal(items[0].media.imageUrl, "https://charonhub.deeplearning.ai/content/images/2026/05/STROKES-1.webp");
  assert.match(items[0].body, /Generative AI/);
  assert.equal((items[0].raw as { issueTagUrl?: string }).issueTagUrl, issueTagUrl);

  const issueWrapper = theBatchPostToSourceItem({
    title: "Weekly issue wrapper",
    slug: "issue-999",
    tags: [{ name: "The Batch Newsletter", slug: "the-batch" }],
  });
  assert.equal(issueWrapper, null);
});

test("The Batch full story enrichment preserves named article facts", () => {
  const story = theBatchPostToSourceItem({
    title: "Top Agentic Performance, Low Cost",
    slug: "top-agentic-performance-low-cost",
    feature_image: "https://charonhub.deeplearning.ai/content/images/2026/06/GLM5.2-1.webp",
    custom_excerpt: "Open model for agentic coding.",
    published_at: "2026-07-01T08:00:00.000-07:00",
    tags: [{ name: "AI Agents", slug: "ai-agents" }],
  });
  assert.ok(story);

  const enriched = enrichTheBatchSourceItemFromHtml(story, THE_BATCH_GLM_STORY_FIXTURE);
  assert.match(theBatchHtmlToText("<p>Input/output: GLM-5.2 accepts tokens.</p>"), /Input\/output: GLM-5\.2/);
  assert.match(enriched.body, /GLM-5\.2/);
  assert.match(enriched.body, /1 million input tokens/);
  assert.match(enriched.body, /753 billion total parameters/);
  assert.equal(enriched.media.imageUrl, "https://charonhub.deeplearning.ai/content/images/2026/06/GLM5.2-1.webp");

  const raw = enriched.raw as {
    fullStory?: {
      articleText?: string;
      sourceLinks?: Array<{ text?: string; url?: string }>;
    };
  };
  assert.match(raw.fullStory?.articleText ?? "", /Builder features/);
  assert.equal(raw.fullStory?.sourceLinks?.[0].url, "https://github.com/zai-org/GLM-5.2");
});

test("The Batch story images flow into evidence and carousel brief images", () => {
  const story = theBatchTagPageToSourceItems(THE_BATCH_TAG_FIXTURE)[0];
  const cluster = scoreResearchCluster({
    id: "batch_image",
    label: story.title,
    keywords: ["generated", "images", "model", "workflow"],
    items: [story],
  }, new Date("2026-05-30T00:00:00.000Z"));
  const evidence = evidenceFromCluster(cluster);
  assert.equal(evidence[0].source, "The Batch (DeepLearning.AI)");
  assert.equal(evidence[0].sourceName, "the_batch");
  assert.equal(evidence[0].media?.imageUrl, "https://charonhub.deeplearning.ai/content/images/2026/05/STROKES-1.webp");
  assert.equal(evidence[0].media?.provider, "the_batch_feature_image");

  const card: InsightCard = {
    id: "batch_card",
    workingTitle: "Generated images are getting a planning step",
    claim: "Some image models now plan and revise layouts before rendering the final image.",
    evidence,
    whyItMatters: "Builders can treat image generation as a staged workflow instead of a one-shot prompt.",
    contentAngles: ["Planning as a practical control layer for generated media."],
    hooks: [{
      hook: "Image generation is becoming a planning problem",
      lines: [
        "Image generation is becoming a planning problem",
        "The model sketches intent before pixels.",
      ],
      style: "curiosity",
      riskLevel: "low",
      whyItWorks: "Reframes a model update as a workflow shift.",
      needsFactCheck: false,
      bestPlatform: "X",
    }],
    scores: cluster.scores,
    confidence: cluster.confidence,
    risks: ["Avoid claiming all image models use this design."],
    suggestedFormats: ["Instagram carousel"],
  };
  const brief = insightCardToCarouselBrief(card);
  assert.equal(brief.slides[0].image.kind, "source_image");
  assert.equal(brief.slides[0].image.sourceImageUrl, story.media.imageUrl);
  assert.deepEqual(brief.slides[0].image.sourceNames, ["the_batch"]);
  assert.deepEqual(brief.slides[0].image.sourceTitles, [story.title]);
  assert.deepEqual(brief.slides[0].image.sourcePageUrls, [story.url]);
});

test("The Batch carousel slides expose GLM-5.2 article facts even with a generic hook", () => {
  const url = "https://www.deeplearning.ai/the-batch/top-agentic-performance-low-cost";
  const card: InsightCard = {
    id: "batch_glm_card",
    workingTitle: "Open models change long-running agent economics",
    claim: "The Batch story is about a named open model, not just a generic open-model trend.",
    evidence: [{
      source: "The Batch (DeepLearning.AI)",
      sourceName: "the_batch",
      sourceItemId: "glm-source",
      title: "Top Agentic Performance, Low Cost: GLM-5.2, designed for coding and long-running agentic jobs, now the top open model",
      url,
      excerpt: [
        "Title: Top Agentic Performance, Low Cost: GLM-5.2, designed for coding and long-running agentic jobs, now the top open model",
        "Summary: Z.ai released GLM-5.2 for coding and long-running agentic jobs.",
        "Article:",
        "Z.ai released GLM-5.2, an open-weights model designed for coding and long-running agentic jobs.",
        "Input/output: GLM-5.2 accepts up to 1 million input tokens and up to 128,000 output tokens at 103 tokens per second.",
        "Architecture: GLM-5.2 is a mixture-of-experts transformer with 753 billion total parameters and 40 billion active parameters per token.",
        "Builder features: The model supports two reasoning levels, function calling, structured output, and context caching.",
      ].join("\n"),
      author: "DeepLearning.AI",
      metrics: { score: 90 },
      timestamp: "2026-07-01T15:00:00.000Z",
      media: {
        hasImage: true,
        hasVideo: false,
        imageUrl: "https://charonhub.deeplearning.ai/content/images/2026/06/GLM5.2-1.webp",
        provider: "the_batch_feature_image",
      },
    }],
    whyItMatters: "Builders need the exact model and tradeoffs before turning the article into a carousel.",
    contentAngles: ["Turn The Batch article into concise slides without losing named facts."],
    hooks: [{
      hook: "Open models changed the agent math this week",
      lines: [
        "Open models changed the agent math this week",
        "The important part is hidden in the workflow.",
      ],
      style: "curiosity",
      riskLevel: "low",
      whyItWorks: "Keeps the hook short while the slides carry the facts.",
      needsFactCheck: false,
      bestPlatform: "X",
    }],
    scores: {
      overall: 0.82,
      engagementVelocity: 0.7,
      sourceQuality: 0.9,
      novelty: 0.7,
      practicalUtility: 0.9,
      controversyOrTension: 0.4,
      crossSourceConfirmation: 0.3,
      audienceFit: 0.9,
      hookability: 0.85,
    },
    confidence: "medium-high",
    risks: ["Do not generalize the claim beyond this article."],
    suggestedFormats: ["Instagram carousel"],
  };

  const brief = insightCardToCarouselBrief(card);
  const storyText = brief.slides.slice(1).flatMap((slide) => [slide.headline, ...slide.lines]).join("\n");
  assert.match(storyText, /GLM-5\.2/);
  assert.match(storyText, /1 million input tokens|Input And Output/);
  assert.match(storyText, /753 billion total parameters|Architecture/);
  assert.ok(brief.slides.slice(1).every((slide) => slide.sourceUrls?.includes(url)));
  assert.ok(brief.slides.slice(1).every((slide) => slide.sourceItemIds?.includes("glm-source")));
});

test("The Batch carousel slides name the Three Key Loops from the article", () => {
  const url = "https://www.deeplearning.ai/the-batch/three-key-loops-for-building-great-software";
  const card: InsightCard = {
    id: "batch_loops_card",
    workingTitle: "AI-assisted coding depends on feedback loops",
    claim: "The article's point is the named loop framework, not just that code generators need process.",
    evidence: [{
      source: "The Batch (DeepLearning.AI)",
      sourceName: "the_batch",
      sourceItemId: "loops-source",
      title: "Three Key Loops for Building Great Software",
      url,
      excerpt: [
        "Title: Three Key Loops for Building Great Software",
        "Summary: AI-assisted agentic coding reinforces iterative software development.",
        "Article:",
        "Agentic coding loop",
        "Use AI coding agents to draft, inspect, test, and revise code in tight iterations.",
        "Customer feedback loop",
        "Get real usage signals quickly so the team can decide what to improve next.",
        "Prioritization loop",
        "Turn customer evidence into a ranked backlog before asking AI to generate more code.",
      ].join("\n"),
      author: "DeepLearning.AI",
      metrics: { score: 90 },
      timestamp: "2026-07-01T15:00:00.000Z",
    }],
    whyItMatters: "The carousel should teach the named loop framework in concise form.",
    contentAngles: ["Expose the article's framework instead of only the hook."],
    hooks: [{
      hook: "Even advanced AI code generators fail without this hidden workflow",
      lines: [
        "Even advanced AI code generators fail without this hidden workflow",
        "The workflow matters more than the code generator.",
      ],
      style: "curiosity",
      riskLevel: "low",
      whyItWorks: "Creates a curiosity gap around the actual framework.",
      needsFactCheck: false,
      bestPlatform: "X",
    }],
    scores: {
      overall: 0.78,
      engagementVelocity: 0.7,
      sourceQuality: 0.9,
      novelty: 0.6,
      practicalUtility: 0.9,
      controversyOrTension: 0.3,
      crossSourceConfirmation: 0.2,
      audienceFit: 0.9,
      hookability: 0.8,
    },
    confidence: "medium-high",
    risks: ["Do not turn the framework into unrelated software advice."],
    suggestedFormats: ["Instagram carousel"],
  };

  const brief = insightCardToCarouselBrief(card);
  const storyText = brief.slides.slice(1).flatMap((slide) => [slide.headline, ...slide.lines]).join("\n");
  assert.match(storyText, /Three Key Loops for Building Great Software/);
  assert.match(storyText, /Agentic Coding Loop/);
  assert.match(storyText, /Customer Feedback Loop/);
  assert.match(storyText, /Prioritization Loop/);
  assert.ok(brief.slides.slice(1).every((slide) => slide.sourceUrls?.includes(url)));
});

test("The Batch hook judge keeps strong stories and drops weak ones", async () => {
  const glm = item({
    source: "the_batch",
    externalId: "glm_batch",
    title: "Top Agentic Performance, Low Cost: GLM-5.2, designed for coding and long-running agentic jobs, now the top open model",
    body: "Z.ai released an open-weights model that rivals proprietary leaders for autonomous agentic tasks. Tags: Machine Learning Research, Large Language Models (LLMs), AI Agents",
    metrics: { score: 90, upvotes: 90 },
  });
  const loops = item({
    source: "the_batch",
    externalId: "loops_batch",
    title: "Three Key Loops for Building Great Software",
    body: "AI-assisted agentic coding reinforces iterative software development. These three loops can guide development and help you decide what to build. Tags: Letters, Technical Insights",
    metrics: { score: 90, upvotes: 90 },
  });
  const weak = item({
    source: "the_batch",
    externalId: "weak_batch",
    title: "AI newsletter shares a short community update",
    body: "A broad recap with community notes, event reminders, and editorial housekeeping.",
    metrics: { score: 50, upvotes: 50 },
  });
  const judged = await withMockGeminiHookJudge([
    {
      index: 0,
      score: 0.78,
      worthCarousel: true,
      reason: "LLM judge: open-weights agentic model with cost/performance tension.",
      bestAngle: "Top open model claim with low-cost agentic tasks.",
    },
    {
      index: 1,
      score: 0.68,
      worthCarousel: true,
      reason: "LLM judge: practical software-building loop framework.",
      bestAngle: "Three repeatable loops for AI-assisted development.",
    },
    {
      index: 2,
      score: 0.2,
      worthCarousel: false,
      reason: "LLM judge: community housekeeping lacks a strong carousel hook.",
    },
  ], () => judgeHookWorthiness([glm, loops, weak]));
  assert.deepEqual(judged.kept.map((entry) => entry.externalId), ["glm_batch", "loops_batch"]);
  assert.deepEqual(judged.dropped.map((entry) => entry.item.externalId), ["weak_batch"]);
  assert.equal((judged.kept[0].raw as { hookJudgment?: { judgedBy?: string } }).hookJudgment?.judgedBy, "gemini");
  assert.ok(((judged.kept[0].raw as { hookJudgment?: { score?: number } }).hookJudgment?.score ?? 0) >= 0.55);
  assert.ok(((judged.kept[1].raw as { hookJudgment?: { score?: number } }).hookJudgment?.score ?? 0) >= 0.55);
  assert.equal(judged.dropped[0].judgment.worthCarousel, false);
});

test("The Batch hook judge requires LLM credentials", async () => {
  const originalGeminiKey = process.env.GEMINI_API_KEY;
  const originalGoogleKey = process.env.GOOGLE_API_KEY;
  delete process.env.GEMINI_API_KEY;
  delete process.env.GOOGLE_API_KEY;
  try {
    await assert.rejects(
      () => judgeHookWorthiness([item({
        source: "the_batch",
        externalId: "needs_llm",
        title: "Top open model for agent workflows",
        body: "A Batch story that must be judged by an LLM.",
      })]),
      /LLM hook judge requires GEMINI_API_KEY or GOOGLE_API_KEY/,
    );
  } finally {
    if (originalGeminiKey === undefined) delete process.env.GEMINI_API_KEY;
    else process.env.GEMINI_API_KEY = originalGeminiKey;
    if (originalGoogleKey === undefined) delete process.env.GOOGLE_API_KEY;
    else process.env.GOOGLE_API_KEY = originalGoogleKey;
  }
});

test("offline CLI filters weak Batch stories and preserves story images in carousel briefs", async () => {
  const dir = await mkdtemp(join(tmpdir(), "batch-research-ideas-"));
  const inputPath = join(dir, "batch-source-items.json");
  const outPath = join(dir, "insight-cards.json");
  const reportPath = join(dir, "report.md");
  const carouselPath = join(dir, "carousel-briefs.json");
  const runsDir = join(dir, "runs");
  const strong = item({
    source: "the_batch",
    externalId: "batch_strong_cli",
    title: "Gemini Flash raises token prices 30% but improves one agent benchmark",
    body: "Builders using model APIs now face a pricing and workflow tradeoff for AI agents.",
    url: "https://www.deeplearning.ai/the-batch/gemini-flash-price-benchmark",
    metrics: { score: 90, upvotes: 90 },
    media: {
      hasVideo: false,
      hasImage: true,
      imageUrl: "https://charonhub.deeplearning.ai/content/images/2026/05/GEMINI3.5FLASH-1.webp",
      provider: "the_batch_feature_image",
    },
  });
  const weak = item({
    source: "the_batch",
    externalId: "batch_weak_cli",
    title: "AI newsletter shares a short community update",
    body: "A broad recap with community notes, event reminders, and editorial housekeeping.",
    url: "https://www.deeplearning.ai/the-batch/community-update",
    metrics: { score: 50, upvotes: 50 },
    media: {
      hasVideo: false,
      hasImage: true,
      imageUrl: "https://charonhub.deeplearning.ai/content/images/2026/05/COMMUNITY.webp",
      provider: "the_batch_feature_image",
    },
  });
  await writeFile(inputPath, JSON.stringify({ items: [strong, weak] }), "utf8");

  assert.equal(await withMockGeminiHookJudge([
    {
      index: 0,
      score: 0.82,
      worthCarousel: true,
      reason: "LLM judge: concrete pricing and benchmark tension for builders.",
      bestAngle: "Pricing jump versus benchmark gain.",
    },
    {
      index: 1,
      score: 0.18,
      worthCarousel: false,
      reason: "LLM judge: community update is too thin for a carousel.",
    },
  ], () => researchCliMain([
    "run",
    "--sources",
    "the_batch",
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
    "--no-memory",
    "--cards",
    "1",
  ])), 0);

  const output = JSON.parse(await readFile(outPath, "utf8")) as { generatedAt: string; sourceCount?: number; cards?: Array<{ evidence?: Array<{ sourceItemId?: string }> }> };
  assert.equal(output.sourceCount, 1);
  assert.equal(output.cards?.[0].evidence?.[0].sourceItemId, strong.id);
  const carousel = JSON.parse(await readFile(carouselPath, "utf8")) as {
    carousels?: Array<{ slides?: Array<{ image?: { sourceImageUrl?: string; sourceImageUrls?: string[]; sourceItemIds?: string[] } }> }>;
  };
  assert.equal(carousel.carousels?.[0].slides?.[0].image?.sourceImageUrl, strong.media.imageUrl);
  assert.deepEqual(carousel.carousels?.[0].slides?.[0].image?.sourceItemIds, [strong.id]);

  const runDir = join(runsDir, output.generatedAt.replace(/[:.]/g, "-"));
  const archivedSources = JSON.parse(await readFile(join(runDir, "source_items.json"), "utf8")) as { count?: number };
  const eligibleSources = JSON.parse(await readFile(join(runDir, "eligible_source_items.json"), "utf8")) as { count?: number; items?: Array<{ id?: string }> };
  const skipped = JSON.parse(await readFile(join(runDir, "skipped_items.json"), "utf8")) as { count?: number; items?: Array<{ reason?: string; item?: { id?: string } }> };
  assert.equal(archivedSources.count, 2);
  assert.equal(eligibleSources.count, 1);
  assert.equal(eligibleSources.items?.[0].id, strong.id);
  assert.equal(skipped.count, 1);
  assert.equal(skipped.items?.[0].reason, "hook_judge_rejected");
  assert.equal(skipped.items?.[0].item?.id, weak.id);
});

test("Batch-only pipeline keeps approved stories as separate carousel briefs", async () => {
  const dir = await mkdtemp(join(tmpdir(), "batch-separate-stories-"));
  const inputPath = join(dir, "source-items.json");
  const outPath = join(dir, "insight-cards.json");
  const carouselPath = join(dir, "carousel-briefs.json");
  const glm = item({
    source: "the_batch",
    externalId: "glm_story",
    title: "Top Agentic Performance, Low Cost: GLM-5.2 for long-running agentic jobs",
    body: "GLM-5.2 accepts 1 million input tokens and uses 753 billion total parameters for agentic coding.",
    url: "https://www.deeplearning.ai/the-batch/top-agentic-performance-low-cost",
    metrics: { score: 95, upvotes: 95 },
  });
  const loops = item({
    source: "the_batch",
    externalId: "loops_story",
    title: "Three Key Loops for Building Great Software",
    body: "Agentic coding loop. Customer feedback loop. Prioritization loop.",
    url: "https://www.deeplearning.ai/the-batch/three-key-loops-for-building-great-software",
    metrics: { score: 90, upvotes: 90 },
  });
  await writeFile(inputPath, JSON.stringify({ items: [glm, loops] }), "utf8");

  assert.equal(await withMockGeminiHookJudge([
    {
      index: 0,
      score: 0.84,
      worthCarousel: true,
      reason: "LLM judge: named open model with concrete agentic coding facts.",
    },
    {
      index: 1,
      score: 0.8,
      worthCarousel: true,
      reason: "LLM judge: named software loop framework.",
    },
  ], () => researchCliMain([
    "run",
    "--input",
    inputPath,
    "--out",
    outPath,
    "--carousel-out",
    carouselPath,
    "--no-memory",
    "--cards",
    "2",
  ])), 0);

  const output = JSON.parse(await readFile(outPath, "utf8")) as {
    cardCount?: number;
    cards?: Array<{ evidence?: Array<{ sourceItemId?: string }>; workingTitle?: string }>;
  };
  assert.equal(output.cardCount, 2);
  assert.deepEqual(output.cards?.flatMap((card) => card.evidence?.map((entry) => entry.sourceItemId) ?? []).sort(), [glm.id, loops.id].sort());

  const carousel = JSON.parse(await readFile(carouselPath, "utf8")) as CarouselBriefOutput;
  assert.equal(carousel.carouselCount, 2);
  const storyText = carousel.carousels.flatMap((brief) => brief.slides.flatMap((slide) => [slide.headline, ...slide.lines])).join("\n");
  assert.match(storyText, /GLM-5\.2/);
  assert.match(storyText, /Three Key Loops for Building Great Software/);
});

test("research CLI accepts The Batch source aliases and live flags", () => {
  const parsed = parseArgs([
    "run",
    "--sources",
    "batch,thebatch,the_batch",
    "--the-batch-live",
    "--the-batch-issue-url",
    "https://www.deeplearning.ai/the-batch/tag/jun-26-2026",
    "--the-batch-queue",
    "out/research_idea_generator/the_batch_unread_sources.json",
  ]);
  assert.equal(parsed.command, "run");
  assert.deepEqual(parsed.options.sources, ["the_batch", "the_batch", "the_batch"]);
  assert.equal(parsed.options.theBatchLive, true);
  assert.equal(parsed.options.theBatchIssueUrl, "https://www.deeplearning.ai/the-batch/tag/jun-26-2026");
  assert.equal(parsed.options.theBatchQueue, "out/research_idea_generator/the_batch_unread_sources.json");
});

test("carousel brief archive scanner queues unpublished run briefs idempotently", async () => {
  const dir = await mkdtemp(join(tmpdir(), "carousel-brief-queue-"));
  const runsDir = join(dir, "runs");
  const defaultRun = join(runsDir, "2026-07-01T00-00-00-000Z");
  const batchRun = join(runsDir, "the_batch", "2026-07-02T00-00-00-000Z");
  const queuePath = join(dir, "queue.json");
  await mkdir(defaultRun, { recursive: true });
  await mkdir(batchRun, { recursive: true });

  const routerBrief = {
    id: "brief-router",
    sourceInsightCardId: "card-router",
    workingTitle: "Model routing becomes AI cost control",
    hook: "Stop choosing one model for every coding workflow",
    hookStyle: "contrarian",
    hookRiskLevel: "low",
    hookNeedsFactCheck: false,
    hookBestPlatform: "X",
    confidence: "medium",
    score: 0.61,
    suggestedFormat: "instagram_carousel",
    slideCount: 2,
    slides: [
      {
        slideNumber: 1,
        type: "cover",
        headline: "Stop choosing one model for every coding workflow",
        lines: [],
        altText: "Cover",
        image: {
          kind: "source_image",
          role: "cover",
          altText: "Cover image",
          rationale: "Use source image.",
          sourceImageUrl: "https://example.com/router.png",
          sourceImageUrls: ["https://example.com/router.png"],
          sourcePageUrls: ["https://example.com/router"],
          sourceItemIds: ["router"],
          sourceNames: ["github"],
          sourceTitles: ["builder/router"],
        },
      },
    ],
    instagramDescription: "A routing carousel.",
    evidenceSourceItemIds: ["router"],
    evidenceUrls: ["https://example.com/router"],
  };
  const batchBrief = {
    ...routerBrief,
    id: "brief-batch",
    sourceInsightCardId: "card-batch",
    workingTitle: "Open models change agent costs",
    hook: "Stop paying API premiums for long-running agents",
    score: 0.82,
    evidenceSourceItemIds: ["batch"],
    evidenceUrls: ["https://www.deeplearning.ai/the-batch/top-agentic-performance-low-cost"],
    slides: [{
      ...routerBrief.slides[0],
      image: {
        ...routerBrief.slides[0].image,
        sourceImageUrl: "https://charonhub.deeplearning.ai/content/images/2026/06/GLM5.2-1.webp",
        sourceImageUrls: ["https://charonhub.deeplearning.ai/content/images/2026/06/GLM5.2-1.webp"],
        sourcePageUrls: ["https://www.deeplearning.ai/the-batch/top-agentic-performance-low-cost"],
        sourceItemIds: ["batch"],
        sourceNames: ["the_batch"],
        sourceTitles: ["Top Agentic Performance, Low Cost"],
      },
    }],
  };
  const defaultOutput: CarouselBriefOutput = {
    generatedAt: "2026-07-01T00:00:00.000Z",
    audience: "ai_builders",
    sourceInsightGeneratedAt: "2026-07-01T00:00:00.000Z",
    carouselCount: 1,
    carousels: [routerBrief],
  };
  const batchOutput: CarouselBriefOutput = {
    generatedAt: "2026-07-02T00:00:00.000Z",
    audience: "ai_builders",
    sourceInsightGeneratedAt: "2026-07-02T00:00:00.000Z",
    carouselCount: 2,
    carousels: [batchBrief, routerBrief],
  };
  await writeFile(join(defaultRun, "carousel_briefs.json"), JSON.stringify(defaultOutput), "utf8");
  await writeFile(join(batchRun, "carousel_briefs.json"), JSON.stringify(batchOutput), "utf8");

  const firstScan = await scanCarouselBriefRunArchives({
    runsDir,
    queuePath,
    now: new Date("2026-07-03T00:00:00.000Z"),
  });
  assert.equal(firstScan.scannedFiles.length, 2);
  assert.equal(firstScan.briefCount, 3);
  assert.equal(firstScan.added, 2);
  assert.equal(firstScan.queue.items.length, 2);
  assert.equal(unpublishedCarouselBriefs(firstScan.queue).length, 2);
  assert.ok(firstScan.queue.items.every((entry) => entry.channelId === "aibrief_jp"));
  assert.ok(firstScan.queue.items.every((entry) => entry.status === "scheduled"));
  assert.deepEqual(
    firstScan.queue.items.map((entry) => entry.scheduledAt),
    ["2026-07-03T09:00:00+09:00", "2026-07-03T12:00:00+09:00"],
  );
  assert.ok(firstScan.queue.items.some((entry) => entry.workingTitle === "Open models change agent costs"));
  assert.equal(
    firstScan.queue.items.find((entry) => entry.briefId === "brief-batch")?.sourceImageUrls[0],
    "https://charonhub.deeplearning.ai/content/images/2026/06/GLM5.2-1.webp",
  );

  const routerQueueId = carouselBriefQueueId(routerBrief);
  const renderedManifestPath = join(batchRun, "rendered", "manifest.json");
  const renderedAt = "2026-07-03T03:10:39.925Z";
  const publishedQueue = {
    ...firstScan.queue,
    items: firstScan.queue.items.map((entry) => {
      if (entry.id === routerQueueId) {
        return { ...entry, status: "published", publishedAt: "2026-07-04T00:00:00.000Z" };
      }
      if (entry.briefId === "brief-batch") {
        return { ...entry, renderedManifestPath, renderedAt };
      }
      return entry;
    }),
  };
  await writeFile(queuePath, JSON.stringify(publishedQueue), "utf8");

  const secondScan = await scanCarouselBriefRunArchives({
    runsDir,
    queuePath,
    now: new Date("2026-07-05T00:00:00.000Z"),
  });
  assert.equal(secondScan.added, 0);
  assert.equal(secondScan.queue.items.length, 2);
  assert.equal(secondScan.queue.items.find((entry) => entry.id === routerQueueId)?.status, "published");
  const renderedBatch = secondScan.queue.items.find((entry) => entry.briefId === "brief-batch");
  assert.equal(renderedBatch?.status, "scheduled");
  assert.equal(renderedBatch?.renderedManifestPath, renderedManifestPath);
  assert.equal(renderedBatch?.renderedAt, renderedAt);
  assert.equal(unpublishedCarouselBriefs(secondScan.queue).length, 1);

  const parsed = parseArgs(["scan-briefs", "--runs-dir", runsDir, "--queue", queuePath]);
  assert.equal(parsed.command, "scan-briefs");
  assert.equal(parsed.options.runsDir, runsDir);
  assert.equal(parsed.options.briefQueue, queuePath);
  assert.equal(await researchCliMain(["scan-briefs", "--runs-dir", runsDir, "--queue", queuePath]), 0);
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

test("evidence selection keeps source diversity before filling from dominant sources", () => {
  const githubItems = Array.from({ length: 10 }, (_, index) => item({
    source: "github",
    externalId: `diverse_repo_${index}`,
    title: `builder/repo-${index}`,
    body: "GitHub repo about local agent workflows.",
    metrics: { upvotes: 1000 - index, comments: 10, score: 1000 - index },
  }));
  const cluster = scoreResearchCluster({
    id: "diverse",
    label: "local agent workflows",
    keywords: ["local", "agent", "workflow"],
    items: [
      ...githubItems,
      item({
        source: "hacker_news",
        externalId: "diverse_hn",
        title: "HN discussion about local agent workflows",
        body: "Builders compare local agent tooling.",
        metrics: { upvotes: 20, comments: 15, score: 20 },
      }),
      item({
        source: "reddit",
        externalId: "diverse_reddit",
        title: "Reddit discussion about local agent workflows",
        body: "A practical thread about local agent tooling.",
        subreddit: "LocalLLaMA",
        metrics: { upvotes: 15, comments: 12, score: 15 },
      }),
    ],
  }, new Date("2026-07-01T00:00:00.000Z"));
  const evidence = evidenceFromCluster(cluster);
  assert.equal(evidence.length, 8);
  assert.ok(evidence.some((entry) => entry.sourceName === "github"));
  assert.ok(evidence.some((entry) => entry.sourceName === "hacker_news"));
  assert.ok(evidence.some((entry) => entry.sourceName === "reddit"));
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
  assert.ok(brief.slides.every((slide) => slide.image));
  assert.equal(brief.slides[0].image.kind, "source_image");
  assert.equal(brief.slides[0].image.sourceImageUrl, "https://opengraph.githubassets.com/1/builder/llm-router");
  assert.equal(brief.slides[1].image.kind, "generated_prompt");
  assert.match(brief.slides[1].image.promptBase ?? "", /model routing/i);
  assert.match(brief.instagramDescription, /Evidence base:/);
  assert.match(brief.instagramDescription, /Publish note:/);
});

test("carousel slide images handle multiple source assets and no-image sources", () => {
  const baseCard: InsightCard = {
    id: "card_images",
    workingTitle: "AI builders are comparing model routers",
    claim: "Builders are comparing routing gateways for cost, latency, and fallback behavior.",
    evidence: [
      {
        source: "GitHub",
        sourceName: "github",
        title: "builder/llm-router",
        url: "https://github.com/builder/llm-router",
        excerpt: "Route LLM requests by cost and latency.",
        author: "builder",
        metrics: { score: 100 },
        timestamp: "2026-06-21T00:00:00.000Z",
      },
      {
        source: "GitHub",
        sourceName: "github",
        title: "builder/gateway",
        url: "https://github.com/builder/gateway",
        excerpt: "Gateway for fallback providers.",
        author: "builder",
        metrics: { score: 90 },
        timestamp: "2026-06-21T00:00:00.000Z",
      },
    ],
    whyItMatters: "Router design changes cost and reliability before model choice changes.",
    contentAngles: ["Compare routers as infrastructure, not model picks."],
    hooks: [{
      hook: "Model routing is becoming infrastructure",
      lines: [
        "Model routing is becoming infrastructure",
        "The gateway decides which model sees each task.",
        "That makes routing a cost and reliability control.",
      ],
      style: "curiosity",
      riskLevel: "low",
      whyItWorks: "Frames routing as hidden infrastructure.",
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
    risks: ["Do not claim universal adoption from two repos."],
    suggestedFormats: ["Instagram carousel"],
  };

  const multiRepoBrief = insightCardToCarouselBrief(baseCard);
  assert.equal(multiRepoBrief.slides[0].image.kind, "generated_prompt");
  assert.equal(multiRepoBrief.slides[0].image.sourceImageUrls?.length, 2);
  assert.match(multiRepoBrief.slides[0].image.rationale, /Multiple source images/);
  assert.match(multiRepoBrief.slides[0].image.promptBase ?? "", /source images as visual references/i);

  const redditBrief = insightCardToCarouselBrief({
    ...baseCard,
    id: "card_reddit_image",
    evidence: [{
      source: "Reddit r/LocalLLaMA",
      sourceName: "reddit",
      sourceItemId: "reddit_image",
      title: "Local inference screenshot",
      url: "https://i.redd.it/local-inference.png",
      excerpt: "Screenshot of local inference setup.",
      author: "redditor",
      metrics: { score: 30 },
      timestamp: "2026-06-21T00:00:00.000Z",
      media: {
        hasImage: true,
        hasVideo: false,
        imageUrl: "https://i.redd.it/local-inference.png",
        provider: "reddit_direct_image",
      },
    }],
  });
  assert.equal(redditBrief.slides[0].image.kind, "source_image");
  assert.equal(redditBrief.slides[0].image.sourceImageUrl, "https://i.redd.it/local-inference.png");
  assert.deepEqual(redditBrief.slides[0].image.sourceNames, ["reddit"]);

  const hnBrief = insightCardToCarouselBrief({
    ...baseCard,
    id: "card_hn_no_image",
    evidence: [{
      source: "Hacker News",
      sourceName: "hacker_news",
      sourceItemId: "hn_no_image",
      title: "Ask HN: Model routers in production",
      url: "https://news.ycombinator.com/item?id=123",
      excerpt: "Discussion about routing models in production.",
      author: "hn_user",
      metrics: { score: 45 },
      timestamp: "2026-06-21T00:00:00.000Z",
    }],
  });
  assert.ok(hnBrief.slides.every((slide) => slide.image.kind === "generated_prompt"));
  assert.ok(hnBrief.slides.every((slide) => (slide.image.sourceImageUrls?.length ?? 0) === 0));
  assert.match(hnBrief.slides[0].image.promptBase ?? "", /No source image is available/i);
});

test("list carousel slides do not add repeated canned body lines", () => {
  const card: InsightCard = {
    id: "card_list_local",
    workingTitle: "The rise of local-first AI agent frameworks",
    claim: "Developers are testing local agent runtimes, local tool access, and local API debugging.",
    evidence: [{
      source: "GitHub",
      title: "local-agent-framework",
      url: "https://github.com/example/local-agent-framework",
      excerpt: "Local-first runtime for agent workflows.",
      author: "builder",
      metrics: { score: 100 },
      timestamp: "2026-06-21T00:00:00.000Z",
    }],
    whyItMatters: "Local-first workflows change how builders debug and secure agents.",
    contentAngles: ["Local agent tooling as a practical workflow shift."],
    hooks: [{
      hook: "3 local agent capabilities builders are testing",
      lines: [
        "3 local agent capabilities builders are testing",
        "1. local-first agent runtimes for offline execution",
        "2. terminal-based agents with local tool access",
        "3. local API traffic interceptors for debugging",
      ],
      style: "list",
      riskLevel: "low",
      whyItWorks: "Turns the evidence into concrete local-agent capabilities.",
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
    risks: ["Do not claim broad adoption from one repo."],
    suggestedFormats: ["Instagram carousel"],
  };
  const brief = insightCardToCarouselBrief(card);
  assert.equal(brief.slideCount, 4);
  assert.deepEqual(brief.slides.slice(1).map((slide) => slide.headline), [
    "1. Local-First Agent Runtimes For Offline Execution",
    "2. Terminal-Based Agents With Local Tool Access",
    "3. Local API Traffic Interceptors For Debugging",
  ]);
  assert.ok(brief.slides.slice(1).every((slide) => slide.lines.length === 0));
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
