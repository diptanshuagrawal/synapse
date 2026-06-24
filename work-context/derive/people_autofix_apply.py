#!/usr/bin/env python3
"""people_autofix_apply.py — append resolved identity mappings to people.yaml.

The maker step of the ingest-autofix routine. Takes a JSON list of *resolved*
new actors (each already looked-up to a real identity by the routine's chat
layer) and APPENDS them to config/people.yaml as new entries.

Design — why append-only text, not yaml.dump round-trip:
  config/people.yaml is hand-curated. A safe_load → safe_dump round-trip would
  reorder/restyle every existing entry. We only ever ADD, never touch existing
  lines: parse the file to learn which identities already exist (idempotency),
  then append formatted YAML blocks to the end of the list. Existing content is
  byte-for-byte untouched.

Policy (the routine's "tiered auto" SAFE class only):
  - scope MUST be "org" or "external". "team" is REFUSED — team membership is a
    human decision, never auto-applied.
  - an entry whose every identity key (email / jira_id / github / git_names /
    github_aliases / slack_id) is already present in the roster is SKIPPED
    (idempotent — safe to run daily, safe to re-run after a manual add).
  - an entry must carry at least one resolvable key (email | jira_id | github).

Input (--in FILE or stdin), a JSON array:
    [{"scope":"org","email":"a.b@example.com","name":"A B",
      "jira_id":"712020:...","github":"org-ab","git_names":["A B","org-ab"],
      "slack_id":"U0...","slack_handle":"a.b"},
     {"scope":"external","name":"tech-bot","github":"tech-bot",
      "git_names":["tech-bot"]}]

Output (JSON to stdout):
    {"applied":[{...}], "skipped":[{entry,reason}], "n_applied":N, "n_skipped":N}

Exit codes
----------
    0   ran (even if 0 applied)
    2   bad input / refused entry (e.g. scope=team, no resolvable key)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover
    print("PyYAML required (use .venv/bin/python)", file=sys.stderr)
    raise

_REPO_ROOT = Path(__file__).resolve().parent.parent
PEOPLE_YAML = _REPO_ROOT / "config" / "people.yaml"

ALLOWED_SCOPES = {"org", "external"}
RESOLVABLE_KEYS = ("email", "jira_id", "github")
# field render order in the appended block (only present fields are written)
FIELD_ORDER = ("email", "name", "scope", "github", "jira_id", "slack_id", "slack_handle")
LIST_FIELDS = ("github_aliases", "git_names")
# every key shape that identifies a person — used for dedup
IDENTITY_SCALARS = ("email", "jira_id", "github", "slack_id")
IDENTITY_LISTS = ("git_names", "github_aliases")


def _existing_identities(people: list[dict]) -> set[str]:
    """Every identity token already present in the roster."""
    seen: set[str] = set()
    for p in people:
        for k in IDENTITY_SCALARS:
            v = p.get(k)
            if v:
                seen.add(str(v))
        for k in IDENTITY_LISTS:
            for v in (p.get(k) or []):
                if v:
                    seen.add(str(v))
        legacy = p.get("git_name")
        if legacy:
            seen.add(str(legacy))
        # a name with no email is itself a valid actor key (Automation-style)
        nm = p.get("name")
        if nm and not p.get("email"):
            seen.add(str(nm))
    return seen


def _entry_tokens(entry: dict) -> set[str]:
    """Every identity token this candidate would introduce."""
    toks: set[str] = set()
    for k in IDENTITY_SCALARS:
        v = entry.get(k)
        if v:
            toks.add(str(v))
    for k in IDENTITY_LISTS:
        for v in (entry.get(k) or []):
            if v:
                toks.add(str(v))
    nm = entry.get("name")
    if nm and not entry.get("email"):
        toks.add(str(nm))
    return toks


def _yaml_scalar(v: str) -> str:
    """Quote a scalar only when YAML would otherwise mis-parse it.

    org-domain emails / account-ids / names are plain scalars; quote defensively
    if the value leads with a YAML indicator, embeds ': ' / ' #', or carries a
    newline/CR (which would otherwise corrupt the appended block).
    """
    s = str(v)
    needs = (
        not s
        or s[0] in "!&*?|>%@`\"'#-[]{},:"
        or ": " in s
        or " #" in s
        or "\n" in s
        or "\r" in s
        or s != s.strip()
    )
    return yaml.safe_dump(s, default_flow_style=True).strip() if needs else s


def _format_block(entry: dict) -> str:
    """Render one entry as an appendable YAML list item (2-space indent)."""
    lines: list[str] = []
    first = True
    for k in FIELD_ORDER:
        if k not in entry or entry[k] in (None, ""):
            continue
        prefix = "- " if first else "  "
        lines.append(f"{prefix}{k}: {_yaml_scalar(entry[k])}")
        first = False
    for k in LIST_FIELDS:
        vals = [v for v in (entry.get(k) or []) if v]
        if not vals:
            continue
        prefix = "- " if first else "  "
        lines.append(f"{prefix}{k}:")
        first = False
        for v in vals:
            lines.append(f"  - {_yaml_scalar(v)}")
    return "\n".join(lines) + "\n"


def _validate(entry: dict) -> str | None:
    """Return a refusal reason, or None if the entry is acceptable."""
    scope = entry.get("scope")
    if scope not in ALLOWED_SCOPES:
        return f"scope {scope!r} not in {sorted(ALLOWED_SCOPES)} (team is never auto-applied)"
    if not any(entry.get(k) for k in RESOLVABLE_KEYS):
        return f"no resolvable key (need one of {list(RESOLVABLE_KEYS)})"
    return None


def apply(entries: list[dict], path: Path = PEOPLE_YAML) -> dict:
    """Append acceptable, novel entries to the people.yaml at `path`.

    Pure-additive: existing file bytes are never rewritten, only appended to.
    """
    doc = yaml.safe_load(path.read_text()) or {}
    people = doc.get("people", []) or []
    seen = _existing_identities(people)

    applied: list[dict] = []
    skipped: list[dict] = []
    blocks: list[str] = []

    for entry in entries:
        reason = _validate(entry)
        if reason:
            skipped.append({"entry": entry, "reason": reason})
            continue
        toks = _entry_tokens(entry)
        clash = toks & seen
        if clash:
            skipped.append({"entry": entry, "reason": f"already mapped: {sorted(clash)}"})
            continue
        blocks.append(_format_block(entry))
        seen |= toks  # so a duplicate WITHIN this batch is also skipped
        applied.append(entry)

    if blocks:
        text = path.read_text()
        if text and not text.endswith("\n"):
            text += "\n"
        path.write_text(text + "".join(blocks))

    return {
        "applied": applied,
        "skipped": skipped,
        "n_applied": len(applied),
        "n_skipped": len(skipped),
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--in", dest="infile", help="JSON array file (default: stdin)")
    ap.add_argument("--path", help="people.yaml path (default: config/people.yaml)")
    args = ap.parse_args()

    raw = Path(args.infile).read_text() if args.infile else sys.stdin.read()
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"bad JSON input: {e}", file=sys.stderr)
        return 2
    if not isinstance(entries, list):
        print("input must be a JSON array of entries", file=sys.stderr)
        return 2

    path = Path(args.path) if args.path else PEOPLE_YAML
    result = apply(entries, path)
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
