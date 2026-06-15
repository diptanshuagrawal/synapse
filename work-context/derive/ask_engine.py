"""
ask_engine.py — query primitives used by the `/ask` skill.

Stateless functions over `topic_brief`, `topic_brief_member`, `embedding`, and
`events`. Each function returns plain dicts/lists so the chat router can
compose them without re-doing SQL. Scripts NEVER call an LLM API; chat does
the synthesis.

Primitives
----------
    find_clusters_by_query(text, k_subjects=20)
        Embed text via OpenAI, return clusters ranked by member-similarity.

    clusters_for_person(person, since=None, until=None)
        All clusters a person touched (events.actor match), with their reply
        counts in each cluster + cluster status/label/timestamps.

    clusters_active_in_window(since, until, participant=None)
        Clusters whose `last_activity_ts` falls in window. Optional
        participant filter narrows to those they touched.

    ticket_gaps(since=None, until=None)
        Slack subjects in clusters that have decisions/blockers in
        topic_brief but no `event_refs` pointing to a EX-* AND no nearby
        Jira subject (cosine sim ≥ 0.65) in the embedding table.

    root_causes_in_window(since, until)
        Clusters with non-null root_cause whose last_activity_ts is in
        window, grouped by root_cause text.

CLI (debugging)
---------------
    .venv/bin/python derive/ask_engine.py search --query "instant-pay migration to service-a"
    .venv/bin/python derive/ask_engine.py person --name frank --since 2026-03-01 --until 2026-03-31
    .venv/bin/python derive/ask_engine.py window --since 2026-05-18 --until 2026-05-19
    .venv/bin/python derive/ask_engine.py gaps
    .venv/bin/python derive/ask_engine.py rootcauses --since 2026-04-19 --until 2026-05-19
"""

from __future__ import annotations

import argparse
import json
import re
import struct
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from ingest.common import get_db, _load_people  # noqa: E402
from derive.sources_config import home_team, jira_project_keys  # noqa: E402

_JIRA_KEY = (jira_project_keys() or ["EX"])[0]


# ── Helpers ────────────────────────────────────────────────────────────────


def _unpack(b: bytes) -> list[float]:
    n = len(b) // 4
    return list(struct.unpack(f"<{n}f", b))


def _build_actor_map() -> dict[str, list[str]]:
    """Canonical name → list of actor-IDs across all sources.

    Recognises `git_names` (list) for unlinked GitHub accounts whose commit
    author shows as a raw display name rather than `example-*` login. Accepts
    legacy singular `git_name` for backward compat.
    """
    m: dict[str, list[str]] = defaultdict(list)
    for p in _load_people():
        canon = p.get("canonical")
        if not canon:
            continue
        for key in ("slack_id", "github", "jira_id", "email"):
            v = p.get(key)
            if v:
                m[canon].append(v)
        git_names = p.get("git_names") or []
        legacy = p.get("git_name")
        if legacy:
            git_names = list(git_names) + [legacy]
        for gn in git_names:
            if gn:
                m[canon].append(gn)
    return m


def _resolve_person(name: str) -> list[str]:
    """Match canonical (case-insensitive substring) → list of actor IDs."""
    m = _build_actor_map()
    name_low = name.lower()
    matches: list[str] = []
    for canon, ids in m.items():
        if name_low in canon.lower():
            matches.extend(ids)
    return matches


def _topic_brief_row(conn, cluster_id: int) -> dict | None:
    row = conn.execute(
        "SELECT cluster_id, label, summary, status, root_cause, "
        "       member_count, first_ts, last_activity_ts, "
        "       decisions_json, blockers_json, participants_json, "
        "       source_breakdown_json, confidence, owner_distribution_json "
        "  FROM topic_brief WHERE cluster_id = ?",
        (cluster_id,),
    ).fetchone()
    if not row:
        return None
    keys = ["cluster_id", "label", "summary", "status", "root_cause",
            "member_count", "first_ts", "last_activity_ts",
            "decisions_json", "blockers_json", "participants_json",
            "source_breakdown_json", "confidence", "owner_distribution_json"]
    out = dict(zip(keys, row))
    for jkey in ("decisions_json", "blockers_json", "participants_json",
                 "source_breakdown_json", "owner_distribution_json"):
        if out.get(jkey):
            try:
                out[jkey] = json.loads(out[jkey])
            except (json.JSONDecodeError, TypeError):
                pass
    # Derive home-team share as a convenience field for downstream filtering.
    dist = out.get("owner_distribution_json") or {}
    if isinstance(dist, dict):
        out["home_team_owned_pct"] = round(dist.get(home_team(), 0.0), 3)
    else:
        out["home_team_owned_pct"] = 0.0
    return out


