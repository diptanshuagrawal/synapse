#!/usr/bin/env python3
"""Java/Play service derivative extractor (Phase 1 — deterministic skeleton).

service-c is a Java **Play Framework** monolith, not Spring: routes
live in `conf/routes` text files, not annotations. This extractor emits the same
Skeleton schema as the Go extractor, filling it from Play conventions:

  - endpoints : parsed from every `conf/*routes` file
                (`METHOD  /path  controllers.Class.method(param:Type, ...)`)
  - tables    : CREATE TABLE from .sql migrations (reuses the Go extractor's
                SQL parser — SQL is language-agnostic)
  - kafka     : best-effort graph discovery (Play/service-c has no annotation-based
                consumers; expect few/none — gaps accepted by design)

Defaults (per decision):
  - request  = the route's typed params (Play has no response type in routes)
  - response = "(unknown)"

No LLM. No network. Semantics filled later by the chat pass.

Usage:
    python derive/service_derive/play_extractor.py --repo <mirror> --svc service-c
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import go_extractor as gx  # noqa: E402  (reuse Skeleton/Endpoint/Table + SQL/kafka parsers)

# METHOD  /path  controllers.Pkg.Class.method(params)
_ROUTE_RE = re.compile(
    r"^(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\s+(\S+)\s+@?([\w.]+)\.(\w+)\s*(?:\((.*)\))?\s*$"
)


def parse_routes(path: Path, rel: str) -> list[gx.Endpoint]:
    endpoints: list[gx.Endpoint] = []
    for line in path.read_text(errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("->"):
            continue
        m = _ROUTE_RE.match(s)
        if not m:
            continue
        method, http_path, fqclass, fn, params = m.groups()
        cls = fqclass.split(".")[-1]  # AccessListController
        endpoints.append(
            gx.Endpoint(
                service=cls,
                rpc=fn,
                request=(params or "").strip() or "()",
                response="(unknown)",
                http_method=method,
                http_path=http_path,
                proto_file=rel,
            )
        )
    return endpoints


def build_play_skeleton(repo: Path, svc: str) -> gx.Skeleton:
    skel = gx.Skeleton(service=svc, repo=str(repo), commit=gx._git_commit(repo))

    # Endpoints: every conf/*routes file (conf/routes, conf/v2.routes, ...).
    routes_files = [
        p for p in repo.rglob("conf/*routes")
        if p.is_file() and ".git/" not in str(p)
    ]
    for p in sorted(routes_files):
        skel.endpoints.extend(parse_routes(p, gx._rel(repo, p)))

    # Tables: reuse the language-agnostic SQL parser.
    for p, rel in gx._iter_files(repo, ".sql"):
        parent = p.parent.name.lower()
        if "postgres" in parent:
            dialect = "postgresql"
        elif "mssql" in parent or "sqlserver" in parent:
            dialect = "mssql"
        elif "mysql" in parent:
            dialect = "mysql"
        else:
            dialect = "unknown"
        skel.tables.extend(gx.parse_sql(p, rel, dialect))
    skel.tables = gx.dedupe_tables(skel.tables)

    # Kafka: best-effort graph discovery (no annotations in Play; usually empty).
    discovered = gx.discover_kafka_via_graph(repo)
    if discovered is not None:
        listeners, producers = discovered
        for struct, fp in listeners:
            p = Path(fp)
            rel = gx._rel(repo, p) if p.is_absolute() else fp
            if p.exists():
                lst = gx.parse_listener(p, rel, known_struct=struct)
                if lst:
                    skel.kafka_listeners.append(lst)
        for struct, fp in producers:
            p = Path(fp)
            rel = gx._rel(repo, p) if p.is_absolute() else fp
            if p.exists():
                prod = gx.parse_producer(p, rel, known_struct=struct)
                if prod:
                    skel.kafka_producers.append(prod)

    skel.notes = [
        "Framework: Java/Play. Endpoints parsed from conf/*routes.",
        "request = route params; response = (unknown) — Play routes carry no response type.",
        "Kafka discovery is best-effort (no annotation-based consumers); gaps expected.",
        "Call-edges (handler->repo->table) deferred to code-graph enrichment.",
        "Semantic fields filled by chat LLM pass.",
    ]
    return skel


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--svc", required=True)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    repo = Path(args.repo).expanduser().resolve()
    if not repo.is_dir():
        print(f"error: repo not found: {repo}", file=sys.stderr)
        return 2

    skel = build_play_skeleton(repo, args.svc)
    out_path = (
        Path(args.out) if args.out
        else Path("derived/services") / f"{args.svc}.skeleton.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(asdict(skel), indent=2))

    print(f"service       : {skel.service} @ {skel.commit}")
    print(f"endpoints     : {len(skel.endpoints)}")
    print(f"tables        : {len(skel.tables)}")
    print(f"kafka listener: {len(skel.kafka_listeners)}")
    print(f"kafka producer: {len(skel.kafka_producers)}")
    print(f"written       : {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
