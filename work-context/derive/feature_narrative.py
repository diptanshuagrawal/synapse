#!/usr/bin/env python3
"""feature_narrative.py — render a feature's end-to-end delivery narrative.

Phase 1 of the feature-narrative + Feature Score work
(PRD: prd/feature-narrative-scorer.md). Deterministic: the timeline and the
dated release stream are facts pulled from events.db / feature_release /
feature_stage — no LLM. (Phase 2 adds the Feature Score; a chat skill can later
wrap this for prose polish under the plain-English contract.)

Two units, same renderer:
  * slug  → domain rollup (the whole workstream's delivery history)
  * epic  → bounded journey (one epic, stages anchored to its creation)

Output is markdown written to derived/features/feature-<scope>.md, plus a short
summary to stdout. Sections: TL;DR · Timeline · Release stream · Scope ·
Data gaps. The release stream IS the "when / what shipped" spine of the ask.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from ingest.common import get_db  # noqa: E402
from derive.feature_resolve import FeatureArtefacts, resolve  # noqa: E402
from derive.feature_stages import STAGE_ORDER, compute_stages  # noqa: E402

OUT_DIR = _REPO_ROOT / "derived" / "features"
STREAM_CAP = 40  # rows shown in the release table; overflow is counted, never silently dropped

_STAGE_LABEL = {"planning": "Planning", "trd": "TRD / design", "code_dev": "Code dev", "rollout": "Rollout"}
_OUTCOME_LABEL = {
    "released": "released", "emergency": "released (emergency)",
    "rolled_back": "rolled back", "cancelled": "cancelled", "pending": "awaiting release",
}


def _date(ts: str | None) -> str:
    return ts[:10] if ts else "—"


def _days_between(a: str | None, b: str | None) -> int | None:
    if not a or not b:
        return None
    try:
        da = datetime.fromisoformat(a.replace("Z", "+00:00"))
        db = datetime.fromisoformat(b.replace("Z", "+00:00"))
        return (db - da).days
    except ValueError:
        return None


def _release_stream(conn, fa: FeatureArtefacts) -> list[dict]:
    """Distinct CMRs (one per release) ordered by release date, newest first."""
    if not fa.release_cmrs:
        return []
    ph = ",".join("?" for _ in fa.release_cmrs)
    rows = conn.execute(
        f"SELECT cmr_subject, title, url, released_at, approved_at, approved_by, "
        f"release_owner, impacted_areas, outcome, pr_urls_json "
        f"FROM feature_release WHERE cmr_subject IN ({ph}) "
        f"GROUP BY cmr_subject "
        f"ORDER BY (released_at IS NULL), released_at DESC",
        fa.release_cmrs,
    ).fetchall()
    out = []
    for r in rows:
        out.append({
            "cmr": r[0], "title": (r[1] or "").replace("[Epic ", "").split("] ", 1)[-1],
            "url": r[2], "released_at": r[3], "approved_at": r[4], "approved_by": r[5],
            "owner": r[6], "impacted": r[7], "outcome": r[8],
            "prs": json.loads(r[9] or "[]"),
        })
    return out


def _outcome_tally(stream: list[dict]) -> dict[str, int]:
    t: dict[str, int] = {}
    for r in stream:
        t[r["outcome"]] = t.get(r["outcome"], 0) + 1
    return t


def render(conn, fa: FeatureArtefacts) -> str:
    stages = compute_stages(conn, fa)
    by_stage = {s["stage"]: s for s in stages}
    stream = _release_stream(conn, fa)
    tally = _outcome_tally(stream)
    released = [r for r in stream if r["released_at"]]
    first_rel = released[-1]["released_at"] if released else None
    last_rel = released[0]["released_at"] if released else None

    unit = (f"epic journey — {fa.epic}" if fa.mode == "epic" else "domain rollup")
    L: list[str] = []
    L.append(f"# {fa.name} — delivery narrative")
    L.append("")
    L.append(f"_Unit: {unit} · slug `{fa.slug}` · generated {datetime.now(timezone.utc).date()}_")
    L.append("")

    # ── TL;DR ────────────────────────────────────────────────────────────────
    L.append("## TL;DR")
    L.append("")
    rolled = tally.get("rolled_back", 0)
    emerg = tally.get("emergency", 0)
    if released:
        L.append(f"- {len(released)} releases shipped, {_date(first_rel)} → {_date(last_rel)}.")
    if tally.get("pending"):
        L.append(f"- {tally['pending']} change(s) approved/awaiting release.")
    health = []
    if emerg:
        health.append(f"{emerg} emergency")
    if rolled:
        health.append(f"{rolled} rolled back")
    if tally.get("cancelled"):
        health.append(f"{tally['cancelled']} cancelled")
    if health:
        L.append(f"- Release health flags: {', '.join(health)}.")
    elif released:
        L.append("- No rollbacks or emergency releases — clean delivery.")
    plan = by_stage.get("planning")
    if plan:
        L.append(f"- Planning anchored {_date(plan['entered_at'])}.")
    missing = [_STAGE_LABEL[s] for s in STAGE_ORDER if s not in by_stage]
    if missing:
        L.append(f"- Not detected in data: {', '.join(missing)}.")
    L.append("")

    # ── Timeline ───────────────────────────────────────────────────────────────
    L.append("## Timeline")
    L.append("")
    prev = None
    for stage in STAGE_ORDER:
        s = by_stage.get(stage)
        if not s:
            L.append(f"- **{_STAGE_LABEL[stage]}** — not detected")
            continue
        gap = _days_between(prev, s["entered_at"])
        gap_s = f" (+{gap}d)" if gap is not None and gap >= 0 else ""
        conf = "" if s["confidence"] == "high" else f" · {s['confidence']}-confidence"
        L.append(f"- **{_STAGE_LABEL[stage]}** — {_date(s['entered_at'])}{gap_s} · {s['artefact_count']} artefact(s){conf}")
        prev = s["entered_at"] or prev
    L.append("")
    if any(_days_between(by_stage[a]["entered_at"], by_stage[b]["entered_at"]) is not None
           and _days_between(by_stage[a]["entered_at"], by_stage[b]["entered_at"]) < 0
           for a, b in zip(STAGE_ORDER, STAGE_ORDER[1:]) if a in by_stage and b in by_stage):
        L.append("> Note: stages are not strictly sequential — this feature's releases / artefacts "
                 "span a wider window than the planning anchor (expected for a long-running domain).")
        L.append("")

    # ── Release stream ──────────────────────────────────────────────────────────
    L.append("## Release stream")
    L.append("")
    if not stream:
        L.append("_No release records (CMRs) attributed to this feature._")
    else:
        L.append(f"{len(stream)} release record(s). " + (f"Showing latest {STREAM_CAP}." if len(stream) > STREAM_CAP else ""))
        L.append("")
        L.append("| Date | Change | Outcome | Owner | PRs |")
        L.append("|------|--------|---------|-------|-----|")
        for r in stream[:STREAM_CAP]:
            prs = ", ".join(f"[{p}](https://github.com/{p.replace('#', '/pull/')})" for p in r["prs"][:3]) or "—"
            owner = (r["owner"] or "").split("@")[0]
            title = (r["title"] or r["cmr"]).replace("|", "\\|")[:60]
            link = f"[{title}]({r['url']})" if r["url"] else title
            L.append(f"| {_date(r['released_at'])} | {link} | {_OUTCOME_LABEL.get(r['outcome'], r['outcome'])} | {owner} | {prs} |")
        if len(stream) > STREAM_CAP:
            L.append("")
            L.append(f"_…{len(stream) - STREAM_CAP} older release(s) not shown._")
    L.append("")

    # ── Scope ────────────────────────────────────────────────────────────────
    L.append("## Scope")
    L.append("")
    c = fa.counts()
    L.append(f"- Epics: {', '.join(fa.epics) or '—'}")
    L.append(f"- Jira tickets: {c['jira']} · PRs: {c['github']} · Confluence pages: {c['confluence']} · Slack threads: {c['slack']}")
    if fa.mode == "epic":
        L.append(f"- Release attribution: {fa.release_scope} ({len(fa.release_cmrs)} CMRs)")
    L.append("")

    # ── Data gaps ──────────────────────────────────────────────────────────────
    L.append("## Data silent on")
    L.append("")
    gaps = []
    if "code_dev" not in by_stage:
        gaps.append("Code-dev start — no PRs tie to this feature's shipped CMRs.")
    if by_stage.get("trd", {}).get("confidence") == "low":
        gaps.append("TRD — no Confluence page is explicitly linked; date inferred from keyword-matched pages.")
    if fa.mode == "epic" and fa.release_scope == "slug":
        gaps.append("Per-epic release isolation — this epic has no release records of its own; "
                    "releases shown are the slug's, bounded to the epic's start.")
    gaps.append("Production telemetry (adoption, latency, error rate) is not captured — rollout = release record, not live prod metrics.")
    for g in gaps:
        L.append(f"- {g}")
    L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="Render a feature delivery narrative.")
    ap.add_argument("token", help="slug, Jira epic key, or feature name")
    ap.add_argument("--stdout", action="store_true", help="print markdown instead of writing a file")
    args = ap.parse_args()

    conn = get_db()
    fa = resolve(conn, args.token)
    if fa is None:
        print(f"no feature matched {args.token!r}")
        return 1
    md = render(conn, fa)
    if args.stdout:
        print(md)
        return 0
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    scope = f"{fa.slug}-{fa.epic}" if fa.mode == "epic" else fa.slug
    out = OUT_DIR / f"feature-{scope}.md"
    out.write_text(md)
    print(f"wrote {out.relative_to(_REPO_ROOT)}  ({len(md.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