# ── 1. Semantic search → cluster ranking ────────────────────────────────────


def find_clusters_by_query(query: str, k_subjects: int = 20) -> list[dict]:
    """Embed the query string, find top-K subjects by cosine, group by cluster.

    Returns: [{cluster_id, label, status, hit_count, top_subjects: [...], topic_brief}, ...]
    Sorted by hit_count desc.
    """
    import numpy as np
    from derive.openai_client import embed
    conn = get_db()

    qvec = np.array(embed([query])[0], dtype=np.float32)
    qnorm = np.linalg.norm(qvec) or 1.0
    qvec = qvec / qnorm

    rows = conn.execute("SELECT subject, vector, source FROM embedding").fetchall()
    if not rows:
        return []
    subs = [r[0] for r in rows]
    # Bulk-decode every vector blob in one pass: concat the raw little-endian
    # float32 bytes, reinterpret as one (N, dim) array. ~45x faster than per-row
    # struct.unpack into Python lists (2.7s -> 0.06s at 35k vecs), less memory.
    # bytearray() makes the buffer writable so downstream in-place ops are safe.
    vecs = np.frombuffer(
        bytearray(b"".join(r[1] for r in rows)), dtype=np.float32
    ).reshape(len(rows), -1)
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    vecs = vecs / norms
    sims = vecs @ qvec
    order = np.argsort(-sims)[:k_subjects]

    subject_to_cluster = dict(conn.execute(
        "SELECT subject, cluster_id FROM topic_brief_member"
    ).fetchall())

    cluster_hits: dict[int, list[dict]] = defaultdict(list)
    unaffiliated: list[dict] = []
    for i in order:
        subj = subs[i]
        cid = subject_to_cluster.get(subj)
        entry = {"subject": subj, "source": rows[i][2], "similarity": float(sims[i])}
        if cid is None:
            unaffiliated.append(entry)
        else:
            cluster_hits[cid].append(entry)

    out: list[dict] = []
    for cid, hits in sorted(cluster_hits.items(), key=lambda kv: -len(kv[1])):
        brief = _topic_brief_row(conn, cid)
        out.append({
            "cluster_id": cid,
            "label": brief["label"] if brief else None,
            "status": brief["status"] if brief else None,
            "hit_count": len(hits),
            "top_subjects": hits,
            "topic_brief": brief,
        })
    if unaffiliated:
        out.append({
            "cluster_id": None,
            "label": "<unaffiliated>",
            "status": None,
            "hit_count": len(unaffiliated),
            "top_subjects": unaffiliated,
            "topic_brief": None,
        })
    return out


# ── 2. Person + time range → clusters ───────────────────────────────────────


def clusters_for_person(person: str, since: str | None = None, until: str | None = None) -> list[dict]:
    """Clusters touched by a person in [since, until). Returns one entry per
    cluster: {cluster_id, label, status, reply_count, first_touch_ts,
    last_touch_ts, topic_brief}."""
    conn = get_db()
    actor_ids = _resolve_person(person)
    if not actor_ids:
        return []
    where_actor = " OR ".join(["e.actor = ?"] * len(actor_ids))
    params: list[str] = list(actor_ids)
    where_ts = ""
    if since:
        where_ts += " AND e.ts >= ?"
        params.append(since)
    if until:
        where_ts += " AND e.ts < ?"
        params.append(until)

    sql = f"""
        SELECT m.cluster_id, e.ts
          FROM events e
          JOIN topic_brief_member m ON m.subject = e.subject
         WHERE ({where_actor}) {where_ts}
    """
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        return []
    bucket: dict[int, dict] = {}
    for cid, ts in rows:
        b = bucket.setdefault(cid, {"reply_count": 0, "first_touch_ts": ts, "last_touch_ts": ts})
        b["reply_count"] += 1
        if ts < b["first_touch_ts"]:
            b["first_touch_ts"] = ts
        if ts > b["last_touch_ts"]:
            b["last_touch_ts"] = ts

    out: list[dict] = []
    for cid in sorted(bucket, key=lambda c: -bucket[c]["reply_count"]):
        brief = _topic_brief_row(conn, cid)
        out.append({
            "cluster_id": cid,
            "label": brief["label"] if brief else None,
            "status": brief["status"] if brief else None,
            **bucket[cid],
            "topic_brief": brief,
        })
    return out


