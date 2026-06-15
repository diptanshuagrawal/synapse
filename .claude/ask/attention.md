# /ask · attention — dispatch chunk

Loaded by the `/ask` router (Phase 3). Self-contained — the render template is
below; no narrative chunk needed.

```bash
.venv/bin/python derive/ask_engine.py window --since "<iso>" --until "<iso>" --participant "owner"
```

Filter the response: clusters with `status='ACTIVE'` AND (blockers_json non-empty OR root_cause non-null). For each:
- "Cluster {label}: {N} blockers — {1-line top blocker}"
- "Cluster {label}: root_cause = {...} — last touched {ts}"
Cap output at 10 items. Sort by `last_activity_ts` desc.

Length guidance: TL;DR is the worklist (each bullet states WHAT'S BLOCKED).
Detail section may be skipped if TL;DR fully covers.
