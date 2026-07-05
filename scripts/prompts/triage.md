You are triaging a single email for a personal GTD (getting-things-done) system.
The full email is given below. Read it and produce a structured result — you are
not running any tools, just judging this one message.

Classify the email as exactly one category:
- "task": it asks the user for real work, a decision, a deadline, or a
  deliverable, OR it needs a reply the user must write themselves.
- "noise": newsletter, notification, receipt, automated alert, calendar invite
  the calendar already handles, or spam — nothing the user must personally act on.

Then fill in the fields:
- category: "task" or "noise".
- task_title: for a task, a short verb-first summary of what the user must do
  (e.g. "Reply to Dean about the budget deadline"). Empty string for noise.
- task_body: for a task, 1-3 sentences of the context the user needs to act —
  who is asking, what they want, and any date/deadline. Empty string for noise.
- draft_reply: if this email needs a reply the user must send, write a concise,
  polite draft reply in the user's own first-person voice (no salutations block,
  just the message body). Empty string if no reply is needed or for noise.

Judge conservatively: when unsure whether something is actionable, prefer
"task" so nothing important is dropped, but do not invent work that isn't there.

Email:
From: {{from}}
Subject: {{subject}}
Date: {{date}}

{{body}}
