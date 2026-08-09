# Reel candidate evaluation against measured winners

`scripts/evaluate_reel_candidates.py` reads reconciled `candidates.json` files
from `reel-app`, sends each hook and transcript through a two-pass semantic LLM
review, and writes a reviewable Markdown report plus machine-readable JSON. The
default for candidate batches is `--analysis-mode llm`.

It does not modify source candidates, localize copy, render, schedule, convert
to Trial, remove, or publish anything.

## Required data-transfer approval

LLM mode sends these fields to the configured Gemini API account:

- each unpublished candidate's primary hook and transcript, identified only by
  an opaque local join reference;
- each winner's published hook, selected source hook, and source transcript,
  also identified by an opaque reference, plus Top-10 category membership;
- on the second pass, exact fixed-24-hour evidence for the winner analogues
  selected before metrics were visible.

Source titles, uploaders, URLs, platform media IDs, permalinks, Japanese
scripts, candidate chapters, timestamps, and candidate durations remain local
and are joined into the finished report after the model responds.
Candidate `hook_variants`, winner `source_hook_variants`, and localized hook
option lists are deliberately excluded from the prompts, citation evidence,
and evaluation output. Their source files are not modified.

Do not run LLM mode unless that transfer is approved for the account and
material. The API key is loaded from `GEMINI_API_KEY` or `GOOGLE_API_KEY` in
the process environment, repository `.env`, or `~/.hermes/.env`. No candidate
or winner content is sent when using the
explicit `--analysis-mode diagnostic` mode. LLM mode also requires the
`--approve-gemini-data-transfer` acknowledgement so an unattended command
cannot begin sending this material accidentally.

## Run a fixed batch

Explicit paths are preferred because they make the reviewed batch unambiguous:

```sh
uv run --frozen python scripts/evaluate_reel_candidates.py \
  --analysis-mode llm \
  --approve-gemini-data-transfer \
  --candidates /Users/aiagent/GitHub/reel-app/outputs/Ybrl4FYM57c \
  --candidates /Users/aiagent/GitHub/reel-app/outputs/_cmpIveXnvE \
  --candidates /Users/aiagent/GitHub/reel-app/outputs/P3KDebPTUrw \
  --markdown-out out/reel_candidate_evaluation.latest.md \
  --json-out out/reel_candidate_evaluation.latest.json
```

For an operator convenience view, select the three most recently modified
candidate files:

```sh
uv run --frozen python scripts/evaluate_reel_candidates.py \
  --analysis-mode llm \
  --approve-gemini-data-transfer \
  --latest-from /Users/aiagent/GitHub/reel-app/outputs \
  --latest-count 3 \
  --markdown-out out/reel_candidate_evaluation.latest.md \
  --json-out out/reel_candidate_evaluation.latest.json
```

Modification time identifies the newest pipeline outputs, not the newest
source-video publication dates. Use explicit paths for a durable review.

When a `candidates.json` mixes already-published clips with prospective clips,
use an exact slug allowlist so realized posts are not judged as hypothetical
candidates or allowed to cite themselves:

```sh
uv run --frozen python scripts/evaluate_reel_candidates.py \
  --analysis-mode llm \
  --approve-gemini-data-transfer \
  --candidates /path/to/candidates.json \
  --candidate-slug 003-prospective-candidate \
  --candidate-slug 004-another-prospective-candidate
```

The report records the requested slugs and the before/after candidate count.
Refresh the winner library first so newly matured published siblings can serve
as labeled same-source evidence. The report distinguishes same-source support
from repeatability across independent source videos.

The default measured input is:

```text
out/reel_report.moneyball.winner_library.json
```

Refresh that library before evaluating a new batch:

```sh
uv run --frozen python scripts/run_moneyball_analytics.py --channel aibrief_jp
```

## What the LLM receives

For each reconciled candidate, the semantic judge receives:

- candidate primary hook, source transcript, and an opaque local reference;
- winner published hook, selected source hook, source transcript, opaque
  reference, and Top-10 category membership;
- in Pass B only, winner fixed-24-hour metric values, ranks, raw
  numerators/denominators, actual observation age, evidence family, and
  provenance flags for the already selected analogues.

Source titles, uploader names, source URLs, timestamps, durations, platform
media IDs, Instagram permalinks, Japanese scripts, and the candidate-generator
`score`, `hook_score`, `value_score`, prior `reason`, and
`opening_assessment` are deliberately excluded from model prompts. They remain
local and are joined into the report where useful. This prevents the new judge
from repeating the old judge's answer and keeps the transfer within the
approved field scope.