# ── 3. Time window → active clusters ────────────────────────────────────────


def _compute_window_state(first_ts: str | None, last_ts: str | None,
                          since: str, until: str) -> str:
    """Derive cluster lifetime ↔ window relationship.

    `status` column on topic_brief reflects CURRENT state (ACTIVE/STALE/...) —
    not the cluster's state during a historical window. For retros looking
    back >30d, the current STALE label is uninformative ("was it stale THEN?").

    `window_state` derives from lifetime timestamps:
      `fully_in`     — entire lifetime inside window
      `started_in`   — born in window, still alive after
      `ended_in`     — born before window, ended in window
      `spans`        — alive before AND after window (cluster carried through)
      `pre_window`   — fully ended before since (caller should filter out)
      `post_window`  — born after until (caller should filter out)

    Reader semantics: `fully_in` / `started_in` / `ended_in` / `spans` all mean
    "the cluster had real lifetime overlap with the window" — narrative can
    render any of these as "active workstream during <window>".
    """
    if not first_ts or not last_ts:
        return "unknown"
    if last_ts < since:
        return "pre_window"
    if first_ts >= until:
        return "post_window"
    in_first = since <= first_ts < until
    in_last = since <= last_ts < until
    if in_first and in_last:
        return "fully_in"
    if in_first:
        return "started_in"
    if in_last:
        return "ended_in"
    return "spans"  # first_ts < since AND last_ts >= until


def clusters_active_in_window(since: str, until: str, participant: str | None = None,
                              include_recurring: bool = False) -> list[dict]:
    """Clusters whose lifetime overlaps [since, until).

    Filter is `first_ts < until AND last_activity_ts >= since` (lifetime
    overlap), NOT `last_activity_ts in [since, until)` (which the old
    implementation used and missed long-running clusters carried through
    the window).

    Each result gets an extra `window_state` field — render narrative
    against window_state, NOT against the current `status` column. The
    current `status` is "where the cluster is TODAY", not "where it was
    during the asked window". For recent windows the two are similar;
    for historical windows (>30d ago) they diverge sharply.

    `include_recurring=False` filters RECURRING clusters from results
    (templates / channel-join noise). Set True for retros that want to
    quantify noise.
    """
    conn = get_db()
    where = ["first_ts < ?", "last_activity_ts >= ?"]
    params: list = [until, since]
    if not include_recurring:
        where.append("status != 'RECURRING'")
    rows = conn.execute(
        f"SELECT cluster_id FROM topic_brief WHERE {' AND '.join(where)} "
        f" ORDER BY last_activity_ts DESC",
        params,
    ).fetchall()
    out: list[dict] = []
    actor_map = _build_actor_map()
    canonical_filter = None
    if participant:
        for canon in actor_map:
            if participant.lower() in canon.lower():
                canonical_filter = canon
                break
    for (cid,) in rows:
        brief = _topic_brief_row(conn, cid)
        if not brief:
            continue
        if canonical_filter:
            parts = brief.get("participants_json") or []
            names = {p.get("person") for p in parts if isinstance(p, dict)}
            if canonical_filter not in names:
                continue
        brief["window_state"] = _compute_window_state(
            brief.get("first_ts"), brief.get("last_activity_ts"), since, until
        )
        out.append(brief)
    return out


# ── 4. Ticket gaps ──────────────────────────────────────────────────────────


_CBST_RX = re.compile(rf"\b{_JIRA_KEY}-\d+\b", re.IGNORECASE)


