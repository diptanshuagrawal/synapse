"""Dump fallback/uncached subjects in window as JSON for chat-session classification.

Flow:
  1. Collect subjects in window
  2. Detect new Jira epics (no existing slug match) and auto-extend config/projects.yaml
     with `epic-ex-<num>` slug entries — so the chat sees their dedicated slug
  3. Order subjects: newly-auto-slugged epics first, then other jira, then confluence,
     then github (epics define domains; downstream events anchor to them)
  4. Write pending JSON + rules.md (rules.md picks up the freshly added slugs)

epic_domain (when non-empty) is the slug the verdict MUST include first
(deterministic mapping from Jira epic → project slug; apply_verdicts re-orders to front).
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
import llm_classifier as lc  # noqa: E402
import rollup as r            # noqa: E402
from derive.sources_config import org_match_tokens  # noqa: E402

DB = lc.ROOT / "index" / "events.db"
PROJECTS_YAML = lc.ROOT / "config" / "projects.yaml"
# Auto-slug proposals go through the same approval pipeline as chat-driven
# slug suggestions: write to `state/pending_slug_creation.json`, chat reviews
# via `/slug-epics`, then `apply_epic_slugs.py` mutates `projects.yaml`. The
# dump phase MUST NOT touch projects.yaml — that violates the "chat approves
# all config mutations" invariant.
PENDING_SLUG_PATH = lc.ROOT / "state" / "pending_slug_creation.json"
PENDING_SLUG_RULES = lc.ROOT / "state" / "pending_slug_creation.json.rules.md"
JIRA_KEY_RE = re.compile(r"^([A-Z]+)-(\d+)$")

# Words that don't help future PR-to-domain keyword matching.
_STOP_WORDS = {
    # English glue
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for", "of",
    "with", "by", "from", "as", "is", "was", "are", "were", "this", "that",
    "these", "those", "be", "been", "being", "do", "does", "did", "have", "has",
    "had", "will", "would", "should", "we", "our", "us", "via", "into",
    # Domain glue
    "epic", "phase", "implementation", "support", "new",
    "fix", "fixing", "bug", "update", "updates", "added", "adding", "add",
    "remove", "removed", "removing", "issue", "issues", "task", "ticket",
    "create", "creating", "created", "use", "using", "test", "tests",
} | set(org_match_tokens())


def _load_team_enum() -> list[dict]:
    """Read config/teams.yaml for the ownership classifier."""
    import yaml
    teams_path = _REPO_ROOT / "config" / "teams.yaml"
    if not teams_path.exists():
        return []
    with teams_path.open() as f:
        cfg = yaml.safe_load(f)
    return cfg.get("teams", []) or []


def _author_team_map(teams: list[dict]) -> dict[str, str]:
    """Flatten teams.yaml `contributors_github` lists into author → team_id.

    Last writer wins on duplicate authors; surface dup keys in stderr so the
    owner can disambiguate via projects.yaml or richer per-author config.
    """
    seen: dict[str, str] = {}
    dups: list[tuple[str, str, str]] = []  # (author, first_team, second_team)
    for t in teams:
        tid = t.get("id", "")
        for author in (t.get("contributors_github") or []):
            if author in seen and seen[author] != tid:
                dups.append((author, seen[author], tid))
                continue
            seen[author] = tid
    if dups:
        for a, t1, t2 in dups:
            print(f"  WARN dup author in teams.yaml: {a} → {t1} (kept), also listed under {t2}",
                  file=sys.stderr)
    return seen


def _rules_md(project_slugs: list[str]) -> str:
    enum_risk = "security, data-loss, panic, race, migration, breaking-api"
    teams = _load_team_enum()
    team_ids = [t.get("id", "") for t in teams if t.get("id")]
    author_map = _author_team_map(teams)

    body = [
        "# Classification rules (mirrors llm_classifier.SYSTEM_PROMPT)",
        "",
        lc.SYSTEM_PROMPT.strip(),
        "",
        "## Hard schema",
        "- `domains`: subset of project slug enum (see below). Empty list OK.",
        "- `summary`: ≤ 200 chars, action-first, present tense.",
        f"- `risk_flags`: subset of {{{enum_risk}}}. Empty list when none.",
        "- `confidence`: 0–1.",
        "- `epic_domain` field in input → MUST appear in `domains`; apply_verdicts re-orders to front.",
        "",
        "### Ownership fields (NEW — required)",
        "- `owned_by_primary`: ONE team id from the team enum (see below). Pick the team",
        "  whose WORK is the subject of the thread / ticket / PR / page.",
        "- `co_owners`: list of additional team ids when work is genuinely shared. Empty `[]` is fine.",
        "- `owned_by_confidence`: 0–1.",
        "- `ownership_reasoning`: ≤ 200 chars — cite signals (author, participants, artifacts named in body).",
        "",
        "## Ownership decision tree (apply IN ORDER — do NOT skip steps)",
        "",
        "### Step 1: GitHub PRs — empty body REQUIRES diff fetch",
        "If a GitHub subject has `body` empty AND `matterai_summary` empty, you MUST run:",
        "```",
        "gh pr diff <num> --repo <owner/repo>",
        "```",
        "Refusing to fetch the diff = invalid verdict. NEVER attribute based on repo + title alone.",
        "If diff is >20k lines (HTTP 406), fall back to:",
        "```",
        "gh pr view <num> --repo <owner/repo> --json author,title,files,additions,deletions",
        "```",
        "Touched-file paths + author identity are sufficient signal.",
        "",
        "### Step 2: Author-first ownership lookup",
        "After identifying the author (PR login / Jira email / Slack actor id), CONSULT the",
        "author→team table below FIRST. Author-match overrides repo-default.",
        "Example: PR by `org-example-dev1` on service-a → `payments-domain-team` primary",
        "(NOT home-team, even though service-a is a home-team repo).",
        "",
        "### Step 3: Repo / content heuristic (only when author absent from table)",
        "Use the repo + touched-file paths to infer team. service-a / service-b / service-d",
        "→ home-team. payments-main paths → payments-domain-team. service-e → service-e-team. etc.",
        "",
        "### Step 4: Sync / back-merge / release PRs",
        "Title literally 'sync' / 'merge main' / 'back-merge' / 'release X' with no other signal →",
        "`domains: []`, `confidence: 0.80`, `owned_by_primary` = author's team per Step 2.",
        "",
        "## Author → team table (built from `teams.yaml::contributors_github`)",
    ]
    if author_map:
        for author in sorted(author_map):
            body.append(f"- `{author}` → `{author_map[author]}`")
    else:
        body.append("- (none — populate via `teams.yaml::contributors_github`)")
    body.extend([
        "",
        "Authors NOT in the table: fall through to Step 3 (repo + content) but mark",
        "`owned_by_confidence ≤ 0.70` since the signal is weaker.",
        "",
        "## DO NOT force-fit",
        "- If no slug in the enum cleanly fits a subject, return `domains: []` with confidence 0.80.",
        "- Old generic slugs (`product-bau`, `domain-migration`, `cash`, `instant-pay`) are NOT catch-alls.",
        "- A new epic-like Jira ticket with no matching slug should have been auto-slugged",
        "  before this dump ran — if you still see one without a fitting slug, do not invent one.",
        "",
        "## Ownership rules",
        "- Prefer `home-team` only when the subject's WORK is owned by them — NOT when",
        "  the team only RESPONDED to someone else's incident.",
        "- Bot-rooted alert headers (PagerDuty / Dweep / `:alert1:`) — judge by REPLIES, not the bot.",
        "- Cross-team incidents that two teams co-own → `primary` = most-active team,",
        "  `co_owners` = the other(s).",
        "- Vendor incidents (VendorX, NPCI, SBI) → `external`.",
        "- OOO / HR / channel-join / org-wide announcements → `external`.",
        "- When in doubt with confidence < 0.7 → use `unknown` and flag for review.",
        "",
        "## Project slug enum (use exact strings)",
        *[f"- `{s}`" for s in project_slugs],
        "",
        "## Team id enum (use exact strings)",
        *[f"- `{tid}`" for tid in team_ids],
        "",
        "## Verdict echo-back",
        "Each verdict object MUST include `subject` and `content_hash` unchanged from the dump.",
        "",
        "## Confidence threshold",
        "- confidence < 0.7 → verdict REJECTED by apply_verdicts (not stored). Subject stays",
        "  uncached and will appear in the next manual-rollup dump for re-classification.",
        "- `owned_by_confidence < 0.6` → ownership fields nulled; subject re-queued for review.",
        "- For thin GitHub subjects: fetch the PR diff inline (chat has gh CLI) and classify.",
        "  Do NOT set `needs_diff: true` — that flag is dead.",
        "- 'sync' / conflict-resolve PRs: domains=[] confidence=0.80, owned_by_primary by author.",
        "",
        "## Team descriptions (read these to decide ownership)",
        "",
    ])
    for t in teams:
        tid = t.get("id", "")
        name = t.get("name", "")
        desc = (t.get("description", "") or "").strip().replace("\n", " ")
        if len(desc) > 400:
            desc = desc[:400] + "…"
        body.append(f"### `{tid}` — {name}")
        body.append("")
        body.append(desc)
        body.append("")
    return "\n".join(body) + "\n"


def _tokenize_title(title: str) -> list[str]:
    """Strip [bracketed] prefixes, lowercase, drop stop-words, dedupe."""
    t = re.sub(r"\[[^\]]*\]", " ", title or "")
    # Preserve hyphenated compounds like "e-nach" as one token by replacing
    # internal hyphens with placeholder, then split on spaces only.
    t = re.sub(r"([A-Za-z])-([A-Za-z])", r"\1__\2", t)
    t = re.sub(r"[^A-Za-z0-9 _]", " ", t).lower()
    raw = [w.replace("__", "-") for w in t.split() if w]
    seen: set[str] = set()
    out: list[str] = []
    for w in raw:
        if w in _STOP_WORDS or len(w) <= 1:
            continue
        if w in seen:
            continue
        seen.add(w)
        out.append(w)
    return out


def _slug_from_title(title: str, fallback_key: str) -> str:
    """Derive a human-readable kebab-case slug from a Jira title.

    Examples:
      'Card Transactions Migration from service-c to service-a' → 'card-transactions-migration'
      'Cheque Flow Optimizations' → 'cheque-flow-optimizations'
      'E-nach development and nach process optimisation' → 'e-nach-development-process'

    Falls back to `epic-<key>` if title yields no usable tokens.
    """
    toks = _tokenize_title(title)
    if not toks:
        m = JIRA_KEY_RE.match(fallback_key)
        return f"epic-{m.group(1).lower()}-{m.group(2)}" if m else fallback_key.lower()
    slug = "-".join(toks[:4])
    if len(slug) > 40:
        slug = slug[:40].rsplit("-", 1)[0]
    return slug


def _keywords_from_title(title: str) -> list[str]:
    """Generate high-signal keywords for future PR matching.

    Multi-word bigrams ONLY. Unigrams from banking titles are too generic
    ("transaction", "card", "migration", "charges") and would cause every
    banking PR to match every auto-slug. Bigrams are specific enough.
    """
    toks = _tokenize_title(title)
    kws: list[str] = []
    seen: set[str] = set()
    for i in range(len(toks) - 1):
        bg = f"{toks[i]} {toks[i+1]}"
        if bg not in seen:
            seen.add(bg)
            kws.append(bg)
    return kws[:6]


def _detect_new_epic_slugs(
    subjects: list,
    projects: list[dict],
    epic_to_slug: dict[str, str],
) -> dict[str, dict]:
    """Find jira subjects that need their own auto-slug.

    A jira subject S needs a new slug when:
      - source == jira
      - epic_key is empty (S is not anchored to an existing epic)
      - S is NOT already mapped to any slug via jira_epics
      - NO existing slug's keywords match S's title/body (i.e., S is not a leaf
        task that already maps to a known domain).

    The keyword check prevents auto-slugging child tasks that have clear domain
    fit but no parent epic (e.g. "Withholding Failure code updations" → fits
    cash-withholding, do NOT create epic-ex-2711).

    Returns: { jira_key: {"slug": auto-slug, "name": short title} }
    """
    existing_slugs = {p["slug"] for p in projects}
    claimed_keys: set[str] = set()
    for p in projects:
        for ek in (p.get("jira_epics") or []):
            claimed_keys.add(ek)

    auto: dict[str, dict] = {}
    for s in subjects:
        if s.source != "jira":
            continue
        # Hard filter: only true Jira Epics get a new auto-slug.
        # CMR/Task/Bug/Story/Incident tickets re-use existing slugs via keyword
        # matching in the chat classification step.
        if (getattr(s, "issue_type", "") or "").lower() != "epic":
            continue
        if s.epic_key:
            continue
        if s.subject in claimed_keys:
            continue
        if epic_to_slug.get(s.subject):
            continue
        # Keyword check: skip if any existing slug's keywords already match this epic.
        text = f"{s.title} {s.body or ''}".lower()
        keyword_match = False
        for p in projects:
            for kw in (p.get("keywords") or []):
                if kw and kw.lower() in text:
                    keyword_match = True
                    break
            if keyword_match:
                break
        if keyword_match:
            continue
        slug = _slug_from_title(s.title or "", s.subject)
        if not slug:
            continue
        # Disambiguate if slug already taken: suffix with jira number
        base = slug
        attempt = 1
        while slug in existing_slugs or slug in {v["slug"] for v in auto.values()}:
            attempt += 1
            num_part = JIRA_KEY_RE.match(s.subject)
            slug = f"{base}-{num_part.group(2)}" if num_part else f"{base}-{attempt}"
            if attempt > 3:
                break
        if slug in existing_slugs:
            continue
        name = (s.title or s.subject).strip()
        if len(name) > 80:
            name = name[:77] + "..."
        keywords = _keywords_from_title(s.title or "")
        auto[s.subject] = {
            "slug": slug,
            "name": name,
            "jira_key": s.subject,
            "keywords": keywords,
        }
    return auto


def _write_pending_slug_proposals(
    auto: dict[str, dict],
    projects: list[dict],
    pending_path: Path,
    rules_path: Path,
) -> int:
    """Write auto-slug proposals to the pending_slug_creation.json pipeline.

    DOES NOT mutate config/projects.yaml. The chat session reviews these
    proposals via `/slug-epics`, emits verdicts, and only then
    `apply_epic_slugs.py` writes to projects.yaml. This honors the
    "scripts never mutate shared config without chat approval" invariant.

    Returns count of proposals written (after de-duping against existing
    slugs + any proposals already pending from a prior dump).
    """
    if not auto:
        return 0
    import yaml
    from ingest.common import atomic_write_json, atomic_write_text

    existing_slugs = {p["slug"] for p in projects}

    # Merge with any already-pending proposals so a re-run of dump_pending
    # doesn't lose earlier suggestions (e.g. if chat hasn't run /slug-epics
    # yet). De-dupe by epic_key.
    pending_existing: list[dict] = []
    if pending_path.exists():
        try:
            data = json.loads(pending_path.read_text())
            if isinstance(data, list):
                pending_existing = data
        except (json.JSONDecodeError, ValueError):
            pending_existing = []
    pending_by_epic = {p.get("epic_key"): p for p in pending_existing if p.get("epic_key")}

    added = 0
    for jira_key, meta in auto.items():
        if meta["slug"] in existing_slugs:
            continue
        if jira_key in pending_by_epic:
            continue  # already proposed in a prior dump
        pending_by_epic[jira_key] = {
            "epic_key": jira_key,
            "slug": meta["slug"],
            "name": meta["name"],
            "keywords": meta.get("keywords") or [],
            "source": "auto_dump_pending",
        }
        added += 1

    if not added and not pending_existing:
        return 0

    payload = sorted(pending_by_epic.values(), key=lambda p: p.get("epic_key") or "")
    atomic_write_json(pending_path, payload)
    atomic_write_text(rules_path, _slug_proposal_rules_md())
    return added


def _slug_proposal_rules_md() -> str:
    return (
        "# Pending slug-creation proposals\n"
        "\n"
        "These epic Jira tickets had no parent epic, no existing slug mapping,\n"
        "and no keyword match against any existing project. `dump_pending`\n"
        "proposes a kebab-case slug derived from the epic title.\n"
        "\n"
        "## Verdict schema (consumed by `apply_epic_slugs.py`)\n"
        "Each verdict is an object in a JSON array:\n"
        "```\n"
        "{\n"
        "  \"epic_key\": \"EX-1234\",            // required\n"
        "  \"slug\":     \"epic-ex-1234\",       // required if not merging\n"
        "  \"name\":     \"Short human name\",  // optional\n"
        "  \"keywords\": [\"kw1\", \"kw2\"],    // optional\n"
        "  \"merge_into\": \"existing-slug\"    // optional — if set, slug ignored;\n"
        "                                       //  epic_key appends to that slug's jira_epics\n"
        "}\n"
        "```\n"
        "\n"
        "## Workflow\n"
        "1. Review proposals in `state/pending_slug_creation.json`.\n"
        "2. Emit `state/verdicts.epic_slugs.json` (array of verdicts above).\n"
        "3. Run `apply_epic_slugs.py` → mutates `config/projects.yaml`,\n"
        "   archives verdicts file, clears this pending file.\n"
    )


def _sort_key(record: dict, auto_slug_subjects: set[str]) -> tuple:
    """Order subjects for chat triage:
      0. ALL Jira Epics (issue_type=Epic) — these define / anchor domains.
         Auto-slugged ones sub-sort before pre-existing epics.
      1. Non-Epic Jira (Task / CMR / Bug / Story / Incident) — anchored to existing epics or domains.
      2. Confluence — usually documents an epic's design / TRD.
      3. GitHub — PRs that reference Jira tickets, classified last so parents resolve first.
    """
    src = record["source"]
    itype = (record.get("issue_type") or "").lower()
    if src == "jira" and itype == "epic":
        return (0, 0 if record["subject"] in auto_slug_subjects else 1)
    if src == "jira":
        return (1,)
    if src == "confluence":
        return (2,)
    if src == "github":
        return (3,)
    return (4,)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=28)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    conn = sqlite3.connect(str(DB))
    lc.ensure_schema(conn)
    projects = r.load_projects()
    epic_to_slug = lc._build_epic_to_slug(projects)

    people, _ = r.load_people()
    team_handles: set[str] = set()
    for p in people.values():
        for key in ("github", "email", "jira_id", "slack_id", "git_name"):
            if p.get(key):
                team_handles.add(p[key])
    since = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat().replace("+00:00", "Z")
    subjects = r.collect_subjects(conn, since, projects, team_handles=team_handles)

    # ── Detect candidate new epic slugs (jira epics with no parent + no slug
    #    + no keyword match). DO NOT mutate projects.yaml here — write proposals
    #    to `state/pending_slug_creation.json` so chat reviews via /slug-epics
    #    and `apply_epic_slugs.py` performs the actual config mutation.
    auto = _detect_new_epic_slugs(subjects, projects, epic_to_slug)
    proposed = _write_pending_slug_proposals(
        auto, projects, PENDING_SLUG_PATH, PENDING_SLUG_RULES
    )
    if proposed:
        slugs_preview = sorted(meta["slug"] for meta in auto.values())[:10]
        suffix = "..." if proposed > 10 else ""
        print(
            f"dump_pending: {proposed} new epic-slug proposal(s) written to "
            f"{PENDING_SLUG_PATH.relative_to(lc.ROOT)}: {slugs_preview}{suffix}\n"
            f"  → run /slug-epics in chat, then `apply_epic_slugs.py` "
            f"before applying classification verdicts.",
            file=sys.stderr,
        )
    # The auto-slugs are visible to chat-classification through:
    #   (a) the per-subject `edom` field below (anchors via `auto[subject]`),
    #   (b) the rules.md enum which we extend in-memory with proposed slugs
    #       so chat won't be told the slug doesn't exist.
    proposed_slugs = [meta["slug"] for meta in auto.values()]
    project_slugs = [p["slug"] for p in projects] + [
        s for s in proposed_slugs if s not in {p["slug"] for p in projects}
    ]
    auto_slug_subjects = set(auto.keys())

    pending: list[dict] = []
    for s in subjects:
        h = lc._content_hash(s)
        row = conn.execute(
            "SELECT source FROM subject_summary WHERE subject=? AND content_hash=?",
            (s.subject, h),
        ).fetchone()
        if row is None or row[0] == "fallback":
            # epic_domain anchoring priority:
            #   1. just-auto-slugged in this run → use new slug
            #   2. subject IS an existing epic mapped in projects.yaml → use its slug
            #   3. child of an epic via [Epic X-N] prefix → use parent's slug
            if s.subject in auto_slug_subjects:
                edom = auto[s.subject]["slug"]
            elif s.source == "jira" and not s.epic_key and epic_to_slug.get(s.subject):
                edom = epic_to_slug[s.subject]
            else:
                edom = epic_to_slug.get(s.epic_key, "") if s.epic_key else ""
            pending.append({
                "subject": s.subject,
                "source": s.source,
                "issue_type": (getattr(s, "issue_type", "") or "") if s.source == "jira" else "",
                "title": s.title,
                "body": (s.body or "")[:2000],
                "matterai_summary": (s.matterai_summary or "")[:500],
                "matterai_severity": s.matterai_severity or {},
                "epic_key": s.epic_key,
                "epic_domain": edom,
                "epic_body": (s.epic_body or "")[:1000],
                "confluence_body": (s.confluence_body or "")[:1000],
                "content_hash": h,
            })

    # Sort: epics → jira children → confluence → github
    pending.sort(key=lambda rec: _sort_key(rec, auto_slug_subjects))

    out_path = Path(args.out)
    out_path.write_text(json.dumps(pending, indent=2))
    rules_path = out_path.with_suffix(out_path.suffix + ".rules.md")
    rules_path.write_text(_rules_md(project_slugs))
    print(f"dump_pending: {len(pending)} subjects → {out_path}")
    print(f"dump_pending: rules → {rules_path}")


if __name__ == "__main__":
    main()
