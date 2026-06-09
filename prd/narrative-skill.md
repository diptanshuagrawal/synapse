# `/narrative` skill — detailed PRD

> ⚠️ **SUPERSEDED 2026-05-22.** The `/narrative` slash command was removed and
> folded into **`/ask person_range`** (sole entry point for per-person
> narratives). `.claude/commands/narrative.md` no longer exists. This doc is
> retained for design history; for current behaviour see `work-context/README.md`
> → "Per-person signals + retros" and `ARCHITECTURE.md` §5.8a–b. The
> `jira_metrics.py` module reference below is still accurate.

**Owner:** owner · **Status:** ~~Live (iterating)~~ → Superseded by `/ask person_range` · **Last revised:** 2026-05-13
**Parent:** [`PRD.md`](PRD.md) · **Skill source:** ~~`.claude/commands/narrative.md`~~ (removed) → `.claude/commands/ask.md` · **Module:** `$HOME/context/work-context/derive/jira_metrics.py`

> Companion: [`USER-JOURNEYS.md`](USER-JOURNEYS.md) — Journey 1 (1:1 prep), Journey 4 (perf-review draft), Journey 5 (cycle retro) all consume `/narrative` output.

---

## 1. Problem

An EM running a 7-person team needs a defensible per-person narrative to:

- Prep weekly 1:1s without re-reading every Jira ticket + PR.
- Draft cycle-end performance notes from evidence, not vibes.
- Spot drift early: a heavy reviewer suddenly going quiet, a top deliverer flipping to all-ops, a senior IC sliding into coordinator mode.

Manual synthesis is slow (~30 min / person / cycle) AND lossy: humans anchor on whoever was loudest in standup. Existing dashboards (Linear, Jellyfish) optimise for org-level rollups; they don't produce the **person × cycle × evidence-quoted** narrative an EM needs.

`/narrative` solves this by reading `events.db` + materialised views, computing a fixed set of attribution signals through `derive/jira_metrics.py`, and writing a markdown manager-note to `management/narratives/per-person/<handle>-<start>-to-<end>.md` (or `team/` for whole-team output).

---

## 2. Goals

- **G1.** One command produces one manager-note ≤ 5 minutes wall-clock.
- **G2.** Every claim in the note traces to a Phase 1 query result — no fabrication.
- **G3.** Attribution rules are single-sourced in `derive/jira_metrics.py`. Skills consume; never re-implement.
- **G4.** Narrative wraps numbers; numbers don't wrap narrative. Headlines are sentences, not tier-name shouts.
- **G5.** Output is durable in `management/narratives/` (referenced in 1:1 prep + trend comparison), not scratch.

## 3. Non-goals

- Not a dashboard. No charts; just markdown.
- Not real-time. Runs against the latest ingest cursor; ~30 min staleness is acceptable.
- Not a team-comparison tool. Per-person and whole-team views exist; cross-cycle trending is a separate skill (`/quarterly-retro`, future).
- Not a performance verdict. Surfaces signals; manager interprets.

---

## 4. User story

```
As an EM with 7 reports,
I want to type "/narrative <handle> <window>" and get a manager-note
that names what the person owns, where they shipped, what ops they
absorbed, and where the data is thin,
so I can run 1:1s and cycle reviews from evidence instead of memory.
```

**Acceptance tests:**

1. `/narrative` (no args) → trailing 30-day whole-team note at `management/narratives/team/<start>-to-<end>.md`.
2. `/narrative bob 60` → trailing 60-day single-person note at `management/narratives/per-person/bob-example-<start>-to-<end>.md`.
3. `/narrative "Alice Example"` → resolves name → canonical → trailing 30-day note.
4. `/narrative 2026-04-01 2026-05-01` → explicit range, whole team.
5. Every per-section claim cites the ticket / PR / page that backs it.
6. Sections with zero signal print `_None observed in window._` — never padded.
7. Output is recomputable: re-running same args overwrites previous file (idempotent).

---

## 5. Input parsing

First token determines scope:

| First token | Treated as | Second token |
|-------------|-----------|-------------|
| canonical / github / name in `config/people.yaml` | `HANDLE` | optional N days or date |
| integer N | `N days` (whole team) | nothing |
| ISO date | window start | window end |
| empty | trailing 30d whole team | nothing |

Window guardrails:
- `end < start` → error, stop.
- `window > 365` days → confirm with user before proceeding.
- `window < 7` days → confirm.

Date math:
- Explicit dates → `START = "<start>T00:00:00Z"`, `END = "<end>T23:59:59Z"`.
- Integer N → `END = now UTC`, `START = END - N days at 00:00:00Z`.

