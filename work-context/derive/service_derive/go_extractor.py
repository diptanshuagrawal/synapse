#!/usr/bin/env python3
"""Go service derivative extractor (Phase 1 — deterministic skeleton).

Parses a Go service repo's source directly and emits a JSON skeleton with the
deterministic spine of the service-derivative schema:

  - endpoints  : gRPC services + rpcs from .proto (incl. google.api.http path)
  - tables     : CREATE TABLE defs from .sql migrations (cols, types, comments)
  - kafka      : listeners + producers (struct, payload type, config key)

No LLM. No network. Pure structure -> reproducible, zero tokens. The semantic
("why") fields are filled by a later chat-driven pass over this skeleton.

Topic strings are NOT in Go source (they come from runtime config), so the
extractor records the Kafka *config key* and leaves `topic` null. Call-edges
(handler -> repo -> table) are left empty here; they come from the code-graph
in a later enrichment step.

Usage:
    python derive/service_derive/go_extractor.py --repo /path/to/mirror --svc service-a
    python derive/service_derive/go_extractor.py --repo ~/.code-review-graph/repos/service-a \
        --svc service-a --out derived/services/service-a.skeleton.json
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #


@dataclass
class Endpoint:
    service: str
    rpc: str
    request: str
    response: str
    http_method: str | None
    http_path: str | None
    proto_file: str
    # P1/P2: resolved proto message fields (name/type/repeated/rule). rule is the
    # buf.validate human message when present — deterministic validation signal.
    request_fields: list = field(default_factory=list)
    response_fields: list = field(default_factory=list)


@dataclass
class Column:
    name: str
    type: str
    constraints: list[str]
    comment: str | None = None


@dataclass
class Table:
    name: str
    dialect: str          # postgresql | mssql | unknown (parent dir of migration)
    columns: list[Column]
    comment: str | None
    sql_file: str


@dataclass
class KafkaListener:
    struct: str
    constructor: str | None        # NewXxx -> join key against the registry
    payload_type: str | None
    listener_name: str | None      # enum.ListenerNameXxx
    config_key: str | None         # c.XxxConsumerConfig
    topic: str | None              # always None here (config-resolved)
    go_file: str


@dataclass
class KafkaProducer:
    struct: str
    payload_type: str | None
    config_key: str | None         # c.XxxProducerConfig (best effort)
    topic: str | None              # always None here (config-resolved)
    go_file: str


@dataclass
class Skeleton:
    service: str
    repo: str
    commit: str | None
    endpoints: list[Endpoint] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)
    kafka_listeners: list[KafkaListener] = field(default_factory=list)
    kafka_producers: list[KafkaProducer] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Proto parsing -> endpoints
# --------------------------------------------------------------------------- #

_SERVICE_RE = re.compile(r"\bservice\s+(\w+)\s*\{")
_RPC_RE = re.compile(
    r"\brpc\s+(\w+)\s*\(\s*(?:stream\s+)?([\w.]+)\s*\)\s*"
    r"returns\s*\(\s*(?:stream\s+)?([\w.]+)\s*\)\s*(\{)?",
)
_HTTP_RE = re.compile(
    r"\b(get|post|put|delete|patch)\s*:\s*\"([^\"]+)\"", re.IGNORECASE
)


def _strip_line_comments(text: str) -> str:
    # Remove // comments but keep string literals intact enough for our regexes.
    out = []
    for line in text.splitlines():
        idx = line.find("//")
        out.append(line[:idx] if idx != -1 else line)
    return "\n".join(out)


def _balanced_block(text: str, open_idx: int) -> tuple[str, int]:
    """Given index of an opening '{', return (inner_text, index_after_close)."""
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1 : i], i + 1
    return text[open_idx + 1 :], len(text)


_MESSAGE_RE = re.compile(r"\bmessage\s+(\w+)\s*\{")
_FIELD_RE = re.compile(r"(?:(repeated|optional)\s+)?([\w.]+)\s+(\w+)\s*=\s*\d+\s*(\[)?")
_RULE_MSG_RE = re.compile(r'message\s*:\s*"([^"]+)"')
_FIELD_SKIP = {"message", "enum", "oneof", "option", "reserved", "map",
               "returns", "rpc", "service", "package", "import", "syntax"}


def _balanced_brackets(text: str, open_idx: int) -> tuple[str, int]:
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == "[":
            depth += 1
        elif text[i] == "]":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1 : i], i + 1
    return text[open_idx + 1 :], len(text)


def _parse_message_fields(inner: str) -> list[dict]:
    fields: list[dict] = []
    for fm in _FIELD_RE.finditer(inner):
        rep, ftype, fname, bracket = fm.group(1), fm.group(2), fm.group(3), fm.group(4)
        if ftype in _FIELD_SKIP or fname in _FIELD_SKIP:
            continue
        rule = None
        if bracket == "[":
            opts, _ = _balanced_brackets(inner, fm.end() - 1)
            rm = _RULE_MSG_RE.search(opts)
            if rm:
                rule = rm.group(1)
        fields.append(
            {"name": fname, "type": ftype, "repeated": rep == "repeated", "rule": rule}
        )
    return fields


def collect_proto_messages(repo: Path) -> dict[str, list[dict]]:
    """Map bare message name -> [field dicts] across all .proto files."""
    msgs: dict[str, list[dict]] = {}
    for p, _rel in _iter_files(repo, ".proto"):
        raw = _strip_line_comments(p.read_text(errors="replace"))
        for m in _MESSAGE_RE.finditer(raw):
            inner, _ = _balanced_block(raw, m.end() - 1)
            msgs[m.group(1)] = _parse_message_fields(inner)
    return msgs


def parse_proto(path: Path, rel: str) -> list[Endpoint]:
    raw = _strip_line_comments(path.read_text(errors="replace"))
    endpoints: list[Endpoint] = []

    # Map each service to its byte span so rpcs can be attributed.
    services = [(m.group(1), m.start()) for m in _SERVICE_RE.finditer(raw)]
    if not services:
        return endpoints

    def service_for(pos: int) -> str:
        name = services[0][0]
        for svc_name, svc_pos in services:
            if svc_pos <= pos:
                name = svc_name
            else:
                break
        return name

    for m in _RPC_RE.finditer(raw):
        rpc, req, resp, has_block = m.group(1), m.group(2), m.group(3), m.group(4)
        http_method = http_path = None
        if has_block:
            inner, _ = _balanced_block(raw, m.end() - 1)
            hm = _HTTP_RE.search(inner)
            if hm:
                http_method = hm.group(1).upper()
                http_path = hm.group(2)
        endpoints.append(
            Endpoint(
                service=service_for(m.start()),
                rpc=rpc,
                request=req,
                response=resp,
                http_method=http_method,
                http_path=http_path,
                proto_file=rel,
            )
        )
    return endpoints


# --------------------------------------------------------------------------- #
# SQL parsing -> tables
# --------------------------------------------------------------------------- #

_CREATE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([#\w.\[\]`\"]+)\s*\(",
    re.IGNORECASE,
)
_TABLE_COMMENT_RE = re.compile(
    r"COMMENT\s+ON\s+TABLE\s+[`\"\[]?([\w.]+)[`\"\]]?\s+IS\s+'((?:[^']|'')*)'",
    re.IGNORECASE,
)
_COL_COMMENT_RE = re.compile(
    r"COMMENT\s+ON\s+COLUMN\s+[`\"\[]?([\w.]+)[`\"\]]?\s+IS\s+'((?:[^']|'')*)'",
    re.IGNORECASE,
)
_NON_COLUMN_PREFIX = re.compile(
    r"^(CONSTRAINT|PRIMARY|FOREIGN|UNIQUE|INDEX|KEY|CHECK)\b", re.IGNORECASE
)


def _paren_block(text: str, open_idx: int) -> tuple[str, int]:
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1 : i], i + 1
    return text[open_idx + 1 :], len(text)


def _strip_sql_comments(s: str) -> str:
    # Drop `-- ...` inline/line comments so they aren't parsed as columns.
    return "\n".join(line.split("--", 1)[0] for line in s.splitlines())


def _split_top_level_commas(s: str) -> list[str]:
    parts, depth, cur = [], 0, []
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur))
    return parts


def _bare_short_name(qualified: str) -> str:
    return qualified.split(".")[-1]


def _clean_table_name(raw_name: str) -> str:
    """Strip brackets/backticks/quotes and schema prefix: [dbo].[t] -> t."""
    n = raw_name.translate(str.maketrans("", "", '[]`"'))
    return n.split(".")[-1]


def dedupe_tables(tables: list[Table]) -> list[Table]:
    """Collapse re-declared tables (common in migration trees); keep the
    definition with the most columns per (dialect, name)."""
    best: dict[tuple[str, str], Table] = {}
    for t in tables:
        key = (t.dialect, t.name)
        if key not in best or len(t.columns) > len(best[key].columns):
            best[key] = t
    return list(best.values())


def parse_sql(path: Path, rel: str, dialect: str) -> list[Table]:
    raw = path.read_text(errors="replace")
    tables: list[Table] = []

    table_comments = {
        _bare_short_name(m.group(1)): m.group(2).replace("''", "'")
        for m in _TABLE_COMMENT_RE.finditer(raw)
    }
    col_comments: dict[tuple[str, str], str] = {}
    for m in _COL_COMMENT_RE.finditer(raw):
        tbl_col = m.group(1).split(".")
        if len(tbl_col) == 2:
            col_comments[(tbl_col[0], tbl_col[1])] = m.group(2).replace("''", "'")

    for m in _CREATE_RE.finditer(raw):
        table = _clean_table_name(m.group(1))
        if not table or table.startswith("#"):
            continue  # skip temp tables (#tmp) and unparseable names
        inner, _ = _paren_block(raw, m.end() - 1)
        inner = _strip_sql_comments(inner)
        cols: list[Column] = []
        for chunk in _split_top_level_commas(inner):
            line = chunk.strip()
            if not line or _NON_COLUMN_PREFIX.match(line):
                continue
            toks = line.split()
            if len(toks) < 2:
                continue
            col_name = toks[0].strip('`"[]')
            col_type = toks[1].strip(",")
            constraints = [t for t in toks[2:] if t.upper() not in {","}]
            cols.append(
                Column(
                    name=col_name,
                    type=col_type,
                    constraints=constraints,
                    comment=col_comments.get((table, col_name)),
                )
            )
        tables.append(
            Table(
                name=table,
                dialect=dialect,
                columns=cols,
                comment=table_comments.get(table),
                sql_file=rel,
            )
        )
    return tables


# --------------------------------------------------------------------------- #
# Kafka parsing -> listeners / producers
# --------------------------------------------------------------------------- #

_TYPE_DECL_RE = re.compile(r"\btype\s+(\w+)\s+struct\b")
_UNMARSHAL_RE = re.compile(r"Unmarshal\(\s*\w+\.Value\s*,\s*&(\w+)\s*\)")
_VAR_DECL_RE = re.compile(r"\bvar\s+(\w+)\s+([\w.]+)")
_PRODUCE_SIG_RE = re.compile(
    r"\b(?:Produce|Publish)\s*\(([^)]*)\)", re.IGNORECASE
)


def _first_struct(text: str) -> str | None:
    m = _TYPE_DECL_RE.search(text)
    return m.group(1) if m else None


_CTOR_RE = re.compile(r"\bfunc\s+New(\w+)\s*\(")


def _payload_from_unmarshal(text: str) -> str | None:
    um = _UNMARSHAL_RE.search(text)
    if not um:
        return None
    var = um.group(1)
    for vm in _VAR_DECL_RE.finditer(text):
        if vm.group(1) == var:
            return vm.group(2)
    return None


def _best_constructor(text: str, struct: str | None) -> str | None:
    """Pick the New* ctor; prefer one matching the known struct (case-insensitive)."""
    ctors = [m.group(1) for m in _CTOR_RE.finditer(text)]
    if not ctors:
        return None
    if struct:
        for c in ctors:
            if c.lower() == struct.lower():
                return c
    return ctors[0]


def parse_listener(
    path: Path, rel: str, known_struct: str | None = None
) -> KafkaListener | None:
    text = path.read_text(errors="replace")
    struct = known_struct or _first_struct(text)
    if struct is None:
        return None
    # Constructor is the reliable join key against the registry wiring.
    constructor = _best_constructor(text, struct)
    return KafkaListener(
        struct=struct,
        constructor=constructor,
        payload_type=_payload_from_unmarshal(text),
        listener_name=None,   # filled from registry pass
        config_key=None,      # filled from registry pass
        topic=None,
        go_file=rel,
    )


def parse_producer(
    path: Path, rel: str, known_struct: str | None = None
) -> KafkaProducer | None:
    text = path.read_text(errors="replace")
    struct = known_struct or _first_struct(text)
    if struct is None:
        return None
    payload = None
    sm = _PRODUCE_SIG_RE.search(text)
    if sm:
        params = sm.group(1)
        # take the last param's type (e.g. "ctx context.Context, event paymentsbo.RoundUpRequested")
        last = params.split(",")[-1].strip()
        toks = last.split()
        if len(toks) >= 2:
            payload = toks[-1]
    return KafkaProducer(
        struct=struct,
        payload_type=payload,
        config_key=None,
        topic=None,
        go_file=rel,
    )


# Registry: map enum.ListenerNameXxx -> consumer-config key, in cmd/consumer.
_REGISTRY_ENTRY_RE = re.compile(
    r"enum\.(ListenerName\w+)\.String\(\)\s*:\s*\{(.*?)\n\s*\},",
    re.DOTALL,
)
_CONSUMERCFG_RE = re.compile(r"ConsumerConfig\s*:\s*c\.(\w+)")
# Inner ctor passed to kafka.NewListener(...) -- exclude the wrapper itself.
_INLINE_CTOR_RE = re.compile(r"kafka\.NewListener\(\s*(?:[\w.]+\.)?New(\w+)\s*\(")
# Bare variable passed to kafka.NewListener(varName)
_VAR_CALLBACK_RE = re.compile(r"kafka\.NewListener\(\s*(\w+)\s*[,)]")
# var := pkg.NewXxx(  -> resolves a var name to its constructor
_VAR_ASSIGN_RE = re.compile(r"(\w+)\s*:?=\s*[\w.]+\.New(\w+)\s*\(")


def parse_registry(repo: Path) -> dict[str, dict[str, str]]:
    """constructor-name (NewXxx -> Xxx) -> {listener_name, config_key}."""
    out: dict[str, dict[str, str]] = {}
    candidates = list(repo.glob("cmd/**/consumer.go")) + list(
        repo.glob("cmd/**/server.go")
    )
    for c in candidates:
        text = c.read_text(errors="replace")
        # Whole-file var -> constructor map for resolving var-registered listeners.
        var_to_ctor = {m.group(1): m.group(2) for m in _VAR_ASSIGN_RE.finditer(text)}
        for m in _REGISTRY_ENTRY_RE.finditer(text):
            listener_name = m.group(1)
            body = m.group(2)
            cfg_m = _CONSUMERCFG_RE.search(body)
            ctor = None
            inline = _INLINE_CTOR_RE.search(body)
            if inline:
                ctor = inline.group(1)
            else:
                var_m = _VAR_CALLBACK_RE.search(body)
                if var_m:
                    ctor = var_to_ctor.get(var_m.group(1))
            if ctor:
                out[ctor] = {
                    "listener_name": listener_name,
                    "config_key": cfg_m.group(1) if cfg_m else None,
                }
    return out


# --------------------------------------------------------------------------- #
# Graph-backed Kafka discovery (convention-agnostic)
# --------------------------------------------------------------------------- #
#
# The code-graph does NOT model Go interface satisfaction (INHERITS == 0) and
# stores only the receiver in a method's params (no arg types). So we discover
# Kafka structs by combining two graph signals that DO survive:
#   1. name pattern (Class name contains Listener / Producer / Publisher)
#   2. behaviour (the struct has a consume/produce method)
# This finds listeners/producers anywhere in the repo, independent of how (or
# where) they are wired -- which is what file-globbing could not do.

_CONSUME_METHODS = ("Process", "ProcessBatch", "HandleMessage", "Consume", "Handle")
_PRODUCE_METHODS = ("Produce", "Publish", "Send", "Emit")
# Name suffixes that look kafka-ish but are interfaces/config/DTOs/collections.
_NOT_A_STRUCT = re.compile(
    r"(Interface|Service|Config|Request|Response|Wrapper|Name)$|s$", re.IGNORECASE
)


def _graph_db(repo: Path) -> Path | None:
    db = repo / ".code-review-graph" / "graph.db"
    return db if db.exists() else None


def _structs_with_method(con: sqlite3.Connection, methods: tuple[str, ...]) -> set[str]:
    q = (
        "SELECT DISTINCT parent_name FROM nodes "
        "WHERE kind='Function' AND is_test=0 AND parent_name!='' "
        f"AND name IN ({','.join('?' * len(methods))})"
    )
    return {r[0] for r in con.execute(q, methods)}


def _classes_like(con: sqlite3.Connection, patterns: tuple[str, ...]) -> dict[str, str]:
    """Return {class_name: file_path} for non-test classes matching any LIKE pattern."""
    out: dict[str, str] = {}
    for pat in patterns:
        for name, fp in con.execute(
            "SELECT name, file_path FROM nodes "
            "WHERE kind='Class' AND is_test=0 AND name LIKE ?",
            (pat,),
        ):
            out.setdefault(name, fp)
    return out


def discover_kafka_via_graph(
    repo: Path,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]] | None:
    """Return (listeners, producers) as [(struct_name, file_path), ...] or None."""
    db = _graph_db(repo)
    if db is None:
        return None
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        consumers = _structs_with_method(con, _CONSUME_METHODS)
        producers_beh = _structs_with_method(con, _PRODUCE_METHODS)
        listener_classes = _classes_like(con, ("%Listener%",))
        producer_classes = _classes_like(con, ("%Producer%", "%Publisher%"))
    finally:
        con.close()

    def keep(name: str) -> bool:
        return not _NOT_A_STRUCT.search(name)

    listeners = [
        (n, fp)
        for n, fp in listener_classes.items()
        if n in consumers and keep(n)
    ]
    producers = [
        (n, fp)
        for n, fp in producer_classes.items()
        if n in producers_beh and keep(n)
    ]
    return sorted(listeners), sorted(producers)


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #

SKIP_DIRS = {"vendor", "proto/vendors", ".git", "node_modules", "rpc"}


def _rel(repo: Path, p: Path) -> str:
    return str(p.relative_to(repo))


def _git_commit(repo: Path) -> str | None:
    head = repo / ".git" / "HEAD"
    try:
        ref = head.read_text().strip()
        if ref.startswith("ref:"):
            ref_path = repo / ".git" / ref.split(" ", 1)[1].strip()
            return ref_path.read_text().strip()[:12]
        return ref[:12]
    except OSError:
        return None


def _iter_files(repo: Path, suffix: str):
    for p in repo.rglob(f"*{suffix}"):
        rel = _rel(repo, p)
        if any(rel == d or rel.startswith(d + "/") for d in SKIP_DIRS):
            continue
        if rel.endswith("_test.go"):
            continue
        yield p, rel


def build_skeleton(repo: Path, svc: str) -> Skeleton:
    skel = Skeleton(service=svc, repo=str(repo), commit=_git_commit(repo))

    # Endpoints
    for p, rel in _iter_files(repo, ".proto"):
        skel.endpoints.extend(parse_proto(p, rel))
    # P1/P2: resolve request/response message fields (+ validate rules).
    msgs = collect_proto_messages(repo)
    for e in skel.endpoints:
        e.request_fields = msgs.get(e.request.split(".")[-1], [])
        e.response_fields = msgs.get(e.response.split(".")[-1], [])

    # Tables
    for p, rel in _iter_files(repo, ".sql"):
        parent = p.parent.name.lower()
        if "postgres" in parent:
            dialect = "postgresql"
        elif "mssql" in parent or "sqlserver" in parent:
            dialect = "mssql"
        else:
            dialect = "unknown"
        skel.tables.extend(parse_sql(p, rel, dialect))
    skel.tables = dedupe_tables(skel.tables)

    # Kafka -- graph-backed discovery, file-glob fallback.
    registry = parse_registry(repo)
    discovered = discover_kafka_via_graph(repo)
    if discovered is not None:
        skel.notes.append("Kafka discovery: code-graph (name + consume/produce method).")
        listeners, producers = discovered
        seen_l: set[tuple[str, str]] = set()
        for struct, fp in listeners:
            p = Path(fp)
            rel = _rel(repo, p) if p.is_absolute() else fp
            key = (struct, rel)
            if not p.exists() or key in seen_l:
                continue
            seen_l.add(key)
            lst = parse_listener(p, rel, known_struct=struct)
            if lst:
                reg = registry.get(lst.constructor) if lst.constructor else None
                if reg:
                    lst.listener_name = reg.get("listener_name")
                    lst.config_key = reg.get("config_key")
                skel.kafka_listeners.append(lst)
        seen_p: set[tuple[str, str]] = set()
        for struct, fp in producers:
            p = Path(fp)
            rel = _rel(repo, p) if p.is_absolute() else fp
            key = (struct, rel)
            if not p.exists() or key in seen_p:
                continue
            seen_p.add(key)
            prod = parse_producer(p, rel, known_struct=struct)
            if prod:
                skel.kafka_producers.append(prod)
    else:
        skel.notes.append("Kafka discovery: file-glob fallback (no code-graph found).")
        for p, rel in _iter_files(repo, "_listener.go"):
            lst = parse_listener(p, rel)
            if lst:
                reg = registry.get(lst.constructor) if lst.constructor else None
                if reg:
                    lst.listener_name = reg.get("listener_name")
                    lst.config_key = reg.get("config_key")
                skel.kafka_listeners.append(lst)
        for suffix in ("_producer.go", "_publisher.go"):
            for p, rel in _iter_files(repo, suffix):
                prod = parse_producer(p, rel)
                if prod:
                    skel.kafka_producers.append(prod)

    skel.notes += [
        "Kafka topic strings are config-resolved (c.*Config.Topic); not in Go source.",
        "Call-edges (handler->repo->table) deferred to code-graph enrichment.",
        "Semantic fields (responsibility, endpoint purpose, payload meaning) filled by chat LLM pass.",
    ]
    return skel


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", required=True, help="Path to the Go service mirror")
    ap.add_argument("--svc", required=True, help="Service name (output key)")
    ap.add_argument(
        "--out",
        default=None,
        help="Output JSON path (default: derived/services/<svc>.skeleton.json)",
    )
    args = ap.parse_args()

    repo = Path(args.repo).expanduser().resolve()
    if not repo.is_dir():
        print(f"error: repo not found: {repo}", file=sys.stderr)
        return 2

    skel = build_skeleton(repo, args.svc)

    out_path = (
        Path(args.out)
        if args.out
        else Path("derived/services") / f"{args.svc}.skeleton.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(asdict(skel), indent=2))

    n_http = sum(1 for e in skel.endpoints if e.http_path)
    print(f"service       : {skel.service} @ {skel.commit}")
    print(f"endpoints     : {len(skel.endpoints)} ({n_http} with HTTP path)")
    print(f"tables        : {len(skel.tables)}")
    print(f"kafka listener: {len(skel.kafka_listeners)}")
    print(f"kafka producer: {len(skel.kafka_producers)}")
    print(f"written       : {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
