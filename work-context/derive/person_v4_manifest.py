"""
person_v4_manifest.py — deterministic RENDER MANIFEST for /ask person_range.

PARALLEL VERSION — not wired into live /ask. Consumed by the parallel skill
.claude/commands/ask-v2.md and validated standalone before any wiring.

Why this exists
---------------
Two runs of the same /ask over the same window produced different narratives
(missed a workload quote, different cited tickets, different framing). The
scripts are deterministic; the *synthesis* (what to surface / cite / read / how
to phrase) happens in the chat model and is stochastic.

This module moves every SELECTION decision out of the model and into code:

  Layer A — render manifest: the exact, ranked, capped list of what to cite in
            each section, plus deterministic TL;DR fact seeds.
  Layer B — body extraction: scan the person's OWN authored bodies (slack incl.
            replies, jira comments, PR descriptions) for sentiment / risk /
            rollback / impact / dates per config/body_extractors.yaml, emit as
            flags + caveats. No "did the model open this thread" variance.
  (Layer C — verify gate — lives in derive/verify_render.py.)

The model's job shrinks to PHRASING the manifest. It cannot pick a different
top-5, miss a flag, or drop a section.

Determinism contract
---------------------
Same (name, since, until) + same events.db → byte-identical manifest. All
ordering is total (explicit tiebreak on subject id). No clocks, no randomness,
no set-iteration order leaks (sorted() everywhere).

Usage
-----
    .venv/bin/python derive/person_v4_manifest.py --name <canon> \\
        --since <iso> --until <iso> [--no-cache]

Emits one JSON manifest to stdout. Re-runs person_v3 + person_deepread under the
hood (or reads supplied --v3-json / --deep-json for fast iteration).

With --bundle-dir <dir>, additionally persists all three JSONs in ONE call —
<dir>/<canonical>_manifest.json, _v3.json, _deep.json — and prints a small
summary instead of the manifest. This is the /ask person_range fast path: the
chat session runs one command and Reads the files it needs, instead of
re-running person_v3/person_deepread as separate subprocesses.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

import yaml  # noqa: E402

from ingest.common import get_db  # noqa: E402
from derive.sources_config import slack_workspace  # noqa: E402
from derive.person_deepread import _person_aliases  # noqa: E402
from derive.person_profile import _ph  # noqa: E402

_VENV_PY = _PKG_ROOT / ".venv" / "bin" / "python"
_EXTRACTORS_PATH = _PKG_ROOT / "config" / "body_extractors.yaml"

# ----- section caps (deterministic; documented so reviewers can audit) -------
CAP_TLDR = 7
CAP_SHIPPED = 20
CAP_DESIGNED = 12
CAP_DB = 10
CAP_OPS = 8
CAP_WORKSTREAMS = 10
CAP_FLAG_EVIDENCE = 3  # snippets kept per flag kind


# ----- issue-type rank for stable shipped ordering --------------------------
_ITYPE_RANK = {"Story": 0, "Task": 1, "Bug": 2, "CMR": 3}


def _run_json(script: str, name: str, since: str, until: str) -> dict:
    out = subprocess.run(
        [str(_VENV_PY), str(_PKG_ROOT / "derive" / script),
         "--name", name, "--since", since, "--until", until],
        capture_output=True, text=True, cwd=str(_PKG_ROOT),
    )
    if out.returncode != 0:
        raise RuntimeError(f"{script} failed: {out.stderr[:400]}")
    return json.loads(out.stdout)


# =========================================================================
# Layer B — deterministic body extraction over the person's OWN content
# =========================================================================

def _load_extractors() -> dict:
    return yaml.safe_load(_EXTRACTORS_PATH.read_text())


def _own_bodies(aliases: list[str], since: str, until: str) -> list[dict]:
    """Every body-bearing row authored by the person in window — slack replies
    included (we do NOT restrict to thread_started), jira comments, PR bodies."""
    conn = get_db()
    ph = _ph(aliases)
    rows = conn.execute(
        f"""SELECT source, event_type, ts, subject, channel_id,
                   COALESCE(body,'') AS body
            FROM events
            WHERE actor IN ({ph}) AND ts >= ? AND ts < ?
              AND body IS NOT NULL AND LENGTH(body) > 0
            ORDER BY ts, subject""",
        (*aliases, since, until),
    ).fetchall()
    return [
        {"source": s, "event_type": et, "ts": (ts or "")[:16],
         "subject": sub, "channel_id": ch, "body": body}
        for s, et, ts, sub, ch, body in rows
    ]


def _scan_phrases(bodies: list[dict], phrases: list[str]) -> list[dict]:
    """Deterministic substring scan. Returns hits ordered by (ts, subject)."""
    low = [(p, p.lower()) for p in phrases]
    hits: list[dict] = []
    for b in bodies:
        bl = b["body"].lower()
        for orig, p in low:
            if p in bl:
                idx = bl.find(p)
                snippet = b["body"][max(0, idx - 60): idx + len(p) + 60].strip()
                snippet = re.sub(r"\s+", " ", snippet)
                hits.append({
                    "phrase": orig, "subject": b["subject"],
                    "source": b["source"], "ts": b["ts"], "snippet": snippet,
                })
                break  # one hit per body per category
    # total order: ts then subject
    return sorted(hits, key=lambda h: (h["ts"], h["subject"]))


def _scan_regexes(bodies: list[dict], patterns: list[str]) -> list[dict]:
    compiled = [re.compile(p, re.IGNORECASE) for p in patterns]
    hits: list[dict] = []
    for b in bodies:
        for rx in compiled:
            m = rx.search(b["body"])
            if m:
                idx = m.start()
                snippet = b["body"][max(0, idx - 40): m.end() + 40].strip()
                snippet = re.sub(r"\s+", " ", snippet)
                hits.append({
                    "match": m.group(0).strip(), "subject": b["subject"],
                    "source": b["source"], "ts": b["ts"], "snippet": snippet,
                })
                break
    return sorted(hits, key=lambda h: (h["ts"], h["subject"]))


def extract_body_facts(aliases: list[str], since: str, until: str) -> dict:
    ext = _load_extractors()
    bodies = _own_bodies(aliases, since, until)
    out: dict = {}
    for kind in ("sentiment_flags", "risk_phrases", "rollback_flags",
                 "approver_phrases"):
        cfg = ext.get(kind, {}) or {}
        hits = _scan_phrases(bodies, cfg.get("phrases", []))
        if hits:
            out[kind] = {"severity": cfg.get("severity", "note"),
                         "hits": hits[:CAP_FLAG_EVIDENCE * 3]}
    impact = _scan_regexes(bodies, ext.get("impact_regexes", []))
    if impact:
        out["impact_numbers"] = {"hits": impact[:CAP_FLAG_EVIDENCE * 3]}
    return out


# =========================================================================
# Layer A — render manifest from v3 + deepread (pure selection, no prose)
# =========================================================================

def _is_jira(subject: str) -> bool:
    return bool(re.match(r"^[A-Z]+-\d+$", subject or ""))


def _ticket_meta(deep: dict) -> dict:
    return {t["subject"]: t for t in deep.get("assigned_tickets", [])}


def _rank_shipped(items: list[dict], tmeta: dict) -> list[dict]:
    """Stable ranked shipped list: jira tickets only, by (issue-type rank,
    -story_points, subject)."""
    seen, picked = set(), []
    for it in items:
        sub = it.get("subject", "")
        if not _is_jira(sub) or sub in seen:
            continue
        seen.add(sub)
        m = tmeta.get(sub, {})
        picked.append({
            "cite": sub,
            "title": (it.get("title") or m.get("title") or "").strip(),
            "role": it.get("role", ""),
            "issue_type": m.get("issue_type", ""),
            "story_points": m.get("story_points"),
        })
    picked.sort(key=lambda x: (
        _ITYPE_RANK.get(x["issue_type"], 9),
        -(x["story_points"] or 0),
        x["cite"],
    ))
    return picked


# Design-doc title priority — a contract/TRD/schema/approach doc is a stronger
# headline than a setup/cloner doc. Body bytes are a poor tiebreak because most
# pages truncate at the same 5000-byte cap, so a byte-sort picks an arbitrary
# doc. Rank by what KIND of doc it is first, then bytes, then id.
_DESIGN_TITLE_PRIORITY = (
    ("api contract", 0), ("contract", 1), ("trd", 1), ("sequence diagram", 2),
    ("schema", 2), ("approach", 3), ("design", 3), ("accounting", 4),
)


def _design_rank(title: str) -> int:
    t = (title or "").lower()
    for kw, rank in _DESIGN_TITLE_PRIORITY:
        if kw in t:
            return rank
    return 8  # setup / local / cloner / misc — lowest


def _designed(v3: dict, deep: dict) -> list[dict]:
    """Confluence pages authored/edited, ranked by doc-kind priority then body
    bytes then subject. Excludes inline-comment rows."""
    seen, pages = set(), []
    for c in deep.get("confluence", []):
        title = (c.get("title") or "").strip()
        if title.lower().startswith("inline comment"):
            continue
        if c["subject"] in seen:
            continue
        seen.add(c["subject"])
        pages.append({"cite": c["subject"], "title": title,
                      "body_bytes": c.get("body_bytes") or 0,
                      "_rank": _design_rank(title)})
    pages.sort(key=lambda x: (x["_rank"], -x["body_bytes"], x["cite"]))
    for p in pages:
        p.pop("_rank", None)
    return pages[:CAP_DESIGNED]


_DB_KEYWORDS = ("reader db", "reader-db", "index", "partition", "autovacuum",
                "deadlock", "postgres", "db user", "archival")


def _bucket_db(shipped: list[dict]) -> list[dict]:
    out = [s for s in shipped
           if any(k in (s["title"] or "").lower() for k in _DB_KEYWORDS)]
    return out[:CAP_DB]


_ROLE_VERB = {"AUTHOR": "drove", "RESOLVER": "drove resolution of",
              "DECIDER": "called the shots on", "REVIEWER": "reviewed",
              "RESPONDER": "weighed in on"}

# Pure-coordination / membership / reminder clusters are NOT workstreams — drop
# by label pattern regardless of event count (the old n<4 gate let n>=4 noise
# like "channel-join membership" through). Substring match, case-insensitive.
_NOISE_LABEL_PATTERNS = (
    "channel-join", "membership event", "standup", "join reminder",
    "release-branch announc", "release branch announc", "fyi link relay",
    "issue forwarding", "status ping", "wfh notice", "late-login",
    "oncall review meeting ping", "report request template",
)


def _is_noise_label(label: str) -> bool:
    low = (label or "").lower()
    return any(p in low for p in _NOISE_LABEL_PATTERNS)


def _workstreams(v3: dict) -> list[dict]:
    """Led-first, all real workstreams (no event-count cap on led ones); drop
    only pure coordination/reminder noise by label pattern. Deterministic."""
    led, contributed = [], []
    for w in v3.get("workstreams", []):
        label = w.get("label") or ""
        if _is_noise_label(label):
            continue
        item = {
            "name": label,
            "role": w.get("role", ""),
            "verb": _ROLE_VERB.get(w.get("role", ""), "worked on"),
            "led": bool(w.get("led")),
            "n_subjects": w.get("n_person_subjects") or 0,
            "role_drift": bool(w.get("role_drift")),
            "lifetime_role": w.get("lifetime_role"),
        }
        (led if item["led"] else contributed).append(item)
    led.sort(key=lambda x: (-x["n_subjects"], x["name"]))
    contributed.sort(key=lambda x: (-x["n_subjects"], x["name"]))
    # keep ALL led workstreams (they're the leadership signal), cap contributed
    return led + contributed[:CAP_WORKSTREAMS]


# Slugs that are coordination / infra / process, NOT a design knowledge-surface.
# Excluded from breadth + bus-factor so "primary ownership" reflects real
# feature/design areas, not UAT-branch chores or release announcements.
_NON_DESIGN_SLUG_PATTERNS = ("uat", "coordination", "efficiency", "releases",
                             "announc", "release-")


def _is_design_slug(slug: str) -> bool:
    low = (slug or "").lower()
    return not any(p in low for p in _NON_DESIGN_SLUG_PATTERNS)


# Owner-level roles for bus-factor — these are the "knows it deeply" roles.
# REVIEWER / RESPONDER / participant are NOT ownership, so a slug others only
# review is still a sole-owner risk on authorship.
_OWNER_ROLES = {"AUTHOR", "DECIDER", "RESOLVER"}


def _slug_owners(conn, footprint_entry: dict) -> set[str]:
    """Distinct owner-level people across the clusters mapped to this slug,
    read from each cluster's topic_brief.participants_json. This is the REAL
    sole-ownership signal — how many people own the area — not a traffic proxy."""
    owners: set[str] = set()
    for c in footprint_entry.get("clusters", []):
        cid = c.get("cluster_id")
        if cid is None:
            continue
        row = conn.execute(
            "SELECT participants_json FROM topic_brief WHERE cluster_id=?", (cid,)
        ).fetchone()
        if not row or not row[0]:
            continue
        try:
            parts = json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            continue
        for p in parts:
            if (p.get("role") or "").upper() in _OWNER_ROLES:
                owners.add((p.get("person") or "").strip().lower())
    owners.discard("")
    return owners


def _footprint(v3: dict, canonical: str, conn) -> dict:
    """Project-level breadth + TRUE bus-factor. AUTHOR on a design slug = primary
    ownership (surfaced for every design slug, render-contract rule). Bus-factor =
    a design slug where the person is the ONLY owner-level participant (distinct
    owner count == 1, and it's him) — computed from participants, NOT from event
    volume. Coordination/infra slugs are excluded so the signal isn't UAT noise."""
    canon = (canonical or "").strip().lower()
    raw = v3.get("project_footprint", [])
    rows, bus_factor = [], []
    for p in raw:
        slug = p.get("project_slug")
        top_role = p.get("top_role_in_project")
        is_author_design = top_role == "AUTHOR" and _is_design_slug(slug)
        owners = _slug_owners(conn, p) if is_author_design else set()
        sole = bool(is_author_design and owners and owners.issubset({canon}))
        rows.append({
            "slug": slug,
            "top_role": top_role,
            "window_events": p.get("window_event_count_total") or 0,
            "clusters": p.get("cluster_count") or 0,
            "role_drift_clusters": p.get("role_drift_cluster_count") or 0,
            "owner_count": len(owners) if is_author_design else None,
            "sole_owner": sole,
        })
        if sole:
            bus_factor.append(slug)
    rows.sort(key=lambda x: (-x["window_events"], x["slug"] or ""))
    # "primary design ownership" = AUTHOR on a design slug with MEANINGFUL
    # activity. A 1-2 event slug isn't ownership; counting it inflates breadth.
    author_design = [r["slug"] for r in rows
                     if r["top_role"] == "AUTHOR" and _is_design_slug(r["slug"])
                     and r["window_events"] >= 5]
    # bus_factor ordered by activity (most-active sole-owned area first)
    bf_order = {r["slug"]: -r["window_events"] for r in rows}
    bus_factor.sort(key=lambda s: (bf_order.get(s, 0), s or ""))
    return {"projects": rows, "author_slugs": author_design,
            "breadth_author_count": len(author_design),
            "bus_factor_candidates": bus_factor}


def _role_drift(v3: dict) -> list[dict]:
    """Meaningful window-vs-lifetime role shifts, deduped by slug, mapped to
    plain direction. 'stepped into' (now AUTHOR) vs 'stepped back' (now responder)."""
    seen, out = set(), []
    rank = {"AUTHOR": 3, "RESOLVER": 3, "DECIDER": 3, "REVIEWER": 2,
            "RESPONDER": 1, None: 0}
    for d in v3.get("role_drift", []):
        slug = d.get("project_slug")
        if slug in seen or not _is_design_slug(slug):
            continue
        wr, lr = d.get("window_role"), d.get("lifetime_role")
        if rank.get(wr, 0) > rank.get(lr, 0):
            direction = "stepped into ownership"
        elif rank.get(wr, 0) < rank.get(lr, 0):
            direction = "stepped back to a supporting role"
        else:
            continue
        seen.add(slug)
        out.append({"slug": slug, "label": d.get("label"),
                    "window_role": wr, "lifetime_role": lr,
                    "direction": direction})
    # 'stepped into' first (the growth signal), then by slug
    out.sort(key=lambda x: (x["direction"] != "stepped into ownership",
                            x["slug"] or ""))
    return out


def _review_concentration(v3: dict) -> dict | None:
    rc = v3.get("review_concentration")
    if not rc or not rc.get("total"):
        return None
    return {"label": rc.get("label"), "comments": rc.get("comments"),
            "reviews": rc.get("reviews"), "commits": rc.get("commits"),
            "total": rc.get("total")}


def _own_prs(deep: dict) -> list[dict]:
    """PRs the person opened in window (subject is owner/repo#N)."""
    return [{"cite": p["subject"], "title": p.get("title", ""),
             "opened": p.get("opened_ts")}
            for p in deep.get("prs", []) if "#" in (p.get("subject") or "")]


# Thread-kind classifier from body text — lets the narrative tell a launch /
# coordination thread from an oncall / alert thread without naming the channel.
_THREAD_KIND_PATTERNS = (
    ("oncall", ("oncall", "on-call", "5xx", "alert", "grafana", "sentry",
                "latency", "spike", "pagerduty", "opsgenie")),
    ("release", ("prod readiness", "go-live", "go live", "release",
                 "deploy", "rollout", "cutover")),
    ("incident", ("incident", "rca", "root cause", "outage", "sev")),
)
# Pure-noise thread bodies to skip (reminders, standups, membership).
_THREAD_NOISE = ("standup", "join reminder", "wfh", "late-login",
                 "release-branch announc")


def _classify_thread(body: str) -> str:
    low = (body or "").lower()
    for kind, pats in _THREAD_KIND_PATTERNS:
        if any(p in low for p in pats):
            return kind
    return "coordination"


def _key_threads(deep: dict, top_n: int = 6) -> list[dict]:
    """Top threads the person started, by reply count — the coordination /
    launch / oncall artefacts that carry the month's high-signal discussion.
    Already returned by deepread; selected + classified here deterministically."""
    out = []
    for t in deep.get("slack_threads", []):
        body = t.get("preview") or t.get("body") or ""
        if any(p in body.lower() for p in _THREAD_NOISE):
            continue
        out.append({
            "cite": t.get("subject"),
            "channel_id": t.get("channel_id"),
            "reply_count": t.get("reply_count") or 0,
            "kind": _classify_thread(body),
            "preview": re.sub(r"\s+", " ", body)[:160],
        })
    out.sort(key=lambda x: (-x["reply_count"], x["cite"] or ""))
    return out[:top_n]


def _slack_url(subject: str) -> str | None:
    """slack:CH:ts -> archive permalink. None if not a slack subject."""
    if not subject or not subject.startswith("slack:"):
        return None
    parts = subject.split(":")
    if len(parts) != 3:
        return None
    _, ch, ts = parts
    return f"https://{slack_workspace()}.slack.com/archives/{ch}/p{ts.replace('.', '')}"


def _narrative_signals(v3: dict) -> dict:
    """Sprint cadence, domain-ownership shares, ops load, quality flags, pace —
    all already computed by v3; surfaced for the rich render."""
    v1 = v3.get("v1_signals", {})
    qual = v3.get("quality", {})
    pace = v3.get("pace", {})
    # domain ownership where the person actually authored merged PRs
    owned = [{"domain": d.get("domain"), "share_pct": d.get("share_pct"),
              "label": d.get("label"),
              "authored": d.get("person_authored_merged")}
             for d in v1.get("domain_ownership", [])
             if (d.get("person_authored_merged") or 0) > 0]
    owned.sort(key=lambda x: -(x["share_pct"] or 0))
    return {
        "tier_verdict": (v3.get("rating", {})
                         .get("v1_feature_verdict", {}).get("tier_deviation")),
        "feature_yardstick_applicable":
            v3.get("rating", {}).get("feature_yardstick_applicable"),
        "team_rank": v1.get("team_rank"),
        "sp_attributed": v1.get("sp_attributed"),
        "team_median_sp": v1.get("team_median_sp"),
        "team_top_sp": v1.get("team_top_sp"),
        "team_size": v1.get("team_sp_count"),
        "by_sprint": v1.get("by_sprint", []),
        "ops_tickets_n": v1.get("ops_tickets_n"),
        "domain_ownership": owned,
        "matterai_critical_flags": qual.get("pr_matterai_critical_flags"),
        "matterai_quality_p50": qual.get("pr_matterai_quality_p50_pct"),
        "pr_cycle_median_days": pace.get("pr_cycle_median_days"),
        "same_day_prs": pace.get("same_day_pr_count"),
        "slow_prs_over_14d": pace.get("slow_pr_count_over_14d"),
        "prs_shipped": pace.get("shipped"),
        "prs_in_flight": pace.get("in_flight"),
    }


def _flags(v3: dict, body_facts: dict) -> list[dict]:
    flags: list[dict] = []
    contrib = v3.get("contribution", {})
    qual = v3.get("quality", {})
    # structural: commits-in-PR without own PRs
    if (contrib.get("substantive_pr_commits", 0) > 0
            and qual.get("pr_count_in_window", 0) == 0):
        flags.append({
            "kind": "commit_without_pr",
            "metric": {"commits_in_pr": contrib["substantive_pr_commits"],
                       "pr_reviews": contrib.get("pr_reviews_total", 0),
                       "own_prs": 0},
        })
    # body-derived
    if "sentiment_flags" in body_facts:
        h = body_facts["sentiment_flags"]
        flags.append({
            "kind": "workload_sentiment",
            "severity": h["severity"],
            "evidence": [{"subject": x["subject"], "phrase": x["phrase"],
                          "snippet": x["snippet"]} for x in h["hits"][:CAP_FLAG_EVIDENCE]],
        })
    if "risk_phrases" in body_facts:
        h = body_facts["risk_phrases"]
        flags.append({
            "kind": "risk_callout",
            "evidence": [{"subject": x["subject"], "phrase": x["phrase"],
                          "snippet": x["snippet"]} for x in h["hits"][:CAP_FLAG_EVIDENCE]],
        })
    return flags


def _caveats(v3: dict) -> list[dict]:
    cav = []
    chain = (v3.get("v1_signals") or {}).get("attribution_chain", {})
    if chain.get("creation_fallback", 0) > chain.get("changelog", 0):
        cav.append({"kind": "sp_attribution_fallback",
                    "changelog": chain.get("changelog", 0),
                    "fallback": chain.get("creation_fallback", 0)})
    if (v3.get("quality") or {}).get("pr_count_in_window", 0) == 0:
        cav.append({"kind": "no_own_prs",
                    "implication": "no code-quality / merge-speed signal this window"})
    return cav


def _tldr_facts(v3: dict, shipped: list[dict], designed: list[dict],
                flags: list[dict], v3rating: dict, footprint: dict,
                nsig: dict) -> list[dict]:
    """Deterministic top-N TL;DR seeds in fixed priority order."""
    facts: list[dict] = []
    # 1 — work-mix + tier verdict line (always). When the feature yardstick
    #     applies, state the tier verdict; otherwise say it's not the yardstick.
    if nsig.get("feature_yardstick_applicable") and nsig.get("tier_verdict"):
        wm = f"Feature-weighted month — lands {nsig['tier_verdict']} for level."
    else:
        wm = v3rating.get("note", "").split(".")[0]
    facts.append({"key": "work_mix", "fact": wm, "cite": []})
    # 2 — top shipped delivery
    if shipped:
        top = shipped[0]
        facts.append({"key": "top_delivery",
                      "fact": f"shipped {top['title']}",
                      "cite": [s["cite"] for s in shipped[:3]]})
    # 3 — workload flag (high priority so it never drops)
    wf = next((f for f in flags if f["kind"] == "workload_sentiment"), None)
    if wf:
        facts.append({"key": "workload",
                      "fact": "self-reported workload strain in-thread",
                      "cite": [e["subject"] for e in wf["evidence"][:1]]})
    # 4 — design output
    if designed:
        facts.append({"key": "design",
                      "fact": f"authored design docs incl. {designed[0]['title']}",
                      "cite": [d["cite"] for d in designed[:2]]})
    # 5 — commit-without-PR inversion
    cf = next((f for f in flags if f["kind"] == "commit_without_pr"), None)
    if cf:
        m = cf["metric"]
        facts.append({"key": "commit_inversion",
                      "fact": f"{m['commits_in_pr']} commits into others' PRs, "
                              f"{m['pr_reviews']} reviews, {m['own_prs']} own PRs",
                      "cite": []})
    # 6 — design-ownership breadth (always meaningful for a design engineer);
    #     escalate to a bus-factor note only when a thin sole-owner slug exists.
    author = footprint.get("author_slugs", [])
    if author:
        bf = footprint.get("bus_factor_candidates", [])
        if bf:
            fact = (f"primary design owner across {len(author)} areas incl. "
                    f"{', '.join(author[:3])} — sole owner on "
                    f"{', '.join(bf[:2])} (knowledge concentration)")
        else:
            fact = (f"primary design owner across {len(author)} areas incl. "
                    f"{', '.join(author[:3])}")
        facts.append({"key": "ownership_breadth", "fact": fact, "cite": []})
    # 7 — risk callout
    rf = next((f for f in flags if f["kind"] == "risk_callout"), None)
    if rf:
        facts.append({"key": "risk",
                      "fact": "raised a correctness/safety callout",
                      "cite": [e["subject"] for e in rf["evidence"][:1]]})
    return facts[:CAP_TLDR]


def build_manifest(name: str, since: str, until: str,
                   v3: dict | None = None, deep: dict | None = None) -> dict:
    pa = _person_aliases(name)
    if not pa:
        return {"error": f"unknown person: {name}"}
    canonical, aliases = pa

    if v3 is None:
        v3 = _run_json("person_v3.py", name, since, until)
    if deep is None:
        deep = _run_json("person_deepread.py", name, since, until)

    body_facts = extract_body_facts(aliases, since, until)
    tmeta = _ticket_meta(deep)

    delivery = v3.get("delivery", {})
    shipped = _rank_shipped(delivery.get("shipped", {}).get("primary", []), tmeta)
    designed = _designed(v3, deep)
    db_platform = _bucket_db(shipped)
    ops = _rank_shipped(delivery.get("ops", {}).get("primary", []), tmeta)[:CAP_OPS]
    workstreams = _workstreams(v3)
    footprint = _footprint(v3, canonical, get_db())
    role_drift = _role_drift(v3)
    review_conc = _review_concentration(v3)
    own_prs = _own_prs(deep)
    key_threads = _key_threads(deep)
    nsig = _narrative_signals(v3)
    flags = _flags(v3, body_facts)
    caveats = _caveats(v3)
    rating = v3.get("rating", {})
    tldr = _tldr_facts(v3, shipped, designed, flags, rating, footprint, nsig)

    # verify manifest = every must-appear token.
    # Only SECTION artefacts (jira ids + page ids) become literal tokens; the
    # gate matches pages by id-or-title. flag/caveat presence is matched by the
    # gate's marker logic — we do NOT add their slack/PR evidence subjects as
    # literal tokens (slack renders as a different URL and would never match
    # verbatim; the flag:<kind> token already covers it).
    verify = set()
    for s in shipped + designed + db_platform + ops:
        verify.add(s["cite"])
    for f in flags:
        verify.add(f"flag:{f['kind']}")
    for c in caveats:
        verify.add(f"caveat:{c['kind']}")

    # title lookup so the gate can match a page by its title when prose cites
    # the doc by name rather than by id/url.
    cite_titles = {}
    for s in designed + shipped + db_platform + ops:
        if s.get("title"):
            cite_titles[s["cite"]] = s["title"]

    return {
        "schema_version": "render-1",
        "person": canonical,
        "window": {"since": since, "until": until},
        "headline": {
            "work_mix": rating.get("window_work_mix"),
            "baseline_role": rating.get("baseline_role_120d"),
            "feature_yardstick_applicable": rating.get("feature_yardstick_applicable"),
            "verdict_note": rating.get("note"),
            "tldr_facts": tldr,
        },
        "sections": {
            "shipped": shipped,
            "designed": designed,
            "db_platform": db_platform,
            "ops": ops,
            "workstreams": workstreams,
            "own_prs": own_prs,
        },
        "footprint": footprint,
        "role_drift": role_drift,
        "review_concentration": review_conc,
        "key_threads": key_threads,
        "narrative_signals": nsig,
        "flags": flags,
        "caveats": caveats,
        "behavioral": v3.get("behavioral", {}),
        "completion": v3.get("completion", {}),
        "ticket_fate": v3.get("ticket_fate", {}),
        "contribution": v3.get("contribution", {}),
        "verify_manifest": sorted(verify),
        "cite_titles": cite_titles,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", required=True)
    ap.add_argument("--since", required=True)
    ap.add_argument("--until", required=True)
    ap.add_argument("--v3-json", help="path to pre-computed person_v3 JSON (skip subprocess)")
    ap.add_argument("--deep-json", help="path to pre-computed person_deepread JSON")
    ap.add_argument("--bundle-dir",
                    help="write <dir>/<canonical>_{manifest,v3,deep}.json in one "
                         "call and print a path summary instead of the manifest")
    args = ap.parse_args()

    v3 = json.load(open(args.v3_json)) if args.v3_json else None
    deep = json.load(open(args.deep_json)) if args.deep_json else None

    if args.bundle_dir:
        if v3 is None:
            v3 = _run_json("person_v3.py", args.name, args.since, args.until)
        if deep is None:
            deep = _run_json("person_deepread.py", args.name, args.since, args.until)

    manifest = build_manifest(args.name, args.since, args.until, v3=v3, deep=deep)

    if args.bundle_dir:
        if "error" in manifest:
            sys.stdout.write(json.dumps(manifest, indent=2))
            sys.exit(1)
        d = Path(args.bundle_dir)
        d.mkdir(parents=True, exist_ok=True)
        canon = manifest["person"]
        written = {}
        for key, obj in (("manifest", manifest), ("v3", v3), ("deep", deep)):
            p = d / f"{canon}_{key}.json"
            p.write_text(json.dumps(obj, indent=1, default=str))
            written[key] = str(p)
        sys.stdout.write(json.dumps({
            "bundle": written,
            "person": canon,
            "window": {"since": args.since, "until": args.until},
            "verify_items": len(manifest.get("verify_manifest", [])),
        }, indent=2))
        return

    sys.stdout.write(json.dumps(manifest, indent=2, default=str))


if __name__ == "__main__":
    main()
