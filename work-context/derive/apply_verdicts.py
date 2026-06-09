"""Apply chat-session-emitted verdicts JSON to subject_summary cache.

Validation mirrors llm_classifier.SYSTEM_PROMPT + tool schema:
  - domains      ⊆ project slug enum
  - risk_flags   ⊆ {security, data-loss, panic, race, migration, breaking-api}
  - summary      ≤ 200 chars (truncated with ellipsis if over)
  - confidence   ∈ [0, 1]
  - epic anchor  : if epic_domain in pending hint → re-ordered to front (via _apply_epic_anchor)

Invalid rows are dropped from the INSERT batch and printed to stderr.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import llm_classifier as lc  # noqa: E402
import rollup as r            # noqa: E402

DB = lc.ROOT / "index" / "events.db"
DERIVED_VERDICTS = lc.ROOT / "derived" / "verdicts"
MODEL_TAG = "claude-opus-4-7[1m]-chat"

RISK_ENUM = {"security", "data-loss", "panic", "race", "migration", "breaking-api"}
SUMMARY_MAX = 200
OWNERSHIP_REASONING_MAX = 300
OWNERSHIP_CONF_THRESHOLD = 0.6   # below this → ownership fields nulled


def _load_team_ids() -> set[str]:
    """Read config/teams.yaml team-id enum for ownership validation."""
    import yaml
    teams_path = Path(__file__).resolve().parent.parent / "config" / "teams.yaml"
    if not teams_path.exists():
        return set()
    with teams_path.open() as f:
        cfg = yaml.safe_load(f)
    return {t.get("id", "") for t in (cfg.get("teams", []) or []) if t.get("id")}


def _validate(v: dict, pending: dict, slug_set: set[str], epic_to_slug: dict[str, str],
              team_id_set: set[str] | None = None) -> tuple[dict | None, list[str]]:
    """Return (cleaned_verdict, errors)."""
    errs: list[str] = []
    sub = v.get("subject")
    h = v.get("content_hash")
    if not sub or not h:
        return None, ["missing subject or content_hash"]
    p = pending.get(sub)
    if p is None:
        errs.append(f"subject {sub} not in pending file — stale verdict")
        return None, errs
    if p.get("content_hash") != h:
        errs.append(f"{sub}: content_hash mismatch (pending={p.get('content_hash')} verdict={h})")
        return None, errs

    domains_raw = v.get("domains") or []
    invalid_slugs = [d for d in domains_raw if d not in slug_set]
    if invalid_slugs:
        errs.append(f"{sub}: invalid slugs {invalid_slugs} dropped")
    domains = [d for d in domains_raw if d in slug_set]

    risk_raw = v.get("risk_flags") or []
    invalid_risk = [r_ for r_ in risk_raw if r_ not in RISK_ENUM]
    if invalid_risk:
        errs.append(f"{sub}: invalid risk_flags {invalid_risk} dropped")
    risk_flags = [r_ for r_ in risk_raw if r_ in RISK_ENUM]

    summary = (v.get("summary") or "").strip().replace("\n", " ")
    if len(summary) > SUMMARY_MAX:
        summary = summary[:SUMMARY_MAX - 1] + "…"
        errs.append(f"{sub}: summary truncated to {SUMMARY_MAX}")

    conf = float(v.get("confidence", 0.85))
    conf = max(0.0, min(1.0, conf))

    # Build SubjectVerdict for epic-anchor reuse.
    sv = lc.SubjectVerdict(domains=domains, summary=summary, risk_flags=risk_flags,
                            confidence=conf, source="claude")
    sv = lc._apply_epic_anchor(sv, p.get("epic_key", ""), epic_to_slug)

    # ── Ownership fields (NEW) ────────────────────────────────────────
    team_ids = team_id_set or set()
    owned_primary = v.get("owned_by_primary") or None
    co_raw = v.get("co_owners") or []
    owned_conf = float(v.get("owned_by_confidence", 0.0))
    owned_conf = max(0.0, min(1.0, owned_conf))
    reasoning = (v.get("ownership_reasoning") or "").strip().replace("\n", " ")
    if len(reasoning) > OWNERSHIP_REASONING_MAX:
        reasoning = reasoning[:OWNERSHIP_REASONING_MAX - 1] + "…"

    if owned_primary and team_ids and owned_primary not in team_ids:
        errs.append(f"{sub}: unknown owned_by_primary {owned_primary!r} — nulled")
        owned_primary = None
        owned_conf = 0.0
        reasoning = ""
        co_clean = []
    else:
        co_clean = [c for c in co_raw if c in team_ids] if team_ids else list(co_raw)
        dropped_co = [c for c in co_raw if c not in (set(co_clean) | {None})]
        if dropped_co:
            errs.append(f"{sub}: dropped unknown co_owners {dropped_co}")

    # If ownership confidence too low, null the ownership fields. Domain
    # classification is still kept (separate confidence + threshold).
    if owned_primary and owned_conf < OWNERSHIP_CONF_THRESHOLD:
        errs.append(
            f"{sub}: ownership conf {owned_conf:.2f} below {OWNERSHIP_CONF_THRESHOLD} — nulled"
        )
        owned_primary = None
        owned_conf = 0.0
        reasoning = ""
        co_clean = []

    return {
        "subject": sub,
        "content_hash": h,
        "domains": sv.domains,
        "summary": sv.summary,
        "risk_flags": sv.risk_flags,
        "confidence": sv.confidence,
        "detail": (v.get("detail") or "").strip(),
        "needs_diff": bool(v.get("needs_diff")),
        # ownership
        "owned_by_primary": owned_primary,
        "co_owners": co_clean,
        "owned_by_confidence": owned_conf,
        "ownership_reasoning": reasoning,
    }, errs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--pending", required=True,
                    help="pending JSON path (used to validate hashes + join context)")
    args = ap.parse_args()

    conn = sqlite3.connect(str(DB))
    lc.ensure_schema(conn)

    pending_list = json.loads(Path(args.pending).read_text())
    pending = {p["subject"]: p for p in pending_list}
    projects = r.load_projects()
    slug_set = {p["slug"] for p in projects}
    epic_to_slug = lc._build_epic_to_slug(projects)
    team_id_set = _load_team_ids()
    verdicts_raw = json.loads(Path(args.inp).read_text())

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    final: list[dict] = []
    written = 0
    needs_diff_subjects: list[str] = []

    for v in verdicts_raw:
        cleaned, errs = _validate(v, pending, slug_set, epic_to_slug, team_id_set)
        for e in errs:
            print(f"  WARN {e}", file=sys.stderr)
        if cleaned is None:
            continue
        # Reject verdicts that need diffs OR are low-confidence.
        # Both stay uncached → LLM path (with diff access) re-classifies them.
        CONF_THRESHOLD = 0.7
        if cleaned["needs_diff"]:
            needs_diff_subjects.append(cleaned["subject"])
            print(
                f"  SKIP {cleaned['subject']}  needs_diff=true"
                " — not cached; stays pending for next /rollup chat session",
                file=sys.stderr,
            )
            continue
        if cleaned["confidence"] < CONF_THRESHOLD:
            print(
                f"  SKIP {cleaned['subject']}  conf={cleaned['confidence']:.2f} < {CONF_THRESHOLD}"
                " — not cached; stays pending for next /rollup chat session",
                file=sys.stderr,
            )
            continue
        sub = cleaned["subject"]
        h = cleaned["content_hash"]
        # Drop any prior rows for this subject (regardless of old content_hash).
        # PK is (subject, content_hash) so content drift would otherwise leave
        # stale rows; downstream readers (jira_metrics, link_clusters_to_projects)
        # filter by subject alone and would double-count.
        conn.execute("DELETE FROM subject_summary WHERE subject=?", (sub,))
        conn.execute(
            "INSERT OR REPLACE INTO subject_summary "
            "(subject, content_hash, domains, summary, risk_flags, confidence, "
            " source, model, classified_at, input_tokens, output_tokens, detail, "
            " owned_by_primary, co_owners_json, owned_by_confidence, ownership_reasoning) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                sub, h,
                json.dumps(cleaned["domains"]),
                cleaned["summary"],
                json.dumps(cleaned["risk_flags"]),
                cleaned["confidence"],
                "claude",
                MODEL_TAG,
                now,
                0, 0,
                cleaned["detail"],
                cleaned["owned_by_primary"],
                json.dumps(cleaned["co_owners"]),
                cleaned["owned_by_confidence"],
                cleaned["ownership_reasoning"],
            ),
        )
        written += 1
        p = pending[sub]
        final.append({
            "subject": sub,
            "source": p.get("source", ""),
            "title": p.get("title", ""),
            "epic_key": p.get("epic_key", ""),
            "domains": cleaned["domains"],
            "summary": cleaned["summary"],
            "risk_flags": cleaned["risk_flags"],
            "confidence": cleaned["confidence"],
            "needs_diff": cleaned["needs_diff"],
            "classified_at": now,
            "content_hash": h,
            "owned_by_primary": cleaned["owned_by_primary"],
            "co_owners": cleaned["co_owners"],
            "owned_by_confidence": cleaned["owned_by_confidence"],
            "ownership_reasoning": cleaned["ownership_reasoning"],
        })
        own = cleaned["owned_by_primary"] or "-"
        print(
            f"+ {sub}  domains={cleaned['domains']}  risk={cleaned['risk_flags']}  "
            f"own={own} ({cleaned['owned_by_confidence']:.2f})"
        )
    conn.commit()

    DERIVED_VERDICTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (DERIVED_VERDICTS / "latest.json").write_text(json.dumps(final, indent=2))
    (DERIVED_VERDICTS / f"{stamp}.json").write_text(json.dumps(final, indent=2))
    print(f"apply_verdicts: wrote {written} rows + final → {DERIVED_VERDICTS}/latest.json")
    if needs_diff_subjects:
        print(f"apply_verdicts: needs_diff flagged on {len(needs_diff_subjects)}: {needs_diff_subjects}", file=sys.stderr)


if __name__ == "__main__":
    main()
