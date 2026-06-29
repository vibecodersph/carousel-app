Score each carousel title candidate against four filters.

Filters:
- saveable, 0 to 5: someone would save this for later use.
- authority, 0 to 5: we can rank it with more credibility than a random list.
- decisionUseful, 0 to 5: it helps a real decision, not trivia.
- compounds, 0 to 5: it can become a repeatable series or update cleanly.

Kill rules:
- Kill if saveable, authority, or decisionUseful is below 3.
- Kill if total is below 14.
- Auto-kill any generic listicle that any account could write with no point of view.
- A substantive angle and twist are mandatory.

Return strict JSON only:
{
  "saveable": 0,
  "authority": 0,
  "decisionUseful": 0,
  "compounds": 0,
  "killed": true,
  "reason": "short reason"
}

Never use em dashes.
