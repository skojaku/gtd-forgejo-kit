---
name: wiki
description: Rules for the personal wiki under wiki/ (inbox, pieces, hubs, literature). Use when reading, creating, editing, or promoting notes; when assigning tags or linking notes to hubs. For searching, use `hq wiki find`.
---

# HQ Wiki

The wiki lives at `wiki/` under the repo root:

```
wiki/
├── inbox/       # raw / unpromoted notes
├── pieces/      # single-topic notes, the main building blocks
├── hubs/        # index notes: links + concise summaries only
├── literature/  # notes on academic papers
├── templates/   # piece.md, hub.md, paper.md — copy when creating new notes
└── tags.md      # canonical tag registry — the ONLY allowed tags
```

**Search** with `hq wiki find "<terms>"` — it returns paths, matching lines, and Forgejo links (dossier-ready).

## Promotion pipeline

1. New notes arrive in `wiki/inbox/`.
2. Promote ready notes into `wiki/pieces/` from `wiki/templates/piece.md`.
3. Assign each piece to one or more hub notes in `wiki/hubs/`; create missing hubs from `wiki/templates/hub.md`.
4. Hub notes contain **only** links and concise summaries — no long-form prose.

## Hard constraints

- Piece notes ≤ 300 lines, at least one tag and one hub link each.
- Only tags listed in `wiki/tags.md`. If a needed tag doesn't exist, flag it for the user — do not invent tags.
- Tags are hierarchical: `topic/subtopic` (e.g. `ml/transformers`). Consult `wiki/tags.md` first.

## Literature notes

- Use `wiki/templates/paper.md`, place in `wiki/literature/`, filename as a lowercase-hyphen slug.
- The arxiv service auto-creates stubs with `status: to-read`; update stubs rather than creating duplicates.

## Frontmatter

Set `created:` (YYYY-MM-DD) on creation and bump `updated:` on every edit.