---

## 6. Module contracts (`derive/jira_metrics.py`)

**Single source of truth.** Skills (`/narrative`, `/retro`, future `/sprint`, `/quarterly-retro`) consume; never re-implement.

| Function | Returns | Owns rule |
|----------|---------|-----------|
| `load_people_lookup()` | `{alias → canonical}` | identity map from `config/people.yaml` |
| `get_aliases_for(canonical)` | `list[str]` | per-person alias expansion (github, email, jira_id, name, slack_handle) |
| `all_team_canonicals()` | `list[str]` | who counts as "the team" |
| `compute_done_credits(conn, start, end, people)` | `list[TicketCredit]` | **dedup** (one credit per ticket, latest Done) + **attribution chain** (changelog → creation_fallback → unknown) |
| `filter_credits_for(credits, canonical)` | `list[TicketCredit]` | scope credits to one person |
| `aggregate_velocity_by_actor(credits)` | `{canonical: {sp, tickets, by_source}}` | team SP ranking |
| `aggregate_velocity_by_sprint(credits)` | `{sprint_name: {sp, tickets, state}}` | per-sprint breakdown |
| `team_velocity_baseline(credits)` | `{median_sp, mean_sp, ranking}` | team-wide SP distribution for tier inference |
| `attribution_source_summary(credits)` | `{changelog: N, creation_fallback: N, unknown: N}` | data-quality caveat surfacing |
| `compute_pr_author_ownership(conn, aliases, start, end)` | `[{domain, person_authored_merged, team_merged, share_pct, label}]` | **status→Done EXCLUDED**; PR-author share only. Labels: OWNED ≥40% · DROVE ≥25% · CONTRIBUTED ≥1 PR <25% · JIRA_ONLY 0 PRs |
| `detect_ops_tickets(conn, aliases, start, end)` | `list[OpsTicket]` | title-regex against `OPS_PATTERNS` |
| `strip_epic_prefix(title)` | `str` | rendering helper |

**Adding a new metric:** add to module first, then skill calls it. NEVER inline new SQL or new regex in skill body — breaks the contract.

---

## 7. Phase 1 — raw signal queries (per person)

Sixteen queries / module calls. All scoped to `aliases IN (...)` and `ts BETWEEN start AND end`.

| Code | What | Source |
|------|------|--------|
| 1a | Activity counts by event_type | direct SQL |
| 1b | PRs authored (`pr_opened` actor) | direct SQL |
| 1c | PRs merged + cycle hours (open→merge) | direct SQL |
| 1d | Reviews given | direct SQL |
| 1e | Jira status_change events (transitions driven) | direct SQL — bucketed into `→ Done` vs other |
| 1f | Jira issues created | direct SQL |
| 1g | Jira comments | direct SQL |
| 1h | Confluence pages (created / updated) | direct SQL |
| 1i | Confluence comments | direct SQL |
| 1j | Domain breadth via `subject_summary.domains` | direct SQL |
| 1k | Risk-flagged PRs they authored | `subject_summary.risk_flags` |
| 1l | Working-hours pattern (UTC bins) | direct SQL — post-processed into weekend / after-hours |
| 1m | Cross-team reach (non-team `event_refs.ref_value`) | direct SQL |
| 1n | Anti-patterns (drive-by merges, stale PRs, re-work rate) | direct SQL |
| 1o | Per-domain ownership | **`compute_pr_author_ownership`** module call |
| 1p | TRD authorship | direct SQL on materialised `trd_owners` table |
| 1q | Tech-lead inference per domain | derived from 1d + 1p |
| 1r | Mentor signal proxy | derived from 1d (reviews on PRs by below-median team members) |
| 1s | Story points delivered | **`compute_done_credits` + `filter_credits_for`** module call |
| 1s-2 | Ticket-closer clerical signal | direct SQL (`→ Done` transitions) — kept separate from 1s |
| 1t | Sprint velocity | **`aggregate_velocity_by_sprint`** module call |
| 1u | Team velocity baseline + rank | **`aggregate_velocity_by_actor`** module call |
| 1v | Ops & incident response | **`detect_ops_tickets`** module call |

---

## 8. Phase 2 — narrative inference labels

Computed in this order; later labels can override earlier ones in headline construction:

