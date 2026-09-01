Namespace cost tracker: pull chargeback $/day for owned namespaces, trend vs baseline, and recall the WHY (capacity changes, CDC, incidents, migrations) from events.db with receipts. Owner-invoked. Read-only on every external source; never posts.

## Usage — `/ns-cost [namespace ...] [months=YYYY-MM,YYYY-MM] [--no-fetch] [--snapshot]`

If invoked with `help`, `-h`, or `--help`: print this Usage block verbatim and STOP.

**What it does:** answers "which of my namespaces got more expensive, when, and due to what" in one run. Three layers:
1. **Detect** — daily EKS cost per namespace from the chargeback Superset (fetched through the owner's logged-in browser; no headless auth exists).
2. **Trend** — weekday-avg vs baseline month, ramp shape, first day over threshold (deterministic: `derive/ns_cost.py report`).
3. **Explain** — candidate causes recalled from `index/events.db` (Slack/Jira/GitHub/Confluence/meetings) for the flagged window, classified by THIS session with receipts.

**Params:**
- `namespace ...` (optional) — subset; default = all namespaces in config.
- `months=` (optional) — billing periods to fetch; default = baseline month + current month.
- `--no-fetch` — skip the browser pull, run report+context on stored readings only.
- `--snapshot` — also capture/diff kubectl capacity snapshots (needs `kube.enabled` in config).

**Config:** `work-context/config/ns_cost.yaml` (gitignored; template `ns_cost.example.yaml`). ALL org identity (Superset host, dashboard UUID, slice ids, namespace names) comes from this config — never hardcode any of it in this skill or in outputs destined for the public repo.

## Phase 0 — Config + preconditions

```bash
cd $HOME/context/work-context && .venv/bin/python - <<'EOF'
import yaml; print(yaml.safe_load(open('config/ns_cost.yaml'))['superset']['base_url'])
EOF
```
If config missing → tell owner to copy the example, STOP. If `--no-fetch` → Phase 3.

## Phase 1 — Fetch chargeback data (browser, logged-in session)

Use Claude-in-Chrome (the user's real Chrome — it holds the Superset session). Open a tab on `superset.base_url`. If a login page appears, ask the owner to log in, wait, retry once.

**Fast path (API, preferred).** Via `javascript_tool` on that tab:
1. `GET {base}/api/v1/security/csrf_token/` → token (fetch with `credentials: 'include'`).
2. Resolve slice id if `slices.daily_by_namespace.id == 0`:
   `GET {base}/api/v1/chart/?q=(filters:!((col:slice_name,opr:eq,value:'<chart_name>')))` → take `result[0].id`, then WRITE it back into `ns_cost.yaml` so later runs skip this.
3. `GET {base}/api/v1/chart/{id}` → parse `result.query_context` (JSON string).
4. Patch `queries[0].filters` to:
   - `{col: columns.namespace, op: 'IN', val: [<namespaces>]}`
   - `{col: columns.billing_period, op: 'IN', val: [<months>]}`
   and set `result_format: 'json'`, generous `row_limit`.
5. `POST {base}/api/v1/chart/data` with `X-CSRFToken` header + patched query_context → rows of (day, namespace, cost).
6. **Async mode:** this Superset runs global async queries — the first POST may return `202` with a job id. Re-POST the SAME body with `force:false` every ~4s until `200` (the job lands in cache); ~10 tries max. Result rows are wide (one column per namespace, `usage_date` in epoch ms) — pivot to long form before Phase 2.

**Fallback (UI).** If query_context is null or the POST 4xxes: drive the dashboard (`{base}/superset/dashboard/{dashboard_uuid}/`) — set the Namespace native filter to the target namespaces, Billing Period to the months, Apply, then read the "Daily EKS cost trend by Namespace" chart via its View-as-table / read_page. Also grab `%idle` from the "EKS cost by namespace" table while there (session-level signal; not stored).

Trino behind this dashboard is flaky — on a transient "worker node" error, retry once after ~60s before giving up on a chart.

Normalize whatever you got to readings JSON and write it to the scratchpad:
```json
[{"namespace": "<ns>", "date": "YYYY-MM-DD", "usd": 12.34}, ...]
```

## Phase 2 — Store

```bash
cd $HOME/context/work-context
.venv/bin/python derive/ns_cost.py store --file <scratchpad>/ns_cost_readings.json
```
Idempotent upsert into `metrics_readings` (metric `ns-cost:<namespace>`, one row per namespace-day). Re-fetching a window is safe.

## Phase 3 — Trend report

```bash
.venv/bin/python derive/ns_cost.py report [--ns <ns> ...] [--asof YYYY-MM-DD]
```
Per namespace: baseline weekday avg, recent-5-weekday avg, `delta_usd_day`, `delta_pct`, `shape` (rising/plateau/falling), `first_day_over_threshold`, `flagged`. The asof/partial day is excluded automatically. Trust these numbers — do not recompute.

## Phase 4 — Cause recall + synthesis (flagged namespaces only)

For each flagged namespace:
```bash
.venv/bin/python derive/ns_cost.py context --ns <ns> --since <first_over - 14d>
```
Returns candidate events (keyword-matched + cause-signal-filtered, deduped by thread). YOU classify them — the script only recalls.

Synthesis rules:
- Attribute causes to one of: **requests raised / avg replicas up / new workloads (CDC, jobs) / sidecar / storage / unknown**. Every claimed cause needs a receipt (date + source + subject) from the candidates. No receipt → list under "unverified hypotheses", never as fact.
- The chargeback number is **EKS compute only** — RDS/MSK/Aurora changes are NOT in it; say so if a candidate suggests a DB cause.
- A gradual ramp ≈ traffic/HPA; a step ≈ a deploy/config change on that date — check candidates ±3 days around `first_day_over_threshold`.
- Read $/day, not %: small-base namespaces look dramatic in %.
- If candidates are empty: say the change is **undocumented in team channels** and point to the mechanical next hop (kubectl snapdiff / Grafana requests-vs-usage). Never invent a cause.

## Phase 5 (only with `--snapshot`) — Capacity snapshot + diff

```bash
.venv/bin/python derive/ns_cost.py snapshot [--ns <ns> ...]
.venv/bin/python derive/ns_cost.py snapdiff --ns <ns>     # needs >=2 snapshots
```
Snapshots (deploy requests/replicas + HPA min/max) accumulate in `state/ns_cost/`; the diff catches silent capacity changes between runs. Requires `kube.enabled: true` + explicit `kube.context` in config — never guess a context.

## Output (chat only)

Per namespace, one block:
- **Headline:** `<ns>: $B → $R/day (+$D, +P%), <shape> since <date>` — or "within threshold".
- **Why (with receipts):** 1-3 causes, each `cause — receipt (date, source, link/subject)`.
- **Unverified / gaps:** hypotheses without receipts; what would confirm (snapdiff, Grafana).
- **Suggested action:** rightsize / revert / justify-and-hold, one line.
End with a totals line across flagged namespaces ($/day and ~$/month).

## Hard rules

- Read-only everywhere: never edit the dashboard, never post to Slack/Jira; the only writes are metrics_readings, `state/ns_cost/`, and pinning resolved slice ids into `ns_cost.yaml`.
- Session-scoped auth: the fetch rides the owner's logged-in browser. If auth is missing, ask — do not attempt any credential entry.
- Public-repo hygiene: org hosts/UUIDs/namespace names stay in gitignored config; keep them out of anything committed.
