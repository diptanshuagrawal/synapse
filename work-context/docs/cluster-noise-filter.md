# Cluster-input noise filter

**What this is:** exclude high-volume automation channels from the clustering
input so real engineering work resolves into fine workstreams instead of being
swamped by recurring-noise clusters.

**Nothing is deleted** — subjects stay in `events.db` + `embedding`; only the
clustering input is filtered.

## Why

Slack backfill tripled the corpus (~12k → ~38k subjects). ~94% is recurring
automation (alert/recon/digest/metrics bots in dedicated channels like
`example-recon`, `*_alerts`, `example-tracker`, `example-txn-alerts`).

At min-size 15 the raw corpus made 606 clusters — ~560 redundant noise clusters,
and the ~1,630 members of real work were squeezed into ~22 coarse buckets.

## How it decides

- Logic: `derive/cluster_noise_filter.py` + `config/cluster_exclude.yaml`.
- The 5-step decision ladder (force_include → force_exclude → protect_classes →
  measured ratio → name bootstrap) is documented in the
  **`config/cluster_exclude.yaml` header** — not repeated here.
- `refresh` snapshots the decision into the `cluster_excluded_channel` table, so
  exclusions stay stable after excluded subjects leave `topic_brief`.

**Measured on 2026-06-09 ground truth** (size-15 RECURRING labels):
24 channels excluded · 92% of noise removed · 18% of real lost · ~11.8k subjects
survive for clustering (from ~38.6k). Residual loss = genuinely-mixed alert
channels → `force_include` to rescue any.

## Automatic upkeep

`cluster_noise_filter.py refresh` runs as **Phase 4 of `/refresh-embeddings`**,
before every re-cluster. New automation channels are caught automatically
(measured ratio for labeled channels; `name_patterns` bootstraps new ones). No
re-labeling needed — just occasionally groom `force_include` / `force_exclude`.

## Tuning

All in `config/cluster_exclude.yaml`:
- raise `noise_ratio_threshold` → 0.95: keep more real, leak more noise.
- lower → 0.85: kill more noise, lose more real.
- add a channel id/name to `force_include` to keep its triage in clustering.

## One-time correction (make the size-15 layout obsolete)

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

# 3. apply the rebuild (DESTRUCTIVE: rewrites topic_brief; backup done in step 0)
.venv/bin/python derive/cluster_diff.py apply

# 4. dump the new+relabel clusters for labeling
.venv/bin/python derive/finalize_refresh.py dump
#   IMPORTANT: dump reads topic_brief_member, correct ONLY after step 3.

# 5. label (parallel): shard state/pending_cluster_finalize.json → fan-out
#    label agents using derive/cluster_finalize_rules.md →
#    merge to state/verdicts.cluster_finalize.json

# 6. apply labels
.venv/bin/python derive/finalize_refresh.py apply

# 7. sanity
.venv/bin/python derive/topic_brief_validate.py --json
```

Expected after step 2: far fewer total clusters, dominated by REAL workstreams,
at finer granularity than the 22 size-15 buckets.
