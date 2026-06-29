# VibeCoders PH Instagram Voice

The Instagram voice for VibeCoders PH: witty, Taglish-native, cover-first.

This is a channel specialization of the base VibeCoders brand voice, not a
replacement. Everything in the base voice still holds: warm with people, sharp
with craft, no generic affirmations, no guru tone, no bootcamp-ad voice, no
em dashes, no slay/ate/bestie brand-as-Gen-Z.

## What Changes For IG

1. Audience widens. IG talks to devs and common folk alike. The dev is still
   the core. The non-dev is the reach.
2. Taglish becomes the default register. We code-switch the way Filipinos
   actually talk, not English with a Tagalog garnish.
3. Wit moves to the center. Smart-funny, never clown-funny.

## The Voice In One Breath

A Pinoy builder who is genuinely funny and actually ships. Talks to you like a
barkada na magaling mag-code, not a guru, not a brand, not a bootcamp ad. Makes
you laugh about the thing, then shows you the thing works. Taglish kasi totoong
tao. Honest about tools, warm with people, never the reverse.

## Cover Page System

The cover is slide 1. The swipe from slide 1 to slide 2 is the only event that
matters.

Cover anatomy:

- Kicker or handle: the account handle or a short section label.
- Headline: the hook. Punchy or editorial, depending on the post.
- Accent keywords: one or more words/short phrases in brand accent color. They
  should be the funny, surprising, or emotional terms, never more than half the
  headline.
- Swipe line: short and natural. Examples: "swipe mo", "tuloy sa slide 2",
  "paano? swipe".

Two cover modes:

- Punchy hook mode, default: 4 to 8 words, maximum pull.
- Editorial headline mode: one full sentence, up to about 14 words, witty
  headline cadence, usually for news or launch posts.

## Both-Audiences Test

Before a cover ships, it must pass both:

- A non-dev reads it and laughs, winces, or thinks "ako 'to."
- A dev reads the same line and nods, because it is also true about the actual
  thing.

If it only passes one, rewrite.

## Wit Rules

Pinoy wit here is observational, relatable, and true. We apply everyday Filipino
life to AI, tech, and the builder grind.

Good sources of humor:

- Builder life as Pinoy life: side projects that ghost you, puyat culture,
  "next week ko na tatapusin," payday-driven tool subscriptions.
- AI hype fatigue: may bagong model na naman, screenshot-hoarding experts,
  course sellers, LinkedIn thought-leader cosplay.
- Everyday anchors: GCash, sari-sari, traffic sa EDSA, group chat dynamics,
  KKB, left on read, utang, kapitbahay, tara coffee.

Rules:

- Funny plus true. The joke must carry a real insight.
- One joke per cover.
- Punch up at hype or sideways at our own builder habits.
- Never punch down at beginners, members, or the reader's intelligence.
- Self-deprecation is welcome. We ghost our side projects too.

Avoid:

- Forced memes or references that age in a week.
- Corny dad-joke energy or pun pile-ups.
- Generic listicle headers.
- English-only community-facing covers.
- Hype openers like "stop scrolling" or "this changed everything."
- Brand talking like a Gen-Z teen: slay, ate, bestie.

## Taglish Rules

- Covers and captions are Taglish-native by default.
- Body slides can lean English for technical precision, with Taglish framing.
- Never translate inline. No "astig (which means cool)."
- Code-switch naturally. If it sounds like English with Tagalog garnish,
  rewrite.

## Caption Voice

The carousel does the work. The caption should open the loop, add one more
true/funny line, and give one clean action.

- Open with a reason to swipe, in Taglish. No housekeeping.
- One main idea. Witty first, useful right after.
- Keep it short. Prefer 3 to 4 short blocks: hook, useful line, one CTA,
  hashtags plus source.
- One CTA only: save, send, comment, or DM keyword. No CTA stacking.
- Hashtags should be clean and non-spammy: a few PH/community tags, a few AI
  tags, one or two broad.
- One emoji max, often zero.
- End with source attribution when the post is based on a public source.
- Avoid generic hype phrases like "completely change," "game-changing,"
  "ultimate guide," "must-read," "let us know in the comments below," and
  "stop scrolling."

## Hook Archetypes

Problem recognition:

- "Ginagamit mo pala nang mali ang ChatGPT, at hindi mo [alam]."

Contrarian claim:

- "Hindi design ang dahilan kung bakit bumagsak ang [carousel] mo."

Numbered promise:

- "3 AI drops ngayong linggo na magbabago sa [trabaho] mo."

Curiosity gap:

- "May ginagawa ang mga nag-vi-viral na account na hindi [ginagawa] mo."

Named target:

- "Para sa devs na mas mabilis mag-ghost ang side project kaysa sa [ex] niyo."

Hugot or joke hook:

- "AI will not leave you on read. Yan lang naman ang [hanap] mo, di ba?"

## Copy-Paste Prompt Block For Automation

Use this as the cover-generation instruction in the pipeline.

```text
You write Instagram cover lines for VibeCoders PH, the AI account for Filipinos
who build things and the curious folk around them.

Voice: witty Pinoy builder talking to a barkada. Taglish-native, code-switch the
way Filipinos actually talk. Smart-funny, never clown-funny. Warm with people,
sharp with craft.

Hard rules:
- ZERO em dashes. Use commas, periods, colons, or parentheses.
- The cover must work for BOTH a non-dev, who laughs or feels seen, and a dev,
  who nods because it is also true about the real thing. If it only works for
  one, rewrite.
- One joke per cover. The joke must carry a real insight, not just be a joke.
- Punch up at hype or sideways at our own builder habits, never down at people.
- One or more accent words/short phrases: the funny, surprising, or emotional
  terms. Never noun-of-convenience highlights like "tools" or "tips." Mark each
  with [brackets], and keep highlighted text under 50% of the headline.
- Punchy mode: 4 to 8 words. Editorial mode, for news or launch posts: one
  sentence, up to about 14 words, witty headline cadence.
- No generic listicle headers, no solution-as-hooks, no vague promises, no hype
  openers like "stop scrolling" or "we are excited to announce."
- No slay/ate/bestie brand voice. One emoji max, usually zero. No ALL CAPS.

For cover output: kicker line, headline with one or more accent words/phrases in
[brackets], and swipe line.

For caption output: short Taglish Instagram caption with one main idea, one CTA,
clean hashtags, and source attribution when provided.
```

## Pre-Post Cover Checklist

- Passes the both-audiences test: a non-dev laughs or feels seen, a dev nods.
- One or more accent words/phrases, all funny, surprising, or emotional, with
  highlighted text under 50% of the headline.
- One joke, and it carries a true insight.
- Taglish-native, not English with a Tagalog garnish.
- Right cover mode for the post.
- Not a listicle header, solution-as-hook, vague promise, or hype opener.
- Humor punches up or sideways, never down at a member or beginner.
- Zero em dashes. One emoji max. No ALL CAPS for emphasis.
- Swipe line present.
