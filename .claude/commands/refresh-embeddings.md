Incrementally refresh the embedding + clustering pipeline after new ingestion. Detects subjects added since the last embed, embeds only the delta, runs HDBSCAN, diffs new clusters against the labelled old ones by Jaccard overlap, preserves matching labels, and dumps only the changed clusters for chat-labeling.

Owner-invoked. Designed to keep the per-ingest cost bounded — no full re-label of the existing 300+ clusters.

## Usage — `/refresh-embeddings [apply] [min-cluster-size=N] [jaccard=F] [dry-run]`

If invoked with `help`, `-h`, or `--help` as the argument: print this Usage block verbatim and STOP — do not run anything.

**What it does:** Incrementally refreshes the embedding + clustering pipeline — embeds only new subjects, re-clusters, preserves matching labels by Jaccard overlap, dumps only changed clusters for chat-labeling.

**Steps:**
- Detect subjects added since the last embed.
- Embed only the delta (no full re-embed).
- HDBSCAN re-cluster.
- Jaccard-diff new clusters vs labelled old ones; preserve labels on matches.
- Dump only the changed clusters for chat-labeling.

**Usage:** `/refresh-embeddings [apply] [min-cluster-size=N] [jaccard=F] [dry-run]`
- all optional — defaults: plan-only (no apply), min-cluster-size=5, jaccard=0.8.

## When to use

After ANY of:

