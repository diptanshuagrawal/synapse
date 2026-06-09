Validate the current `embedding` table. Runs the full sanity battery and reports cluster quality, near-duplicates, outliers, cross-source links, and random-anchor k-NN spot-checks. Use after every embedding batch (50, 500, 5k) before scaling further.

Owner-invoked. No routine fire. No state mutation — read-only over `embedding`.

## Usage — `/embed-validate [min-cluster-size=N] [dup-threshold=F] [out-path]`

If invoked with `help`, `-h`, or `--help` as the argument: print this Usage block verbatim and STOP — do not run anything.

**What it does:** Read-only validation of the `embedding` table — cluster quality, near-duplicates, outliers, cross-source links, and random-anchor k-NN spot-checks.

**Usage:** `/embed-validate [min-cluster-size=N] [dup-threshold=F] [out-path]`
- all optional — tuning overrides; defaults applied when omitted.

## Arguments

`$ARGUMENTS` (optional):

- `min-cluster-size=<N>` — HDBSCAN min cluster size. Default 2 (tight, good for small batches). Use 3+ for ≥500-subject runs.
- `dup-threshold=<F>` — cosine sim cutoff for near-dup pairs. Default 0.92. Drop to 0.85 to surface looser overlaps.
- `out=<path>` — also write report to this path. Default `/tmp/validate_<n>.txt` where `<n>` = current row count.

Example invocations:

```
/embed-validate
/embed-validate min-cluster-size=3 dup-threshold=0.90
/embed-validate out=/tmp/validate_full.txt min-cluster-size=4
```

## Phase 1 — Setup

```bash
cd $HOME/context/work-context
```

Parse `$ARGUMENTS` into shell vars `MIN_CLUSTER_SIZE`, `DUP_THRESHOLD`, `OUT_PATH`. Apply defaults when missing.

Quick row-count check — fail fast if table is empty:

```bash
.venv/bin/python -c "
import sqlite3
n = sqlite3.connect('index/events.db').execute('SELECT COUNT(*) FROM embedding').fetchone()[0]
print(f'embeddings: {n}')
assert n > 0, 'embedding table empty — run derive/embed_subjects.py first'
"
```

If empty: stop, recommend `.venv/bin/python derive/embed_subjects.py --subjects-file <file>` first.

If `OUT_PATH` unset, set it to `/tmp/validate_${N}.txt` where `N` = the count above.

## Phase 2 — Run wrapper

```bash
.venv/bin/python derive/validate_embeddings.py \
    --min-cluster-size "$MIN_CLUSTER_SIZE" \
    --dup-threshold "$DUP_THRESHOLD" \
    --out "$OUT_PATH"
```

The wrapper prints to stdout AND writes the report. Capture stdout in full — every printed subject carries a clickable URL (slack/jira/confluence/github).

## Phase 3 — Interpret + summarize

After the wrapper completes, surface a one-screen verdict. Read `OUT_PATH` and extract:

1. **Intra-source coherence** (section 1):
   - Healthy: 0.20 ≤ mean ≤ 0.55 per source.
   - `mean > 0.7` → embeddings collapsed (format-bias / boilerplate). Investigate before scaling.
   - `mean < 0.05` → embeddings carry no signal. Wrong model or empty content.

2. **Cluster count vs noise** (section 2):
   - Healthy: 30–60% of subjects in clusters, rest noise.
   - >80% noise → min-cluster-size too high, OR corpus genuinely heterogeneous.
   - <10% noise → min-cluster-size too low; everything's getting glued together.

3. **Cross-source clusters** (section 3): the prize. ≥1 cluster spanning ≥2 sources = embeddings learned topic, not format. Zero cross-source clusters = warning sign.

   **Diverse-surface ≠ bad cluster.** When members have different surface vocabulary (e.g. one talks about Redis, one about AWS, one about an service-e rollout) but share an *actionable role* (e.g. "ops alert → owner takes action"), that's the **stronger** signal — embedding learned semantic role, not lexical overlap. Do NOT downgrade such clusters. Score them GREEN.

   Conversely, a cluster where every member is from one source and shares high lexical overlap (same emoji header, same templated phrasing) is the **weaker** signal — could be format-clustering even after the prefix fix. Eyeball before trusting.