def ticket_gaps(since: str | None = None, until: str | None = None,
                sim_threshold: float = 0.65) -> list[dict]:
    """Slack subjects in clusters with decisions/blockers, NOT linked to a
    EX-* via event_refs, AND no jira subject within cosine sim ≥ threshold.

    Returns: [{subject, cluster_id, cluster_label, evidence_text,
    nearest_jira: {subject, similarity} | null}, ...]
    """
    import numpy as np
    conn = get_db()

    # 1. Slack members of clusters that have ≥1 decision or ≥1 blocker.
    candidate_rows = conn.execute("""
        SELECT m.subject, m.cluster_id, tb.label, tb.decisions_json, tb.blockers_json
          FROM topic_brief_member m
          JOIN topic_brief tb ON tb.cluster_id = m.cluster_id
         WHERE m.source = 'slack'
           AND (
             (tb.decisions_json IS NOT NULL AND tb.decisions_json != '[]')
             OR (tb.blockers_json IS NOT NULL AND tb.blockers_json != '[]')
           )
    """).fetchall()
    if not candidate_rows:
        return []

    # 2. Already-linked slack subjects: those with an event_refs row whose target
    #    matches EX-*.
    try:
        linked = {
            r[0] for r in conn.execute(f"""
                SELECT DISTINCT er.source_event_id
                  FROM event_refs er
                 WHERE er.target_subject LIKE '{_JIRA_KEY}-%'
            """).fetchall()
        }
    except Exception:
        # event_refs may have different column names; fall back to "none linked"
        linked = set()

    # 3. Embeddings for similarity check.
    embed_rows = conn.execute(
        "SELECT subject, vector, source FROM embedding"
    ).fetchall()
    embed_map: dict[str, np.ndarray] = {}
    jira_subjects: list[tuple[str, np.ndarray]] = []
    for subj, vec_blob, src in embed_rows:
        vec = np.array(_unpack(vec_blob), dtype=np.float32)
        n = np.linalg.norm(vec) or 1.0
        vec = vec / n
        embed_map[subj] = vec
        if src == "jira":
            jira_subjects.append((subj, vec))
    jira_matrix = np.stack([v for _, v in jira_subjects]) if jira_subjects else None
    jira_ids = [s for s, _ in jira_subjects]

    out: list[dict] = []
    for subj, cid, label, dec_json, blk_json in candidate_rows:
        if subj in linked:
            continue
        # ts-range filter via events (parent's first ts).
        if since or until:
            ts_row = conn.execute(
                "SELECT MIN(ts) FROM events WHERE subject = ?", (subj,)
            ).fetchone()
            min_ts = ts_row[0] if ts_row else None
            if since and (min_ts is None or min_ts < since):
                continue
            if until and (min_ts is None or min_ts >= until):
                continue
        # Pull evidence text from decision/blocker bound to this subject if any.
        evidence_text = None
        for jkey in (dec_json, blk_json):
            if not jkey:
                continue
            try:
                items = json.loads(jkey)
            except Exception:
                continue
            for it in items or []:
                if it.get("evidence_subject") == subj:
                    evidence_text = it.get("text")
                    break
            if evidence_text:
                break
        # Nearest jira subject.
        nearest = None
        if jira_matrix is not None and subj in embed_map:
            qv = embed_map[subj]
            sims = jira_matrix @ qv
            best_idx = int(np.argmax(sims))
            best_sim = float(sims[best_idx])
            if best_sim < sim_threshold:
                # Genuinely a gap.
                nearest = {"subject": jira_ids[best_idx], "similarity": best_sim}
            else:
                # Existing jira covers this — skip.
                continue
        out.append({
            "subject": subj,
            "cluster_id": cid,
            "cluster_label": label,
            "evidence_text": evidence_text,
            "nearest_jira": nearest,
        })
    return out


# ── 5. Root causes in window ────────────────────────────────────────────────


