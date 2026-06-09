"""
slack_api_client.py — direct Slack Web API wrapper for ingest scripts.

Owns:
  - Auth via SLACK_USER_TOKEN (xoxp-) from ~/context/.env
  - Tier-3 rate-limit handling (50 req/min for conversations.* — sleep on 429)
  - Cursor pagination for conversations.history + conversations.replies
  - JSON → ParsedMessage adapter (matches derive/slack_upsert.py shape)

Scope:
  - NO LLM calls. Fail-loud if ANTHROPIC_API_KEY is in env (mirrors rollup pattern).
  - NO write-side endpoints. Read-only methods only.

Skills/scripts call this; MCP code path untouched. See prd/slack-app-migration.md.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from dataclasses import dataclass
from http.client import IncompleteRead
from pathlib import Path
from typing import Iterator, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from derive.slack_upsert import ParsedMessage  # noqa: E402


SLACK_API_BASE = "https://slack.com/api"

# Disk-cache for users.list (avoids 22s rehydration each cron fire).
# TTL: 24h. Lazy refresh via name_resolver covers gaps for newly-added users.
_USERS_CACHE_PATH = _PKG_ROOT / "state" / "slack_users_cache.json"
_USERS_CACHE_TTL_S = 24 * 3600
USER_AGENT = "context-slack-ingest/1.0"

# ── Auth + env guards ──────────────────────────────────────────────────────


def _load_env() -> dict[str, str]:
    """Load .env if present. Checks ~/context/.env (preferred) and ./work-context/.env.

    ~/context/.env wins if both exist. Minimal parser; no python-dotenv dep.
    """
    candidates = [
        _PKG_ROOT.parent / ".env",  # ~/context/.env
        _PKG_ROOT / ".env",          # ~/context/work-context/.env (legacy/fallback)
    ]
    out: dict[str, str] = {}
    for env_path in reversed(candidates):  # later overrides earlier
        if not env_path.exists():
            continue
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            v = v.strip().strip('"').strip("'")
            out[k.strip()] = v
    return out


def _assert_auth_clean(env: dict[str, str]) -> str:
    """Return SLACK_USER_TOKEN. Fail-loud on missing or LLM-key contamination."""
    merged = {**os.environ, **env}
    if "ANTHROPIC_API_KEY" in merged and merged.get("ANTHROPIC_API_KEY", "").strip():
        raise RuntimeError(
            "ANTHROPIC_API_KEY is present in env. Slack ingest scripts must "
            "never run alongside LLM creds (chat-only-classification policy). "
            "Unset ANTHROPIC_API_KEY before invoking."
        )
    token = merged.get("SLACK_USER_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            f"SLACK_USER_TOKEN missing. Add to {_PKG_ROOT.parent / '.env'} as "
            "SLACK_USER_TOKEN=xoxp-... (see runbook/slack-token-rotate.md)."
        )
    if not token.startswith("xoxp-"):
        raise RuntimeError(
            f"SLACK_USER_TOKEN must start with 'xoxp-' (user token). Got prefix: "
            f"{token[:6]!r}. Bot tokens (xoxb-) not supported in this path."
        )
    return token


# ── Rate limit + transport ─────────────────────────────────────────────────


@dataclass
class RateLimit:
    """Tier-3 budget tracker. Slack tier-3 = ~50 req/min for conversations.*."""

    max_per_min: int = 45         # leave headroom under 50
    window_start: float = 0.0
    calls_in_window: int = 0

    def acquire(self) -> None:
        now = time.monotonic()
        if now - self.window_start >= 60:
            self.window_start = now
            self.calls_in_window = 0
        if self.calls_in_window >= self.max_per_min:
            sleep_for = 60 - (now - self.window_start) + 0.5
            if sleep_for > 0:
                time.sleep(sleep_for)
            self.window_start = time.monotonic()
            self.calls_in_window = 0
        self.calls_in_window += 1


class SlackClient:
    """Read-only Slack Web API client.

    Public methods:
        auth_test() -> dict
        conversations_info(channel_id) -> dict
        history(channel_id, oldest=None, latest=None, limit=200, cursor=None) -> dict
        replies(channel_id, ts, oldest=None, limit=200, cursor=None) -> dict
        users_info(user_id) -> dict

    Iterators (auto-paginate):
        iter_history(channel_id, oldest=None) -> Iterator[dict]   # yields each message
        iter_replies(channel_id, ts) -> Iterator[dict]            # yields each reply
    """

    def __init__(self, token: Optional[str] = None, env: Optional[dict] = None):
        if token is None:
            env = env if env is not None else _load_env()
            token = _assert_auth_clean(env)
        self._token = token
        self._rate = RateLimit()
        self._max_retries = 4

    # ── low-level ──

    def _call(self, method: str, params: dict) -> dict:
        self._rate.acquire()
        url = f"{SLACK_API_BASE}/{method}"
        data = urlencode({k: v for k, v in params.items() if v is not None}).encode()
        req = Request(
            url,
            data=data,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
                "User-Agent": USER_AGENT,
            },
        )
        attempt = 0
        while True:
            try:
                with urlopen(req, timeout=30) as resp:
                    body = resp.read().decode()
                payload = json.loads(body)
            except HTTPError as e:
                if e.code == 429 and attempt < self._max_retries:
                    retry_after = int(e.headers.get("Retry-After", "5"))
                    time.sleep(retry_after + 1)
                    attempt += 1
                    continue
                raise
            except URLError:
                if attempt < self._max_retries:
                    time.sleep(2 ** attempt)
                    attempt += 1
                    continue
                raise
            except (IncompleteRead, ConnectionResetError,
                    TimeoutError, socket.timeout) as e:
                # Transient mid-stream truncation / connection drop. Slack
                # has done this intermittently on large channel-list responses
                # and history pages. Exponential backoff + retry from scratch
                # (idempotent — GET-style POST with cursor/oldest params).
                if attempt < self._max_retries:
                    time.sleep(2 ** attempt)
                    attempt += 1
                    continue
                raise

            if payload.get("ok"):
                return payload
            err = payload.get("error", "unknown")
            if err == "ratelimited" and attempt < self._max_retries:
                time.sleep(int(payload.get("retry_after", 5)) + 1)
                attempt += 1
                continue
            raise RuntimeError(f"Slack API {method} failed: {err} | payload={payload}")

    # ── auth / metadata ──

    def auth_test(self) -> dict:
        return self._call("auth.test", {})

    def conversations_info(self, channel_id: str) -> dict:
        return self._call("conversations.info", {"channel": channel_id})

    def users_info(self, user_id: str) -> dict:
        return self._call("users.info", {"user": user_id})

    def users_conversations(
        self,
        user_id: Optional[str] = None,
        types: str = "public_channel,private_channel,mpim",
        limit: int = 200,
        cursor: Optional[str] = None,
        exclude_archived: bool = True,
    ) -> dict:
        """List conversations a user is a member of.

        Scope: `users:read` (or `channels:read` + `groups:read` + `mpim:read`
        depending on `types`). User-token can query OTHER users' channels
        when `user_id` provided.
        """
        params = {
            "types": types,
            "limit": limit,
            "exclude_archived": "true" if exclude_archived else "false",
        }
        if user_id:
            params["user"] = user_id
        if cursor:
            params["cursor"] = cursor
        return self._call("users.conversations", params)

    def iter_users_conversations(
        self,
        user_id: Optional[str] = None,
        types: str = "public_channel,private_channel,mpim",
        limit: int = 200,
    ) -> Iterator[dict]:
        """Paginate users.conversations, yielding each channel dict."""
        cursor: Optional[str] = None
        while True:
            page = self.users_conversations(
                user_id=user_id, types=types, limit=limit, cursor=cursor,
            )
            for ch in page.get("channels", []):
                yield ch
            cursor = page.get("response_metadata", {}).get("next_cursor", "")
            if not cursor:
                return

    def usergroups_list(self) -> dict:
        """List subteams/user-groups. Returns dict with `usergroups` array.
        Scope: usergroups:read (not currently requested) OR works via user-token
        for subteams the user can see. If 403, caller catches + skips expansion.
        """
        return self._call("usergroups.list", {"include_disabled": "true"})

    def build_subteams_cache(self) -> dict[str, str]:
        """S... → handle (e.g. 'service-c-team'). Empty dict on permission error."""
        try:
            r = self.usergroups_list()
        except RuntimeError:
            return {}
        cache: dict[str, str] = {}
        for g in r.get("usergroups", []):
            sid = g.get("id", "")
            handle = g.get("handle", "") or g.get("name", "") or sid
            if sid.startswith("S"):
                cache[sid] = handle
        return cache

    def iter_users_list(self, limit: int = 200) -> Iterator[dict]:
        """Yield every active user in the workspace. Used to seed name cache."""
        cursor: Optional[str] = None
        while True:
            page = self._call("users.list", {"limit": limit, "cursor": cursor})
            for u in page.get("members", []):
                yield u
            cursor = page.get("response_metadata", {}).get("next_cursor", "")
            if not cursor:
                return

    def build_users_cache(self, force_refresh: bool = False) -> dict[str, str]:
        """U... → display_name (or real_name fallback). Includes deleted users
        because historical messages reference them. Bots not included.

        Disk-cached at state/slack_users_cache.json with 24h TTL. Lazy refresh
        via name_resolver covers newly-added users between rebuilds.
        Pass force_refresh=True to bypass cache (e.g. one-shot scripts).
        """
        # Disk cache fast-path.
        if not force_refresh and _USERS_CACHE_PATH.exists():
            try:
                age_s = time.time() - _USERS_CACHE_PATH.stat().st_mtime
                if age_s < _USERS_CACHE_TTL_S:
                    with _USERS_CACHE_PATH.open() as f:
                        cached = json.load(f)
                    if isinstance(cached, dict) and cached:
                        return cached
            except (OSError, json.JSONDecodeError):
                pass  # fall through to fresh build

        # Capture identity signals while building the users cache.
        try:
            import sys as _sys
            from pathlib import Path as _Path
            _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
            from derive.identity_signals import (
                init as _init_signals, record_user_dict as _record_user,
            )
            from ingest.common import get_db as _get_db
            _sig_conn = _get_db()
            _init_signals(_sig_conn)
        except Exception:  # never let signal capture break the cache build
            _sig_conn = None

        cache: dict[str, str] = {}
        for u in self.iter_users_list():
            uid = u.get("id", "")
            if not (uid.startswith("U") or uid.startswith("W")):
                continue
            prof = u.get("profile", {})
            name = (
                prof.get("display_name") or
                prof.get("real_name") or
                u.get("real_name") or
                u.get("name") or
                uid
            )
            cache[uid] = name
            if _sig_conn is not None:
                try:
                    _record_user(_sig_conn, "slack", u)
                except Exception:
                    pass

        if _sig_conn is not None:
            try:
                _sig_conn.commit()
            except Exception:
                pass

        # Persist to disk (atomic via tmp + rename). Best-effort.
        try:
            _USERS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = _USERS_CACHE_PATH.with_suffix(".json.tmp")
            with tmp.open("w") as f:
                json.dump(cache, f)
            tmp.replace(_USERS_CACHE_PATH)
        except OSError:
            pass

        return cache

    # ── history + replies ──

    def history(
        self,
        channel_id: str,
        oldest: Optional[str] = None,
        latest: Optional[str] = None,
        limit: int = 200,
        cursor: Optional[str] = None,
    ) -> dict:
        return self._call(
            "conversations.history",
            {
                "channel": channel_id,
                "oldest": oldest,
                "latest": latest,
                "limit": limit,
                "cursor": cursor,
                "inclusive": "true",
            },
        )

    def replies(
        self,
        channel_id: str,
        ts: str,
        oldest: Optional[str] = None,
        limit: int = 200,
        cursor: Optional[str] = None,
    ) -> dict:
        return self._call(
            "conversations.replies",
            {
                "channel": channel_id,
                "ts": ts,
                "oldest": oldest,
                "limit": limit,
                "cursor": cursor,
                "inclusive": "true",
            },
        )

    def iter_history(
        self,
        channel_id: str,
        oldest: Optional[str] = None,
        limit: int = 200,
    ) -> Iterator[dict]:
        cursor: Optional[str] = None
        while True:
            page = self.history(channel_id, oldest=oldest, limit=limit, cursor=cursor)
            for msg in page.get("messages", []):
                yield msg
            cursor = page.get("response_metadata", {}).get("next_cursor", "")
            if not cursor:
                return

    def iter_replies(
        self,
        channel_id: str,
        ts: str,
        limit: int = 200,
    ) -> Iterator[dict]:
        cursor: Optional[str] = None
        while True:
            page = self.replies(channel_id, ts, limit=limit, cursor=cursor)
            for msg in page.get("messages", []):
                yield msg
            cursor = page.get("response_metadata", {}).get("next_cursor", "")
            if not cursor:
                return


# ── JSON → ParsedMessage adapter ───────────────────────────────────────────

import re as _re

_MENTION_RX = _re.compile(r"<@([UW][A-Z0-9]+)>")
_SUBTEAM_RX = _re.compile(r"<!subteam\^([SC][A-Z0-9]+)>")


def _expand_mentions(body: str, users_cache: dict[str, str], name_resolver=None) -> str:
    """Rewrite raw `<@U…>` to `<@U…|name>` using users_cache. Matches MCP-text shape.

    Falls back to name_resolver (users.info) on cache miss; result cached.
    """
    def sub(m: "_re.Match") -> str:
        uid = m.group(1)
        name = users_cache.get(uid)
        if not name and name_resolver:
            name = name_resolver(uid)
        return f"<@{uid}|{name}>" if name else m.group(0)
    return _MENTION_RX.sub(sub, body)


def _expand_subteams(body: str, subteams_cache: dict[str, str]) -> str:
    """Rewrite `<!subteam^S…>` to `<!subteam^S…|@handle>` using cache."""
    if not subteams_cache:
        return body
    def sub(m: "_re.Match") -> str:
        sid = m.group(1)
        handle = subteams_cache.get(sid)
        return f"<!subteam^{sid}|@{handle}>" if handle else m.group(0)
    return _SUBTEAM_RX.sub(sub, body)


def _flatten_attachments_blocks(msg: dict) -> str:
    """Extract human-readable text from a message's `attachments[]` and
    `blocks[]` arrays.

    Used when `msg['text']` is empty — typical for bot integrations
    (Opsgenie, Grafana alerts, Slack apps) that place all content in
    rich block/attachment payloads. Without this, those messages embed
    as empty strings and cluster on similarity-1.0 noise.

    Walks (in order):
      - attachments[].pretext / title / text / fallback / fields[].value
      - blocks[].text.text                  (section header/body)
      - blocks[].fields[].text              (section field grid)
      - blocks[].elements[].text            (actions, context — direct)
      - blocks[].elements[].elements[].text (rich_text_section nesting)

    Dedupes verbatim repeats (Slack often emits text in both `text` and
    `fallback`). Returns a single newline-joined string.
    """
    parts: list[str] = []
    seen: set[str] = set()

    def _add(s):
        if not s:
            return
        s = s.strip() if isinstance(s, str) else ""
        if not s or s in seen:
            return
        seen.add(s)
        parts.append(s)

    for att in msg.get("attachments") or []:
        if not isinstance(att, dict):
            continue
        _add(att.get("pretext"))
        _add(att.get("title"))
        _add(att.get("text"))
        # `fallback` repeats `text` 80% of the time but occasionally
        # contains extra context — include only when distinct.
        _add(att.get("fallback"))
        for f in att.get("fields") or []:
            if isinstance(f, dict):
                t = f.get("title")
                v = f.get("value")
                if t and v:
                    _add(f"{t}: {v}")
                else:
                    _add(v or t)

    def _walk_block(b):
        if not isinstance(b, dict):
            return
        t = b.get("text")
        if isinstance(t, dict):
            _add(t.get("text"))
        elif isinstance(t, str):
            _add(t)
        for f in b.get("fields") or []:
            if isinstance(f, dict):
                _add(f.get("text"))
        for el in b.get("elements") or []:
            _walk_block(el)

    for bl in msg.get("blocks") or []:
        _walk_block(bl)

    return "\n".join(parts)


_FILE_KEEP_FIELDS = ("id", "name", "title", "mimetype", "size", "mode",
                     "permalink", "user", "filetype", "is_external")


def _files_to_struct(files: list[dict]) -> Optional[str]:
    """Return JSON-encoded list of compact file dicts, or None if empty.

    Drops Slack's heavy fields (url_private, thumbnails, image dimensions).
    Preserves what's useful for downstream queries.
    """
    if not files:
        return None
    out: list[dict] = []
    for f in files:
        slim = {k: f[k] for k in _FILE_KEEP_FIELDS if k in f}
        if slim:
            out.append(slim)
    return json.dumps(out, sort_keys=True) if out else None


def _summarize_files(files: list[dict]) -> str:
    """Append a single line listing attached files. Returns '' if no files."""
    if not files:
        return ""
    names = []
    for f in files:
        n = f.get("name") or f.get("title") or f.get("id", "?")
        # Skip tombstoned/deleted file entries with no name
        if not n:
            continue
        mode = f.get("mode", "")
        if mode == "tombstone":
            names.append(f"{n} [deleted]")
        else:
            names.append(n)
    if not names:
        return ""
    return "\n[files: " + ", ".join(names) + "]"


def api_message_to_parsed(
    msg: dict,
    users_cache: Optional[dict[str, str]] = None,
    name_resolver: Optional[callable] = None,
    subteams_cache: Optional[dict[str, str]] = None,
) -> ParsedMessage:
    """Map a Slack API message dict to derive.slack_upsert.ParsedMessage.

    users_cache: U... → display_name. Optional; falls back to user-<id>.
    name_resolver: optional callable(uid) -> name. Used to retry deactivated users
                   missed by users.list. Result cached back into users_cache.
    subteams_cache: S... → handle. Optional; if provided, expand subteam mentions.
    Bot messages: API gives `bot_id` (B...) and `username`. File attachments are
    summarised into a `[files: ...]` suffix on body.
    """
    users_cache = users_cache if users_cache is not None else {}
    is_bot = "bot_id" in msg and not msg.get("user")
    actor_id = msg.get("bot_id") if is_bot else msg.get("user", "")
    if not actor_id:
        actor_id = msg.get("user", "") or msg.get("bot_id", "")

    if is_bot:
        actor_name = msg.get("username") or msg.get("bot_profile", {}).get("name") or f"bot-{actor_id}"
    else:
        actor_name = users_cache.get(actor_id)
        if not actor_name and actor_id and name_resolver:
            try:
                actor_name = name_resolver(actor_id)
                if actor_name:
                    users_cache[actor_id] = actor_name
            except Exception:
                actor_name = None
        if not actor_name:
            actor_name = f"user-{actor_id}"

    ts = msg.get("ts", "")
    raw_body = msg.get("text", "") or ""
    if not raw_body.strip():
        # Bot integrations (Opsgenie, Grafana, Slack apps) ship content in
        # `attachments` / `blocks` and leave top-level `text` empty. Recover
        # the semantic content so these messages embed against real meaning.
        raw_body = _flatten_attachments_blocks(msg)
    body = _expand_mentions(raw_body, users_cache, name_resolver)
    if subteams_cache:
        body = _expand_subteams(body, subteams_cache)
    body = body.rstrip()
    body += _summarize_files(msg.get("files") or [])

    thread_ts = msg.get("thread_ts")
    is_thread_reply = bool(thread_ts) and thread_ts != ts
    thread_parent_ts = thread_ts if is_thread_reply else None

    reactions_json = None
    if msg.get("reactions"):
        reactions_json = json.dumps(
            {r["name"]: r.get("count", 1) for r in msg["reactions"]}, sort_keys=True
        )

    reply_count = msg.get("reply_count") if not is_thread_reply else None
    edited = "edited" in msg

    files_json = _files_to_struct(msg.get("files") or [])

    return ParsedMessage(
        actor_id=actor_id,
        actor_name=actor_name,
        ts=ts,
        body=body,
        is_bot=is_bot,
        edited=edited,
        thread_parent_ts=thread_parent_ts,
        reactions_json=reactions_json,
        reply_count=reply_count,
        files_json=files_json,
        raw_block=json.dumps(msg, sort_keys=True)[:2000],
    )


def make_name_resolver(client: "SlackClient", users_cache: dict[str, str]):
    """Return a closure that does on-demand users.info lookup for cache misses."""
    miss_negative_cache: set[str] = set()

    def resolver(uid: str) -> Optional[str]:
        if uid in miss_negative_cache:
            return None
        try:
            r = client.users_info(uid)
            u = r.get("user", {})
            prof = u.get("profile", {})
            name = (prof.get("display_name") or prof.get("real_name") or
                    u.get("real_name") or u.get("name") or "")
            if name:
                users_cache[uid] = name
                return name
            miss_negative_cache.add(uid)
            return None
        except Exception:
            miss_negative_cache.add(uid)
            return None

    return resolver


# ── Self-check CLI ─────────────────────────────────────────────────────────


def _selftest() -> int:
    """Run `python -m ingest.slack_api_client` to verify env + auth."""
    env = _load_env()
    try:
        token = _assert_auth_clean(env)
    except RuntimeError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 1
    client = SlackClient(token=token)
    info = client.auth_test()
    print(json.dumps({
        "ok": True,
        "user": info.get("user"),
        "team": info.get("team"),
        "url": info.get("url"),
        "user_id": info.get("user_id"),
        "team_id": info.get("team_id"),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_selftest())
