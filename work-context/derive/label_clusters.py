"""
label_clusters.py — chat-driven cluster labeling.

Mirrors the `/rollup` pattern: scripts NEVER call an LLM API. The Claude
session in the user's chat does the actual reasoning, then runs `apply` to
persist results. Aligned with project memory feedback_openai_embeddings_only
and project_chat_only_classification.

Flow
----
    1. `dump`  — re-cluster `embedding` table, write member content to
                 state/pending_cluster_labels.json (+ sibling rules.md).
    2. (chat)  — Claude reads dump + rules, produces
                 state/verdicts.cluster_labels.json with one label per cluster.
    3. `apply` — read verdicts, persist to `topic_brief` + `topic_brief_member`.

Each phase is idempotent and recoverable independently.

Files
-----
    state/pending_cluster_labels.json
        {"computed_at": ..., "min_cluster_size": N, "clusters": [
            {"cluster_id": int, "n_members": int, "sources_breakdown": "...",
             "members": [{"subject": str, "source": str, "content": str}, ...]},
            ...]}

    state/pending_cluster_labels.json.rules.md
        Role-aware prompt + examples + JSON output shape Claude must produce.

    state/verdicts.cluster_labels.json
        [{"cluster_id": int, "label": str, "what_work": str, "confidence": float}]

CLI
---
    .venv/bin/python derive/label_clusters.py dump [--min-cluster-size 3]
    .venv/bin/python derive/label_clusters.py apply
    .venv/bin/python derive/label_clusters.py status
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from ingest.common import get_db  # noqa: E402
from derive.subject_content import get_content  # noqa: E402

ROOT = _PKG_ROOT
PENDING_PATH = ROOT / "state" / "pending_cluster_labels.json"
RULES_PATH = ROOT / "state" / "pending_cluster_labels.json.rules.md"
VERDICTS_PATH = ROOT / "state" / "verdicts.cluster_labels.json"
# Maintained rules source — edit this file to change labelling rules.
# dump copies it into RULES_PATH on every invocation so chat reads the
# latest version without us re-stamping it in code.
RULES_SOURCE_PATH = ROOT / "derive" / "cluster_label_rules.md"


_RULES_MD = """\
# Cluster label rules — READ THIS BEFORE READING SUBJECTS

You are labeling clusters of work items grouped by a semantic embedding
model. Items come from one or more sources: Slack threads (`slack:CH:ts`),
Jira tickets (`EX-NNNN`), Confluence pages (`page:NNNN`), GitHub PRs
(`owner/repo#N`).

Your job: name the **SHARED WORK** across each cluster's members. Not the
topic. Not the keywords. Not the source. The work.

## What "work" means here

- A workstream ("TDS compliance: TRD + UTR generation")
- A coordination event ("DR drill UAT readiness")
- A refactor / migration ("Payout V2 schema redesign")
- A recurring template (only when the cluster IS literally recurring —
  e.g. "Opsgenie Grafana firing-template across N alerts")

## Good labels — produce these

- "TDS compliance: TRD work + UTR generation logic across Jira and Slack"
- "DR drill UAT coordination + readiness page"
- "DB ops: vacuum tuning + range-partition indexes"
- "Payout V2 refactor: schema redesign + credentials + PR"
- "Recurring Opsgenie alert: IMPS/Accounting svc API errors"

## Bad labels — do NOT produce

- "Redis"                                   (too narrow, ignores cross-source role)
- "Things from service-c-Transactions"      (source/team, not work)
- "Bugs"                                    (too generic)
- "Slack threads about deployments"         (source + topic, no work frame)

## Rules

1. Lead with the work (verb-ish noun phrase), not the topic.
2. If the cluster spans ≥2 sources, name the bridge between them in the label.
3. If members share a literal template (same emoji header, same boilerplate
   alert text), call it out: "Recurring <bot> alert: <what's firing>".
4. Label ≤ 15 words. what_work ≤ 25 words.
5. Confidence: 0.9+ only if every member clearly fits the work frame.
   Drop below 0.5 if ≥1 member feels forced into the cluster.

## Output shape

For every cluster in the dump, emit one entry:

```json
{
  "cluster_id": <int>,
  "label": "<≤15 word label>",
  "what_work": "<one sentence ≤25 words>",
  "confidence": <0.0 - 1.0>
}
```

Wrap all entries in a single JSON array. Save to:

    $HOME/context/work-context/state/verdicts.cluster_labels.json

Then run `apply`:

