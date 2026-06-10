# yaaos WorkspaceAgent — customer-deployed Go binary.
#
# Two-stage build: golang:1.26-alpine compiles the static binary; the
# runtime stage is `node:24-bookworm-slim` because the agent execs the
# Claude Code CLI (a Node.js program installed via `npm i -g`) for every
# `InvokeClaudeCode` command, and also shells out to `git` for repo
# clone. Using the official Node image instead of `debian:bookworm-slim`
# + apt's `npm` package avoids the ~200-package `node-*` apt purge dance
# (npm ships bundled with node here, no apt cleanup needed).
#
# Published as linux/arm64 only — yaaos customers are arm64 hosts.
# The agent is PID 1; SIGTERM from the orchestrator reaches it directly
# so graceful shutdown works without an init wrapper.
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
FROM golang:1.26-alpine AS builder

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

# Build tags. Empty for production (the default). The e2e/test stack passes
# BUILD_TAGS=agent_test so the agent honors YAAOS_AGENT_ACCEPT_IDENTITY_CHANGE
# (the identity-change seam); the production image never sets it, so that code
# path is not compiled in. See internal/supervisor/identity_seam_*.go.
ARG BUILD_TAGS=""

# VERSION is stamped into the binary via -ldflags so `service.version` in OTel
# and the supervisor's claim payload carry the real release string. The publish
# pipeline passes --build-arg VERSION=<semver>; local dev builds omit it and
# get the "0.0.0-dev" default baked into main.agentVersion.
ARG VERSION=""

# Static binary. CGO_ENABLED=0 keeps the binary glibc/musl-agnostic so it
# runs cleanly on the Debian runtime stage. -trimpath removes local build
# paths from the binary so customers don't see /home/<runner>/... in
# stack traces. ${BUILD_TAGS:+-tags ${BUILD_TAGS}} adds -tags only when the
# arg is non-empty. ${VERSION:+-X main.agentVersion=${VERSION}} injects the
# version only when VERSION is provided so the default "0.0.0-dev" fallback
# applies in local/unversioned builds.
#
# The two cache mounts hold the Go build cache (GOCACHE) and module cache
# (GOMODCACHE). They're build-time only — never baked into a layer — so the
# static binary stays byte-identical. In CI the `preserve-data` Docker setting
# persists BuildKit's cache store across runs, so an unchanged-deps source edit
# recompiles incrementally instead of cold (~30s → a few seconds).
RUN --mount=type=cache,target=/root/.cache/go-build \
    --mount=type=cache,target=/go/pkg/mod \
    CGO_ENABLED=0 GOOS=linux go build \
    -trimpath \
    ${BUILD_TAGS:+-tags ${BUILD_TAGS}} \
    -ldflags "-s -w${VERSION:+ -X main.agentVersion=${VERSION}}" \
    -o /out/agent \
    ./cmd/agent

# ── Stage 2: runtime ───────────────────────────────────────────────────
FROM node:24-bookworm-slim

# `git` is required: the agent shells out to it for ProvisionWorkspace
# (HTTPS clone with installation-token auth), and the Claude Code CLI
# the agent later spawns also depends on it. `ca-certificates` lets the
# clone reach GitHub over HTTPS. Node + npm ship in the base image, so
# we only need apt for git + ca-certificates. We don't install curl,
# ssh, less, or any other userland — the surface area stays small.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && npm install -g @anthropic-ai/claude-code \
 && rm -rf /var/lib/apt/lists/* /root/.npm

# Run as a non-root user (UID 65532, matching the distroless `nonroot`
# convention we used before — same UID so existing ECS task definitions
# don't need to change). The user owns /var/agent so the agent can
# create workspaces under it.
RUN groupadd --system --gid 65532 nonroot \
 && useradd --system --uid 65532 --gid nonroot --home-dir /home/nonroot \
            --create-home --shell /usr/sbin/nologin nonroot \
 && mkdir -p /var/agent/workspaces /var/log/yaaos-agent \
 && chown -R nonroot:nonroot /var/agent /var/log/yaaos-agent
USER 65532:65532
WORKDIR /home/nonroot

# The binary itself.
COPY --from=builder --chown=nonroot:nonroot /out/agent /usr/local/bin/agent

# Default workspace root — overridable via YAAOS_WORKSPACE_ROOT env. The
# ECS task definition mounts a tmpfs or EBS volume here in production.
ENV YAAOS_WORKSPACE_ROOT=/var/agent/workspaces

ENTRYPOINT ["/usr/local/bin/agent"]
CMD ["supervisor"]
