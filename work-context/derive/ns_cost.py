#!/usr/bin/env python3
"""ns-cost engine — deterministic half of the /ns-cost skill.

The chat session fetches chargeback data (via the logged-in browser) and does
the cause synthesis; this script does everything mechanical:

  store     read daily readings JSON (stdin or --file) -> upsert metrics_readings
  report    per-namespace trend vs baseline month -> JSON
  context   recall candidate-cause events from index/events.db -> JSON
  snapshot  kubectl capacity snapshot (requests/replicas/HPA) per namespace
  snapdiff  diff the two most recent snapshots for a namespace

Readings JSON shape (list):
  [{"namespace": "liability-core-service", "date": "2026-08-21", "usd": 22.01}, ...]

No network calls from this script. Metrics DB = work-context/events.db
(metrics_readings, same store the daily metrics digest uses); cause recall
reads index/events.db read-only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent          # work-context/
CONFIG = ROOT / "config" / "ns_cost.yaml"
METRICS_DB = ROOT / "events.db"
INDEX_DB = ROOT / "index" / "events.db"
SNAP_DIR = ROOT / "state" / "ns_cost"

METRIC_PREFIX = "ns-cost:"
SOURCE_CHANNEL = "superset-chargeback"

# Signals that a matched event plausibly explains a COST move.
# strong = capacity/cost language, worth surfacing on its own;
# weak = generic change language, needs >=2 distinct hits to qualify.
STRONG_RE = re.compile(
    r"scal(e|ed|ing|eup|edown)|hpa|replica|vertic|autoscal|capacity|"
    r"provision|backfill|cdc|connector|debezium|cutover|over-?provision|"
    r"rightsiz|idle|cost|sidecar|istio|throttl|node ?pool|instance type",
    re.IGNORECASE,
)
WEAK_RE = re.compile(
    r"cpu|memory|resourc|request|incident|rollout|releas|deploy|migrat|traffic",
    re.IGNORECASE,
)
# automated chatter that never explains cost
NOISE_RE = re.compile(
    r"codecov|orca security|dependabot|brace-expansion|ci quality gates|"
    r"snyk|renovate\[bot\]",
    re.IGNORECASE,
)


def load_config() -> dict:
    if not CONFIG.exists():
        sys.exit(f"missing {CONFIG} — copy config/ns_cost.example.yaml and fill it in")
    with open(CONFIG) as f:
        return yaml.safe_load(f)


def open_db(path: Path, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(path)
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def is_weekday(d: str) -> bool:
    return datetime.strptime(d, "%Y-%m-%d").weekday() < 5


# ── store ────────────────────────────────────────────────────────────────

def cmd_store(args: argparse.Namespace) -> None:
    raw = Path(args.file).read_text() if args.file else sys.stdin.read()
    readings = json.loads(raw)
    if not isinstance(readings, list):
        sys.exit("readings JSON must be a list")
    db = Path(args.db) if args.db else METRICS_DB
    conn = open_db(db)
    n = 0
    for r in readings:
        ns, day, usd = r["namespace"], r["date"], float(r["usd"])
        datetime.strptime(day, "%Y-%m-%d")  # validate
        conn.execute(
            """INSERT OR REPLACE INTO metrics_readings
               (metric, value, numeric_value, unit, metric_date,
                source_channel, source_ts, source_file_id, read_confidence)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (METRIC_PREFIX + ns, f"${usd:.2f}/day", usd, "USD/day", day,
             SOURCE_CHANNEL, day, None, r.get("confidence", "clean")),
        )
        n += 1
    conn.commit()
    conn.close()
    print(json.dumps({"stored": n, "db": str(db)}))


# ── report ───────────────────────────────────────────────────────────────

def _avg(vals: list[float]) -> float | None:
    return round(sum(vals) / len(vals), 2) if vals else None


