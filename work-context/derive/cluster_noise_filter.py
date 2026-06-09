"""Cluster-input noise filter.

Decides which Slack channels are AUTOMATION (alert/recon/digest/metrics bots)
and excludes their subjects from embedding clustering, so real engineering work
clusters cleanly at a small min-cluster-size instead of being swamped by
hundreds of recurring digest clusters.

See config/cluster_exclude.yaml for the decision rules. The decision is
snapshotted into the `cluster_excluded_channel` table by `refresh` (run AFTER a
labeling pass, BEFORE the next re-cluster); `_fresh_clusters` in cluster_diff.py
then reads `excluded_subjects()` to drop those rows from the clustering input.

CLI
---
    .venv/bin/python derive/cluster_noise_filter.py refresh   # recompute + snapshot table
    .venv/bin/python derive/cluster_noise_filter.py status    # show current snapshot + impact
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from ingest.common import get_db  # noqa: E402

CONFIG_PATH = _PKG_ROOT / "config" / "cluster_exclude.yaml"
SLACK_CHANNELS_PATH = _PKG_ROOT / "config" / "slack_channels.yaml"


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def load_config() -> dict:
    cfg = yaml.safe_load(CONFIG_PATH.read_text()) or {}
    cfg.setdefault("noise_ratio_threshold", 0.90)
    cfg.setdefault("min_subjects_for_ratio", 20)
    cfg.setdefault("protect_classes", ["team", "cross-team", "working-group"])
    cfg.setdefault("name_patterns", [])
    cfg.setdefault("force_include", [])
    cfg.setdefault("force_exclude", [])
    return cfg


def _channel_meta() -> dict[str, dict]:
    if not SLACK_CHANNELS_PATH.exists():
        return {}
    y = yaml.safe_load(SLACK_CHANNELS_PATH.read_text()) or {}
    out = {}
    for ch in y.get("channels", []):
        out[ch.get("id")] = ch
    return out


def _ensure_table(conn) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS cluster_excluded_channel (
               channel_id  TEXT PRIMARY KEY,
               name        TEXT,
               reason      TEXT,      -- force_exclude | ratio | name-bootstrap
               noise       INTEGER,
               real        INTEGER,
               ratio       REAL,
               decided_at  TEXT
           )"""
    )


def compute_excluded(conn) -> dict[str, dict]:
    """Decide the exclusion set from CURRENT topic_brief labels + config.

    Returns {channel_id: {name, reason, noise, real, ratio}}.
    """
    cfg = load_config()
    meta = _channel_meta()
    th = float(cfg["noise_ratio_threshold"])
    min_n = int(cfg["min_subjects_for_ratio"])
    protect = set(cfg["protect_classes"])
    name_re = (
        re.compile("|".join(re.escape(p) for p in cfg["name_patterns"]), re.I)
        if cfg["name_patterns"] else None
    )
    force_inc = {str(x) for x in cfg["force_include"]}
    force_exc = {str(x) for x in cfg["force_exclude"]}

    # name <-> id resolution for the override lists
    name_to_id = {(m.get("name") or ""): cid for cid, m in meta.items()}

    def in_overrides(cid: str, name: str, overrides: set) -> bool:
        return cid in overrides or name in overrides

    # per-channel RECURRING-share from current topic_brief, counting each member
    # subject ONCE (a subject has many event rows — join straight to events would
    # multiply the tally and skew the ratio).
    rows = conn.execute(
        """SELECT ch,
                  SUM(noise) AS noise,
                  SUM(real)  AS real
             FROM (
               SELECT m.subject,
                      (SELECT e.channel_id FROM events e
                         WHERE e.subject = m.subject AND e.channel_id IS NOT NULL
                         LIMIT 1) AS ch,
                      CASE WHEN t.status = 'RECURRING' THEN 1 ELSE 0 END AS noise,
                      CASE WHEN t.status != 'RECURRING' THEN 1 ELSE 0 END AS real
                 FROM topic_brief_member m
                 JOIN topic_brief t ON t.cluster_id = m.cluster_id
             )
            WHERE ch IS NOT NULL
            GROUP BY ch"""
    ).fetchall()
    tally = {r[0]: (r[1] or 0, r[2] or 0) for r in rows}

    # also consider channels that have events but no labeled members (new channels)
    for cid, _m in meta.items():
        tally.setdefault(cid, (0, 0))

    out: dict[str, dict] = {}
    for cid, (noise, real) in tally.items():
        if not cid:
            continue
        name = (meta.get(cid, {}).get("name") or cid)
        cls = meta.get(cid, {}).get("class", "")
        tot = noise + real
        ratio = noise / tot if tot else 0.0

        if in_overrides(cid, name, force_inc):
            continue  # never exclude
        if in_overrides(cid, name, force_exc):
            out[cid] = {"name": name, "reason": "force_exclude", "noise": noise, "real": real, "ratio": round(ratio, 3)}
            continue
        if cls in protect:
            continue
        if tot >= min_n:
            if ratio >= th:
                out[cid] = {"name": name, "reason": "ratio", "noise": noise, "real": real, "ratio": round(ratio, 3)}
        else:
            if name_re and name_re.search(name):
                out[cid] = {"name": name, "reason": "name-bootstrap", "noise": noise, "real": real, "ratio": round(ratio, 3)}
    return out


