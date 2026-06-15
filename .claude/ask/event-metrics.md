# /ask · event_metrics — dispatch chunk

Loaded by the `/ask` router (Phase 3). Self-contained.

A deterministic COUNT/FREQUENCY over `events` — the route for "how many times did X
occur in <range>". Reaches automation channels that are excluded from clustering, so
it is the ONLY intent that can answer alert-frequency questions.

1. From the question, extract **keyword terms** (the thing being counted) and the
   **window** (Phase 2 resolves since/until). Optionally narrow with `--channel`
   (a named channel) and/or `--source slack` to drop unrelated code/PR matches.
2. Run:

   ```bash
   .venv/bin/python derive/ask_engine.py events \
       --terms "trial balance" mismatch --any \
       --since <ISO> --until <ISO> [--source slack] [--channel <name>]
   ```

   - Default AND-matches all terms; pass `--any` for OR (e.g. synonyms).
   - `--source slack` excludes GitHub/Jira text hits (a PR mentioning "mismatch" is
     not an alert firing). Use it for alert-frequency questions.
3. Render from the JSON:
   - Headline number: `total` occurrences over `distinct_days` days.
   - **per_channel** breakdown — name the channels (e.g. `cbs_accounting_alerts`),
     since one logical alert often fans across channels. Drop `channel: null` rows
     (non-slack) unless the question is cross-source.
   - **per_day** spikes — call out the peak day(s) if the distribution is lumpy.
   - Cite 2-3 `sample_citations` (ts + channel + snippet) as evidence.
4. Honesty: this counts MESSAGE occurrences matching the terms, not deduplicated
   incidents — say so ("179 alert messages across 22 days; a single incident can
   emit several"). If the terms are ambiguous, state which terms you matched and
   offer to narrow (specific channel, AND vs OR, dedupe by thread).

This intent does NOT save cluster/embedding artefacts — it is a direct query. Still
save the answer file per the router's Phase 5.
