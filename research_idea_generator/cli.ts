#!/usr/bin/env node
import { mkdir, writeFile } from "node:fs/promises";
import { dirname } from "node:path";
import { loadDotEnv } from "../sourcing/env.ts";
import { readJsonFile, writeJsonFile } from "../sourcing/utils.ts";
import { scanCarouselBriefRunArchives, unpublishedCarouselBriefs } from "./briefQueue.ts";
import { generateCarouselBriefs } from "./carouselBriefs.ts";
import { enhanceInsightCardWithGemini } from "./generator.ts";
import { generateHookVariants } from "./hooks.ts";
import { runResearchIdeaGenerator } from "./pipeline.ts";
import { renderInsightReport } from "./report.ts";
import { loadTaxonomy } from "./taxonomy.ts";
import type { InsightCardOutput, ResearchGeneratorOptions, ResearchProvider, ResearchSourceName } from "./types.ts";

function parseSources(value: string): ResearchSourceName[] {
  return value.split(",").map((source) => {
    const normalized = source.trim().toLowerCase();
    if (normalized === "hn" || normalized === "hackernews") return "hacker_news";
    if (normalized === "batch" || normalized === "thebatch" || normalized === "the_batch") return "the_batch";
    if (normalized === "reddit" || normalized === "github" || normalized === "hacker_news") return normalized;
    throw new Error(`Unknown research source: ${source}`);
  });
}

function parseProvider(value: string): ResearchProvider {
  if (value === "local" || value === "gemini") return value;
  throw new Error(`Unknown provider: ${value}`);
}

export function parseArgs(argv: string[]): { command: string; options: ResearchGeneratorOptions } {
  const [command = "run", ...rest] = argv;
  const options: ResearchGeneratorOptions = {};
  for (let i = 0; i < rest.length; i += 1) {
    const arg = rest[i];
    if (arg === "--sources") options.sources = parseSources(rest[++i] ?? "");
    else if (arg === "--days") options.days = Number(rest[++i]);
    else if (arg === "--cards") options.cards = Number(rest[++i]);
    else if (arg === "--hooks-per-card") options.hooksPerCard = Number(rest[++i]);
    else if (arg === "--provider") options.provider = parseProvider(rest[++i] ?? "");
    else if (arg === "--input") options.input = rest[++i];
    else if (arg === "--out") options.out = rest[++i];
    else if (arg === "--report") options.report = rest[++i];
    else if (arg === "--carousel-out") options.carouselOut = rest[++i];
    else if (arg === "--runs-dir") options.runsDir = rest[++i];
    else if (arg === "--queue") options.briefQueue = rest[++i];
    else if (arg === "--no-archive") options.noArchive = true;
    else if (arg === "--memory") options.memory = rest[++i];
    else if (arg === "--no-memory") options.noMemory = true;
    else if (arg === "--reddit-queue") options.redditQueue = rest[++i];
    else if (arg === "--reddit-live") options.redditLive = true;
    else if (arg === "--the-batch-queue") options.theBatchQueue = rest[++i];
    else if (arg === "--the-batch-live") options.theBatchLive = true;
    else if (arg === "--the-batch-issue-url") options.theBatchIssueUrl = rest[++i];
    else if (arg === "--taxonomy") options.taxonomyPath = rest[++i];
    else if (arg === "--max-items-per-source") options.maxItemsPerSource = Number(rest[++i]);
    else throw new Error(`Unknown argument: ${arg}`);
  }
  return { command, options };
}

async function writeTextFile(path: string, text: string): Promise<void> {
  await mkdir(dirname(path), { recursive: true });
  await writeFile(path, text, "utf8");
}

async function writeCarouselBriefsFromCards(options: ResearchGeneratorOptions): Promise<{
  input: string;
  out: string;
  carouselCount: number;
}> {
  const input = options.input ?? "out/research_idea_generator/insight_cards.json";
  const out = options.out ?? options.carouselOut ?? "out/research_idea_generator/carousel_briefs.json";
  const insightCards = await readJsonFile<InsightCardOutput>(input, {
    generatedAt: new Date(0).toISOString(),
    audience: "ai_builders",
    sourceCount: 0,
    clusterCount: 0,
    cardCount: 0,
    cards: [],
  });
  const carouselBriefs = generateCarouselBriefs(insightCards);
  await writeJsonFile(out, carouselBriefs);
  return { input, out, carouselCount: carouselBriefs.carouselCount };
}

