#!/usr/bin/env python3
"""
restore_people.py — best-effort reconstruction of people.yaml entries lost
to an accidental git checkout.

Takes a list of canonical names, resolves each via:
  1. state/slack_users_cache.json (6747 users) — fuzzy match canonical →
     display_name → slack_id.
  2. Slack users.info(slack_id) on top match → email, real_name, profile.title.
  3. events.db — find rows where actor's resolved name matches canonical,
     extract co-occurring github/jira_id from raw_path if useful.

Output: proposal table per canonical with confidence + matched fields.
Apply via --apply (appends to config/people.yaml). DOES NOT silently
overwrite existing entries — surfaces collisions.

Usage:
    python -m derive.restore_people <name1> <name2> ...           # dry
    python -m derive.restore_people --apply <name1> <name2> ...   # write yaml
    python -m derive.restore_people --canonical-file path.txt     # batch
"""

from __future__ import annotations

import argparse
import json
import sys
from difflib import SequenceMatcher
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from ingest.slack_api_client import SlackClient  # noqa: E402

PEOPLE_YAML = _REPO_ROOT / "config" / "people.yaml"
USERS_CACHE = _REPO_ROOT / "state" / "slack_users_cache.json"


def _load_users_cache() -> dict[str, str]:
    if not USERS_CACHE.exists():
        raise RuntimeError(f"missing {USERS_CACHE} — cannot reconstruct")
    with USERS_CACHE.open() as f:
        return json.load(f)


def _existing_canonicals() -> set[str]:
    with PEOPLE_YAML.open() as f:
        cfg = yaml.safe_load(f)
    return {p.get("canonical") for p in cfg.get("people", []) if p.get("canonical")}


def _norm(s: str) -> str:
    return s.lower().replace(" ", "").replace(".", "").replace("-", "").replace("_", "")


def _score(canonical: str, display: str) -> float:
    """Fuzzy similarity between canonical and a Slack display name.

    Boost for substring containment, then SequenceMatcher ratio.
    """
    c = _norm(canonical)
    d = _norm(display)
    if not c or not d:
        return 0.0
    if c in d or d in c:
        # Strong signal. Prefer shorter display when canonical is a prefix
        # (e.g. canonical=vs matches "vs" exactly over "Vivek Sharma").
        return 0.95 - (abs(len(d) - len(c)) / max(len(d), 50)) * 0.3
    return SequenceMatcher(None, c, d).ratio()


def _best_slack_matches(canonical: str, users: dict[str, str],
                        top: int = 3) -> list[tuple[float, str, str]]:
    """Return top-N (score, slack_id, display_name) candidates."""
    scored = [(_score(canonical, name), uid, name) for uid, name in users.items()]
    scored.sort(reverse=True)
    return scored[:top]


def _resolve_one(client: SlackClient, canonical: str, users: dict[str, str],
                 existing: set[str]) -> dict:
    """Returns a proposal dict for one canonical name."""
    if canonical in existing:
        return {"canonical": canonical, "status": "ALREADY-EXISTS", "skip": True}

    candidates = _best_slack_matches(canonical, users, top=3)
    top_score, top_sid, top_name = candidates[0] if candidates else (0.0, None, None)

    if not top_sid or top_score < 0.55:
        return {
            "canonical": canonical,
            "status": "NO-CONFIDENT-MATCH",
            "candidates": candidates,
            "skip": True,
        }

    # Hit slack users.info for email + real_name + title.
    proposal = {
        "canonical": canonical,
        "status": "MATCHED",
        "confidence": round(top_score, 2),
        "slack_id": top_sid,
        "slack_handle": top_name,
        "candidates": candidates,
    }
    try:
        info = client.users_info(top_sid)
        user = info.get("user", {})
        profile = user.get("profile", {}) or {}
        proposal["email"] = profile.get("email")
        proposal["name"] = profile.get("real_name") or user.get("real_name") or top_name
        proposal["title"] = profile.get("title")
        # display_name for slack_handle preference (drop the cached fallback)
        if profile.get("display_name"):
            proposal["slack_handle"] = profile["display_name"]
        proposal["is_deleted"] = bool(user.get("deleted"))
        proposal["is_bot"] = bool(user.get("is_bot"))
    except Exception as e:
        proposal["users_info_error"] = str(e)
    return proposal


