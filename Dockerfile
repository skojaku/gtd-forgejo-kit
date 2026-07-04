FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8

# Base tools + locale
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates git bash tzdata python3 python3-yaml jq \
        poppler-utils tmux locales util-linux ripgrep \
    && locale-gen en_US.UTF-8 \
    && ln -s /usr/bin/python3 /usr/bin/python \
    && rm -rf /var/lib/apt/lists/*

# Docker CLI (DooD — runner containers spawn agent containers via host daemon)
RUN apt-get update && apt-get install -y --no-install-recommends docker.io \
    && rm -rf /var/lib/apt/lists/*

# Node.js 22 (required for pi and gws)
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# pi (coding agent) + Google Workspace CLI (gws).
# ubuntu:24.04 ships glibc 2.39, so the pre-built gws npm binary runs
# without the musl/cargo workarounds documented in wiki/pieces/gws-setup.md
# (which target older distros).
# pi talks to the host's ollama via network_mode: host (localhost:11434).
RUN npm install -g @mariozechner/pi-coding-agent @googleworkspace/cli@0.22.5 \
    && gws --help >/dev/null

# Hermes Agent — alternative agent harness, trialed alongside pi. The installer
# links the `hermes`
# binary system-wide into /usr/local/bin (code in /usr/local/lib), so it is
# available to the runtime `youruser` user. Per-user state — config, profiles,
# and the persistent "brain" at ~/.hermes/memories/MEMORY.md — lives in the
# bind-mounted ~/.hermes (see docker-compose.yaml); initialize it once on the
# host via scripts/hermes-setup.sh. Needs Python 3.11+ (this image has 3.12).
RUN curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash -s -- --skip-setup \
    && hermes --version >/dev/null

# Discord adapter for the hq-github Hermes gateway (docker-compose hq-agent service).
RUN export UV_INSTALL_DIR=/usr/local/bin \
    && curl -LsSf https://astral.sh/uv/install.sh | sh \
    && uv pip install --python /usr/local/lib/hermes-agent/venv/bin/python \
         "/usr/local/lib/hermes-agent[messaging]" \
    && /usr/local/lib/hermes-agent/venv/bin/python -c "import discord; print('discord', discord.__version__)"

# supercronic — non-root cron for containers
ARG SUPERCRONIC_VERSION=0.2.33
ARG SUPERCRONIC_SHA256=feefa310da569c81b99e1027b86b27b51e6ee9ab647747b49099645120cfc671
RUN curl -fsSLo /usr/local/bin/supercronic \
        "https://github.com/aptible/supercronic/releases/download/v${SUPERCRONIC_VERSION}/supercronic-linux-amd64" \
    && echo "${SUPERCRONIC_SHA256}  /usr/local/bin/supercronic" | sha256sum -c - \
    && chmod +x /usr/local/bin/supercronic

# yq (mikefarah, Go binary) — fallback YAML parser for bin/hq on hosts without
# PyYAML (this image has python3-yaml, so it's belt-and-braces).
ARG YQ_VERSION=4.53.3
ARG YQ_SHA256=fa52a4e758c63d38299163fbdd1edfb4c4963247918bf9c1c5d31d84789eded4
RUN curl -fsSLo /usr/local/bin/yq \
        "https://github.com/mikefarah/yq/releases/download/v${YQ_VERSION}/yq_linux_amd64" \
    && echo "${YQ_SHA256}  /usr/local/bin/yq" | sha256sum -c - \
    && chmod +x /usr/local/bin/yq

# Mirror the host layout so HQ scripts' hardcoded paths resolve unchanged.
# Host: /home/youruser with PATH containing ~/.local/bin, ~/.npm-global/bin, ~/miniforge3/bin.
# UID/GID match your host account; tolerate pre-existing ids on rebuild.
RUN groupadd -g 565400513 youruser 2>/dev/null || true \
    && useradd -u 407876623 -g 565400513 -M -d /home/youruser -s /bin/bash youruser 2>/dev/null || true \
    && mkdir -p \
        /home/youruser/.npm-global/bin \
        /home/youruser/.local/bin \
        /home/youruser/miniforge3/bin \
        /home/youruser/.cache \
        /home/youruser/.email-triage \
        /home/youruser/.pi \
        /home/youruser/.hermes \
        /home/youruser/.agents \
        /home/youruser/.config \
    && ln -sf "$(command -v gws)" /home/youruser/.npm-global/bin/gws \
    && ln -sf "$(command -v pi)"  /home/youruser/.npm-global/bin/pi \
    && ln -sf /home/youruser/HQ/bin/hq /home/youruser/.npm-global/bin/hq \
    && chown -R 407876623:565400513 /home/youruser \
    && chmod -R 0775 /home/youruser

# `hq` lives only in the bind-mounted repo, so unlike gws/pi (npm-installed into
# /usr/bin) it isn't on any default PATH. Symlink it into /usr/local/bin — the
# one bin dir present in every PATH variant, including the host PATH the
# container inherits via env_file — so the hermes gateway resolves bare `hq`.
RUN ln -sf /home/youruser/HQ/bin/hq /usr/local/bin/hq

# Entrypoint + default command (cron daemon)
COPY docker/entrypoint.sh /usr/local/bin/hq-entrypoint
RUN chmod +x /usr/local/bin/hq-entrypoint
ENTRYPOINT ["/usr/local/bin/hq-entrypoint"]
CMD ["/usr/local/bin/supercronic", "-passthrough-logs", "/home/youruser/HQ/docker/crontab"]