async function writeFormulaHooksForCards(options: ResearchGeneratorOptions): Promise<{
  input: string;
  out: string;
  report: string;
  carouselOut: string;
  cardCount: number;
}> {
  const input = options.input ?? "out/research_idea_generator/insight_cards.json";
  const out = options.out ?? input;
  const report = options.report ?? "out/research_idea_generator/report.md";
  const carouselOut = options.carouselOut ?? "out/research_idea_generator/carousel_briefs.json";
  const taxonomy = await loadTaxonomy(options.taxonomyPath);
  const insightCards = await readJsonFile<InsightCardOutput>(input, {
    generatedAt: new Date(0).toISOString(),
    audience: taxonomy.audience,
    sourceCount: 0,
    clusterCount: 0,
    cardCount: 0,
    cards: [],
  });
  const cards: InsightCardOutput["cards"] = [];
  for (const card of insightCards.cards) {
    const localCard = {
      ...card,
      hooks: generateHookVariants({
        title: card.workingTitle,
        claim: card.claim,
        evidence: card.evidence,
        count: options.hooksPerCard ?? Math.max(taxonomy.hookStyles.length, card.hooks.length),
        styles: taxonomy.hookStyles,
      }),
    };
    cards.push(
      options.provider === "gemini"
        ? await enhanceInsightCardWithGemini(localCard, {
          hooksPerCard: options.hooksPerCard ?? localCard.hooks.length,
          hookStyles: taxonomy.hookStyles,
        }) ?? localCard
        : localCard,
    );
  }
  const output: InsightCardOutput = {
    ...insightCards,
    cardCount: cards.length,
    cards,
  };
  await writeJsonFile(out, output);
  await writeTextFile(report, renderInsightReport(output));
  await writeJsonFile(carouselOut, generateCarouselBriefs(output));
  return { input, out, report, carouselOut, cardCount: output.cardCount };
}

export async function main(argv = process.argv.slice(2)): Promise<number> {
  await loadDotEnv();
  const { command, options } = parseArgs(argv);
  if (command === "run") {
    const output = await runResearchIdeaGenerator(options);
    console.log(JSON.stringify({
      generatedAt: output.generatedAt,
      sourceCount: output.sourceCount,
      clusterCount: output.clusterCount,
      cardCount: output.cardCount,
      out: options.out ?? "out/research_idea_generator/insight_cards.json",
      report: options.report ?? "out/research_idea_generator/report.md",
      carouselOut: options.carouselOut ?? "out/research_idea_generator/carousel_briefs.json",
      runsDir: options.runsDir ?? "out/research_idea_generator/runs",
    }, null, 2));
    return 0;
  }
  if (command === "briefs") {
    const result = await writeCarouselBriefsFromCards(options);
    console.log(JSON.stringify({
      input: result.input,
      out: result.out,
      carouselCount: result.carouselCount,
    }, null, 2));
    return 0;
  }
  if (command === "hooks") {
    const result = await writeFormulaHooksForCards(options);
    console.log(JSON.stringify({
      input: result.input,
      out: result.out,
      report: result.report,
      carouselOut: result.carouselOut,
      cardCount: result.cardCount,
    }, null, 2));
    return 0;
  }
  if (command === "scan-briefs") {
    const result = await scanCarouselBriefRunArchives({
      runsDir: options.runsDir,
      queuePath: options.briefQueue,
    });
    console.log(JSON.stringify({
      queue: result.queuePath,
      runsDir: result.runsDir,
      scannedFiles: result.scannedFiles.length,
      briefCount: result.briefCount,
      added: result.added,
      updated: result.updated,
      unchanged: result.unchanged,
      unpublishedCount: unpublishedCarouselBriefs(result.queue).length,
      statusCounts: result.statusCounts,
    }, null, 2));
    return 0;
  }
  throw new Error(`Unknown command: ${command}`);
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((error) => {
    console.error(error instanceof Error ? error.message : String(error));
    process.exitCode = 1;
  });
}
