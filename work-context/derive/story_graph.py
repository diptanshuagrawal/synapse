"""
story_graph.py — walk the cross-source link graph in events.db.

Every event participates in a property graph via `event_refs`. A "story" =
a connected component reachable from any starting subject through ref edges:
PR → Jira ticket → Confluence page → Slack thread.

This module owns the SQL JOIN logic for navigating (events, event_refs)
including the page-prefix mismatch:

    events.subject               event_refs.ref_value (matching ref_type)
    -------------                -------------------------------------
    EX-2629                      EX-2629                    (ticket)
    example-org/service-a#629    example-org/service-a#629  (pull_request)
    page:EXAMPLE_PAGE_ID         EXAMPLE_PAGE_ID            (page)   ← mismatch handled here
    slack:C0EXAMPLE…:1778…       slack:C0EXAMPLE…:1778…      (slack_thread)

Consumers: /narrative (ops section evidence), /retro, future /story skill.

Late-add safety: walks operate at query time; no precomputation of canonical
mappings. Adding a person to people.yaml later doesn't invalidate any walk.

Public API:
    StoryGraph(conn)
        .outgoing(subject)            → list[Link]  — refs FROM events of this subject
        .incoming(subject)            → list[Link]  — events that ref this subject
        .neighbours(subject)          → list[Link]  — both directions
        .walk(subject, depth=2)       → list[Link]  — BFS to `depth` hops
        .related_subjects(subject, depth=2) → list[(subject, source, distance)]
"""

from __future__ import annotations

import sqlite3
from collections import deque
from dataclasses import dataclass
from typing import Iterator, Optional

# ── Subject-form translation per ref_type ────────────────────────────────────
#
# When joining event_refs.ref_value → events.subject, ref_value may need a
# prefix to match subject form. Currently only `page` differs.

_REF_TO_SUBJECT_EXPR = {
    "ticket":       "ref_value",
    "pull_request": "ref_value",
    "slack_thread": "ref_value",
    "page":         "'page:' || ref_value",
    # person/project never resolve to a subject (no events.subject = canonical).
}

_REF_FROM_SUBJECT_EXPR = {
    # When given a subject string, derive the ref_value to look up.
    # Inverse of above.
    "page":         lambda s: s.removeprefix("page:") if s.startswith("page:") else s,
    "ticket":       lambda s: s,
    "pull_request": lambda s: s,
    "slack_thread": lambda s: s,
}


@dataclass
class Link:
    """One graph edge between two subjects (or a ref-only edge if no inverse subject)."""

    from_subject: str
    from_source: str          # source of the from-event
    via_ref_type: str         # 'ticket' | 'pull_request' | 'page' | 'slack_thread' | 'person' | 'project'
    via_ref_value: str        # canonical ref_value
    to_subject: Optional[str]  # NULL when ref points to something with no events row (e.g. orphan ticket reference)
    to_source: Optional[str]
    to_ts: Optional[str]
    direction: str            # 'out' or 'in'


