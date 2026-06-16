"""Central org-identity config — the single place that knows org-specific values.

No org identity is hardcoded anywhere in code. Everything resolves here from
`config/sources.yaml` (real values, gitignored), falling back to
`config/sources.example.yaml` (generic placeholders, committed), with a
per-key environment-variable override on top.

A fresh clone with no `sources.yaml` and no env set resolves entirely to the
generic example placeholders — it leaks nothing.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


@lru_cache(maxsize=1)
def _raw() -> dict:
    real = _CONFIG_DIR / "sources.yaml"
    example = _CONFIG_DIR / "sources.example.yaml"
    path = real if real.exists() else example
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def _get(dotted: str, env: str | None = None, default=None):
    """Resolve `a.b.c` from config, with optional env override (highest precedence)."""
    if env and os.environ.get(env):
        return os.environ[env]
    cur = _raw()
    for key in dotted.split("."):
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    return cur if cur is not None else default


def _as_list(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [s.strip() for s in v.split(",") if s.strip()]
    return list(v)


# ── Typed accessors (callers use these, never raw literals) ──────────────────

def atlassian_host() -> str:
    return _get("atlassian.host", env="JIRA_DOMAIN", default="your-org.atlassian.net")

def owner_email() -> str:
    return _get("org.owner_email", env="ATLASSIAN_EMAIL", default="owner@example.com")

def email_domain() -> str:
    return _get("org.email_domain", default="example.com")

def jira_project_keys() -> list[str]:
    return _as_list(_get("jira.project_keys", env="JIRA_PROJECT_KEYS", default=["EX"]))

def github_org() -> str:
    return _get("github.org", env="GITHUB_ORG", default="example-org")

def github_repos() -> list[str]:
    return _as_list(_get("github.repos", env="GITHUB_REPOS", default=[]))

def github_handle_prefixes() -> list[str]:
    """Login/owner-name prefixes that mark a repo owner as belonging to the org."""
    return _as_list(_get("github.handle_prefixes", default=["org-"]))

def home_team() -> str:
    return _get("teams.home", default="home-team")

def coowner_team() -> str:
    return _get("teams.coowner", default="payments-team")

def slack_workspace() -> str:
    return _get("slack.workspace", env="SLACK_WORKSPACE", default="example")

def standup_channel() -> str:
    """Slack channel id the daily-standup routine posts to. Empty if unset."""
    return _get("slack.standup_channel", env="STANDUP_CHANNEL", default="")

def slack_mcp_server() -> str:
    """Slack MCP server id (the hash in mcp__<id>__slack_*). Empty if unset."""
    return _get("slack.mcp_server", env="SLACK_MCP_SERVER", default="")

def rollup_channel() -> str:
    """Slack channel id the rollup-classify routine posts its run-summary to. Empty if unset."""
    return _get("slack.rollup_channel", env="ROLLUP_CHANNEL", default="")

def org_match_tokens() -> list[str]:
    """Org-specific lowercase shorthands used to match channel/slug/ticket names
    (e.g. the jira-key + service abbreviations). Generic by default."""
    return _as_list(_get("match.tokens", default=["ex"]))

def recurring_prefixes() -> list[str]:
    """Org-specific recurring-message title prefixes (templated digests/CMRs)."""
    return _as_list(_get("recurring.prefixes", default=[]))

def launchd_prefix() -> str:
    """Reverse-DNS label prefix for this user's launchd agents."""
    return _get("launchd.prefix", default="com.example")

def owner_handle() -> str:
    """Canonical short slug for the repo owner (used in roster matching)."""
    return _get("org.owner_handle", default="owner")

def matterai_bot() -> str:
    """Login of the org's MatterAI PR-review bot."""
    return _get("github.matterai_bot", default="matterai-example[bot]")

def claude_review_marker() -> str:
    """HTML-comment marker that prefixes the Claude Code Review bot's summary comment.
    Reliable identifier because the bot posts as github-actions[bot] (overloaded)."""
    return _get("github.claude_review_marker", default="<!-- add-pr-comment:claude-review-summary -->")

def codegraph_repos() -> list[str]:
    """Repo short-names mirrored for the code-graph build (may differ from github.repos)."""
    return _as_list(_get("github.codegraph_repos", default=["service-a", "service-b", "service-c"]))

def mom_channels() -> list[str]:
    """Slack channel ids that host weekly-sync MoM posts."""
    return _as_list(_get("slack.mom_channels", default=["C0EXAMPLE"]))

def slack_permalink(channel_id: str, ts: str) -> str:
    """Build a Slack archive permalink for the configured workspace."""
    return (f"https://{slack_workspace()}.slack.com/archives/"
            f"{channel_id}/p{str(ts).replace('.', '')}")
