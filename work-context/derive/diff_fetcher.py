"""
GitHub PR diff fetcher with head_sha-keyed disk cache.

Used by llm_classifier.py to fetch PR file changes only when Claude pass-1
classification is ambiguous. Reuses GITHUB_TOKEN env (same as ingest/github.py).

On-disk cache: derived/.diff_cache/{owner}/{repo}/{pr}.json
  { head_sha, fetched_at, files: [{filename, additions, deletions, patch}] }
Cache key is head_sha — fetch is skipped if HEAD lookup matches stored sha.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests

ROOT = Path(__file__).parent.parent
CACHE_DIR = ROOT / "derived" / ".diff_cache"

MAX_PATCH_PER_FILE = 2000   # bytes
MAX_TOTAL_PATCH    = 30000  # bytes
MAX_FILES          = 50

log = logging.getLogger("diff-fetcher")


@dataclass
class DiffFiles:
    head_sha: str
    files: list[dict] = field(default_factory=list)

    def to_text(self) -> str:
        """Render as compact context block for the LLM."""
        if not self.files:
            return ""
        lines: list[str] = []
        total = 0
        for f in self.files[:MAX_FILES]:
            header = f"--- {f['filename']} (+{f['additions']}/-{f['deletions']})"
            patch  = f.get("patch") or ""
            block  = header + "\n" + patch + "\n"
            if total + len(block) > MAX_TOTAL_PATCH:
                lines.append(f"… ({len(self.files) - len(lines)} more files truncated)")
                break
            lines.append(block)
            total += len(block)
        return "\n".join(lines)


class GitHubDiffClient:
    BASE = "https://api.github.com"

    def __init__(self, token: str) -> None:
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })

    def _get(self, path: str) -> Optional[dict | list]:
        url = f"{self.BASE}{path}"
        for attempt in range(3):
            try:
                resp = self.session.get(url, timeout=20)
            except requests.RequestException as e:
                log.warning("GET %s attempt %d failed: %s", path, attempt + 1, e)
                time.sleep(2 ** attempt)
                continue
            if resp.status_code == 403 and "rate limit" in resp.text.lower():
                reset = int(resp.headers.get("X-RateLimit-Reset", time.time() + 60))
                wait  = min(60, max(1, reset - int(time.time())))
                log.warning("Rate limited on %s. Sleep %ds.", path, wait)
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                log.warning("GET %s → %d attempt %d", path, resp.status_code, attempt + 1)
                time.sleep(2 ** attempt)
                continue
            if resp.status_code == 404:
                log.info("GET %s → 404", path)
                return None
            if not resp.ok:
                log.warning("GET %s → %d: %s", path, resp.status_code, resp.text[:200])
                return None
            return resp.json()
        return None

    def head_sha(self, repo: str, pr_number: int) -> Optional[str]:
        data = self._get(f"/repos/{repo}/pulls/{pr_number}")
        if not isinstance(data, dict):
            return None
        return (data.get("head") or {}).get("sha")

    def files(self, repo: str, pr_number: int) -> list[dict]:
        out: list[dict] = []
        page = 1
        while True:
            data = self._get(f"/repos/{repo}/pulls/{pr_number}/files?per_page=100&page={page}")
            if not isinstance(data, list) or not data:
                break
            for f in data:
                patch = f.get("patch") or ""
                if len(patch) > MAX_PATCH_PER_FILE:
                    patch = patch[:MAX_PATCH_PER_FILE] + "\n…(truncated)"
                out.append({
                    "filename":  f.get("filename"),
                    "additions": f.get("additions", 0),
                    "deletions": f.get("deletions", 0),
                    "patch":     patch,
                })
            if len(data) < 100 or len(out) >= MAX_FILES:
                break
            page += 1
        return out


def _cache_path(subject: str) -> Path:
    """subject e.g. 'example-org/service-a#605' → cache/example-org/service-a/605.json"""
    if "#" not in subject:
        return CACHE_DIR / "_unknown" / f"{subject.replace('/', '_')}.json"
    repo, num = subject.split("#", 1)
    parts = repo.split("/")
    if len(parts) != 2:
        return CACHE_DIR / "_unknown" / f"{subject.replace('/', '_').replace('#', '_')}.json"
    return CACHE_DIR / parts[0] / parts[1] / f"{num}.json"


def _read_cache(subject: str) -> Optional[DiffFiles]:
    p = _cache_path(subject)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text())
        return DiffFiles(head_sha=d["head_sha"], files=d["files"])
    except (json.JSONDecodeError, KeyError, OSError) as e:
        log.warning("cache read %s failed: %s", p, e)
        return None


def _write_cache(subject: str, diff: DiffFiles) -> None:
    p = _cache_path(subject)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "head_sha":   diff.head_sha,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "files":      diff.files,
    }))


_CLIENT: Optional[GitHubDiffClient] = None


def _client() -> Optional[GitHubDiffClient]:
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        log.warning("GITHUB_TOKEN not set — diff fetch disabled")
        return None
    _CLIENT = GitHubDiffClient(token)
    return _CLIENT


def fetch_diff(subject: str) -> Optional[DiffFiles]:
    """Fetch diff for a subject (e.g. 'owner/repo#123').

    Returns None if subject is not a GitHub PR or fetch fails.
    Returns DiffFiles (possibly empty files list) on success.
    Uses head_sha cache: skips refetch if PR head sha matches cached sha.
    """
    if "#" not in subject:
        return None
    repo, num_s = subject.split("#", 1)
    if "/" not in repo or not num_s.isdigit():
        return None
    pr_number = int(num_s)

    client = _client()
    if client is None:
        return None

    cached = _read_cache(subject)
    fresh_sha = client.head_sha(repo, pr_number)
    if fresh_sha is None:
        # Couldn't resolve head — fall back to cached if any.
        return cached
    if cached and cached.head_sha == fresh_sha:
        return cached

    files = client.files(repo, pr_number)
    diff = DiffFiles(head_sha=fresh_sha, files=files)
    _write_cache(subject, diff)
    return diff