1. **TRD AUTHORED** — `trd_owners.owner = canonical`. Show owner_score + margin over next contributor.
2. **OWNED domains** (1o, ≥40% share).
3. **TECH LEAD domains** (top-1 reviewer in domain ∧ ≥3 distinct PR authors reviewed ∧ TRD authored). All-of, not any-of.
4. **DROVE domains** (25-40% share, NOT owner).
5. **CONTRIBUTED domains** (touched, <25% share).
6. **Productivity tier** (from 1s + 1u — assigned-only, NOT transitioner):
   - `TOP-DELIVERER` — sp_attributed top-2 of team
   - `STEADY-DELIVERER` — sp_attributed ≥ team median
   - `LIGHT-DELIVERER` — sp_attributed < 50% team median (flag in headline)
   - `NON-IC-WINDOW` — 0 PRs authored + 0 sp_attributed
7. **TICKET-STEWARD signal** — high `transitioner_done` (≥10) AND low `sp_attributed` (< 30% team median). Standup-runner / EM-coordinator pattern. Dedicated bullet, NOT a productivity claim.
8. **OPS / INCIDENT RESPONSE signal** — ≥3 ops-pattern hits. Dedicated section with enumerated tickets. Per `feedback_people_summary_doneitems.md`, ops items get own bullets, never parenthetical.
9. **MENTOR signal** — qualitative bullet if non-trivial.

---

## 9. Phase 3 — output

### Paths

- Single person: `management/narratives/per-person/<canonical>-<START_DATE>-to-<END_DATE>.md`
- Whole team: `management/narratives/team/<START_DATE>-to-<END_DATE>.md`

### Whole-team file opening

Before per-person sections, the whole-team output MUST include:

```markdown
# Engineering Narrative — <START_DATE> to <END_DATE>

**Window:** <N> days · **Generated:** <YYYY-MM-DD HH:MM IST>
**Scope:** <N> team members from `config/people.yaml`

---

## Team overview

One short paragraph (3–5 sentences). What landed structurally this window. Which
domains saw the most movement and who drove them. Where the design / TRD work
concentrated. One sentence on cross-cutting risk.

---
```

Per-person sections follow. A `## Cross-team contributors` appendix follows the last
per-person section if any non-`people.yaml` actor has ≥20 events in window
(see §9.3 below).

### Per-person section structure

1. **Blockquote headline** (1-2 sentences) — strongest signal the data supports. See §10 for headline patterns.
2. **Domain ownership** — OWNED / DROVE / CONTRIBUTED / JIRA_ONLY bullets with evidence (PR numbers, ticket counts, share %).
3. **Shipping cadence** — PRs merged with cycle times, top-signal ship called out.
4. **Delivery velocity (assigned-and-shipped)** — SP delivered, productivity tier, attribution chain breakdown, per-sprint table, transitioner clerical signal kept separate.
5. **Ticket authoring** — Jira issues created in window.
6. **Design / docs participation** — TRDs owned / contributed, Confluence pages.
7. **Ops & incident response** — section only if ≥3 hits; otherwise `_None observed in window._`.
8. **Leadership signals** — TRD authorship, tech-lead labels, mentor signal.
9. **Working hours** — weekend + after-hours counts with UTC caveat.
10. **Open threads** — pending TRD response, stale PRs, sprint-active epics.
11. **Caveats** — surface data-quality issues (ingest gaps, terminal-state rule, etc).
12. **Activity (raw)** — event_type counts table as evidence.
13. **1:1 prep candidates** — actionable questions for next conversation.

### 9.3 Section ordering + cross-team appendix (whole-team output)

- **Section ordering**: strongest leadership signal first (TRD AUTHORED + OWNED ≥2 + TECH LEAD ≥1). Within tier, sort by total event volume descending.
- **Cross-team contributors appendix**: actors with ≥20 events in window but NOT in `config/people.yaml`. Format:

  ```markdown
  ## Cross-team contributors

  _Decide whether to add to `people.yaml` (own reporting line) or note as cross-team contributor._

  - **<github-handle>** — touched <domain>; <N> events (<commit_count> commits, <review_count> reviews).
  - …
  ```

---

## 10. Headline construction rules

The blockquote MUST surface the strongest signal. Patterns (use the strongest applicable):

