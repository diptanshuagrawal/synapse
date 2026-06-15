# /ask · summarize — dispatch chunk

Loaded by the `/ask` router (Phase 3). Before rendering, ALSO Read
`.claude/ask/narrative-style.md` — output style, deep-read, and translation
rules live there.

```bash
.venv/bin/python derive/ask_engine.py search --query "<topic>" --k 30
```

Read JSON. Pick top 1–3 clusters by `hit_count`. Each cluster carries:
- `label`, `status` — for the section header
- `topic_brief.decisions_json` — bullet the decisions
- `topic_brief.blockers_json` — bullet the blockers (call out if `status='ACTIVE'`)
- `topic_brief.root_cause` — surface if non-null
- `topic_brief.participants_json` — top 3 contributors by `contribution_count`
- `top_subjects[]` — cite 3-5 with clickable URLs (use the URL conventions in `derive/validate_embeddings.py:subject_url`)