Alternate hook variants are not evaluation evidence. The judge assesses the
actual primary candidate hook against the winner's published and selected
source hooks; unused alternatives cannot rescue or veto a candidate.

## Two-pass semantic review

### Pass A — metrics hidden

The model reads the candidate and every winner asset. It can see which Reel IDs
belong to each of the six Top-10 categories, but:

- the category IDs are sorted by media ID rather than rank;
- rank, metric value, percentile, reach, and other performance values are
  hidden;
- it must compare audience promise, hook mechanism, tension, proof, payoff, and
  delivery structure;
- it must return a result for every category and may select zero analogues;
- topic, company, speaker, or vocabulary overlap alone is labeled
  `SURFACE_ONLY`.

### Pass B — exact evidence revealed

Only after Pass A is locked does a verifier receive exact 24-hour metrics for
the selected analogue IDs. The verifier cannot add a new winner after seeing
its rank. It challenges claim support, verbatim citations, surface-match risk,
duration confounding, and causal or predictive language before choosing a
decision.

The program then joins exact rank, metric value, raw numerator/denominator,
observation age, permalink, and evidence flags from the library. The model is
not trusted to generate those facts.

## Semantic relationship labels

- `CLOSE_MECHANISM` — same audience promise, hook mechanism, payoff form, and
  broadly comparable delivery.
- `PARTIAL_MECHANISM` — at least two meaningful structural similarities with
  an important divergence.
- `SURFACE_ONLY` — same topic, entity, speaker, or vocabulary without the same
  promise and payoff.
- `CONTRAST` — useful counterexample, not supporting evidence.

## Decisions

- `ADVANCE` — central claims are supported, payoff is delivered, and the
  mechanism has credible evidence across both independent signal families or
  multiple distinct-source close analogues.
- `ADVANCE_AS_TRIAL` — strong supported idea with thin, single-family, novel,
  or materially duration-confounded evidence.
- `REVISE` — valuable material exists, but the hook overclaims, context arrives
  too late, payoff is buried, or a specific structural fix is required.
- `REJECT` — no distinctive supported payoff, the central hook cannot be
  repaired without invention, or the segment is redundant without a new test
  hypothesis.
- `MANUAL_REVIEW` — input is incomplete, attribution is ambiguous, evidence
  conflicts, or the required LLM call failed.

All of these are experiment recommendations, not performance predictions.

## Six comparisons, two evidence families

Every completed candidate has a separate comparison for:

1. total interactions / reach;
2. watch depth;
3. 3-second skip rate;
4. saves / 1,000 reach;
5. views / reached account;
6. balanced aggregate Top 10.

Watch depth, skip rate, and views/reached form the related
`ATTENTION_REPLAY` family. Interactions/reach and saves/reach form the related
`INTENT_ACTION` family. Aggregate membership is correlated summary evidence,
not a third family or a sixth vote.

## Empty-folder false-negative audit

When a source has `clips: []`, every row rejected by
`work/ai_candidate_discriminator.json` receives an independent LLM screen. The
screen does not receive the old score or rejection reason. Only
`LIKELY_FALSE_NEGATIVE` and `POSSIBLE_FALSE_NEGATIVE` rows enter the full
two-pass comparison, up to `--max-false-negative-deep-reviews`. The screen
assigns `TOP_5`, `SECONDARY`, or `NO_DEEP_REVIEW`; the highest-priority rows
receive unique ranks 1–5 and are reviewed in that order. Secondary rows are
eligible only when the configured deep-review limit exceeds the ranked set;
source order is used only as a final stable tie-breaker.

The audit never edits `candidates.json` and reports zero automatic promotions.
Use `--no-false-negative-audit` only when the independent screen is not wanted.

## Reproducibility and cost control

LLM judgment is not mathematically deterministic. Identical requests are cached
under `state/llm_reel_candidate_evaluator_cache`, keyed by model, reasoning
effort, prompt/schema version, the sanitized primary-hook-only winner context,
and candidate input. Raw candidate and winner-library hashes remain in reports
as source-file provenance, but unused variants do not enter the response-cache
identity.
Response ID, model, usage, request hash, and prompt version are stored with
each result. Use `--no-llm-cache` to force a genuinely new review.

The default is the current stable `gemini-3.6-flash` model with medium
reasoning and three concurrent candidate workers. Override with `--model`,
`--reasoning-effort`, and `--workers`. The implementation uses Google's
documented Gemini compatibility endpoint; the installed OpenAI Python package
is only the HTTP protocol client and no request is sent to OpenAI.

