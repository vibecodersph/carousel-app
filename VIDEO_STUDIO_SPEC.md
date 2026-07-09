# Video Studio — Agent-Driven Video Generation (Higgsfield MCP Replica)

**Status:** Draft v1 — 2026-07-07
**Goal:** Recreate the Higgsfield MCP experience — "talk to an agent, get a finished video" — using our own provider API keys (fal.ai, Google, OpenAI, ElevenLabs), with zero dependency on Higgsfield.
**Inspiration:** [I Used Higgsfield AI + Claude to Build a $39,500/Month Faceless Channel](https://youtu.be/wU_bmWb6bhg) and Higgsfield's own [faceless-channel workflow post](https://higgsfield.ai/blog/faceless-channel).

---

## 1. What Higgsfield MCP actually is (research findings)

Understanding this is the whole trick. Higgsfield's MCP is **not** a model — it is a thin, well-designed orchestration surface over ~30 third-party models, sold on convenience (one OAuth login, one credit balance, agent-friendly tool shapes). Everything it does can be rebuilt over aggregator APIs we already have keys for.

### 1.1 The hosted MCP surface (`https://mcp.higgsfield.ai/mcp`)

OAuth-authenticated (we probed it: 401 + OAuth resource metadata), credit-billed, works from Claude web/Code, Cursor, etc. It exposes roughly **five tool buckets**:

| Bucket | What it does | Sync/Async |
|---|---|---|
| **Image generation** | Text→image up to 4K; models: Soul 2.0, Nano Banana Pro, Flux 2, Seedream, GPT Image 2. Params: prompt, model, aspect ratio, reference image, style preset, batch, seed | Sync-ish (5–20 s) |
| **Video generation** | Text/image→video up to 15 s; models: Seedance 2.0, Kling 3.0, Veo 3.1, Wan 2.6, Minimax Hailuo. Params: prompt, model, duration, aspect ratio, motion/camera controls, first/last frame | **Async job**: returns a job handle, agent polls |
| **Marketing Studio presets** | One-shot ad generation from a product URL/photo. 9 presets: UGC, unboxing, product review, hyper-motion, TV spot, etc. Preset encapsulates aspect ratio, pacing, shot grammar | Async |
| **Soul Character (cast consistency)** | Train a character ID from 5–20 reference photos (one-time, ~40 credits), then pass `character_id` into any later generation for a consistent face across shots | Async train, then cheap reuse |
| **History / assets** | Browse, search, fetch past generations by ID; reuse any prior output as a reference for the next generation | Sync, free |

Advanced extras: video-to-prompt analysis (deconstruct a reference video into structure/pacing/shots), virality scoring (hook strength / retention metrics), speech-to-video lip-sync (avatar image + WAV audio), voice cloning, subtitles, upscaling/inpainting, brand kits.

### 1.2 The underlying platform API (what the MCP wraps)

From the official [higgsfield-js SDK](https://github.com/higgsfield-ai/higgsfield-js) (base URL `platform.higgsfield.ai`, `Authorization: Key KEY_ID:KEY_SECRET`):

- Endpoints like `/v1/text2image/soul`, `/v1/image2video/dop`, `/v1/speak/higgsfield`, `flux-pro/kontext/max/text-to-image`, plus `createSoulId`, `getMotions()`, `getSoulStyles()`, `uploadImage()`.
- **Job lifecycle** (this is the pattern to copy): submit → get a `job_set_id` → poll every ~2 s (or register a webhook) → terminal status is one of `queued | in_progress | completed | failed | nsfw`. Failed/NSFW refunds credits. Completed returns `results.raw.url` / `results.min.url`.
- **Motion presets**: a library of 100+ named camera/motion moves (bullet time, 360 orbit, crash zoom…) with a strength value — passed alongside the prompt for image2video.

### 1.3 The skills layer (the prompt engineering)

Higgsfield ships [agent skills](https://github.com/higgsfield-ai/skills) (`/higgsfield:generate`, `soul-id`, `product-photoshoot`, `marketplace-cards`). The skill files are where model selection logic, mode-specific prompt enhancement, and workflow choreography live — **the MCP tools stay dumb and generic; the skill makes them feel smart.** We copy this split exactly.

### 1.4 The UX from the video (faceless channel workflow)

1. **Script**: user points Claude at a target channel; Claude reverse-engineers hooks and writes a beat-timed, retention-optimized script (0:00–5:00, 9 acts).
2. **One command → full video**: "make the 5-minute video in 1080p with Seedance 2.0." The agent structures scenes, maps a timeline, generates every clip + voiceover, keeps characters/style consistent, saves files locally.
3. **Packaging**: title variations, timestamped description, tags, 3 thumbnail variants for A/B testing.
4. **Scale**: repeat pipeline for the next trending topic.

Key insight: **the "magic" is Claude doing orchestration in a loop over dumb async tools + a local file workspace.** Nothing in that loop requires Higgsfield.

---

## 2. The UX contract we are replicating

The user talks to **Claude Code or Codex** in a project directory and says things like:

- "Generate a cinematic 5-second wide shot of a neon-lit Tokyo alley at night, rain on the pavement. Use Kling."
- "Run this scene on Veo, Kling, and Seedance and show me all three."
- "Train a character from these 10 photos, then make a 6-shot vertical reel with her using the UGC preset."
- "Make a 3-minute faceless explainer on X, 1080p 16:9, with voiceover, captions, and 3 thumbnails."

And the agent, without further hand-holding: plans shots, fires generation jobs in parallel, polls, retries rejects with tweaked prompts, assembles the final MP4 with ffmpeg, and drops everything in the project folder.

**Non-goals for v1:** web UI, multi-user accounts, Higgsfield's website builder, virtual try-on.

---

## 3. Architecture

```
┌───────────────────────────────┐
│  Claude Code / Codex (agent)  │  ← skills/AGENTS.md = workflow brains
└──────────────┬────────────────┘
               │ MCP (stdio)
┌──────────────▼────────────────┐
│   video-studio MCP server     │  ← TypeScript, dumb generic tools
│  ┌─────────┐ ┌─────────────┐  │
│  │ router  │ │ job manager │  │  ← provider routing, jobs.jsonl ledger
│  └────┬────┘ └─────────────┘  │
│  ┌────▼──────────────────┐    │
│  │ provider adapters     │    │  fal.ai │ Google │ OpenAI │ ElevenLabs
│  └───────────────────────┘    │
│  ┌───────────────────────┐    │
│  │ local render engine   │    │  ffmpeg: assembly, captions, audio mix
│  └───────────────────────┘    │
└──────────────┬────────────────┘
               │
        projects/<slug>/         ← file workspace = shared state
```

Why an MCP server (vs. a pile of scripts): **both Claude Code and Codex speak MCP** (`claude mcp add`, Codex `~/.codex/config.toml [mcp_servers]`), so we build the tool surface once and get the identical UX in both agents. A thin `vstudio` CLI wraps the same core for manual use, mirroring Higgsfield's CLI.

Language: **TypeScript/Node** (`@modelcontextprotocol/sdk`, `@fal-ai/client`, `openai`, `@google/genai`, `elevenlabs`). ffmpeg invoked as a subprocess.

---

## 4. Tool catalog (the MCP surface)

Tools stay generic and Higgsfield-shaped. All generation tools return immediately with a `job_id`; media lands in the workspace and tools return **local file paths + URLs**, never base64 blobs.

### 4.1 Generation

```
generate_image
  prompt: string (required)
  model?: "auto" | "flux-2" | "nano-banana-pro" | "seedream" | "gpt-image"   (default auto)
  aspect_ratio?: "1:1" | "16:9" | "9:16" | "4:5" | "3:2" | "2:3"             (default 16:9)
  resolution?: "1k" | "2k" | "4k"                                            (default 1k)
  reference_images?: string[]        // paths or URLs, for style/subject reference
  character_id?: string              // trained character (see 4.3)
  style?: string                     // named style preset from styles.json
  negative_prompt?: string
  seed?: number
  batch?: 1 | 2 | 4                                                          (default 1)
  → { job_id }                       // image jobs usually complete in one poll

generate_video
  prompt: string (required)
  model?: "auto" | "kling-3" | "seedance-2" | "veo-3.1" | "wan-2.6" | "hailuo" (default auto)
  mode?: "text2video" | "image2video"                                        (inferred)
  input_image?: string               // first frame (image2video)
  last_frame?: string                // optional end frame for interpolation
  reference_images?: string[]        // subject refs (Seedance takes up to 9)
  character_id?: string
  duration_s?: number                // 2–15, model-clamped
  aspect_ratio?: "16:9" | "9:16" | "1:1"                                     (default 16:9)
  resolution?: "480p" | "720p" | "1080p"                                     (default 720p)
  motion?: string                    // named preset from motions.json, e.g. "orbit-360"
  motion_strength?: number           // 0–1
  audio?: boolean                    // native audio for models that support it (Veo, Kling)
  negative_prompt?: string
  seed?: number
  → { job_id }

tts_voiceover
  text: string | script_path
  voice?: string                     // ElevenLabs voice id/name, or "clone:<id>"
  pace?: "slow" | "normal" | "fast"
  → { job_id }                       // output: wav + word-level timestamps json

lipsync
  input_video_or_image: string
  input_audio: string                // wav/mp3
  → { job_id }                       // fal lipsync models (e.g. sync/lipsync, Kling lipsync)
```

### 4.2 Jobs (the async backbone — mirror Higgsfield exactly)

```
get_job        (job_id)        → { status: queued|in_progress|completed|failed|nsfw,
                                   progress?, outputs?: [{path,url,seed}], error?, cost_usd? }
wait_for_jobs  (job_ids[], timeout_s?=600) → same, blocks; server polls providers every 3–5 s
list_jobs      (status?, project?)         → ledger rows
cancel_job     (job_id)
```

### 4.3 Characters (Soul ID replacement)

```
train_character   (name, image_paths[5..20], mode?: "lora"|"reference")  → { job_id → character_id }
list_characters   ()                                                     → [{id, name, thumb, mode}]
```

Two consistency tiers (see §9): `reference` is instant and free (stores curated refs, injected as `reference_images` downstream); `lora` actually trains a Flux/Wan LoRA on fal and stores the weights URL.

### 4.4 Assets & history

```
list_assets  (project?, type?: image|video|audio, query?)  → ledger rows with paths + prompts
get_asset    (asset_id)                                    → full metadata (prompt, model, seed, job)
```

Every generation is journaled to `assets.jsonl`, so "use the third alley shot from yesterday as the style anchor" works.

### 4.5 Local post-production (Higgsfield does this server-side; we use ffmpeg)

```
assemble_video   (timeline: shots.json path | inline, output?, crossfade_s?, music?, duck_db?)
                 // concatenates clips per timeline, lays voiceover + music, loudness-normalizes
add_captions     (video, transcript_json?, style?: "clean"|"bold-word"|"karaoke")
                 // Whisper alignment if no timestamps; burns ASS subtitles
extract_frame    (video, at_s)                    → png   // for last-frame chaining between shots
upscale          (image_or_video, factor?)        → job   // fal ESRGAN/Topaz-class models
make_thumbnail   (background_image, headline, variant_count?=3)   // text overlay via canvas/ffmpeg
```

### 4.6 Intelligence helpers (LLM-backed, cheap)

```
analyze_video    (url_or_path) → structure/pacing/shot-list breakdown   // Gemini video understanding
score_virality   (video|script) → {hook_strength, retention_risks, suggestions}  // LLM rubric
estimate_cost    (plan: shots.json) → per-shot and total USD before committing
get_spend        (project?) → running total from ledger
```

`estimate_cost` + `get_spend` replace Higgsfield's credit balance and enable budget-capped runs ("keep this under $10").

---

## 5. Controls catalog (parameter semantics — the actual "trick")

Higgsfield's value is that its controls map cleanly onto what the underlying models accept. Ours must too:

| Control | How we implement it |
|---|---|
| Model choice / "auto" | Routing table (§6). "auto" picks by task: dialogue+audio→Veo/Kling, action/multi-ref→Seedance, cheap b-roll→Wan/Hailuo, photoreal stills→Nano Banana Pro/Flux |
| Duration, AR, resolution | Passed through; server clamps to each model's envelope and reports the clamp |
| **Motion presets** | `motions.json`: ~60 named presets, each = a **prompt macro** (proven camera-language phrases) + optional model-native camera params. E.g. `"crash-zoom": "rapid crash zoom toward subject, motion blur, handheld energy"`. This is all Higgsfield's presets are, minus their fine-tuned DoP model |
| Style presets | `styles.json`: named looks (film noir, A24 pastel, 90s VHS…) = prompt prefix/suffix + negative prompt + optional LoRA |
| First/last frame | Native on Kling/Seedance/Veo i2v; enables **shot chaining**: `extract_frame(shot N end) → input_image(shot N+1)` for continuity |
| Character consistency | §9 |
| Seed | Passed through where supported; journaled always |
| Batch / variants | Fan out N jobs with seed jitter; return all |
| Marketing presets | `presets/*.json`: shot-grammar templates (shot count, durations, AR, pacing, prompt scaffolds per shot). UGC, unboxing, review, hyper-motion, TV spot — same nine as Higgsfield |

---

## 6. Provider routing

**fal.ai is the primary backend** — it hosts nearly the entire Higgsfield model catalog under one key (Kling 3.0, Seedance 2.0 Fast/Pro, Veo 3.1 (+Lite), Wan 2.6, Hailuo, Flux 2, Nano Banana Pro, Seedream 5, LoRA trainers, lipsync, upscalers). Direct-provider adapters are fallbacks/cost-optimizers.

| Capability | Primary | Alternate |
|---|---|---|
| Video gen (Kling 3, Seedance 2, Wan 2.6, Hailuo) | fal.ai | Replicate; BytePlus (Seedance direct) |
| Video gen (Veo 3.1) | fal.ai | Google Gemini API direct |
| Image gen (Flux 2, Seedream, Nano Banana Pro) | fal.ai | Gemini API (Nano Banana), OpenAI gpt-image |
| Voiceover + voice clone | ElevenLabs | OpenAI TTS |
| Lip-sync | fal.ai (sync/lipsync, Kling lipsync) | — |
| LoRA character training | fal.ai (Flux LoRA fast trainer) | Replicate |
| Upscale | fal.ai | — |
| Script/SEO/virality/video-analysis | Claude / Gemini (existing keys) | — |
| Assembly, captions, thumbnails | local ffmpeg + Whisper | — |

⚠️ **Sora 2:** reporting indicates OpenAI shut down the Sora 2 API (March 2026) and fal removed it. Do not build against it; treat as optional adapter if it returns.

**Model registry:** exact fal endpoint IDs change as models rev. Keep `models.json` (capability → provider → endpoint id → param envelope → $/unit) and a `scripts/sync-models.ts` that verifies IDs against fal's catalog at build time. Codex should resolve current IDs during M1/M2, not hardcode from this doc.

---

## 7. Workspace layout (file system = shared state)

```
video-studio/
  models.json  motions.json  styles.json  presets/     # control catalogs
  characters/<id>/{refs/, lora.json, card.json}
  projects/<slug>/
    project.json        # brief, AR, resolution, budget cap, target platform
    script.md           # beat-timed script (agent-authored)
    shots.json          # THE central artifact — see below
    jobs.jsonl          # every provider job: id, tool, params, status, cost
    assets.jsonl        # every output: id, type, path, prompt, model, seed, job_id
    assets/{images,clips,audio}/
    renders/            # final.mp4, captioned.mp4, thumbs/
    package/            # titles.md, description.md, tags.txt
```

`shots.json` (the timeline contract between agent and tools):

```json
{ "fps": 24, "aspect_ratio": "16:9", "resolution": "1080p",
  "voiceover": {"path": "assets/audio/vo.wav", "timestamps": "assets/audio/vo.words.json"},
  "music": {"path": "assets/audio/bed.mp3", "duck_db": -12},
  "shots": [
    { "id": "s01", "t_start": 0.0, "duration_s": 6,
      "beat": "HOOK: what if the ocean disappeared?",
      "prompt": "aerial hyperlapse over a drained ocean floor...",
      "model": "seedance-2", "motion": "push-in-slow",
      "character_id": null, "clip": "assets/clips/s01_v2.mp4", "status": "approved" } ] }
```

---

## 8. Pipeline playbooks (encoded as agent skills, not server code)

Ship as Claude Code skills (`.claude/skills/`) **and** an `AGENTS.md` for Codex — same instructions, two front-ends, mirroring Higgsfield's skills repo.

### P1 `/faceless-video <topic>` — the workflow from the video
1. Research topic + (optional) analyze a target channel's hooks → write `script.md` with beat timings and a HOOK in the first 3 s.
2. `tts_voiceover` the narration → word timestamps set the real shot durations.
3. Draft `shots.json`: one shot per beat, prompts written in camera language, style preset applied globally, character refs where a recurring cast exists.
4. `estimate_cost` → confirm against `project.json` budget cap.
5. Fan out `generate_video` for all shots in parallel → `wait_for_jobs`.
6. Review pass: agent inspects failures/NSFW, tweaks prompts, regenerates (max 2 retries/shot).
7. `assemble_video` → `add_captions` (bold-word style for retention).
8. `generate_image` ×3 thumbnail backgrounds → `make_thumbnail` with headline variants.
9. Write `package/`: 5 title variants, timestamped description, tags.
10. Report: final paths, total spend, per-shot cost table.

### P2 `/shot <description>` — single cinematic clip (Pattern 1: quick asset)
### P3 `/bakeoff <description>` — same prompt on Kling vs Seedance vs Veo, side-by-side contact sheet (Pattern 2)
### P4 `/ugc-ad <product-url-or-image>` — Marketing-Studio-style preset: scrape product copy → preset shot grammar → assembled vertical ad (Pattern 3)
### P5 `/character <name> <photos...>` — train + save a reusable cast member

### P6 Long-form continuity trick
Models cap at ~15 s/clip. For minutes-long output: chain shots with `extract_frame` last-frame → next `input_image`, hold one style preset + seed family + character refs. Voiceover timestamps, not clip lengths, drive the cut points.

---

## 9. Character consistency without Soul ID

- **Tier 1 — reference mode (default, free, instant):** curate 5–20 refs → store in `characters/<id>/refs/`. Inject top-K refs into `reference_images` (Nano Banana Pro multi-image for keyframes; Seedance 2.0 reference-to-video takes up to 9 images). Generate character keyframes first, then image2video — the face stays locked because it's in the input frame.
- **Tier 2 — LoRA mode (Soul-ID equivalent, ~$2–5 one-time):** fal Flux LoRA trainer on the ref set → `character_id` resolves to LoRA weights, applied on all keyframe generations. Better for large multi-video casts (the "Maya across 20 scenes" case).

Workflow either way: **character keyframe (image) → animate (image2video)** — never trust text2video to hold a face.

---

## 10. Job manager (server internals)

- Submit via provider adapter → normalize to Higgsfield's status vocabulary: `queued | in_progress | completed | failed | nsfw`.
- Poll queue-based providers every 3–5 s with backoff; fal supports webhooks + `subscribe` — use where available.
- Journal every transition to `jobs.jsonl` (append-only; safe resume after a crashed session — `list_jobs status=in_progress` on startup re-attaches).
- On `completed`: download to `assets/`, write `assets.jsonl` row, record actual cost.
- On `failed`/`nsfw`: keep the row with the error; the *skill* decides whether to rewrite the prompt and retry (intelligence stays agent-side).
- Concurrency cap (default 4 video jobs) to control burn rate.

---

## 11. Cost guardrails

- `models.json` carries $/second (video) and $/image; `estimate_cost` prices a `shots.json` before any spend.
- `project.json.budget_usd` is a hard cap: the server refuses new jobs past it (override flag exists).
- `get_spend` gives running totals; the P1 skill reports cost per finished video (target: a 3-min faceless video ≈ $8–20 depending on model mix — Wan/Hailuo b-roll cheap, Veo/Kling hero shots expensive).

---

## 12. Build plan (milestones for Codex)

| M | Deliverable | Acceptance test |
|---|---|---|
| **M0** | Repo scaffold: MCP server boots on stdio, registers with Claude Code + Codex; workspace init; `models.json` seeded by `sync-models` against live fal catalog | `claude mcp list` / Codex shows tools; `vstudio init demo` creates project |
| **M1** | `generate_image` + jobs + assets ledger (fal Flux/Nano Banana) | "generate a cat in 9:16 on flux" → PNG in `assets/images/`, ledger row |
| **M2** | `generate_video` (Kling + Seedance + Wan via fal), `wait_for_jobs`, `extract_frame` | 5-s clip from prompt; i2v from M1 image; chained 2-shot continuity |
| **M3** | `assemble_video` + `tts_voiceover` (ElevenLabs + word timestamps) + `add_captions` (Whisper align, ASS burn) | 3 clips + VO → captioned 30-s MP4, loudness-normalized |
| **M4** | Characters: reference mode + fal LoRA mode; `make_thumbnail`; `upscale`; `lipsync` | Same face across 3 scenes; 3 thumbnail variants |
| **M5** | Skills: P1–P5 as `.claude/skills/` + `AGENTS.md`; motions/styles/presets catalogs; `estimate_cost`/budget caps | `/faceless-video "why the ocean glows"` runs end-to-end unattended |
| **M6** | Polish: `analyze_video` (Gemini), `score_virality`, `/bakeoff` contact sheets, webhook polling, resume-after-crash | Bake-off returns 3 labeled clips + recommendation |

Suggested stack: Node 22, TypeScript, `@modelcontextprotocol/sdk`, `@fal-ai/client`, `openai`, `@google/genai`, `elevenlabs`, `fluent-ffmpeg` (or raw spawn), `zod` for tool schemas.

### Env
```
FAL_KEY=            # primary generation backend
GEMINI_API_KEY=     # Veo direct (optional), video analysis, Nano Banana fallback
OPENAI_API_KEY=     # gpt-image fallback, Whisper (or use local whisper.cpp)
ELEVENLABS_API_KEY= # voiceover + cloning
ANTHROPIC_API_KEY=  # only if server-side LLM helpers run outside the agent
```

---

## 13. Risks & open questions

1. **fal model ID drift** — mitigated by `sync-models` + registry; never hardcode endpoint IDs in adapters.
2. **Seedance "all-in-one with audio" parity** — Higgsfield leans on Seedance 2.0's native audio+multi-ref. Verify fal's Seedance 2.0 exposes audio + 9-ref inputs; if not, our VO+ffmpeg path covers it (and gives *more* control).
3. **NSFW/safety rejections** vary per provider; the retry-with-rewrite loop in skills is the mitigation.
4. **Long-form pacing quality** — first assembled cuts may feel mechanical; iterate caption style, crossfades, music ducking in M5.
5. **Where this lives** — recommend a fresh repo (`video-studio/`), not carousel-app; carousel-app can later call the same MCP for reels.

---

## 14. Source notes (research trail)

- [Higgsfield MCP product page](https://higgsfield.ai/mcp) — tool buckets, models, controls, setup
- [MCP guide with tool/parameter detail](https://mcp.directory/blog/higgsfield-mcp-guide) — 5 tool categories, params, agent patterns
- [Official higgsfield-js SDK](https://github.com/higgsfield-ai/higgsfield-js) — endpoints, job lifecycle, polling/webhooks, Soul IDs
- [Community MCP wrapper](https://github.com/geopopos/higgsfield_ai_mcp) — minimal 5-tool shape (generate_image, generate_video, create_character, get_generation_status, list_characters)
- [Higgsfield skills repo](https://github.com/higgsfield-ai/skills) — the skills/prompt-enhancement layer
- [Faceless channel workflow](https://higgsfield.ai/blog/faceless-channel) + [Claude MCP video guide](https://higgsfield.ai/blog/Generate-AI-Videos-From-Claude-with-Higgsfield-MCP) — the exact UX in the YouTube video
- [fal.ai model catalog](https://fal.ai/explore/models) — Kling 3.0, Seedance 2.0, Veo 3.1, Wan 2.6, Flux 2, Nano Banana Pro availability
