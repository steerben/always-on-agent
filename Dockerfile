FROM python:3.13-slim

# System deps:
#  - nodejs/npm: the claude-agent-sdk spawns the Claude Code CLI as a subprocess.
#  - git + gh: needed by the open_pull_request tool (only used when DRY_RUN=false).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# The Anthropic CLI that the Agent SDK drives.
RUN npm install -g @anthropic-ai/claude-code

WORKDIR /app
COPY pyproject.toml ./
RUN pip install --no-cache-dir .

COPY agent ./agent
# The synthetic data (issues/, deploys/, contracts/, runbooks/, compliance-policy.md)
# is mounted or copied at deploy time into REPO_DIR (default /app).
COPY . .

EXPOSE 8080

# Default: run the webhook server. For the scheduled trigger, the platform's
# cron should instead invoke:  python -m agent --task all
CMD ["uvicorn", "agent.webhook:app", "--host", "0.0.0.0", "--port", "8080"]
