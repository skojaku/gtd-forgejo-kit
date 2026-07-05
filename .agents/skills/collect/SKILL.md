---
name: collect
description: Build a dossier on a task issue — gather context from Gmail, Drive, and the wiki, and post it as one sourced comment. Use when asked to "collect for #N", "prepare context", "build a dossier", or during a collect job.
---

# Collect — the dossier recipe

Goal: give the user everything they need to act on a task, as clickable sources with short excerpts. You collect and summarize; you never decide, promote, close, book, or send.

Follow these steps exactly, one command each:

1. **Context.** `hq task context <issue>` — read the title, body, fields, last dossier, and `user_comments_since`. Treat the user's comments as directives (what to look for, what to skip).
   - If `last_dossier` is under 3 days old AND `user_comments_since` is empty, stop: reply that the dossier is current. Post nothing.

2. **Search mail.** Up to 2 precise queries: `hq mail find --text "<key terms>" --newer-than 6m --max 5` (or `--from` when a person is named). Pick the relevant hits.

3. **Search drive.** Up to 2 queries: `hq drive find --text "<key terms>" --max 5`. For the single most relevant Google Doc, optionally `hq drive show <id> --query "<term>"`.

4. **Search wiki.** Up to 2 queries: `hq wiki find "<key terms>" --max 5`.

5. **Write entries.json** — only sources that genuinely help this task (fewer, better entries beat padding; 0–12 entries):

```json
{
  "summary": "2-3 sentences: what you found and what it means for this task.",
  "decision_needed": "Only if the user must choose something — state the question. Otherwise omit.",
  "entries": [
    {"source": "gmail", "url": "<url from hq mail output>", "title": "Subject or doc name", "excerpt": "2-3 sentences of what this source says."}
  ]
}
```

- `source` is one of `gmail|drive|wiki|calendar`. Always copy `url` from CLI output — never construct URLs yourself.
- Set `decision_needed` when the sources reveal a fork only the user can resolve (e.g. two conflicting deadlines, an unanswered question from a collaborator).

6. **Post — mandatory.** Write the JSON to a file, then run `hq task dossier <issue> entries.json`. The run is NOT done until this command has printed `"posted": true`. Printing the JSON without posting is a failed run. Post at most one dossier per run.

Never: change task status or fields, send/label email, book calendar, edit wiki files during a collect run.