def root_causes_in_window(since: str, until: str) -> list[dict]:
    """Clusters with non-null root_cause whose lifetime overlaps window.

    Uses lifetime-overlap filter (`first_ts < until AND last_activity_ts >=
    since`) so historical retros catch incident clusters that started
    before window and ended in window. Each result gets `window_state`
    field — render narrative against that, NOT the current `status`.
    """
    conn = get_db()
    rows = conn.execute("""
        SELECT cluster_id FROM topic_brief
         WHERE root_cause IS NOT NULL
           AND first_ts < ?
           AND last_activity_ts >= ?
         ORDER BY last_activity_ts DESC
    """, (until, since)).fetchall()
    out = []
    for (cid,) in rows:
        brief = _topic_brief_row(conn, cid)
        if not brief:
            continue
        brief["window_state"] = _compute_window_state(
            brief.get("first_ts"), brief.get("last_activity_ts"), since, until
        )
        out.append(brief)
    return out


# ── 6. Project-level rollup (cluster_project_map consumer) ──────────────────


def clusters_by_project(slug: str, since: str | None = None, until: str | None = None,
                        min_confidence: float = 0.60) -> list[dict]:
    """All clusters linked to `slug` in `cluster_project_map`, optionally filtered
    by window. Each cluster decorated with `window_state` if since+until given,
    and `link_confidence` + `link_source` from the mapping table.

    Returns clusters sorted by (link_confidence DESC, last_activity_ts DESC).
    """
    conn = get_db()
    rows = conn.execute("""
        SELECT cpm.cluster_id, cpm.confidence, cpm.source, cpm.evidence_json
          FROM cluster_project_map cpm
         WHERE cpm.project_slug = ?
           AND cpm.confidence >= ?
    """, (slug, min_confidence)).fetchall()
    if not rows:
        return []
    out: list[dict] = []
    for cid, conf, src, evidence in rows:
        brief = _topic_brief_row(conn, cid)
        if not brief:
            continue
        if since and until:
            ft = brief.get("first_ts")
            lt = brief.get("last_activity_ts")
            # lifetime-overlap window filter
            if ft and ft >= until:
                continue
            if lt and lt < since:
                continue
            brief["window_state"] = _compute_window_state(ft, lt, since, until)
        brief["link_confidence"] = conf
        brief["link_source"] = src
        try:
            brief["link_evidence"] = json.loads(evidence) if evidence else None
        except (json.JSONDecodeError, TypeError):
            brief["link_evidence"] = None
        out.append(brief)
    out.sort(key=lambda b: (-(b.get("link_confidence") or 0),
                            -(0 if not b.get("last_activity_ts") else 1),
                            b.get("last_activity_ts") or ""), reverse=False)
    return out


def projects_active_in_window(since: str, until: str, min_confidence: float = 0.60) -> list[dict]:
    """Project slugs ranked by aggregate cluster activity in window.

    For each slug: how many linked clusters have lifetime-overlap with window,
    sum of those clusters' member_count, and the top 3 cluster labels for
    one-line narrative rendering.

    Returns: [{project_slug, cluster_count, member_count_total, top_cluster_labels,
               linked_cluster_ids, status_distribution}, ...]
    Sorted by cluster_count desc, then member_count_total desc.
    """
    conn = get_db()
    rows = conn.execute("""
        SELECT cpm.project_slug, cpm.cluster_id, cpm.confidence,
               tb.label, tb.status, tb.member_count, tb.first_ts, tb.last_activity_ts
          FROM cluster_project_map cpm
          JOIN topic_brief tb ON tb.cluster_id = cpm.cluster_id
         WHERE cpm.confidence >= ?
           AND tb.first_ts < ?
           AND tb.last_activity_ts >= ?
    """, (min_confidence, until, since)).fetchall()

    agg: dict[str, dict] = {}
    for slug, cid, _conf, label, status, mc, _ft, _lt in rows:
        a = agg.setdefault(slug, {
            "project_slug": slug,
            "cluster_count": 0,
            "member_count_total": 0,
            "top_cluster_labels": [],
            "linked_cluster_ids": [],
            "status_distribution": {},
        })
        a["cluster_count"] += 1
        a["member_count_total"] += mc or 0
        a["linked_cluster_ids"].append(cid)
        a["top_cluster_labels"].append({"cluster_id": cid, "label": label or "", "members": mc or 0})
        st = status or "UNKNOWN"
        a["status_distribution"][st] = a["status_distribution"].get(st, 0) + 1

    out = list(agg.values())
    for a in out:
        # keep top 3 cluster labels by member count for narrative rendering
        a["top_cluster_labels"] = sorted(
            a["top_cluster_labels"], key=lambda x: -x["members"]
        )[:3]
    out.sort(key=lambda a: (-a["cluster_count"], -a["member_count_total"]))
    return out