class StoryGraph:
    """SQL-backed graph walker over events + event_refs."""

    # ref_types that point to another subject (others are leaf attributes).
    SUBJECT_REF_TYPES: tuple[str, ...] = ("ticket", "pull_request", "page", "slack_thread")

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ── Outgoing: edges from events of subject to other subjects ────────────

    def outgoing(self, subject: str) -> list[Link]:
        """Every ref FROM events with this subject, joined to its destination
        event (if any). Returns Link rows; to_subject is NULL for orphans
        (e.g., ticket reference to a Jira issue never ingested)."""
        results: list[Link] = []

        # Source side: events with this subject. There may be many rows (one
        # per event_type), but each contributes its refs.
        sql_template = """
            SELECT
                src.subject  AS from_subject,
                src.source   AS from_source,
                r.ref_type   AS via_ref_type,
                r.ref_value  AS via_ref_value,
                dst.subject  AS to_subject,
                dst.source   AS to_source,
                dst.ts       AS to_ts
            FROM events src
            JOIN event_refs r ON r.event_id = src.id
            LEFT JOIN events dst ON dst.subject = ({subject_expr})
            WHERE src.subject = ?
              AND r.ref_type = ?
            GROUP BY src.subject, r.ref_type, r.ref_value
            ORDER BY r.ref_value
        """

        for rt in self.SUBJECT_REF_TYPES:
            subject_expr = _REF_TO_SUBJECT_EXPR[rt]
            sql = sql_template.format(subject_expr=subject_expr)
            for row in self.conn.execute(sql, (subject, rt)).fetchall():
                results.append(Link(
                    from_subject=row[0], from_source=row[1],
                    via_ref_type=row[2], via_ref_value=row[3],
                    to_subject=row[4], to_source=row[5], to_ts=row[6],
                    direction="out",
                ))
        return results

    # ── Incoming: events that ref this subject ──────────────────────────────

    def incoming(self, subject: str) -> list[Link]:
        """Every event whose refs point AT this subject. Joins the other way
        around. Orphan-free by construction (the source event exists)."""
        results: list[Link] = []

        # We need: for each ref_type, what ref_value would match this subject?
        # Then find all event_refs rows with that ref_value/ref_type.
        for rt in self.SUBJECT_REF_TYPES:
            inverse = _REF_FROM_SUBJECT_EXPR.get(rt, lambda s: s)
            target_ref_value = inverse(subject)
            if not target_ref_value:
                continue

            sql = """
                SELECT
                    src.subject AS from_subject,
                    src.source  AS from_source,
                    r.ref_type  AS via_ref_type,
                    r.ref_value AS via_ref_value,
                    ?           AS to_subject,
                    NULL        AS to_source,
                    NULL        AS to_ts
                FROM event_refs r
                JOIN events src ON src.id = r.event_id
                WHERE r.ref_type = ?
                  AND r.ref_value = ?
                  AND src.subject != ?
                GROUP BY src.subject, r.ref_type, r.ref_value
                ORDER BY src.ts DESC
            """
            for row in self.conn.execute(
                sql, (subject, rt, target_ref_value, subject),
            ).fetchall():
                # Populate to_source from this subject's own event.
                to_event = self.conn.execute(
                    "SELECT source, MAX(ts) FROM events WHERE subject = ? LIMIT 1",
                    (subject,),
                ).fetchone()
                to_source, to_ts = (to_event[0], to_event[1]) if to_event else (None, None)
                results.append(Link(
                    from_subject=row[0], from_source=row[1],
                    via_ref_type=row[2], via_ref_value=row[3],
                    to_subject=row[4], to_source=to_source, to_ts=to_ts,
                    direction="in",
                ))
        return results

    # ── Both-direction neighbour set ────────────────────────────────────────

    def neighbours(self, subject: str) -> list[Link]:
        """Outgoing + Incoming, deduped on (from_subject, to_subject, via)."""
        out = self.outgoing(subject)
        inc = self.incoming(subject)
        seen: set[tuple] = set()
        merged: list[Link] = []
        for link in out + inc:
            key = (link.from_subject, link.to_subject, link.via_ref_type, link.via_ref_value)
            if key in seen:
                continue
            seen.add(key)
            merged.append(link)
        return merged

    # ── BFS walk to `depth` ─────────────────────────────────────────────────

    def walk(self, subject: str, depth: int = 2) -> list[Link]:
        """BFS to `depth` hops. Returns every Link discovered along the way."""
        visited: set[str] = {subject}
        frontier: deque[tuple[str, int]] = deque([(subject, 0)])
        all_links: list[Link] = []
        seen_links: set[tuple] = set()

        while frontier:
            cur, dist = frontier.popleft()
            if dist >= depth:
                continue
            for link in self.neighbours(cur):
                key = (link.from_subject, link.to_subject, link.via_ref_type, link.via_ref_value)
                if key in seen_links:
                    continue
                seen_links.add(key)
                all_links.append(link)

                # Discover next-hop subjects.
                for nxt in (link.to_subject, link.from_subject):
                    if nxt and nxt not in visited:
                        visited.add(nxt)
                        frontier.append((nxt, dist + 1))
        return all_links

    # ── Convenience: subject-set within radius ──────────────────────────────

    def related_subjects(self, subject: str, depth: int = 2) -> list[tuple[str, str, int]]:
        """Return (subject, source, hop_distance) tuples for every subject
        reachable within `depth` hops, deduped on subject."""
        visited: dict[str, tuple[str, int]] = {}

        # Seed: starting subject distance 0
        seed = self.conn.execute(
            "SELECT source FROM events WHERE subject = ? LIMIT 1", (subject,),
        ).fetchone()
        visited[subject] = (seed[0] if seed else "?", 0)

        frontier: deque[tuple[str, int]] = deque([(subject, 0)])
        while frontier:
            cur, dist = frontier.popleft()
            if dist >= depth:
                continue
            for link in self.neighbours(cur):
                for cand in (link.to_subject, link.from_subject):
                    if not cand or cand in visited:
                        continue
                    src = link.to_source if cand == link.to_subject else link.from_source
                    visited[cand] = (src or "?", dist + 1)
                    frontier.append((cand, dist + 1))

        return sorted(
            ((s, src, d) for s, (src, d) in visited.items() if s != subject),
            key=lambda x: (x[2], x[0]),
        )


# ── CLI helper for spot-checks ──────────────────────────────────────────────


def _cli() -> None:
    import argparse
    import sys
    from pathlib import Path
    _PKG_ROOT = Path(__file__).resolve().parent.parent
    if str(_PKG_ROOT) not in sys.path:
        sys.path.insert(0, str(_PKG_ROOT))
    from ingest.common import get_db  # noqa: E402

    parser = argparse.ArgumentParser(description="Walk the story graph from a subject.")
    parser.add_argument("subject", help="e.g. 'EX-2660' or 'example-org/service-a#629' or 'page:EXAMPLE_PAGE_ID'")
    parser.add_argument("--depth", type=int, default=2)
    parser.add_argument("--mode", choices=["walk", "outgoing", "incoming", "related"], default="related")
    args = parser.parse_args()

    g = StoryGraph(get_db())

    if args.mode == "outgoing":
        links = g.outgoing(args.subject)
        for L in links:
            print(f"  → [{L.via_ref_type}={L.via_ref_value}] {L.to_subject or '(orphan)'} ({L.to_source or '-'})")
    elif args.mode == "incoming":
        links = g.incoming(args.subject)
        for L in links:
            print(f"  ← [{L.via_ref_type}={L.via_ref_value}] {L.from_subject} ({L.from_source})")
    elif args.mode == "walk":
        links = g.walk(args.subject, depth=args.depth)
        for L in links:
            arrow = "→" if L.direction == "out" else "←"
            print(f"  {L.from_subject} {arrow} [{L.via_ref_type}={L.via_ref_value}] {L.to_subject or '(orphan)'}")
    else:  # related
        subs = g.related_subjects(args.subject, depth=args.depth)
        print(f"{len(subs)} related subjects within {args.depth} hops of {args.subject}:")
        by_source: dict[str, list] = {}
        for s, src, d in subs:
            by_source.setdefault(src, []).append((s, d))
        for src in sorted(by_source):
            print(f"\n  [{src}] ({len(by_source[src])})")
            for s, d in by_source[src][:20]:
                print(f"    hop={d}  {s}")
            if len(by_source[src]) > 20:
                print(f"    … and {len(by_source[src]) - 20} more")


if __name__ == "__main__":
    _cli()
