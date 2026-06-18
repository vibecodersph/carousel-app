# Feature Progress Log

## 2026-06-18, AI Brief JP weekly carousel fix

- Read the handoff and mapped the active artifact to `out/weekly_aibrief_jp`.
- Verified the replacement set. Candidate A had current WSJ reporting, but Candidate B was dated June 1 and outside the June 11 to June 18 target week, so the deck ships as cover plus five verified news slides plus CTA.
- Added `weekly_verifier.py` with pre-render gates for entity type, event verb, specificity, and untraced numeric claims.
- Added `--verify` and `--reuse-cover` to `build_weekly_carousel.py`.
- Added curated source-backed input at `channels/aibrief_jp/weekly_2026-06-18.json`.
- Regenerated `out/weekly_aibrief_jp` as a seven-slide artifact with the existing cover preserved.
- Wrote `out/weekly_aibrief_jp/run_manifest.json` with zero blocked records.
- Exported `out/weekly_aibrief_jp/aibrief_jp_weekly_2026-06-18.pptx`, seven slides, 1080 x 1350.
- Ran `uv run python -m unittest discover -s tests -p 'test*.py' -v`, 55 tests passed.
- Checked rendered slide dimensions, source/footer alignment, body fit, no-em-dash rule, and absence of the old bad claims in shipped output.
