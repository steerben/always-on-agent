"""Environment-driven settings for the Always-On Ops Agent."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root = parent of this package directory. The synthetic enterprise data
# (issues/, deploys/, contracts/, runbooks/, compliance-policy.md) lives here.
_DEFAULT_REPO_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    anthropic_api_key: str = ""
    agent_model: str = "claude-opus-4-7"

    repo_dir: Path = _DEFAULT_REPO_DIR

    # When true, side-effecting tools (PR, Slack) only log their intent.
    dry_run: bool = True

    github_repo: str = ""
    pr_base_branch: str = "main"
    slack_webhook_url: str = ""
    # Slack request-signing secret, used to verify interactive button callbacks
    # on POST /slack/actions. Empty disables verification (endpoint rejects all).
    slack_signing_secret: str = ""

    webhook_secret: str = "change-me"

    def resolved_repo_dir(self) -> Path:
        # An empty env value deserializes to the cwd-relative ".", so fall back.
        if not self.repo_dir or str(self.repo_dir) in {".", ""}:
            return _DEFAULT_REPO_DIR
        return self.repo_dir


settings = Settings()