def cmd_report(args: argparse.Namespace) -> None:
    cfg = load_config()
    baseline_month = cfg["baseline_month"]
    threshold = float(cfg.get("threshold_pct", 20))
    weekdays_only = bool(cfg.get("weekdays_only", True))
    names = args.ns or [n["name"] for n in cfg["namespaces"]]
    asof = args.asof or date.today().isoformat()
    db = Path(args.db) if args.db else METRICS_DB
    conn = open_db(db, readonly=True)

    out = []
    for ns in names:
        rows = conn.execute(
            """SELECT metric_date, numeric_value FROM metrics_readings
               WHERE metric = ? AND metric_date <= ? ORDER BY metric_date""",
            (METRIC_PREFIX + ns, asof),
        ).fetchall()
        rows = [(d, v) for d, v in rows if v is not None]
        if weekdays_only:
            rows = [(d, v) for d, v in rows if is_weekday(d)]
        base_vals = [v for d, v in rows if d.startswith(baseline_month)]
        # exclude the asof day itself: usually a partial day in chargeback
        series = [(d, v) for d, v in rows if not d.startswith(baseline_month) and d < asof]
        recent = [v for _, v in series[-5:]]
        prior = [v for _, v in series[-10:-5]]
        baseline, recent_avg, prior_avg = _avg(base_vals), _avg(recent), _avg(prior)

        delta = pct = None
        if baseline and recent_avg is not None:
            delta = round(recent_avg - baseline, 2)
            pct = round(100 * delta / baseline, 1)
        shape = "n/a"
        if recent_avg is not None and prior_avg:
            r = recent_avg / prior_avg
            shape = "rising" if r > 1.07 else ("falling" if r < 0.93 else "plateau")
        first_over = None
        if baseline:
            for d, v in series:
                if v >= baseline * (1 + threshold / 100):
                    first_over = d
                    break
        out.append({
            "namespace": ns,
            "baseline_month": baseline_month,
            "baseline_usd_day": baseline,
            "recent_avg_usd_day": recent_avg,
            "prior5_avg_usd_day": prior_avg,
            "delta_usd_day": delta,
            "delta_pct": pct,
            "shape": shape,
            "first_day_over_threshold": first_over,
            "flagged": bool(pct is not None and pct >= threshold),
            "days_stored": len(rows),
        })
    conn.close()
    print(json.dumps(out, indent=2))


# ── context (cause recall) ───────────────────────────────────────────────

def cmd_context(args: argparse.Namespace) -> None:
    cfg = load_config()
    entry = next((n for n in cfg["namespaces"] if n["name"] == args.ns), None)
    keywords = [args.ns] + ([k.strip() for k in entry.get("keywords", [])] if entry else [])
    projects = entry.get("jira_projects", []) if entry else []
    until = args.until or date.today().isoformat()
    since = args.since or (date.fromisoformat(until) - timedelta(days=45)).isoformat()

    conn = open_db(INDEX_DB, readonly=True)
    kw_sql = " OR ".join(["(subject LIKE ? OR body LIKE ?)"] * len(keywords))
    params: list[str] = []
    for k in keywords:
        params += [f"%{k}%", f"%{k}%"]
    rows = conn.execute(
        f"""SELECT ts, source, subject, actor,
                   substr(replace(body, char(10), ' '), 1, 300) AS snippet, body
            FROM events
            WHERE ts >= ? AND ts <= ?
              AND source IN ('slack','jira','github','confluence','meeting')
              AND ({kw_sql})
            ORDER BY ts""",
        [since, until + "T23:59:59"] + params,
    ).fetchall()
    conn.close()

    hits = []
    for ts, source, subject, actor, snippet, body in rows:
        text = body or ""
        if NOISE_RE.search(text):
            continue
        strong = len(set(m.group(0).lower() for m in STRONG_RE.finditer(text)))
        weak = len(set(m.group(0).lower() for m in WEAK_RE.finditer(text)))
        if strong == 0 and weak < 2:
            continue
        if source == "jira" and projects and not any(p in (subject or "") for p in projects):
            continue
        hits.append({
            "date": ts[:10], "source": source, "subject": subject,
            "actor": actor, "snippet": snippet,
            "score": strong * 3 + min(weak, 3),
        })
    # collapse repeated subjects (threads ingest one row per message), keep max score
    seen: dict[str, dict] = {}
    for h in hits:
        key = h["subject"]
        if key not in seen:
            seen[key] = {**h, "mentions": 1}
        else:
            seen[key]["mentions"] += 1
            seen[key]["last_date"] = h["date"]
            seen[key]["score"] = max(seen[key]["score"], h["score"])
    ranked = sorted(seen.values(), key=lambda h: (-h["score"], h["date"]))[: args.limit]
    out = sorted(ranked, key=lambda h: h["date"])
    print(json.dumps({"namespace": args.ns, "since": since, "until": until,
                      "candidates": out}, indent=2))