- `/slack-ingest` / `/slack-backfill` adds new threads.
- Jira / Confluence / GitHub cron picks up new tickets / pages / PRs.
- You manually edit `config/projects.yaml` keywords (no — that only affects rollup; doesn't touch embeddings).

NOT after:

- Pure rollup classification changes — they don't touch the embedding table.
- `/embed-validate` — read-only.

## Arguments

`$ARGUMENTS` (optional):

- `apply` — also run cluster_diff apply (mutates `topic_brief`). Default: plan only.
- `min-cluster-size=<N>` — HDBSCAN min cluster size. Default 5. Use 3 for very small corpora.
- `jaccard=<F>` — minimum overlap to preserve an old label. Default 0.8. Drop to 0.7 if too many "relabel" actions on essentially-stable clusters.
- `dry-run` — embed step prints what would be embedded; no API calls, no DB writes.

Examples:

```
/refresh-embeddings
/refresh-embeddings apply
/refresh-embeddings apply min-cluster-size=4 jaccard=0.75
/refresh-embeddings dry-run
```

## Phase 1 — Setup

```bash
cd $HOME/context/work-context
```

Parse `$ARGUMENTS` into shell vars: `APPLY` (bool), `MIN_CLUSTER_SIZE`, `JACCARD`, `DRY_RUN`. Defaults: `APPLY=false`, `MIN_CLUSTER_SIZE=5`, `JACCARD=0.8`, `DRY_RUN=false`.

## Phase 2 — Pre-flight status

Show the delta before doing anything expensive:

```bash
.venv/bin/python derive/refresh_embeddings.py status
```

Read the JSON. Surface:

- `n_corpus`, `n_existing`, `n_unchanged`, `n_new`, `n_drifted`
- `embed_required = n_new + n_drifted`

If `embed_required == 0`:

- Print "✓ nothing to embed — corpus and embedding table are in sync."
- Stop. No need to run refresh.

If `embed_required > 0`:

- Print one-line summary.
- Show `new_head` + `drifted_head` (first 5 of each) so owner eyeballs what's about to be embedded.

## Phase 3 — Estimate cost (one-line)

```bash
.venv/bin/python -c "
import sys; need = int(sys.argv[1])
# text-embedding-3-small = \$0.020 per 1M tokens. ~500 tok/subject avg.
est = need * 500 / 1_000_000 * 0.020
print(f'~\${est:.4f} estimated for {need} subjects (text-embedding-3-small, ~500 tok/subject)')
" "$EMBED_REQUIRED"
```

If estimate > $0.50, surface that the corpus delta is unusually large — confirm before continuing in non-dry-run mode.

## Phase 4 — Refresh the noise filter, then run refresh

First snapshot the cluster-input noise filter from the CURRENT (pre-refresh)
`topic_brief` labels. This decides which automation channels (alert/recon/
digest/metrics) are excluded from clustering so real work clusters cleanly.
`_fresh_clusters` reads this snapshot, so it MUST run before the re-cluster.

```bash
.venv/bin/python derive/cluster_noise_filter.py refresh
```

New automation channels are auto-caught (measured RECURRING-ratio ≥ threshold,
or name-pattern bootstrap for channels too new to measure) — see
`config/cluster_exclude.yaml`. Groom `force_include` / `force_exclude` there.

Then run the refresh. NOTE: with noise excluded, the real-work corpus is much
smaller, so use a SMALL `--min-cluster-size` (5–8, default 5) — the historical
15 was sized for the noise-inflated corpus.

```bash
.venv/bin/python derive/refresh_embeddings.py refresh \
    --min-cluster-size "$MIN_CLUSTER_SIZE" \
    --jaccard-threshold "$JACCARD" \
    ${DRY_RUN:+--dry-run} \
    ${APPLY:+--apply}
```

Capture stdout (JSON summary) and stderr (next-step hint).

The JSON has four blocks:

- `detect` — counts from Phase 2.
- `embed`  — `requested`, `skipped_unchanged`, `embedded`, `errors`, `elapsed_sec`.
- `diff_plan` — `n_old_clusters`, `n_new_clusters`, `summary: {preserve, relabel, new, dropped_old}`.
- `apply` — only present if `--apply` was passed.

## Phase 5 — Interpret + verdict

Healthy refresh:

- `embed.embedded` matches `embed_required` (or close — minus `skipped_no_content`).
- `embed.errors` is empty.
- `diff_plan.summary.preserve` >> `relabel + new`. Common shape: preserve=320, relabel=5, new=8.

Warning shapes:

- `relabel + new > 30` → corpus shifted enough that many old labels won't carry forward. Common when one large new channel was backfilled. Expect a non-trivial chat-labeling pass.
- `dropped_old > 20` → many old clusters fragmented or merged. Either OK (true drift) or a sign that `min-cluster-size` is too high; retry with one lower.
- `n_new_clusters < n_old_clusters - 50` → cluster merge happened wholesale. Eyeball one merged cluster (`derive/embedding_query.py neighbors <subj>`) before applying.

## Phase 6 — Hand off to single chat phase (finalize_refresh)

If `apply` was passed AND `summary.new + summary.relabel > 0`, the recommended
flow uses ONE combined chat phase instead of the older two-phase label + enrich
loop. `derive/finalize_refresh.py` produces a single dump per cluster covering
label + enrichment context, and a single verdicts file that fills all fields
(label + status + decisions + blockers + outcomes + followups + risk_areas +
root_cause + stakeholders + artifacts + participant_roles).

```bash
.venv/bin/python derive/finalize_refresh.py dump
# default scope = new+relabel from cluster_diff_plan.json
# override with --cluster-ids 12 45 67 if you want a subset
```

This writes:
- `state/pending_cluster_finalize.json` — combined dump
- `state/pending_cluster_finalize.json.rules.md` — combined rules

In chat:

1. Read `state/pending_cluster_finalize.json.rules.md` first.
2. Read `state/pending_cluster_finalize.json`.
3. Write `state/verdicts.cluster_finalize.json` (one combined entry per cluster
   with all fields). See rules.md for the JSON shape.
4. Run:

```bash
.venv/bin/python derive/finalize_refresh.py apply
```

The apply step:
- updates `topic_brief` with all chat-provided fields
- auto-stubs any `Recurring %` clusters via `derive/auto_recurring.py`
- **auto-links clusters to `projects.yaml` slugs via `derive/link_clusters_to_projects.py`** — populates `cluster_project_map` (deterministic; no LLM). Sources: jira_epic / confluence_page / subject_summary domain / keyword. One cluster may link to multiple projects.
- **re-derives per-cluster ownership via `derive/cluster_ownership_rollup.apply()`** — cluster membership just changed, so `owner_distribution_json` / `home_team_owned_pct` (read by `/ask` + `/retro`) would otherwise go stale until the next `/rollup`. Emits a `cluster_ownership` block in the apply summary. Idempotent; reads corrected subject-level ownership.
- refreshes `state/last_topic_brief_validate.json` so cron-status reflects

### Phase 6.5 — Project-mapping gap loop (owner-triage)

Each finalize apply emits a `cluster_project_link` block:

```json
"cluster_project_link": {
  "clusters_linked": 122,
  "clusters_unmapped": 56,
  "links_total": 250
}
```

`clusters_unmapped > 0` is **expected** — new workstreams in slack/jira have no `projects.yaml` slug yet. To groom:

```bash
.venv/bin/python derive/link_clusters_to_projects.py unmapped
# → prints unmapped cluster ids + labels + member counts, sorted by size
```

For each significant unmapped cluster, choose one of:

1. **Add new slug** to `config/projects.yaml` (kebab-case) with `keywords` + optional `jira_epics` + `confluence_pages`. Re-run `link_clusters_to_projects.py apply` to pick it up.
2. **Extend existing slug keywords** in `projects.yaml`. Same re-run path.
3. **Accept as unmapped** (cross-team ad-hoc, historical, sparse) — no action.

Loop is owner-driven; no automatic slug creation (LLM-driven slug synthesis is reserved for `/slug-epics` over Jira epics specifically).

Older two-phase flow (`label_clusters.py` + `enrich_clusters.py`) still works
for ad-hoc per-tier passes but is no longer the recommended path for refresh
cycles.

## Phase 7 — Verdict block + integrity gate

The topic_brief integrity cache is refreshed automatically by
`finalize_refresh.py apply` (or by `enrich_clusters.py apply` on the older
two-phase path) — feeds the cron-status PIPELINE block. If you ran apply
without using either, refresh manually:

```bash
.venv/bin/python derive/topic_brief_validate.py --json \
    > state/last_topic_brief_validate.json
```

Verdict shape:

```
n_corpus:        <N>
embedded:        <K>     errors: <E>
clusters_old:    <Co>
clusters_new:    <Cn>
preserved:       <P>     (label + enrichment copied as-is)
relabel:         <R>     (member set shifted; chat must relabel)
new:             <Nc>    (no old match; chat must label fresh)
dropped:         <Do>    (old clusters with no carry-forward)
integrity:       <FAIL_count> FAIL · <WARN_count> WARN (post-apply)
```

Strict verdict rules (apply in order):

1. **✗ RED — refresh incomplete** if `--apply` was passed AND `(R + Nc) > 0`
   AND `state/last_topic_brief_validate.json` shows `n_null_label > 0` OR
   `n_null_status > 0`. The label → enrich → validate loop has not closed.
   Required follow-up actions surfaced inline:

   ```
   .venv/bin/python derive/enrich_clusters.py dump --cluster-ids <new+relabel ids>
   # (chat writes state/verdicts.cluster_enrichments.json)
   .venv/bin/python derive/enrich_clusters.py apply
   .venv/bin/python derive/topic_brief_validate.py --json \
       > state/last_topic_brief_validate.json
   ```

2. **⚠ YELLOW — large drift** if `(R + Nc) / Cn > 0.10` regardless of
   integrity state. Sanity-check one relabeled cluster before bulk labelling.

3. **✓ GREEN — refresh clean** when:
   - `(R + Nc) == 0` (no chat work needed)  OR
   - integrity cache shows 0 FAIL findings (label + enrich loop closed).

   In either case, recurring-template clusters are auto-stubbed by
   `derive/auto_recurring.py` when label_clusters apply runs, so the chat
   batch should be just non-Recurring clusters.

## Hard constraints

- `--apply` is the only mutator. Without it, no `topic_brief` row changes.
- NO LLM API calls in any phase. OpenAI used only for embeddings.
- If the OpenAI key is missing, the orchestrator stops before any DB write and surfaces the key path.
- Skill is owner-invoked. Do NOT wire this into the cron — let the owner choose when to incur embedding spend + chat-label cost.

## After write

Sanity-check on next session:

```bash
.venv/bin/python derive/refresh_embeddings.py status
# embed_required should be 0
.venv/bin/python derive/embedding_query.py stats
.venv/bin/python -c "
import sqlite3; c = sqlite3.connect('index/events.db')
print('topic_brief rows:', c.execute('SELECT COUNT(*) FROM topic_brief').fetchone()[0])
print('with label:     ', c.execute('SELECT COUNT(*) FROM topic_brief WHERE label IS NOT NULL').fetchone()[0])
"
```