4. **Near-duplicate count** (section 4):
   - Many `sim=1.000` pairs all from one channel → empty-content embeddings leaked in (the stale-bot bug). Recommend running:
     ```bash
     .venv/bin/python derive/embed_subjects.py --purge-empty
     ```
   - Otherwise: 0.92–0.97 = re-filed tickets / reposts; 0.97–1.00 = true duplicates worth merging.

5. **Outliers** (section 5): least-connected subjects. Expected = bot pings, join-channel messages, short acks. Unexpected = content-extraction bug.

6. **Random-anchor neighbors** (section 6): pick one anchor, eyeball its top-5. They should be coherent *by role*, not necessarily by surface keywords. Example: an oncall-alert slack thread whose neighbors are a production-readiness Jira ticket + an alert-threshold-tuning thread + a deps-update request is **good** — all share "ops attention required" role. Reject only when neighbors look genuinely unrelated by any frame (topic OR role OR audience).

## Labeling-pass guidance (downstream)

When the LLM-label pass runs on these clusters, the prompt MUST be role-aware, not topic-aware. Ask:

- "What work is happening across these items?" → produces *"ops-alert handling + production-readiness actions"*
- NOT "What is the topic?" → produces *"Redis"* / *"AWS"* / *"service-e"* and splits a coherent cluster.

Save labels at the cluster level (`topic_brief.label`), not per-member.

## Phase 4 — Verdict block

Emit one of three verdicts at the end:

```
✓ GREEN — embeddings look healthy. Safe to scale to next batch size.
   intra-source coherence in band, ≥N cross-source clusters, dup count clean.

⚠ YELLOW — embeddings usable but flagged: <one-line reason>. Recommend:
   <one concrete action — e.g. "purge 13 empty-content rows; re-run">

✗ RED — embeddings unsafe to scale. Reason: <one-line>. Required fix:
   <one concrete action — e.g. "fix subject_content.py prefix bug, re-embed">
```

**Cross-source cluster scoring rule:** A cluster spanning ≥2 sources counts toward GREEN even when members share little surface vocabulary, *provided* eyeballing one anchor reveals a shared role/frame (ops alert, design discussion, release coordination, incident triage, etc). Do not downgrade such clusters to YELLOW for being "loose" — surface diversity is evidence the model captured role, not format.

Include a one-block summary table:

```
n_subjects:           <N>
clusters:             <C>   noise: <K>  (K/N = <pct>%)
cross-source:         <X>
near-dup pairs:       <D>   (above sim=<threshold>)
intra-source coherence:
   slack=<f>  jira=<f>  confluence=<f>  github=<f>
report saved:         <OUT_PATH>
```

## Phase 5 — Print clickable jump-points

For top-3 cross-source clusters, repeat the URLs in a "READ THESE" block so the owner can sanity-check one click away:

```
READ THESE to verify clustering quality:
  Cluster <id>: <subj1>  →  <url1>
                <subj2>  →  <url2>
  Cluster <id>: <subj1>  →  <url1>
                <subj2>  →  <url2>
  ...
```

## Hard constraints

- Read-only. NEVER write to `embedding` table from this skill — that's `embed_subjects.py`'s job.
- NO LLM calls. Wrapper is pure numpy/sklearn + sqlite reads.
- If `embed_subjects.py --purge-empty` is recommended, DO NOT run it from this skill — surface the recommendation, let owner decide.
- Wrapper output may be long (3–5 KB for 50 subjects, 30–50 KB for 5k). The report file is the canonical record; stdout is the live view.

## After write

Owner can re-check anytime:

```bash
.venv/bin/python derive/embedding_query.py stats
.venv/bin/python derive/embedding_query.py neighbors <subject> --k 10
.venv/bin/python derive/embedding_query.py similar --content "<query string>" --k 10
.venv/bin/python derive/embedding_query.py duplicates --threshold 0.90
.venv/bin/python derive/embedding_query.py outliers --k 5
```

These point-queries are the building blocks the wrapper composes; useful when chasing a specific finding from the report.