```bash
.venv/bin/python derive/label_clusters.py apply
```
"""


def _load_embeddings(conn):
    import numpy as np
    # ORDER BY subject pins SQLite row order so HDBSCAN renumbers cluster_ids
    # only when membership actually changes (HDBSCAN is order-sensitive on id
    # assignment even though the underlying clustering is deterministic).
    rows = conn.execute("SELECT subject, vector, source FROM embedding ORDER BY subject").fetchall()
    if not rows:
        return [], np.zeros((0, 0), dtype=np.float32), []
    subs = [r[0] for r in rows]
    # Bulk-decode every vector blob in one pass: concat the raw little-endian
    # float32 bytes, reinterpret as one (N, dim) array. ~45x faster than per-row
    # struct.unpack into Python lists (2.7s -> 0.06s at 35k vecs), less memory.
    # bytearray() makes the buffer writable+contiguous (HDBSCAN needs that).
    vecs = np.frombuffer(
        bytearray(b"".join(r[1] for r in rows)), dtype=np.float32
    ).reshape(len(rows), -1)
    srcs = [r[2] for r in rows]
    return subs, vecs, srcs


def _cluster(vecs, min_cluster_size: int):
    from sklearn.cluster import HDBSCAN
    hdb = HDBSCAN(
        min_cluster_size=min_cluster_size,
        min_samples=1,
        metric="cosine",
    )
    return hdb.fit_predict(vecs)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _ensure_tables(conn):
    """No-op shim — canonical schema (incl. confidence/root_cause backfills)
    lives in `ingest/common._ensure_schema`, which `get_db()` calls before
    handing the connection out. Kept as a named hook so existing call sites
    (and future code-reviewers) can still grep for "ensure" without surprise.
    """
    # Intentionally empty: ingest.common.get_db has already ensured the
    # canonical topic_brief / topic_brief_member shape, including
    # ALTER-backfilled columns on pre-existing databases.
    return


def _sources_to_json(breakdown_str: str) -> str:
    """Convert 'slack=4  jira=1' → JSON dict for source_breakdown_json."""
    out = {}
    for tok in breakdown_str.split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            try:
                out[k] = int(v)
            except ValueError:
                pass
    return json.dumps(out, sort_keys=True)


# ── dump ────────────────────────────────────────────────────────────────────


def cmd_dump(args):
    conn = get_db()
    subs, vecs, srcs = _load_embeddings(conn)
    if not subs:
        print("no embeddings — run derive/embed_subjects.py first")
        return
    labels = _cluster(vecs, args.min_cluster_size)
    groups = defaultdict(list)
    for i, lbl in enumerate(labels):
        groups[int(lbl)].append(i)
    groups.pop(-1, None)
    if not groups:
        print("no clusters formed at min_cluster_size=", args.min_cluster_size)
        return

    out_clusters = []
    total_clusters_all = len(groups)
    skipped_below_min = 0
    for cid in sorted(groups, key=lambda c: -len(groups[c])):
        idxs = groups[cid]
        if len(idxs) < args.min_members:
            skipped_below_min += 1
            continue
        if args.max_members is not None and len(idxs) > args.max_members:
            skipped_below_min += 1
            continue
        src_count = defaultdict(int)
        for i in idxs:
            src_count[srcs[i]] += 1
        src_str = "  ".join(f"{k}={v}" for k, v in sorted(src_count.items()))
        members = []
        for i in idxs:
            _, content = get_content(conn, subs[i])
            # Cap each member's content to keep the dump skimmable;
            # Claude only needs enough to identify the work, not the full body.
            cap = args.member_chars
            cont = " ".join(content.split())
            if len(cont) > cap:
                cont = cont[:cap].rstrip() + "…"
            members.append({"subject": subs[i], "source": srcs[i], "content": cont})
        out_clusters.append({
            "cluster_id": cid,
            "n_members": len(idxs),
            "sources_breakdown": src_str,
            "members": members,
        })

    payload = {
        "computed_at": _now_iso(),
        "min_cluster_size": args.min_cluster_size,
        "n_subjects": len(subs),
        "n_clusters": len(out_clusters),
        "clusters": out_clusters,
    }
    PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
    PENDING_PATH.write_text(json.dumps(payload, indent=2))
    if RULES_SOURCE_PATH.exists():
        RULES_PATH.write_text(RULES_SOURCE_PATH.read_text())
    else:
        # Bootstrap fallback if someone deleted the maintained file.
        RULES_PATH.write_text(_RULES_MD)

    print(f"✓ dump complete")
    print(f"  pending file: {PENDING_PATH}")
    print(f"  rules file:   {RULES_PATH}")
    print(f"  clusters:     {len(out_clusters)} (dumped)  /  {total_clusters_all} (total at min_cluster_size={args.min_cluster_size})")
    if skipped_below_min:
        print(f"  skipped:      {skipped_below_min} clusters below --min-members={args.min_members}")
    print(f"  total members: {sum(c['n_members'] for c in out_clusters)}")
    print()
    print(f"Next: in chat, read the rules FIRST, then the dump, then write verdicts to:")
    print(f"  {VERDICTS_PATH}")
    print(f"Then run: .venv/bin/python derive/label_clusters.py apply")


# ── apply ───────────────────────────────────────────────────────────────────


def cmd_apply(args):
    if not VERDICTS_PATH.exists():
        print(f"missing {VERDICTS_PATH} — write verdicts first")
        return
    if not PENDING_PATH.exists():
        print(f"missing {PENDING_PATH} — run dump first to know cluster member sets")
        return
    verdicts = json.loads(VERDICTS_PATH.read_text())
    if not isinstance(verdicts, list):
        print("verdicts file must be a JSON array")
        return
    pending = json.loads(PENDING_PATH.read_text())
    pending_by_id = {c["cluster_id"]: c for c in pending["clusters"]}

    conn = get_db()
    _ensure_tables(conn)

    now = _now_iso()
    applied = 0
    skipped = 0
    errors = []
    for v in verdicts:
        cid = v.get("cluster_id")
        if cid is None or cid not in pending_by_id:
            errors.append(f"cluster_id {cid!r}: not in pending dump")
            skipped += 1
            continue
        label = (v.get("label") or "").strip()
        if not label:
            errors.append(f"cluster_id {cid}: empty label")
            skipped += 1
            continue
        what_work = (v.get("what_work") or "").strip()
        confidence = float(v.get("confidence") or 0.0)
        members = pending_by_id[cid]
        conn.execute(
            "INSERT OR REPLACE INTO topic_brief "
            "(cluster_id, label, summary, confidence, member_count, "
            "source_breakdown_json, computed_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (cid, label, what_work, confidence,
             members["n_members"],
             _sources_to_json(members["sources_breakdown"]),
             now),
        )
        conn.execute("DELETE FROM topic_brief_member WHERE cluster_id = ?", (cid,))
        for m in members["members"]:
            # source is NOT NULL in the existing schema (migration 006).
            # Use INSERT (no OR IGNORE) so a missing source surfaces as an error
            # rather than silently dropping the row.
            conn.execute(
                "INSERT INTO topic_brief_member (cluster_id, subject, source) VALUES (?,?,?)",
                (cid, m["subject"], m.get("source") or "unknown"),
            )
        applied += 1
    conn.commit()
    # Auto-stub `Recurring …` clusters so chat doesn't have to enrich them.
    # See derive/auto_recurring.py — fixes status/decisions/blockers/first_ts
    # in place. Idempotent; safe to invoke on every apply.
    from derive.auto_recurring import stub as _auto_stub
    auto = _auto_stub(conn, dry_run=False)
    print(json.dumps({
        "applied": applied,
        "skipped": skipped,
        "errors": errors,
        "topic_brief_rows": conn.execute("SELECT COUNT(*) FROM topic_brief").fetchone()[0],
        "auto_recurring_stubbed": auto.get("stubbed", 0),
    }, indent=2))


# ── status ──────────────────────────────────────────────────────────────────


def cmd_status(args):
    conn = get_db()
    info = {
        "pending_dump":  PENDING_PATH.exists(),
        "rules_md":      RULES_PATH.exists(),
        "verdicts":      VERDICTS_PATH.exists(),
    }
    if PENDING_PATH.exists():
        info["pending_clusters"] = len(json.loads(PENDING_PATH.read_text())["clusters"])
    if VERDICTS_PATH.exists():
        info["verdicts_count"] = len(json.loads(VERDICTS_PATH.read_text()))
    try:
        info["topic_brief_rows"] = conn.execute("SELECT COUNT(*) FROM topic_brief").fetchone()[0]
    except Exception:
        info["topic_brief_rows"] = 0
    print(json.dumps(info, indent=2))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("dump", help="Re-cluster + dump member content for chat labeling")
    d.add_argument("--min-cluster-size", type=int, default=3)
    d.add_argument("--member-chars", type=int, default=400,
                   help="Cap chars per member's content snippet")
    d.add_argument("--min-members", type=int, default=0,
                   help="Only dump clusters with at least N members (default 0 = all). "
                        "Use to tier-label large clusters first at scale.")
    d.add_argument("--max-members", type=int, default=None,
                   help="Only dump clusters with at most N members (default unbounded). "
                        "Pair with --min-members to dump a tier slice.")
    d.set_defaults(fn=cmd_dump)

    a = sub.add_parser("apply", help="Read verdicts, persist to topic_brief")
    a.set_defaults(fn=cmd_apply)

    st = sub.add_parser("status", help="Show pending/verdicts/persisted state")
    st.set_defaults(fn=cmd_status)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
