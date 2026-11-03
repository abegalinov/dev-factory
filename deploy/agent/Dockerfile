# Agent and verification image for Node 20 projects.
#
# Runs with: read-only root, tmpfs HOME (/home/agent) and /tmp, the host uid, no
# capabilities, and only the task directory mounted at /workspace. So everything the
# tools need at runtime must be baked in here or written under HOME, /tmp or /workspace.
#
# Build:  docker build -t dev-factory-agent-node20:2.1.311 deploy/agent
ARG NODE_VERSION=20
FROM node:${NODE_VERSION}-bookworm

ARG CLAUDE_CODE_VERSION=2.1.311
# Yarn classic is activated at build time; the root filesystem is read-only at runtime.
ENV COREPACK_HOME=/opt/corepack \
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \
    DISABLE_AUTOUPDATER=1 \
    CI=true
RUN npm install --global "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
 && corepack enable \
 && corepack prepare yarn@1.22.22 --activate \
 && chmod -R a+rX "$COREPACK_HOME" \
 && npm cache clean --force

WORKDIR /workspace
CMD ["claude", "--version"]
