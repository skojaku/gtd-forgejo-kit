# Connect Discord (optional)

Mirrors Forgejo "Next" issues into Discord threads, via a [Hermes](https://github.com/NousResearch/hermes-agent) bot. Fully optional — skip this file if you don't want it.

This doc only covers **wiring an existing (or freshly installed) Hermes into HQ**. It does not reproduce Hermes's own onboarding — Hermes already has a setup wizard for that; use it.

---

## 0. Got Hermes?

```bash
hermes --version
```

Not found? Install Hermes Agent first — see its own README: <https://github.com/NousResearch/hermes-agent>. Come back here once `hermes --version` works.

Already have Hermes running for other things (personal Discord bot, other projects)? Good — you don't need a second install, just a dedicated **profile** so HQ's traffic stays isolated from whatever else that Hermes is doing (step 1).

---

## 1. Dedicated profile + Discord connection

HQ expects its own Hermes profile, `hq-local` — keeps HQ's bot token, model, and memory separate from any other bot/profile you already run.

```bash
hermes profile create hq-local
hermes -p hq-local gateway setup
```

The `gateway setup` wizard is Hermes's own — it walks you through creating the Discord bot in the Discord Developer Portal, the MESSAGE CONTENT intent, and saving the bot token into the profile. Follow its prompts; nothing HQ-specific here.

(`./scripts/hermes-setup.sh` in this repo is the recommended way to create it — it also pins the `hq-local` profile to a local-only ollama model and seeds `SOUL.md` with HQ's collector contract, keeping GTD data on the tailnet. Recommended if you're running the full HQ stack.)

---

## 2. Wire it into HQ

```bash
./bin/hq install discord
```

Scaffolds `config/hq.yaml`'s `discord:` section and checks `hermes` is on PATH. Fill in the two IDs it left blank:

```yaml
discord:
  guild_id: ""                       # your Discord server ID
  next_channel_id: ""                # parent text channel that hosts the threads
  bot_token_env: "~/.hermes/profiles/hq-local/.env"   # already correct if you used the profile name above
```

(Right-click the server/channel in Discord with Developer Mode on → **Copy ID**.)

---

## 3. Verify

```bash
./bin/hq discord status       # config completeness, thread counts, gateway container state
./bin/hq discord test-post    # posts a real message to next_channel_id — check Discord
./bin/hq discord sync --dry-run   # logs what a real sync would do, mutates nothing
```

All green? Drop `--dry-run` and `./bin/hq discord sync` runs for real (also runs automatically every 10 min once the `hq-cron` container is up — see `docker/crontab`).

---

## 4. Run the live gateway (optional)

Only needed if you want replies posted *in Discord* to reach the agent, not just one-way issue→thread mirroring:

```bash
docker compose -f deploy/compose.yaml --profile discord up -d hq-discord
```

Off by default — omit this line entirely to never run it. Stop it later with `docker stop hq-discord` (or `docker compose -f deploy/compose.yaml --profile discord down`).
