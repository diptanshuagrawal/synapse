"""Apply chat-session-emitted narratives to person_narrative cache.

Expected input schema (list of dicts):
  {
    actor, content_hash, window_days, body
  }
content_hash MUST echo dump_pending_narrative.py value.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import llm_classifier as lc       # noqa: E402
import narrative as nv            # noqa: E402

DB = lc.ROOT / "index" / "events.db"
DERIVED_NARRATIVES = lc.ROOT / "derived" / "narratives"
MODEL_TAG = "claude-opus-4-7[1m]-chat"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--pending", required=True,
                    help="pending JSON path (used to validate hashes)")
    args = ap.parse_args()

    conn = sqlite3.connect(str(DB))
    nv.ensure_schema(conn)

    pending_list = json.loads(Path(args.pending).read_text())
    pending = {p["actor"]: p for p in pending_list}
    narratives = json.loads(Path(args.inp).read_text())

    final: list[dict] = []
    written = 0
    for n in narratives:
        actor = n.get("actor")
        h = n.get("content_hash")
        body = (n.get("body") or "").strip()
        if not actor or not h or not body:
            print(f"  WARN missing actor/hash/body in {n}", file=sys.stderr)
            continue
        p = pending.get(actor)
        if p is None:
            print(f"  WARN {actor} not in pending — skip", file=sys.stderr)
            continue
        if p.get("content_hash") != h:
            print(f"  WARN {actor}: content_hash mismatch (pending={p['content_hash']} verdict={h})", file=sys.stderr)
            continue
        window_days = int(n.get("window_days") or p.get("window_days"))
        nv.persist(conn, actor, window_days, h, body, source="claude-chat",
                   model=MODEL_TAG)
        written += 1
        final.append({
            "actor": actor,
            "name": p.get("name", actor),
            "window_days": window_days,
            "content_hash": h,
            "body": body,
        })
        print(f"+ {actor}  window={window_days}d  hash={h}")

    DERIVED_NARRATIVES.mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timezone
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (DERIVED_NARRATIVES / "latest.json").write_text(json.dumps(final, indent=2))
    (DERIVED_NARRATIVES / f"{stamp}.json").write_text(json.dumps(final, indent=2))
    print(f"apply_narratives: wrote {written} rows + final → {DERIVED_NARRATIVES}/latest.json")


if __name__ == "__main__":
    main()
