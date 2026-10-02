# syntax=docker/dockerfile:1

# uv comes from its own image rather than curl|sh: COPY --from resolves the
# multi-arch index for us, and the pinned tag is the integrity check.
ARG UV_VERSION=0.12.18
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

# Linux sandbox for running CLI coding agents (Claude Code, Codex, ...)
# against bind-mounted host folders.
FROM node:22-bookworm-slim

# ARG, not ENV: this only needs to suppress debconf during the builds below.
# As an ENV it is recorded in the image config and leaks into every agent's
# runtime environment, where it means nothing.
ARG DEBIAN_FRONTEND=noninteractive

# The base image sets neither. Python coerces the C locale to UTF-8 on its own
# (PEP 538), but rg, jq and git get no such guarantee -- and the corpora these
# agents read are full of non-ASCII names and MIME-encoded headers. SHELL is
# for the tools that shell out by reading it rather than assuming /bin/sh.
ENV LANG=C.UTF-8 \
    SHELL=/bin/bash

# Debian's docker-clean hook deletes downloaded .debs after each install, which
# would defeat the cache mount below, so drop it and tell apt to keep them.
RUN rm -f /etc/apt/apt.conf.d/docker-clean \
    && printf 'Binary::apt::APT::Keep-Downloaded-Packages "true";\n' \
        > /etc/apt/apt.conf.d/keep-cache

# Cache mounts keep the .debs and the package lists outside the image, so an
# ordinary rebuild (a package added here, a CLI version bump below) unpacks
# from local disk instead of re-downloading -- measured at ~2.4x faster on the
# apt step. Note `./sandbox rebuild` passes --no-cache, which DOES empty these
# mounts on Docker 29.x; only the plain `./sandbox build` path benefits.
# There is deliberately no `rm -rf /var/lib/apt/lists/*`: with /var/lib/apt
# mounted that would wipe the cache we just populated, and the lists never
# reach a layer either way.
#
# Dropping EXTERNALLY-MANAGED at the end opts this image out of PEP 668, so a
# bare `pip install x` works instead of erroring. It is safe here precisely
# because of how the container is run: as a non-root user, pip has nowhere to
# write but ~/.local, and the container is stateless, so whatever an agent
# installs is discarded on exit. uv remains the better tool for real work.
#
# `setcap -r /usr/bin/ping` strips the binary's cap_net_raw file capability.
# Containers run with --cap-drop ALL and no-new-privileges, so that capability
# can never be granted, and iputils refuses to start rather than fall back --
# ping fails with a bare "Operation not permitted". Without the file capability
# it opens an unprivileged SOCK_DGRAM ICMP socket instead, which works because
# Docker sets net.ipv4.ping_group_range wide enough to cover uid 1000.
# libcap2-bin is what provides setcap. It also leaves getcap in the image for
# diagnosing this class of failure, though at /usr/sbin/getcap: Debian keeps
# sbin off a non-root login shell's PATH, so it needs the full path.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update -o Acquire::Retries=3 \
    && apt-get install -y --no-install-recommends \
    # --- Python -------------------------------------------------------
    # bookworm's 3.11. Use uv when a project needs a different version.
        python3 \
    # pip and venv are separate packages on Debian; python3 alone has neither.
        python3-pip \
        python3-venv \
    # Ships /usr/bin/python. Debian deliberately omits it, but agents type
    # `python` reflexively and otherwise hit "command not found".
        python-is-python3 \
    # --- Source control ------------------------------------------------
        git \
    # git over ssh, and the ssh-keyscan/ssh-agent bits tools shell out to.
        openssh-client \
    # --- Network -------------------------------------------------------
    # TLS roots. Every https fetch below and at run time depends on these.
        ca-certificates \
        curl \
        wget \
    # dig and nslookup -- separate DNS resolution from connection failures.
        dnsutils \
    # ip and ss. The sandbox's network is the thing most likely to be
    # deliberately restricted, so give the agent a way to see what it has.
        iproute2 \
    # See the setcap note above: usable only because the file capability is
    # stripped, since the container drops all capabilities.
        iputils-ping \
    # Provides setcap for the line at the end of this RUN, and leaves getcap
    # behind for inspecting file capabilities.
        libcap2-bin \
    # --- Search and data shaping ---------------------------------------
    # The three tools agent_kit's CLAUDE.md files actually instruct agents
    # to use over the mail and calendar corpora: rg, jq and python3.
        ripgrep \
        jq \
    # Not used by the indexes today, but the obvious next step for them.
        sqlite3 \
    # --- Files ---------------------------------------------------------
    # Identify a blob by content rather than trusting its extension --
    # mail attachments routinely lie about what they are.
        file \
        tree \
        rsync \
    # The base ships tar and gzip and nothing else. Attachments arrive as
    # all of these.
        unzip \
        zip \
        xz-utils \
    # --- Processes -----------------------------------------------------
    # ps and top: let an agent check whether something it backgrounded
    # is still alive.
        procps \
    # Which process holds a port or a file. Pairs with ss above.
        lsof \
    # --- Building and editing ------------------------------------------
    # Compilers for Python wheels with no prebuilt manylinux binary.
    # The single largest contributor to this layer, at roughly 400MB.
        build-essential \
    # A pager exists (less) and an editor exists (vim-tiny), because tools
    # shell out to $PAGER and $EDITOR and fail oddly when they are absent.
        less \
        vim-tiny \
    && rm -f /usr/lib/python3*/EXTERNALLY-MANAGED \
    && setcap -r /usr/bin/ping