def refresh(conn) -> dict:
    _ensure_table(conn)
    decided = compute_excluded(conn)
    now = _now_iso()
    conn.execute("DELETE FROM cluster_excluded_channel")
    for cid, d in decided.items():
        conn.execute(
            "INSERT INTO cluster_excluded_channel (channel_id, name, reason, noise, real, ratio, decided_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (cid, d["name"], d["reason"], d["noise"], d["real"], d["ratio"], now),
        )
    conn.commit()
    return decided


def excluded_channel_ids(conn) -> set[str]:
    """Read the snapshot table; fall back to a live compute if it is empty."""
    _ensure_table(conn)
    rows = conn.execute("SELECT channel_id FROM cluster_excluded_channel").fetchall()
    if rows:
        return {r[0] for r in rows}
    return set(compute_excluded(conn).keys())


def excluded_subjects(conn) -> set[str]:
    """Slack subjects whose channel is in the exclusion set. Non-slack subjects
    are never excluded."""
    ch_ids = excluded_channel_ids(conn)
    if not ch_ids:
        return set()
    ph = ",".join("?" * len(ch_ids))
    rows = conn.execute(
        f"SELECT DISTINCT subject FROM events WHERE channel_id IN ({ph})", list(ch_ids)
    ).fetchall()
    return {r[0] for r in rows}


def cmd_refresh(_args) -> int:
    conn = get_db()
    decided = refresh(conn)
    excl_subj = excluded_subjects(conn)
    noise = sum(d["noise"] for d in decided.values())
    real = sum(d["real"] for d in decided.values())
    print(json.dumps({
        "excluded_channels": len(decided),
        "labeled_noise_in_excluded": noise,
        "labeled_real_in_excluded": real,
        "excluded_subjects_total": len(excl_subj),
        "by_reason": {
            r: sum(1 for d in decided.values() if d["reason"] == r)
            for r in ("force_exclude", "ratio", "name-bootstrap")
        },
    }, indent=2))
    print("\nExcluded channels (name | reason | noise/real | ratio):", file=sys.stderr)
    for cid, d in sorted(decided.items(), key=lambda kv: -kv[1]["noise"]):
        print(f"  {d['name']:36s} {d['reason']:14s} {d['noise']:5d}/{d['real']:<4d} {d['ratio']}", file=sys.stderr)
    return 0


def cmd_status(_args) -> int:
    conn = get_db()
    _ensure_table(conn)
    rows = conn.execute(
        "SELECT channel_id, name, reason, noise, real, ratio, decided_at "
        "FROM cluster_excluded_channel ORDER BY noise DESC"
    ).fetchall()
    if not rows:
        print(json.dumps({"snapshot": "EMPTY — run `refresh` first"}, indent=2))
        return 0
    excl_subj = excluded_subjects(conn)
    print(json.dumps({
        "excluded_channels": len(rows),
        "excluded_subjects_total": len(excl_subj),
        "decided_at": rows[0][6],
    }, indent=2))
    print("\nchannel | reason | noise/real | ratio", file=sys.stderr)
    for r in rows:
        print(f"  {r[1]:36s} {r[2]:14s} {r[3]:5d}/{r[4]:<4d} {r[5]}", file=sys.stderr)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Cluster-input noise filter.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("refresh", help="Recompute exclusion set + snapshot table")
    sub.add_parser("status", help="Show current snapshot + impact")
    args = ap.parse_args()
    if args.cmd == "refresh":
        return cmd_refresh(args)
    if args.cmd == "status":
        return cmd_status(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
