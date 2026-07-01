import type { InsightCardOutput } from "./types.ts";

function scorePercent(value: number): string {
  return `${Math.round(value * 100)}%`;
}

export function renderInsightReport(output: InsightCardOutput): string {
  const lines: string[] = [
    "# Research Idea Generator Report",
    "",
    `Generated: ${output.generatedAt}`,
    `Audience: ${output.audience}`,
    `Sources analyzed: ${output.sourceCount}`,
    `Clusters found: ${output.clusterCount}`,
    `Cards: ${output.cardCount}`,
    "",
  ];

  output.cards.forEach((card, index) => {
    lines.push(`## ${index + 1}. ${card.workingTitle}`);
    lines.push("");
    lines.push(`Claim: ${card.claim}`);
    lines.push(`Confidence: ${card.confidence}`);
    lines.push(`Score: ${scorePercent(card.scores.overall)}`);
    lines.push("");
    lines.push("### Why It Matters");
    lines.push(card.whyItMatters);
    lines.push("");
    lines.push("### Evidence");
    for (const evidence of card.evidence) {
      const metric = evidence.metrics.score ?? evidence.metrics.upvotes ?? evidence.metrics.views ?? 0;
      lines.push(`- ${evidence.source}: [${evidence.title}](${evidence.url}) (${metric})`);
    }
    lines.push("");
    lines.push("### Hooks");
    for (const hook of card.hooks) {
      const suffix = hook.needsFactCheck ? " needs fact-check" : " low-risk";
      lines.push(`- ${hook.style}: ${hook.hook} [${hook.riskLevel};${suffix}]`);
      for (const hookLine of hook.lines.slice(1)) {
        lines.push(`  - ${hookLine}`);
      }
    }
    lines.push("");
    lines.push("### Risks");
    for (const risk of card.risks) lines.push(`- ${risk}`);
    lines.push("");
  });

  return `${lines.join("\n").trimEnd()}\n`;
}