## Important limits

- Candidate transcripts are source excerpts, not final Japanese Reel scripts.
- The evaluator reads the full selected transcript excerpt, but it does not
  inspect video frames, B-roll, on-screen captions, audio delivery, or material
  outside that selected excerpt.
- Similarity does not prove that the same hook structure caused a winner's
  result.
- Follows, returning viewers, and production efficiency remain unavailable
  where the Graph API or annotations do not provide them.
- Source and uploader concentration are warnings, not automatic proof that a
  candidate will fail.
- If an LLM call fails, the row is `API_ERROR` / `MANUAL_REVIEW`; there is no
  lexical fallback.

For a read-only view of the old transparent rules, use:

```sh
uv run --frozen python scripts/evaluate_reel_candidates.py \
  --analysis-mode diagnostic \
  --candidates /path/to/source-folder
```

That mode is diagnostic-only and must not be represented as semantic content
judgment.

## Recheck the scheduled pipeline

Scheduled mode filters the ledger before evaluation, so only the exact active
clip slugs are judged. This matters because a source `candidates.json` can
contain unscheduled siblings that would otherwise change source counts and
diversity decisions.

For a two-pass LLM evaluation of the actual scheduled hooks, use:

```sh
uv run --frozen python scripts/evaluate_reel_candidates.py \
  --analysis-mode llm \
  --approve-gemini-data-transfer \
  --reasoning-effort high \
  --workers 6 \
  --scheduled-db state/reels.db \
  --scheduled-status scheduled \
  --channel aibrief_jp \
  --exclude-source-video PREVIOUSLY_REVIEWED_VIDEO_ID \
  --markdown-out out/reel_scheduled_candidate_evaluation.llm.md \
  --json-out out/reel_scheduled_candidate_evaluation.llm.json
```

This mode uses the scheduled title—the actual on-video overlay—as the one
primary candidate hook. For registered Trial Reels, that title matches the
experiment variant hook; the baseline caption hook is retained only as local
schedule context. The source-selection hook remains local provenance and
unused hook variants are excluded. Candidates are flattened across source
folders so all workers remain occupied; each candidate still receives its own isolated
Pass A and Pass B responses. Six workers are a conservative bulk default.
Completed passes are cached individually, so an interrupted run can resume
without paying for successful calls again. The report preserves the scheduled
timestamp, content hash, current regular/Trial lane, exact clip slug, and source
folder. It never changes the ledger.

`--exclude-source-video` is an exact, repeatable source-folder exclusion and is
recorded in the report. Bulk LLM runs require explicit approval for the full
scheduled batch because they transfer unpublished primary hooks/transcripts
and internal winner evidence to Gemini.

For the older deterministic schedule diagnostic, use:

```sh
uv run --frozen python scripts/evaluate_reel_candidates.py \
  --analysis-mode diagnostic \
  --scheduled-db state/reels.db \
  --channel aibrief_jp \
  --markdown-out out/reel_scheduled_candidate_evaluation.md \
  --json-out out/reel_scheduled_candidate_evaluation.json \
  --csv-out out/reel_scheduled_candidate_evaluation.csv
```

The scheduled report joins each result to its content hash, slot, Japanese
scheduled hook, media, manifest, current regular/Trial lane, and registered
Trial experiment. Its schedule labels are deliberately conservative:

- `KEEP EXISTING TRIAL` preserves registered one-variable experiments.
- `KEEP REGULAR — SUPPORTED, NOT PROVEN` means the creative passes and has a
  thin or developing measured analogue set.
- `TRIAL CANDIDATE` means the creative passes but has no relevant measured
  analogue; it may be sent to the separate capacity-aware Trial selector.
- `REVISE CREATIVE` identifies a known source-candidate score miss.
- `RESCORE / MANUAL REVIEW` preserves legacy clips whose historical opening
  score is unavailable.
- `SOURCE SUPPORT REVIEW` and `DIVERSITY REVIEW` are review flags, not duplicate
  or removal proof.

The same run displays the next conversion chosen by the existing Trial policy,
including its target date, Facebook effect, capacity/cooldown context, and
whether the candidate evaluator agrees. It does not execute the dry run or
apply the conversion.

Scheduled mode is read-only. It reports zero safe automatic removals and never
changes the Reel or Facebook ledgers, manifests, media, schedule, or Trial
records. Rerun it near publication because the measured winner library and
news freshness change over time.