def _format_yaml_block(p: dict) -> str:
    """Build a yaml row for an approved proposal. Returns indented text."""
    lines = [f"  - email: {p['email']}"]
    lines.append(f"    name: {p.get('name', p['canonical'])}")
    if p.get("title"):
        lines.append(f"    role: {p['title']}")
    if p.get("slack_id"):
        lines.append(f"    slack_id: {p['slack_id']}")
    if p.get("slack_handle"):
        lines.append(f"    slack_handle: {p['slack_handle']}")
    lines.append(f"    canonical: {p['canonical']}")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("names", nargs="*", help="canonical names to reconstruct")
    ap.add_argument("--canonical-file", help="newline-delimited canonical names file")
    ap.add_argument("--apply", action="store_true",
                    help="append resolved entries to config/people.yaml")
    ap.add_argument("--min-confidence", type=float, default=0.7,
                    help="reject matches below this score (default 0.7)")
    args = ap.parse_args()

    names = list(args.names)
    if args.canonical_file:
        names += [
            line.strip()
            for line in Path(args.canonical_file).read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]
    if not names:
        print("ERR: provide canonical names as args or via --canonical-file", file=sys.stderr)
        return 1

    users = _load_users_cache()
    existing = _existing_canonicals()
    print(f"[load] slack_users_cache: {len(users)} users  ·  "
          f"people.yaml: {len(existing)} canonicals\n", flush=True)

    client = SlackClient()
    proposals = [_resolve_one(client, n, users, existing) for n in names]

    # ── Report ──
    print(f"{'canonical':<22}  {'status':<22}  {'conf':>5}  {'slack_id':<13}  {'email':<35}  name")
    print("-" * 130)
    apply_set: list[dict] = []
    for p in proposals:
        cn = p["canonical"][:22]
        st = p["status"][:22]
        if p.get("skip"):
            cand_str = ""
            if p.get("candidates"):
                cand_str = "  cand=" + ", ".join(
                    f"{name}({s:.2f})" for s, _, name in p["candidates"][:2]
                )
            print(f"{cn:<22}  {st:<22}  {'-':>5}  {'-':<13}  {'-':<35}  {cand_str}")
            continue
        conf = p.get("confidence", 0.0)
        sid = p.get("slack_id", "?")
        email = (p.get("email") or "")[:35]
        name = p.get("name", "?")
        flag = ""
        if p.get("is_deleted"):
            flag = " [DEACTIVATED]"
        if conf < args.min_confidence:
            flag += " [LOW-CONF]"
        print(f"{cn:<22}  {st:<22}  {conf:>5.2f}  {sid:<13}  {email:<35}  {name}{flag}")
        # Include deactivated employees — they still appear in historical
        # ingest rows and need a canonical label.
        if conf >= args.min_confidence and p.get("email"):
            apply_set.append(p)

    print(f"\n[summary] {len(apply_set)}/{len(proposals)} entries pass min-confidence "
          f"({args.min_confidence}) + have-email (deactivated included)")

    if not args.apply:
        print("\n[dry] re-run with --apply to append to config/people.yaml")
        return 0

    if not apply_set:
        print("\n[apply] nothing to write")
        return 0

    with PEOPLE_YAML.open("a") as f:
        f.write("\n  # ─── restored " + str(
            __import__("datetime").datetime.now().date()) + " (best-effort from "
            "slack_users_cache + users.info) ───\n")
        for p in apply_set:
            f.write("\n" + _format_yaml_block(p))
    print(f"\n[apply] appended {len(apply_set)} entries to config/people.yaml")
    print("[apply] verify + fill missing github/jira_id manually if needed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
