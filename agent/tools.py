"""Custom SDK MCP tools for the agent's side effects.

These are the ONLY ways the agent can affect the outside world. Git/PR creation
and Slack posting happen here in Python, deterministically, so the model can
neither merge a PR nor run arbitrary shell commands. Both tools honour DRY_RUN.
"""

from __future__ import annotations

import subprocess

import httpx
from claude_agent_sdk import create_sdk_mcp_server, tool

from .config import settings


def _ok(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=settings.resolved_repo_dir(),
        capture_output=True,
        text=True,
    )


# The agent is only authorized to propose changes under these paths. Staging is
# scoped to them so a triage PR can never sweep in unrelated repo changes.
_STAGE_PATHS = ["issues"]


@tool(
    "open_pull_request",
    "Stage all working-tree changes, commit them on a new branch, push, and open "
    "a GitHub pull request against the base branch. Never merges. Returns the PR URL.",
    {
        "branch_name": str,
        "commit_message": str,
        "pr_title": str,
        "pr_body": str,
    },
)
async def open_pull_request(args: dict) -> dict:
    branch = args["branch_name"]
    repo_dir = settings.resolved_repo_dir()

    status = _git("status", "--porcelain", "--", *_STAGE_PATHS)
    if not status.stdout.strip():
        return _ok("No changes under issues/ to propose; skipped PR creation.")

    if settings.dry_run:
        return _ok(
            "[DRY_RUN] Would open a PR.\n"
            f"  repo:   {settings.github_repo or '(unset)'}\n"
            f"  base:   {settings.pr_base_branch}\n"
            f"  branch: {branch}\n"
            f"  title:  {args['pr_title']}\n"
            f"  commit: {args['commit_message']}\n"
            f"  changed files:\n{status.stdout.rstrip()}\n"
            "Set DRY_RUN=false (and configure GITHUB_REPO) to create it for real."
        )

    # Create or switch to the branch off the current HEAD.
    if _git("checkout", "-b", branch).returncode != 0:
        co = _git("checkout", branch)
        if co.returncode != 0:
            return _ok(f"Failed to create/switch to branch {branch}: {co.stderr.strip()}")

    if _git("add", "--", *_STAGE_PATHS).returncode != 0:
        return _ok("Failed to stage changes.")

    commit = _git("commit", "-m", args["commit_message"])
    if commit.returncode != 0:
        return _ok(f"Commit failed: {commit.stderr.strip() or commit.stdout.strip()}")

    push = _git("push", "-u", "origin", branch)
    if push.returncode != 0:
        return _ok(f"Push failed: {push.stderr.strip()}")

    gh_args = [
        "gh", "pr", "create",
        "--base", settings.pr_base_branch,
        "--head", branch,
        "--title", args["pr_title"],
        "--body", args["pr_body"],
    ]
    if settings.github_repo:
        gh_args += ["--repo", settings.github_repo]
    pr = subprocess.run(gh_args, cwd=repo_dir, capture_output=True, text=True)
    if pr.returncode != 0:
        return _ok(f"Branch pushed but `gh pr create` failed: {pr.stderr.strip()}")

    return _ok(f"Pull request opened (not merged): {pr.stdout.strip()}")


@tool(
    "post_to_slack",
    "Post a short notification to the team Slack channel via the configured "
    "incoming webhook. Use after open_pull_request, including the PR URL.",
    {"text": str},
)
async def post_to_slack(args: dict) -> dict:
    text = args["text"]

    if settings.dry_run or not settings.slack_webhook_url:
        reason = "DRY_RUN" if settings.dry_run else "no SLACK_WEBHOOK_URL configured"
        return _ok(f"[{reason}] Would post to Slack:\n{text}")

    resp = httpx.post(settings.slack_webhook_url, json={"text": text}, timeout=10)
    if resp.status_code >= 300:
        return _ok(f"Slack post failed ({resp.status_code}): {resp.text}")
    return _ok("Posted to Slack.")


def build_tool_server():
    """In-process MCP server exposing the side-effect tools."""
    from .action_tools import ACTION_TOOLS

    return create_sdk_mcp_server(
        name="ops",
        version="0.1.0",
        tools=[open_pull_request, post_to_slack, *ACTION_TOOLS],
    )


TOOL_NAMES = ["mcp__ops__open_pull_request", "mcp__ops__post_to_slack"]
