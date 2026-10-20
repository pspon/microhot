# Prompts

These files are where MicroHOT's editorial judgment lives. Edit them freely; the next run uses them.

- `triage.md` decides what is worth your attention. The model returns five integer axes per item
  (substance, new information, evidence, resonance, actionability) plus a content type; the 0–100 score is
  computed in `src/microhot/triage.py` from per-type weights, and compared with the thresholds in
  `config/site.toml`. The reader description in the first paragraph is the biggest lever.
- `write.md` sets how headlines and summaries are written in English and Traditional Chinese (Taiwan), and the
  one-line "why it matters" note for beats with `why = true` in `config/site.toml` (the Canada & Ontario beat).
  The reader description in its "Why it matters" section decides what the notes focus on.
- `digest.md` writes the overview for a big story from several sources' reports (stories with three or more
  sources; see `[digest]` in `config/site.toml`).
- `review.md` writes the short overview at the top of the weekly and monthly reviews.

Keep in mind:

1. Keep the JSON shape in the "Output" section unchanged; the code validates it.
2. Keep each file stable between runs. The date and the items go in the user message, so the system prompt is
   identical on every call and providers can cache it (cheaper and faster).
3. After a change, check the About page's "Near misses" table for a day or two before moving thresholds.

All four are adapted from AIHOT's prompts (MIT); see `NOTICE`.