- `Tech lead on <domain>. Authored the <X> TRD; drove <Y> design conversation. Delivered <N> SP this window (assigned-only).`
- `Top deliverer this window — <N> SP across <M> assigned tickets, leading the team. Owns <domain>.`
- `Steady deliverer on <domain> — <N> SP / <M> tickets assigned-and-shipped; consistent sprint pacing.`
- `Drove <domain> delivery — <N> PRs merged authored by them, <M> tickets shipped (<X> SP).`
- `Heavy reviewer for the team — <N> reviews given across <M> domains. Quality gate. Low IC delivery (<X> SP) by design.`
- `Ticket-steward this window — moved <N> tickets to Done (top in team) but assigned-and-shipped count is <X>. Coordinator pattern; confirm scope in 1:1.`
- `Ops/incident-response heavy — <N> incident / RCA / drill tickets authored or steered. <X> SP from feature work secondary.`
- `Onboarding ramp on <domain> — <N> reviews + <M> small PRs while learning the codebase. Delivery still ramping (<X> SP).`
- `Owns <domain>; pending TRD response is blocking forward motion. Delivery slowed: <X> SP vs <N>-SP prior-window baseline.`
- `Lighter authoring + delivery window vs baseline — <X> SP vs team median <Y>. Flag for 1:1.`
- `Mixed contributor — split across <domains>; no single ownership claim in this window. <X> SP assigned-and-shipped.`

**NEVER** use TOP-DELIVERER framing when `sp_attributed` is mostly transitioner-credit. Reserve "deliverer" framings for `sp_attributed` (assigned-only) signal exclusively.

**NEVER** write filler headlines ("did great work", "strong contributor"). Burns space, adds nothing.

---

## 11. Hard constraints

- **No fabrication.** Empty section → `_None observed in window._`, not padded prose.
- **All claims trace to Phase 1 query results.** No interpretation without evidence.
- **Reviews count goes in activity table ONLY.** Never `"X reviewed N PRs by Y"` in prose — only "heavy reviewer" / "quality gate" qualitative framings.
- **Significant ops items get own bullets** (DR drill, incident, deployment, on-call rotation) — never parenthetical inside another bullet.
- **Strip `[Epic EX-N]` prefixes** from rendered Jira titles.
- **Strip `Comment on ` prefix** from rendered Jira comment titles.
- **Inferred labels MUST show evidence** (OWNED / TECH LEAD / DESIGNED — show PR numbers + ticket count + share %).
- **Output paths are durable** (`management/narratives/`), never `management/drafts/` (drafts is scratch only).

---

## 12. Known caveats / open issues

### 12.1 `pr_opened` ingest gap — affects ownership share

GitHub ingest started after some PRs were opened, so `pr_opened` events are missing for PRs that already existed at ingest-start. `compute_pr_author_ownership` joins on `pr_opened` actor → undercounts ownership for affected PRs.

**Observed cases:**
- One dev's window: a `cash-withholding` domain reported `CONTRIBUTED (~17%)`; real share much higher from `pr_merged` actor count.
- Another dev's window: a `service-b-refactor` domain reported `JIRA_ONLY (0%)`; real share much higher from `pr_merged` actor count.

**Fix:** extend `compute_pr_author_ownership` to fall back to `pr_merged` actor when `pr_opened` missing. Alternatively, backfill `pr_opened` from github API for affected PRs.

### 12.2 Terminal-state rule narrow (handoff §6)

`compute_done_credits` filters via `title LIKE '%→ Done%'`. The EX Jira workflow has 25 status names including `Released`, `Released and Reviewed`, `Released with Emergency`, `Change Released 🧩`, `Pending Release`. Tickets that go via Released path are not credited.

**Three options** (pick one):
- **A** — Widen to `Done` + `Released*` + `Change Released*`. Risk: double-credit on `Pending Release → Done → Released`.
- **B** — Use latest status_change per ticket as terminal-if-Done; verify against current Jira state. Most accurate, slowest.
- **C** — Tight `Done` set + flag `Released` separately in narrative as "shipped to prod" vs "marked Done".

User has not picked yet; tracked in handoff `handoff-2026-05-12-2239.md` §6.

### 12.3 Review count inflation by self-review-comments

Raw `review` event count includes every review-comment event, including self-iteration on own PRs. One dev's window showed ~110 raw reviews but only ~10 were peer reviews — the rest were self-comments on their own PRs (concentrated on a handful of PRs).

**Fix:** module-level helper `compute_peer_review_count(conn, aliases, start, end)` that excludes reviews on PRs authored by aliases themselves. Plug into 1d output before tier inference.

### 12.4 Working-hours UTC raw

`hour_utc` bins assume IST (subtract 5:30 to get UTC). Hardcoded `hour_utc < 3 or > 14` as "outside 08:30–19:30 IST". Doesn't generalise for cross-timezone teams.

**Fix:** add `timezone` field to `config/people.yaml`; module computes per-person bins.

### 12.5 Ingest scope gaps (data, not code)

Some user-relevant work invisible to `events.db`:
- Slack threads (DR drill coordination, incident war-rooms) — Slack ingest not built; planning thread in progress.
- Opsgenie rotations + drill metadata — no ingest.
- Jira `labels` (`drill`, `p0`, `fy-end`) — not captured by ingest.
- Jira `priority` (P0 / P1 routing) — not captured.

