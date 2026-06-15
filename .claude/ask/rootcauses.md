# /ask · rootcauses — dispatch chunk

Loaded by the `/ask` router (Phase 3). Before rendering, ALSO Read
`.claude/ask/narrative-style.md` — deep-read + translation rules apply to the
Detail prose.

```bash
.venv/bin/python derive/ask_engine.py rootcauses --since "<iso>" --until "<iso>"
```

Render as a categorised table:
- group by source domain heuristically from cluster.label (DB / Performance / Migration / etc.)
- per row: cluster_id | label | root_cause | member_count | last_activity_ts

Length guidance: prose categorised by domain in Detail; cite tickets inline.
