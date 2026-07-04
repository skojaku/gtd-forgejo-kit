Triage exactly one email. Message id: {{message_id}}

Step 1 — read it (run from the repo root):
    ./bin/hq mail read {{message_id}}

Step 2 — classify it as exactly one of:
- TASK: it asks the user for real work, a decision, a deadline, a deliverable,
  OR a reply the user must write themselves.
- NOISE: newsletter, notification, receipt, automated alert, spam.

Step 3 — act:
- TASK → write a file /tmp/triage-{{message_id}}.json containing
      {"title": "<verb-first summary>", "notes": "From: <sender>\nEmail: <url from step 1 output>\n<!-- email-thread:{{thread_id}} -->"}
  Copy the `<!-- email-thread:... -->` line EXACTLY as written — it links future
  replies on this email thread back to this issue. Then run:
      ./bin/hq task add /tmp/triage-{{message_id}}.json
  Success = the command prints an issue number.
- NOISE → do nothing.

Rules: never claim you ran a command you did not run. Never use any command
other than `./bin/hq mail read` and `./bin/hq task add`. Never create email
drafts. Do not change task statuses, labels, or email state.

Step 4 — report in one line: the classification, and the issue number / "skipped".
