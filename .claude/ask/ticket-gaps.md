# /ask · ticket_gaps — dispatch chunk

Loaded by the `/ask` router (Phase 3). Self-contained.

```bash
.venv/bin/python derive/ask_engine.py gaps --threshold 0.65
```

Add `--since`/`--until` only if owner specified a range. For each gap:
- "{slack URL}  →  cluster '{label}'  →  evidence: '{evidence_text}'"
- If `nearest_jira` is non-null AND similarity ≥ 0.55 (sub-threshold but interesting): "candidate dup: {jira} (sim={...})"

Length guidance: TL;DR + bullet list of gaps. No Detail section needed.
