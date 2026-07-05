FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8

# Runtime user mapping. UID/GID come from build args so the container user
# matches whichever host account owns the bind-mounted repo and config; no
# author-specific ids are baked in. Compose passes these from .env.
ARG UID=1000
ARG GID=1000

# Base tools + locale
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates git bash tzdata python3 python3-yaml jq \
        poppler-utils locales util-linux ripgrep \
    && locale-gen en_US.UTF-8 \
    && ln -s /usr/bin/python3 /usr/bin/python \
    && rm -rf /var/lib/apt/lists/*

# Node.js 22 (required for gws)
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Google Workspace CLI (gws).
# ubuntu:24.04 ships glibc 2.39, so the pre-built gws npm binary runs
# without the musl/cargo workarounds documented in wiki/pieces/gws-setup.md
# (which target older distros).
RUN npm install -g @googleworkspace/cli@0.22.5 \
    && gws --help >/dev/null

# Hermes Agent — the agent harness for automated collect jobs and the Discord
# gateway. The installer links the `hermes` binary system-wide into
# /usr/local/bin (code in /usr/local/lib), so it is available to the runtime
# user. Per-user state — config, profiles, and the persistent "brain" at
# ~/.hermes/memories/MEMORY.md — lives in the bind-mounted ~/.hermes (see
# deploy/compose.yaml); initialize it once on the host via
# scripts/hermes-setup.sh. Needs Python 3.11+ (this image has 3.12).
RUN curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash -s -- --skip-setup \
    && hermes --version >/dev/null

# Discord adapter for the hq-discord Hermes gateway service.
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

# Generic runtime user `hq`, home /home/hq. The `|| true` guards tolerate a
# UID/GID that already exists on the base image (e.g. the default ubuntu user
# at 1000); the container ultimately runs as the numeric ${UID}:${GID} set by
# compose, so the numeric ownership below is what matters.
RUN groupadd -g "${GID}" hq 2>/dev/null || true \
    && useradd -u "${UID}" -g "${GID}" -M -d /home/hq -s /bin/bash hq 2>/dev/null || true \
    && mkdir -p \
        /home/hq/.local/bin \
        /home/hq/.cache \
        /home/hq/.email-triage \
        /home/hq/.hermes \
        /home/hq/.agents \
        /home/hq/.config/gws \
    && chown -R "${UID}:${GID}" /home/hq \
    && chmod -R 0775 /home/hq

# The `hq` CLI lives only in the bind-mounted repo at /hq, so it isn't on any
# default PATH. Symlink it into /usr/local/bin — the one bin dir present in
# every PATH variant — so bare `hq` resolves everywhere (cron, hermes gateway).
RUN ln -sf /hq/bin/hq /usr/local/bin/hq

# The repo is bind-mounted here at runtime.
WORKDIR /hq

# Entrypoint + default command (cron daemon)
COPY docker/entrypoint.sh /usr/local/bin/hq-entrypoint
RUN chmod +x /usr/local/bin/hq-entrypoint
ENTRYPOINT ["/usr/local/bin/hq-entrypoint"]
CMD ["/usr/local/bin/supercronic", "-passthrough-logs", "/hq/docker/crontab"]
