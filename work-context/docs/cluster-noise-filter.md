# Cluster-input noise filter

## Problem it solves

The Slack backfill tripled the corpus (~12k → ~38k subjects). ~94% of that is
recurring **automation** — alert/recon/digest/metrics bots posting into
dedicated channels (`example-recon`, `*_alerts`, `example-tracker`,
`example-txn-alerts`, …). Clustering the raw corpus at min-size 15 produced 606
clusters of which ~560 were redundant recurring-noise clusters; the ~1,630
members of real engineering work were squeezed into ~22 coarse buckets.

Fix: exclude automation channels from the **clustering input only** (subjects
stay in `events.db` + `embedding` — nothing is deleted), then cluster the
remaining real work at a small min-cluster-size so workstreams resolve finely.

## How the decision is made

`derive/cluster_noise_filter.py` + `config/cluster_exclude.yaml`. Per channel,
in order:

1. `force_include` → never excluded (owner override; rescue a triage channel).
2. `force_exclude` → always excluded (owner override).
3. `protect_classes` (team / cross-team / working-group) → never auto-excluded.
4. **Measured ratio** — if the channel has ≥ `min_subjects_for_ratio` (20)
   labeled subjects in `topic_brief`, exclude iff its RECURRING-share ≥
   `noise_ratio_threshold` (0.90). This is the authority for data-rich channels.
5. **Name bootstrap** — channels with too little data are excluded iff their
   name matches `name_patterns` (catches brand-new alert channels pre-labeling).

The decision is snapshotted into the `cluster_excluded_channel` table by
`refresh`, so exclusions stay stable after excluded subjects leave `topic_brief`.

Measured on the 2026-06-09 ground truth (size-15 RECURRING labels):
**24 channels excluded · 92% of noise removed · 18% of real lost** (the residual
loss is genuinely-mixed alert channels — `force_include` to rescue any).
~11.8k subjects survive for clustering (from ~38.6k).

## Automatic for future runs

`cluster_noise_filter.py refresh` is now Phase 4 of `/refresh-embeddings`, run
before every re-cluster. So new automation channels are caught automatically:
the measured ratio handles labeled channels; `name_patterns` bootstraps new
ones. No re-labeling needed to maintain the filter — just occasionally groom
`force_include` / `force_exclude`.

## Run it (one-time correction — make the size-15 layout obsolete)

```bash
cd $HOME/context/work-context

# 0. (safety) back up the DB
cp index/events.db "index/events.db.bak-pre-noisefilter-$(date +%Y%m%d%H%M)"

# 1. snapshot the exclusion set from current labels
.venv/bin/python derive/cluster_noise_filter.py refresh
.venv/bin/python derive/cluster_noise_filter.py status   # eyeball excluded channels

# 2. re-cluster the filtered corpus at a SMALL min size, diff vs current
.venv/bin/python derive/cluster_diff.py plan --min-cluster-size 6
#   inspect state/cluster_diff_plan.json summary (preserve/relabel/new/dropped)

# 3. apply the rebuild (DESTRUCTIVE: rewrites topic_brief; back up done in step 0)
.venv/bin/python derive/cluster_diff.py apply

# 4. dump the new+relabel clusters for labeling
.venv/bin/python derive/finalize_refresh.py dump
#   IMPORTANT: dump reads topic_brief_member, which is correct ONLY after step 3.

# 5. label (parallel workflow recommended — see this session's transcript):
#    shard state/pending_cluster_finalize.json → fan-out label agents using
#    derive/cluster_finalize_rules.md → merge to state/verdicts.cluster_finalize.json

# 6. apply labels
.venv/bin/python derive/finalize_refresh.py apply

# 7. sanity
.venv/bin/python derive/topic_brief_validate.py --json
```

Expected after step 2: far fewer total clusters, dominated by REAL workstreams
(not recurring noise), at finer granularity than the 22 size-15 buckets.

## Tuning

All in `config/cluster_exclude.yaml`:
- raise `noise_ratio_threshold` toward 0.95 → keep more real, leak more noise.
- lower toward 0.85 → kill more noise, lose more real.
- add a channel id/name to `force_include` to keep its triage in clustering.