# ── CLI ─────────────────────────────────────────────────────────────────────


# ── 7. Raw-event metrics (count / timeline over events.db) ──────────────────
# Deterministic aggregation over `events` — NOT clustered. This is the route
# for "how many times did X occur in <range>" / alert-frequency questions,
# especially for automation channels that are EXCLUDED from clustering
# (see derive/cluster_noise_filter.py) and therefore absent from topic_brief.

_SLACK_CH_YAML = _PKG_ROOT / "config" / "slack_channels.yaml"


def _channel_name_map() -> dict[str, str]:
    """channel_id -> name, from slack_channels.yaml (best-effort)."""
    try:
        import yaml
        y = yaml.safe_load(_SLACK_CH_YAML.read_text()) or {}
        return {c.get("id"): c.get("name") for c in y.get("channels", []) if c.get("id")}
    except Exception:
        return {}


def event_metrics(terms: list[str], since: str, until: str,
                  channels: list[str] | None = None, source: str | None = None,
                  match_any: bool = False, sample: int = 10) -> dict:
    """Count + timeline of events whose title/body match `terms` in [since, until].

    terms       substrings matched (case-insensitive) against title||body.
                AND-joined by default; OR-joined if match_any.
    channels    optional list of channel names OR ids to restrict to.
    source      optional events.source filter (slack/jira/github/confluence).
    Excludes deleted messages (deleted_ts IS NULL).

    Returns: total, distinct_days, per_channel[], per_day[], first_ts, last_ts,
    sample_citations[]. Deterministic — no embedding, no LLM.
    """
    conn = get_db()
    name_map = _channel_name_map()
    id_by_name = {v: k for k, v in name_map.items()}

    where = ["ts >= ?", "ts <= ?", "deleted_ts IS NULL"]
    params: list = [since, until]

    text_expr = "lower(coalesce(title,'') || ' ' || coalesce(body,''))"
    term_clauses = [f"{text_expr} LIKE ?" for _ in terms]
    if term_clauses:
        joiner = " OR " if match_any else " AND "
        where.append("(" + joiner.join(term_clauses) + ")")
        params.extend(f"%{t.lower()}%" for t in terms)

    if channels:
        ch_ids = [id_by_name.get(c, c) for c in channels]  # name→id, else treat as id
        where.append("channel_id IN (" + ",".join("?" * len(ch_ids)) + ")")
        params.extend(ch_ids)
    if source:
        where.append("source = ?")
        params.append(source)

    wsql = " AND ".join(where)
    total = conn.execute(f"SELECT COUNT(*) FROM events WHERE {wsql}", params).fetchone()[0]
    span = conn.execute(
        f"SELECT MIN(ts), MAX(ts), COUNT(DISTINCT substr(ts,1,10)) FROM events WHERE {wsql}", params
    ).fetchone()
    per_channel = [
        {"channel_id": r[0], "channel": name_map.get(r[0], r[0]), "count": r[1]}
        for r in conn.execute(
            f"SELECT channel_id, COUNT(*) FROM events WHERE {wsql} GROUP BY channel_id ORDER BY 2 DESC", params
        ).fetchall()
    ]
    per_day = [
        {"day": r[0], "count": r[1]}
        for r in conn.execute(
            f"SELECT substr(ts,1,10) AS d, COUNT(*) FROM events WHERE {wsql} GROUP BY d ORDER BY d", params
        ).fetchall()
    ]
    cites = [
        {"ts": r[0], "channel": name_map.get(r[1], r[1]), "subject": r[2],
         "snippet": " ".join((r[3] or r[4] or "").split())[:200]}
        for r in conn.execute(
            f"SELECT ts, channel_id, subject, body, title FROM events WHERE {wsql} ORDER BY ts DESC LIMIT ?",
            params + [sample],
        ).fetchall()
    ]
    return {
        "terms": terms, "match": "any" if match_any else "all",
        "since": since, "until": until,
        "channels_filter": channels or None, "source_filter": source,
        "total": total,
        "distinct_days": span[2] if span else 0,
        "first_ts": span[0] if span else None,
        "last_ts": span[1] if span else None,
        "per_channel": per_channel,
        "per_day": per_day,
        "sample_citations": cites,
    }


