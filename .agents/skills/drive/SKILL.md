---
name: drive
description: Google Drive search via `hq drive` (read-only). Use to find work documents, spreadsheets, or slides and pull short excerpts. Trigger on "drive", "document", "doc", "spreadsheet".
---

# Drive

Read-only. Use `hq drive --help` for exact usage; run commands directly.

- `hq drive find --text "..."` and/or `--name "..."` (optionally `--type doc|sheet|slides|pdf`) — returns names, dates, and clickable `url`s.
- `hq drive show <file_id> --query "..."` — plain-text window from a Google Doc around the query.

Pattern: find → show the top hit → cite the `url`. For non-Doc files, link them; the user reads in the browser.
