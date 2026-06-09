#!/usr/bin/env python3
"""build_context_pack.py — deterministic feature->code linker for the TRD builder.

Given a PRD (plain text) and a Go service skeleton, find the code surface the
feature most likely touches: the endpoints / tables / kafka topics whose
IDENTIFIERS overlap the PRD's vocabulary. Output a ranked, provenance-tagged
"context pack" the /trd-build skill feeds to backend-trd-writer.

This is the DETERMINISTIC SEED of the automatic linkage. It owns the FACTS
(every name is copied verbatim from the skeleton, never invented). The skill
then expands blast radius via code-graph MCP (impact_radius / affected_flows).

Zero LLM, zero network. Matching is pure token-overlap so a reviewer can audit
exactly which PRD words pulled each code item in (`matched` field).
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

# Words too generic to be signal — drop from PRD vocab and from code tokens.
STOP = {
    "the", "and", "for", "with", "this", "that", "from", "will", "are", "was",
    "has", "have", "can", "all", "any", "not", "but", "use", "used", "via",
    "into", "onto", "per", "new", "old", "get", "set", "add", "request",
    "response", "service", "data", "value", "type", "name", "code", "id", "ids",
    "list", "map", "string", "int", "int64", "int32", "bool", "bytes", "field",
    "fields", "table", "tables", "column", "columns", "proto", "rpc", "api",
    "system", "user", "users", "flow", "case", "page", "doc", "spec", "prd",
    "trd", "should", "must", "when", "then", "also", "such", "each", "which",
    "between", "within", "their", "them", "they", "would", "could", "about",
}

_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z0-9]+|[A-Z]+")


def tokenize(text: str) -> set[str]:
    """Lowercase token set, splitting camelCase, snake_case and punctuation.

    'InvokeTDSRequest', 'card_transaction', 'card-txn' all explode into their
    component words so PRD prose can match code identifiers.
    """
    out: set[str] = set()
    for raw in re.split(r"[^A-Za-z0-9]+", text):
        if not raw:
            continue
        for part in _CAMEL.findall(raw):
            p = part.lower()
            if len(p) >= 3 and p not in STOP and not p.isdigit():
                out.add(p)
    return out


def basename(qualified: str) -> str:
    """transaction.InvokeTDSRequest -> InvokeTDSRequest."""
    return (qualified or "").split(".")[-1]


def score(item_tokens: set[str], prd: set[str], idf: dict[str, float]) -> tuple[float, list[str]]:
    """IDF-weighted overlap: rare skeleton tokens (rrn, reversal, upi) count
    far more than ubiquitous ones (account, status, amount). `matched` is sorted
    by descending weight so the strongest signal reads first (provenance)."""
    hit = item_tokens & prd
    ranked = sorted(hit, key=lambda t: -idf.get(t, 0.0))
    return round(sum(idf.get(t, 0.0) for t in hit), 2), ranked


def endpoint_tokens(ep: dict) -> set[str]:
    t = tokenize(ep.get("rpc", ""))
    t |= tokenize(basename(ep.get("request", "")))
    t |= tokenize(basename(ep.get("response", "")))
    t |= tokenize(ep.get("service", ""))
    t |= tokenize((ep.get("http_path") or "").replace("/", " "))
    for f in ep.get("request_fields", []) or []:
        t |= tokenize(f.get("name", ""))
    return t


def table_tokens(tb: dict) -> set[str]:
    t = tokenize(tb.get("name", ""))
    for c in tb.get("columns", []) or []:
        t |= tokenize(c.get("name", ""))
    return t


def kafka_tokens(k: dict) -> set[str]:
    t = tokenize(k.get("struct", ""))
    t |= tokenize(k.get("listener_name", ""))
    t |= tokenize(basename(k.get("payload_type") or ""))
    t |= tokenize((k.get("go_file") or "").replace("/", " ").replace(".go", ""))
    return t


def rank(items, tok_fn, prd, idf, top, rel):
    """Rank by IDF-weighted score; keep top-N, then drop the long tail scoring
    below `rel` * the category's best (removes generic-token-only matches)."""
    scored = []
    for it in items:
        n, hit = score(tok_fn(it), prd, idf)
        if n > 0:
            scored.append((n, hit, it))
    scored.sort(key=lambda x: -x[0])
    if scored:
        cutoff = scored[0][0] * rel
        scored = [s for s in scored if s[0] >= cutoff]
    return scored[:top]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prd", required=True, help="Path to PRD plain-text/markdown")
    ap.add_argument("--svc", required=True)
    ap.add_argument("--skeleton", help="default: derived/services/<svc>.skeleton.json")
    ap.add_argument("--out", help="default: derived/trds/<svc>.context.json")
    ap.add_argument("--top", type=int, default=12, help="max items per category")
    ap.add_argument("--rel", type=float, default=0.35,
                    help="keep items scoring >= rel * category best (0-1)")
    args = ap.parse_args()

    skel_path = Path(args.skeleton or f"derived/services/{args.svc}.skeleton.json")
    if not skel_path.exists():
        print(f"ERROR: skeleton not found: {skel_path}", file=sys.stderr)
        return 2
    skel = json.loads(skel_path.read_text())

    prd_path = Path(args.prd)
    if not prd_path.exists():
        print(f"ERROR: PRD not found: {prd_path}", file=sys.stderr)
        return 2
    prd_tokens = tokenize(prd_path.read_text())

    # Build IDF over the WHOLE skeleton corpus so ubiquitous tokens (account,
    # status, amount) are discounted and rare ones (rrn, reversal, upi) dominate.
    corpus = (
        [endpoint_tokens(e) for e in skel.get("endpoints", [])]
        + [table_tokens(t) for t in skel.get("tables", [])]
        + [kafka_tokens(k) for k in skel.get("kafka_listeners", [])]
        + [kafka_tokens(k) for k in skel.get("kafka_producers", [])]
    )
    n_docs = max(1, len(corpus))
    df: dict[str, int] = {}
    for toks in corpus:
        for t in toks:
            df[t] = df.get(t, 0) + 1
    idf = {t: math.log((n_docs + 1) / (c + 1)) + 1.0 for t, c in df.items()}

    eps = rank(skel.get("endpoints", []), endpoint_tokens, prd_tokens, idf, args.top, args.rel)
    tbs = rank(skel.get("tables", []), table_tokens, prd_tokens, idf, args.top, args.rel)
    kls = rank(skel.get("kafka_listeners", []), kafka_tokens, prd_tokens, idf, args.top, args.rel)
    kps = rank(skel.get("kafka_producers", []), kafka_tokens, prd_tokens, idf, args.top, args.rel)

    pack = {
        "svc": args.svc,
        "skeleton_commit": skel.get("commit"),
        "prd_path": str(prd_path),
        "prd_token_count": len(prd_tokens),
        "rel_cutoff": args.rel,
        "candidate_endpoints": [
            {"score": n, "matched": hit, "rpc": e["rpc"],
             "http": f'{e.get("http_method","")} {e.get("http_path") or "(grpc-only)"}',
             "service": e.get("service"), "request": e.get("request"),
             "response": e.get("response"), "proto_file": e.get("proto_file")}
            for n, hit, e in eps
        ],
        "candidate_tables": [
            {"score": n, "matched": hit, "name": t["name"],
             "dialect": t.get("dialect"), "sql_file": t.get("sql_file"),
             "n_columns": len(t.get("columns", []) or [])}
            for n, hit, t in tbs
        ],
        "candidate_kafka_consumers": [
            {"score": n, "matched": hit, "listener": k.get("listener_name") or k.get("struct"),
             "payload": k.get("payload_type"), "go_file": k.get("go_file")}
            for n, hit, k in kls
        ],
        "candidate_kafka_producers": [
            {"score": n, "matched": hit, "struct": k.get("struct"),
             "payload": k.get("payload_type"), "go_file": k.get("go_file")}
            for n, hit, k in kps
        ],
    }

    out_path = Path(args.out or f"derived/trds/{args.svc}.context.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(pack, indent=2))

    print(f"PRD tokens     : {len(prd_tokens)}")
    print(f"skeleton       : {args.svc} @ {skel.get('commit')}")
    print(f"endpoints hit  : {len(pack['candidate_endpoints'])}")
    print(f"tables hit     : {len(pack['candidate_tables'])}")
    print(f"consumers hit  : {len(pack['candidate_kafka_consumers'])}")
    print(f"producers hit  : {len(pack['candidate_kafka_producers'])}")
    print(f"written        : {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
