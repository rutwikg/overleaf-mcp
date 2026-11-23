# Container image for overleaf-mcp.
#
# The server needs only git and network access to Overleaf's Git bridge, so it
# runs fully inside a container. Pass the token by environment and pin the
# project by id (a pinned id needs no registry file):
#
#   docker run --rm -i \
#     -e OVERLEAF_GIT_TOKEN=olp_... \
#     -e OVERLEAF_PROJECT=0123456789abcdef01234567 \
#     overleaf-mcp
#
# To use a registry of several projects instead, mount a folder holding
# projects.json (and optionally token) at /data.

FROM python:3.12-slim

# Unbuffered: the MCP transport is line-oriented JSON over stdio, and a
# buffered reply looks like a hung server to the client.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    OVERLEAF_MCP_HOME=/data

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

# Run as a non-root user; the server needs no privileges of its own.
RUN useradd --create-home --uid 1000 agent && mkdir -p /data && chown agent /data
USER agent
WORKDIR /data

# stdio transport: run with -i, and nothing but protocol goes to stdout.
ENTRYPOINT ["overleaf-mcp"]
