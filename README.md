# gtd-forgejo-kit

I have kept my to-do list in a lot of places over the years: paper, Reminders,
OmniFocus, and most recently GitHub Projects. The tools I kept going back to were
the board-style ones. Not because of the checkboxes, but because each card slowly
collects the things I need before I can decide what to do: the email that started
it, a link to a document, a note from a meeting, my own half-thoughts. The board is
where information piles up until I can act on it.

At some point I started wondering how much of that pile-up a computer could do for
me. When an email arrives, could something read it, decide whether it is actually a
task, and file a card with a draft reply already written? When I open a task, could
the related emails and documents already be sitting there, so I am not digging for
them?

An AI can do this. The problem is what I would have to hand it. My tasks touch
student records, unpublished work, and private messages from other people. I do not
want to send that to a company's servers, and under the policies I work with I am
not allowed to. So the useful version of this idea and the version I am permitted to
use pull in opposite directions.

The way out is to run the AI on my own machine, on data that never leaves it. This
repository is where I landed: a task board that lives in a git server I host myself,
and small AI models running locally that do the gathering. Nothing goes to a cloud
service.

## How it works

Your tasks are issues in a [Forgejo](https://forgejo.org/) repository that only you
can reach. Forgejo is a self-hosted git server, close to a private GitHub that you
run and whose data you hold. Each issue carries a label for its state (Inbox, Next,
Waiting, Done, and the rest), so the issue list is also your board.

Two local models do the work you would rather not:

- A small, fast one watches your inbox. When mail comes in it decides whether it is
  worth a task. If it is, it writes the task card and a draft reply. The rest it
  leaves alone.
- A larger one gathers background. When a task needs context, it searches your
  email, Drive, and notes, and posts what it finds as a comment on the task, with
  links back to each source.

You make every real decision. The AI can file a new task into your Inbox and draft a
reply, but it cannot send email, and it cannot put anything on your calendar unless
you say so explicitly. It gathers and suggests. You choose.

## Why it runs locally

This is the whole point, so it is worth being plain about it. The models run on your
own hardware. Your email, your documents, and your tasks are read on the machine you
control and are never sent to an outside service. If you work with data that you are
responsible for keeping private, whether by preference or by policy, this is what
makes an AI assistant usable at all rather than something you have to keep at arm's
length.

The trade is that you host a few things yourself and the models are smaller than the
big cloud ones. In practice the small model is more than good enough to sort email,
and the larger one is good enough to pull together context. Neither has to be
brilliant, because they are not deciding anything.

## What is in here

- `bin/hq` — one command-line tool for everything: tasks, mail, calendar, Drive,
  wiki search, and the job queue that feeds the AI.
- `deploy/compose.yaml` — a Docker setup that runs Forgejo, the local model server
  (Ollama), and a scheduler that checks your inbox and runs the housekeeping.
- `.agents/skills/` — short, plain-language instructions that tell the AI how to do
  each job (triage an email, collect context for a task).
- `ARCHITECTURE.md` — how tasks map onto Forgejo issues and labels, for when you want
  to change how it works.

## Getting started

There are three levels, depending on how much you want to run.

**Just the command line.** If you already have a Forgejo instance somewhere, you only
need Python, [`gws`](https://github.com/google/googleworkspace-cli) for Google
access, and ripgrep. Copy `config/hq.example.yaml` to `config/hq.yaml`, point it at
your Forgejo, drop your API token in `~/.config/hq/forgejo-token`, and run
`./bin/hq task list`.

**The whole thing on one machine.** This is the usual setup: Forgejo, the local
models, and the inbox-watching scheduler, all in Docker. You need Docker and, for the
larger model, a GPU helps a lot.

```bash
cp .env.example .env          # your timezone, machine details, Forgejo token
./bin/hq setup                # walks you through Forgejo, the token, and the labels
docker compose -f deploy/compose.yaml up -d
./bin/hq doctor               # tells you what, if anything, is still wrong
```

`hq setup` asks you a few questions and does the fiddly parts (creating the
repository, the API token, and the task-state labels). `hq doctor` is the thing to
run whenever something seems off; it checks each piece and prints how to fix what is
broken.

**A second machine for the heavy model.** If you want the context-gathering to run on
a separate box (say, one with a bigger GPU), point its worker at your main machine's
Forgejo and it will pick up jobs from there.

Fuller setup notes, including Google account access for mail and calendar, are in
`ARCHITECTURE.md`.

## A few honest caveats

This is opinionated and built around how I work, so expect to change things. Standing
it up is more effort than installing an app; you are running a small server. And the
local models are not the frontier cloud ones, which is a deliberate cost of keeping
the data at home. If none of that appeals, a hosted to-do app is a perfectly good
answer. I built this because for my data it was the only answer I was comfortable
with.

## License

MIT. See [`LICENSE`](LICENSE).