def cmd_events(args):
    out = event_metrics(
        args.terms, args.since, args.until,
        channels=args.channel or None, source=args.source,
        match_any=args.any, sample=args.sample,
    )
    print(json.dumps(out, indent=2, default=str))


def cmd_search(args):
    out = find_clusters_by_query(args.query, k_subjects=args.k)
    print(json.dumps(out, indent=2, default=str))


def cmd_person(args):
    out = clusters_for_person(args.name, since=args.since, until=args.until)
    print(json.dumps(out, indent=2, default=str))


def cmd_window(args):
    out = clusters_active_in_window(args.since, args.until, participant=args.participant)
    print(json.dumps(out, indent=2, default=str))


def cmd_gaps(args):
    out = ticket_gaps(since=args.since, until=args.until, sim_threshold=args.threshold)
    print(json.dumps(out, indent=2, default=str))


def cmd_rootcauses(args):
    out = root_causes_in_window(args.since, args.until)
    print(json.dumps(out, indent=2, default=str))


def cmd_project(args):
    out = clusters_by_project(args.slug, since=args.since, until=args.until,
                              min_confidence=args.min_confidence)
    print(json.dumps(out, indent=2, default=str))


def cmd_projects_window(args):
    out = projects_active_in_window(args.since, args.until,
                                    min_confidence=args.min_confidence)
    print(json.dumps(out, indent=2, default=str))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search")
    s.add_argument("--query", required=True)
    s.add_argument("--k", type=int, default=20)
    s.set_defaults(fn=cmd_search)
    p = sub.add_parser("person")
    p.add_argument("--name", required=True)
    p.add_argument("--since", default=None)
    p.add_argument("--until", default=None)
    p.set_defaults(fn=cmd_person)
    w = sub.add_parser("window")
    w.add_argument("--since", required=True)
    w.add_argument("--until", required=True)
    w.add_argument("--participant", default=None)
    w.set_defaults(fn=cmd_window)
    g = sub.add_parser("gaps")
    g.add_argument("--since", default=None)
    g.add_argument("--until", default=None)
    g.add_argument("--threshold", type=float, default=0.65)
    g.set_defaults(fn=cmd_gaps)
    r = sub.add_parser("rootcauses")
    r.add_argument("--since", required=True)
    r.add_argument("--until", required=True)
    r.set_defaults(fn=cmd_rootcauses)
    pj = sub.add_parser("project", help="Clusters linked to a project slug; optional window filter.")
    pj.add_argument("--slug", required=True)
    pj.add_argument("--since", default=None)
    pj.add_argument("--until", default=None)
    pj.add_argument("--min-confidence", type=float, default=0.60)
    pj.set_defaults(fn=cmd_project)
    pw = sub.add_parser("projects-window", help="Projects ranked by cluster activity in window.")
    pw.add_argument("--since", required=True)
    pw.add_argument("--until", required=True)
    pw.add_argument("--min-confidence", type=float, default=0.60)
    pw.set_defaults(fn=cmd_projects_window)
    ev = sub.add_parser("events", help="Count/timeline of raw events matching terms in a window "
                                       "(alert-frequency, 'how many times X occurred'). Deterministic, not clustered.")
    ev.add_argument("--terms", nargs="+", required=True, help="substrings matched in title||body (AND by default)")
    ev.add_argument("--since", required=True)
    ev.add_argument("--until", required=True)
    ev.add_argument("--channel", action="append", default=None, help="channel name or id; repeatable")
    ev.add_argument("--source", default=None, help="slack|jira|github|confluence")
    ev.add_argument("--any", action="store_true", help="OR-match terms instead of AND")
    ev.add_argument("--sample", type=int, default=10)
    ev.set_defaults(fn=cmd_events)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