# Two static binaries lifted out of the stage declared at the top of the file.
COPY --from=uv /uv /uvx /usr/local/bin/
# Build-time smoke test: fail here rather than at an agent's first `uv run`.
RUN uv --version
# uv's cache lives in $HOME but venvs are created under the /workspace bind
# mount, a different filesystem -- without this uv warns about failed hardlinks
# on every run.
ENV UV_LINK_MODE=copy

# The base image ships a `node` user at uid/gid 1000. Rename it to `agent`
# rather than creating a new one: keeping uid 1000 keeps bind-mounted file
# ownership sane, and running non-root is required because Claude Code
# refuses --dangerously-skip-permissions when it is running as root.
RUN usermod -l agent node \
    && groupmod -n agent node \
    && usermod -d /home/agent -m agent

# Agent-owned scratch dirs for per-run CLI state. The container is stateless:
# these are recreated empty on every run and discarded when it exits.
RUN mkdir -p /workspace /home/agent/.claude /home/agent/.codex /home/agent/.npm-global \
    && chown -R agent:agent /workspace /home/agent

# Debian's /etc/profile overwrites PATH outright, so a login shell (our CMD)
# would otherwise lose the npm global bin dir and not find claude or codex.
# /etc/profile.d/*.sh is sourced after that assignment, so this survives.
RUN printf 'export PATH=/home/agent/.npm-global/bin:$PATH\n' \
        > /etc/profile.d/10-npm-global.sh \
    && chmod 644 /etc/profile.d/10-npm-global.sh

# npm installs run as `agent` so Claude Code can self-update in place.
USER agent
# The ENV form of the PATH fix above. /etc/profile.d covers login shells;
# this covers everything else -- `docker exec`, ENTRYPOINT, non-login `sh -c`.
ENV PATH=/home/agent/.npm-global/bin:$PATH
# Keep all of Claude Code's state under one directory rather than scattering
# .claude.json into $HOME. Nothing here persists -- auth comes from the
# environment -- but it keeps the container's filesystem tidy and predictable.
ENV CLAUDE_CONFIG_DIR=/home/agent/.claude

# The agent CLIs are pinned at build time and the container is stateless, so
# telemetry, error reporting and self-update checks are just noise here. Set
# before the install so the build-time version checks below inherit them.
ENV CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    DISABLE_TELEMETRY=1 \
    DISABLE_ERROR_REPORTING=1 \
    DISABLE_AUTOUPDATER=1

# Declared here rather than at the top of the file: an ARG in scope is recorded
# on every RUN beneath it, which is why these two used to show up on the 467MB
# apt layer in `docker history`. Bump them to move the agent CLIs.
ARG CLAUDE_VERSION=2.1.282
ARG CODEX_VERSION=0.156.1

# Installed as `agent`, not root: Claude Code self-updates in place and needs
# write access to its own install directory. The cache mount must be owned by
# uid 1000 or npm falls back to an unwritable ~/.npm and re-downloads every
# build. `npm cache clean` is gone on purpose -- the cache is a mount now, it
# never reaches a layer, and cleaning it would discard what we are caching.
# The --version calls are a build-time smoke test: both CLIs resolve their
# native binary through optionalDependencies, and a silent failure there would
# otherwise only surface at the first agent run.
RUN --mount=type=cache,target=/home/agent/.npm,uid=1000,gid=1000,sharing=locked \
    npm config set prefix /home/agent/.npm-global \
    && printf 'export PATH=/home/agent/.npm-global/bin:$PATH\n' >> /home/agent/.bashrc \
    && npm install -g \
        "@anthropic-ai/claude-code@${CLAUDE_VERSION}" \
        "@openai/codex@${CODEX_VERSION}" \
    && claude --version \
    && codex --version

# Last layer that depends on the build context, so everything above stays
# cached when the entrypoint changes. The context is one file (.dockerignore
# excludes the rest), and with this ordering an entrypoint edit rebuilds 4kB
# instead of the ~1GB of apt and npm layers above. `bash -n` is a syntax check:
# the script runs under `set -euo pipefail`, so a typo would otherwise surface
# only at container start.
COPY --chmod=755 docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN bash -n /usr/local/bin/entrypoint.sh

# Where the host directory gets bind-mounted, so an agent starts already
# inside the only tree it is meant to touch.
WORKDIR /workspace
# The entrypoint seeds git identity and Codex auth, then `exec "$@"` hands off,
# so it runs ahead of CMD and of anything passed to `./sandbox run DIR -- CMD`.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
# A login shell, which is what makes /etc/profile.d/10-npm-global.sh load.
# Overridden whenever a command is passed to `./sandbox run`.
CMD ["bash", "-l"]
