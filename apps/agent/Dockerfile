# yaaos WorkspaceAgent — customer-deployed Go binary.
#
# Two-stage build: golang:1.25-alpine compiles the static binary; the
# runtime stage is `debian:bookworm-slim` because the agent shells out
# to `git` for repo clone (and hands off to the Claude Code CLI which
# also expects `git` + a real userspace). Distroless/static would be
# smaller but offers no shell or package manager, which the git
# dependency rules out.
#
# Image is ~80 MB. The agent is
# PID 1; SIGTERM from ECS reaches it directly so graceful shutdown works
# without an init wrapper.
#
# Build:
#   docker build -f apps/agent/Dockerfile -t yaaos-agent:dev apps/agent
#
# Run (local against the backend in docker-compose):
#   docker run --rm \
#     -e YAAOS_BACKEND_URL=http://host.docker.internal:8080 \
#     -e YAAOS_AGENT_VERSION=dev \
#     -e YAAOS_SIGNED_STS_REQUEST=placeholder \
#     yaaos-agent:dev supervisor

# ── Stage 1: build ─────────────────────────────────────────────────────
FROM golang:1.25-alpine AS builder

# Build deps that only the compile step needs.
RUN apk add --no-cache git ca-certificates

WORKDIR /src

# Cache module downloads in a dedicated layer.
COPY go.mod ./
# When go.sum exists, copy it too — the agent currently has no external
# Go deps so the file may be absent; the COPY is conditional via the
# wildcard pattern.
COPY go.su[m] ./
RUN if [ -f go.sum ]; then go mod download; fi

# Source.
COPY cmd cmd
COPY internal internal

# Static binary. CGO_ENABLED=0 keeps the binary glibc/musl-agnostic so it
# runs cleanly on the Debian runtime stage. -trimpath removes local build
# paths from the binary so customers don't see /home/<runner>/... in
# stack traces.
RUN CGO_ENABLED=0 GOOS=linux go build \
    -trimpath \
    -ldflags '-s -w' \
    -o /out/agent \
    ./cmd/agent

# ── Stage 2: runtime ───────────────────────────────────────────────────
FROM debian:bookworm-slim

# `git` is required: the agent shells out to it for CreateWorkspace
# (HTTPS clone with installation-token auth), and the Claude Code CLI
# the agent later spawns also depends on it. `ca-certificates` lets the
# clone reach GitHub over HTTPS. `nodejs` + `npm` are required to install
# the Claude Code CLI (`claude`) which the workspace subprocess execs for
# every `InvokeClaudeCode` command. We don't install curl, ssh, less, or
# any other userland — the surface area stays small.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates nodejs npm \
 && npm install -g @anthropic-ai/claude-code \
 && apt-get purge -y --auto-remove npm \
 && rm -rf /var/lib/apt/lists/* /root/.npm

# Run as a non-root user (UID 65532, matching the distroless `nonroot`
# convention we used before — same UID so existing ECS task definitions
# don't need to change). The user owns /var/agent so the agent can
# create workspaces under it.
RUN groupadd --system --gid 65532 nonroot \
 && useradd --system --uid 65532 --gid nonroot --home-dir /home/nonroot \
            --create-home --shell /usr/sbin/nologin nonroot \
 && mkdir -p /var/agent/workspaces \
 && chown -R nonroot:nonroot /var/agent
USER 65532:65532
WORKDIR /home/nonroot

# The binary itself.
COPY --from=builder --chown=nonroot:nonroot /out/agent /usr/local/bin/agent

# Default workspace root — overridable via YAAOS_WORKSPACE_ROOT env. The
# ECS task definition mounts a tmpfs or EBS volume here in production.
ENV YAAOS_WORKSPACE_ROOT=/var/agent/workspaces

ENTRYPOINT ["/usr/local/bin/agent"]
CMD ["supervisor"]