These show up as `_None observed in window._` for the ops section when the work actually happened in Slack. Surface as caveat when ops claims may undercount.

---

## 13. Performance baseline

Measured wall-clock on a single-person 30-day window:

| Phase | Time |
|-------|------|
| Phase 0 / 1 setup + module debug | ~120s |
| Phase 1 raw query dump | ~30s |
| Phase 1 expansion queries (titles + reviews-by-author) | ~90s |
| Phase 2 drafting + Write | ~180s |
| **Total wall-clock** | **~7 min** |

Bottleneck: chat-driven narrative drafting (~180s), not SQL. Each query <100ms against ~44k events. Module debug round-trip cost ~60s because `get_aliases_for(canonical, people)` signature changed and skill stub had stale 2-arg call.

**Optimisations identified:**
- Cache `compute_done_credits(window)` once per session; reuse across team narratives (currently recomputed per person).
- Pre-compute `compute_peer_review_count` in module (kills self-review-comment inflation + the one-off filter dance).
- Module-signature contract test (`pytest test_jira_metrics_signatures.py`) — kills the debug-round-trip cost.

---

## 14. Roadmap

### Short-term (next 1-2 weeks)

- [ ] Fix `pr_opened` ingest gap in `compute_pr_author_ownership` (12.1) — fallback to `pr_merged` actor.
- [ ] Pick terminal-state rule (12.2 A / B / C) and apply.
- [ ] Add `compute_peer_review_count` to module (12.3).
- [ ] Add `timezone` to `config/people.yaml` (12.4).
- [ ] Module signature contract test (kills debug overhead).

### Medium-term (next 1 cycle)

- [ ] Per-window cache of `compute_done_credits` for whole-team runs.
- [ ] `person_narrative` cache table — hydrate so the same window doesn't recompute.
- [ ] `cycle_hours` column on `events` for direct cycle-time queries (no `julianday` per-row).
- [ ] Slack ingest landing → `detect_ops_tickets` extension to scan Slack threads, not just Jira titles. Unblocks DR-drill visibility (USER-JOURNEYS.md 🔵 markers).

### Long-term

- [ ] `/quarterly-retro` skill consuming same module + four `/narrative` outputs.
- [ ] `/boss-update` skill — same module, manager-of-EM scope.
- [ ] Trend comparison across windows (e.g., "Bob SP this cycle 34 vs prior cycle 28 → ↑21%").

---

## 15. Test plan

### Unit (`derive/jira_metrics.py`)

- `compute_done_credits` dedups EX-2356 (two Done transitions → one credit, latest ts) — regression test.
- `attribution_source_summary` reports correct chain split.
- `compute_pr_author_ownership` returns `JIRA_ONLY` when person has 0 merged PRs but ≥1 Jira touch in domain.
- `detect_ops_tickets` matches `OPS_PATTERNS` regex set; new patterns added there propagate to `/retro` + `/narrative` automatically.

### Integration

- `/narrative` smoke test: `alice 30` produces output file with all 13 sections; no fabricated claims; reviews stay in activity table.
- `/narrative` idempotence: re-running same args overwrites prior file; output stable except for `Generated:` timestamp.

### Data-quality assertions (run before narrative)

- `compute_done_credits` returns no negative SP.
- `attribution_source_summary['unknown']` < 5% of total credits → if higher, surface coverage caveat in headline.
- Every credit has either `sprint_name` set or `sprint_name = None` (no garbage).

---

## 16. References

- Skill source: `$HOME/context/.claude/commands/narrative.md` (definitive Phase 0-3 prose)
- Module: `$HOME/context/work-context/derive/jira_metrics.py`
- TRD-owners materialised view: `$HOME/context/work-context/derive/build_trd_owners.py`
- Memory entries (active):
  - `$HOME/.claude/projects/-Users-owner-context/memory/project_jira_metrics_module.md`
  - `$HOME/.claude/projects/-Users-owner-context/memory/feedback_people_summary_doneitems.md`
  - `$HOME/.claude/projects/-Users-owner-context/memory/feedback_people_summary_format.md`
- Live examples (most recent runs):
  - `management/narratives/per-person/alice-example-2026-04-12-to-2026-05-12.md` (30d)
  - `management/narratives/per-person/bob-example-2026-03-14-to-2026-05-13.md` (60d)
- Handoff trail: `work-context/handoff-2026-05-12-2239.md` §6 (terminal-state open thread)