# ── kubectl snapshots ────────────────────────────────────────────────────

def _kubectl(cfg: dict, *argv: str) -> dict:
    kube = cfg.get("kube") or {}
    if not kube.get("enabled") or not kube.get("context"):
        sys.exit("kube.enabled/context not set in ns_cost.yaml — snapshots disabled")
    cmd = ["kubectl", "--context", kube["context"], *argv, "-o", "json"]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        sys.exit(f"kubectl failed: {res.stderr.strip()[:300]}")
    return json.loads(res.stdout)


def cmd_snapshot(args: argparse.Namespace) -> None:
    cfg = load_config()
    names = args.ns or [n["name"] for n in cfg["namespaces"]]
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    written = []
    for ns in names:
        deps = _kubectl(cfg, "-n", ns, "get", "deploy")
        hpas = _kubectl(cfg, "-n", ns, "get", "hpa")
        snap = {"namespace": ns, "date": today, "deployments": [], "hpas": []}
        for d in deps.get("items", []):
            ctrs = d["spec"]["template"]["spec"]["containers"]
            snap["deployments"].append({
                "name": d["metadata"]["name"],
                "replicas": d["spec"].get("replicas"),
                "containers": [{
                    "name": c["name"],
                    "requests": (c.get("resources") or {}).get("requests", {}),
                } for c in ctrs],
            })
        for h in hpas.get("items", []):
            snap["hpas"].append({
                "name": h["metadata"]["name"],
                "min": h["spec"].get("minReplicas"),
                "max": h["spec"].get("maxReplicas"),
            })
        path = SNAP_DIR / f"{ns}-{today}.json"
        tmp = path.with_suffix(f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps(snap, indent=2))
        tmp.rename(path)
        written.append(str(path))
    print(json.dumps({"written": written}))


def cmd_snapdiff(args: argparse.Namespace) -> None:
    snaps = sorted(SNAP_DIR.glob(f"{args.ns}-*.json"))
    if len(snaps) < 2:
        sys.exit(f"need >=2 snapshots for {args.ns} under {SNAP_DIR} (have {len(snaps)})")
    old, new = (json.loads(p.read_text()) for p in snaps[-2:])

    def index(snap: dict, kind: str) -> dict:
        return {x["name"]: x for x in snap[kind]}

    changes = []
    for kind in ("deployments", "hpas"):
        o, n = index(old, kind), index(new, kind)
        for name in sorted(set(o) | set(n)):
            if name not in o:
                changes.append({"kind": kind, "name": name, "change": "added", "new": n[name]})
            elif name not in n:
                changes.append({"kind": kind, "name": name, "change": "removed"})
            elif o[name] != n[name]:
                changes.append({"kind": kind, "name": name, "change": "modified",
                                "old": o[name], "new": n[name]})
    print(json.dumps({"namespace": args.ns, "old": old["date"], "new": new["date"],
                      "changes": changes}, indent=2))


# ── main ─────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("store")
    s.add_argument("--file")
    s.add_argument("--db", help="override metrics DB (tests)")
    s.set_defaults(func=cmd_store)

    s = sub.add_parser("report")
    s.add_argument("--ns", action="append")
    s.add_argument("--asof")
    s.add_argument("--db", help="override metrics DB (tests)")
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("context")
    s.add_argument("--ns", required=True)
    s.add_argument("--since")
    s.add_argument("--until")
    s.add_argument("--limit", type=int, default=40)
    s.set_defaults(func=cmd_context)

    s = sub.add_parser("snapshot")
    s.add_argument("--ns", action="append")
    s.set_defaults(func=cmd_snapshot)

    s = sub.add_parser("snapdiff")
    s.add_argument("--ns", required=True)
    s.set_defaults(func=cmd_snapdiff)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
