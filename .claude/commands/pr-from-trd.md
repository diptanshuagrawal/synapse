Implement a feature in a real Go service from its code-grounded TRD + PRD, then
raise a PR. Pairs with `/trd-build`: that writes the TRD, this turns it into code.
Owner-invoked. Outward-facing — gates the push behind explicit confirmation.

## Usage — `/pr-from-trd <trd> <prd> <svc>`

If invoked with `help`, `-h`, or `--help`: print this Usage block verbatim and STOP.

**What it does:** Reads the TRD's concrete artifacts (DDL, endpoint contracts,
open questions) + the PRD's intent, implements the change in the service's REAL
dev repo by mirroring its existing sibling code, verifies with the repo's own
toolchain, then opens a PR.

**Args:**
- `<trd>` — path to a `/trd-build` TRD (e.g. `work-context/derived/trds/<slug>.md`).
- `<prd>` — local path OR Confluence URL (intent / acceptance criteria).
- `<svc>` — service alias (`service-a`, `service-b`). Maps to dev repo `$HOME/git/<svc>`.

**Default mode (owner-chosen):** FULL IMPLEMENTATION → READY-FOR-REVIEW PR →
new branch in `$HOME/git/<svc>`. These are aggressive on a live service, so the
hard rules below are non-negotiable.

## HARD RULES (safety — never skip)
1. **Repo target:** ONLY `$HOME/git/<svc>` (the human dev clone). NEVER the codegraph
   mirror at `~/.code-review-graph/repos/**` (it is reset --hard daily).
2. **Clean tree or abort:** if `$HOME/git/<svc>` has uncommitted changes or is not on
   a clean `main`, STOP and report — do not risk the user's WIP.
3. **No red PR:** the repo's build + unit tests MUST pass before any push. A
   failing build never becomes a PR.
4. **Push is gated:** even in ready-PR mode, STOP after verification and show the
   diffstat + test results + TODO list, and require an explicit "push" from the
   user before `git push` + `gh pr create`. This notifies 8 CODEOWNERS — never
   fire it unattended.
5. **No invention:** DDL is copied verbatim from the TRD artifacts. Handlers/
   protos MIRROR the named sibling — reuse existing interfaces, never fabricate a
   new posting/validation API. Anything the TRD left `(unknown)`/`(assign)`
   becomes a `// TODO(trd): …` with a matching PR-body checklist item.
6. **Never** force-push, never target `main` directly, never auto-merge.
7. **Read the flow before you build it:** ZenUML/image diagrams are NOT in the page
   API. If the PRD (or a Confluence-URL TRD) carries an unreadable diagram macro and no
   work browser is connected to read it (Step 1.5), STOP — implementing flow logic from a
   diagram you have not actually read would violate rule 5. Docs whose flow is all inline
   ` ```mermaid ` (already in the artifacts) or that carry no diagram are NOT gated.

## Steps

**1. Extract TRD artifacts (deterministic)**
```bash
cd $HOME/context/work-context && \
  python3 derive/trd/extract_trd_artifacts.py --trd <trd>
```
Read the resulting `<trd>.artifacts.json`: `ddl_blocks`, `endpoints`,
`open_questions`, `mermaid_blocks`, `todo_markers`. Read the full TRD too (§8.1
chosen approach, §8.6 logic). Materialize the PRD (Confluence → fetch via
Atlassian MCP) for intent + acceptance criteria.

`mermaid_blocks` only captures inline ` ```mermaid ` fences from the local TRD. The
**canonical flow often lives in a ZenUML "Diagram as Code Lite" macro or a PNG/image**
on the PRD (or a Confluence-URL TRD) — those are NOT in the page API. While fetching the
PRD body, note whether it contains such a diagram macro (the macro element is present in
the ADF/body even though its rendered content is not). If so, the **Step 1.5 Chrome
diagram pass** must read it before you implement the flow.

**1.5. Chrome diagram pass (read the canonical flow before building it)**

If Step 1 found NO ZenUML/image diagram macro on the PRD/TRD — flow is inline mermaid or
absent — SKIP this phase and proceed. Otherwise it is REQUIRED (rule 7):

1. `mcp__Claude_in_Chrome__list_connected_browsers`. If NONE is connected → **STOP**
   (rule 7): report that the PRD/TRD's flow lives in an unreadable diagram macro and no
   work browser is connected to read it, and ask the user to open the doc in their signed-in
   work browser and re-run. Do NOT infer the flow and do NOT proceed to implement it.
2. If >1 browser, pick the one signed into Confluence (your-org.atlassian.net) —
   `select_browser`; if ambiguous, ask. `tabs_context_mcp` → a tab.
3. For each doc with a diagram (the Confluence PRD always; the TRD too when `<trd>` was
   passed as a Confluence URL): `navigate` to the page, wait for load, expand collapsed
   diagram macros (click "Click here to expand…"), `screenshot` + zoom the rendered SVG/PNG
   **top-to-bottom**. Transcribe EVERY participant + message + opt/alt block in order, per
   the rigor rules in `.claude/commands/doc-sync.md` §3-sequence. An empty stub (heading-only,
   no diagram) → nothing to transcribe; note it and move on.
4. Reconcile the transcribed flow with the TRD §8.6 logic and `mermaid_blocks`. This
   transcription is an IMPLEMENTATION INPUT, not a drift finding — it tells you the exact
   step order, branch (opt/alt) conditions, and participant calls the handler must make.
   Where the diagram and the TRD prose disagree, treat it as a TRD `(unknown)` → `// TODO(trd)`
   + checklist item (rule 5); do not silently pick one.
5. Record which diagrams were actually read (vs. stub/empty) — surfaced at the Step 6
   checkpoint and in the PR body.

**2. Preflight the dev repo**
```bash
cd $HOME/git/<svc> && git fetch origin && git status --porcelain
```
If dirty → abort (rule 2). Else branch off latest main:
```bash
git checkout main && git pull --ff-only origin main && \
  git checkout -b feat/<slug>
```
`<slug>` = kebab-case of the TRD title.

**3. Locate the sibling to mirror (code-graph)**
Read the graph per `.claude/shared/code-graph-access.md` (mirror = REMOTE default branch,
not `~/git`). NB: that's the READ source; the WRITE target stays `$HOME/git/<svc>` per HARD
RULE 1. Use the graph to find the existing analog the TRD points at (e.g. the
`Execute*Transaction` family) and READ its real implementation so the new code
matches conventions:
- `semantic_search_nodes` / `query_graph callees_of` on the sibling rpc.
- Read the sibling proto (`proto/<area>/execute_*.proto`), handler
  (`internal/services/.../server.go`), domain/posting code it calls, and its test.
Do NOT bulk-read the repo — follow the sibling's call path only.

**4. Implement (mirror, don't invent)**
- **Migrations** — generate via the repo's own target so naming/timestamps are
  conventional, then fill SQL from the TRD `ddl_blocks` verbatim:
  `make generate-migration-pg NAME=<slug>` and/or `make generate-migration-mssql NAME=<slug>`.
- **Proto** — add one file per new RPC mirroring the sibling
  (`proto/deposits_transaction/execute_card_transaction.proto` ← `execute_instant-pay_transaction.proto`),
  fields from the TRD endpoint contract. Then `make generate-proto`.
- **Handler + domain logic** — add the sibling-method(s) in the same server file,
  delegating to the SAME domain/posting service the sibling uses. Implement the
  flow per TRD §8.6 **and the Step 1.5 diagram transcription** (step order, opt/alt
  branch conditions, participant calls) — they are the same flow at two altitudes; the
  diagram is authoritative on sequence and branching. For any branch the TRD marks unknown,
  or any diagram↔prose disagreement, emit `// TODO(trd)`.
- **Mocks/wiring** — `make generate-all` (mocks, swagger) and register routes the
  way siblings are registered.
- **Tests** — add unit + integration mirroring the sibling's test
  (`test/integration/depositstransactions/*`), covering the TRD's acceptance cases
  (debit, reversal-missing-original, closed-account, idempotency, atomicity).

**5. Verify (gate — rule 3)**

First bootstrap the service-a local toolchain (missing pieces are tools, not code —
install them, never skip verification):
- `buf` is NOT used — proto builds via `protoc` + the `protoc-gen-*` plugins (present).
- mocks: `mockery` **v3.5.0** (CI-pinned; `.mockery.yml` is v3 format — v2 panics on it):
  `go install github.com/vektra/mockery/v3@v3.5.0` → `make generate-mocks`.
- test runner: `go install gotest.tools/gotestsum@latest`.
- docker: Rancher Desktop (`~/.rd/bin`). Start it (`rdctl start`); for testcontainers export
  `DOCKER_HOST=unix://$HOME/.rd/docker.sock`, `TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE=$HOME/.rd/docker.sock`,
  `TESTCONTAINERS_RYUK_DISABLED=true`.
- custom pg image: `make build-postgres-image` (builds `service-a-postgres:partman`, required by the deposits suite).

Then:
```bash
cd $HOME/git/<svc> && make ready && make test-unit && make test-deposits-integration
```
All must pass. If a failure traces to a TRD `(unknown)`, STOP and report the gap
— do not paper over it. Fix conventional failures and re-run.

**Apple-Silicon caveat:** `make test-deposits-integration` boots a Postgres
container (works) AND an amd64 SQL Server container that segfaults (exit 139)
under qemu on arm64. On an Apple-Silicon Mac the MSSQL leg is **CI-only** (CI
runs amd64 linux). Locally, treat green build + `make test-unit` + the Postgres
leg as the bar; note in the PR body that the MSSQL integration leg is verified by
CI, not locally. Do NOT mutate the user's Rancher VM backend (qemu→vz/Rosetta)
without explicit consent.

**6. Commit + CHECKPOINT (rule 4)**
Commit with a conventional message (co-author trailer per repo norms). Then STOP
and present to the user:
- diffstat (`git diff --stat main`)
- test results summary
- which diagrams the Step 1.5 Chrome pass read (vs. stub/empty / none present)
- every `// TODO(trd)` + unresolved open question
- the PR title + body you will use
Wait for explicit "push" / "go".

**7. Push + open the PR**
```bash
git push -u origin feat/<slug>
gh pr create --repo example-org/<svc> --base main --head feat/<slug> \
  --title "<title>" --body-file <pr-body.md>
```

**Auth fallback (gh fails / 404 on the repo).** The `git push` uses the SSH key
(work access); `gh` uses its own OAuth token, which may be a personal account
with NO API access to the `example-org` org (symptom: `gh pr create` →
`Could not resolve to a Repository` or `gh api repos/<org>/<svc>` → 404).
When that happens, fall back to the ingestion PAT at `~/.secrets/github_pat`
(the same token the github ingestion uses) — it has `repo` scope + org SSO:
```bash
# only after the gh-default attempt fails with a 404/resolve error:
GH_TOKEN="$(cat ~/.secrets/github_pat)" gh pr create \
  --repo example-org/<svc> --base main --head feat/<slug> \
  --title "<title>" --body-file <pr-body.md>
```
Rules for the PAT: read it ONLY from `~/.secrets/github_pat`, NEVER print or log
its value, use it ONLY for this single `gh pr create` (don't persist it to gh
config or env beyond the one command). If the file is absent, stop and give the
user the compare URL (`https://github.com/example-org/<svc>/compare/main...feat/<slug>?expand=1`)
to open the PR in-browser instead.

PR body MUST contain, in order:
- ⚠️ banner: "Auto-generated from TRD by /pr-from-trd — review carefully,
  especially ledger/posting logic."
- Links: PRD + TRD.
- Summary (from PRD intent) + what changed (migrations / proto / handler / tests).
- **Flow source** — which diagram(s) the Step 1.5 Chrome pass read to ground the handler
  flow (or "flow from inline mermaid / TRD prose — no diagram macro present"), so reviewers
  know the sequence logic traces to the canonical diagram.
- **Open items checklist** — every TRD open question + every `// TODO(trd)`,
  as unchecked boxes so reviewers see exactly what's unverified.
- Test evidence (commands run + pass/fail).
Reviewers come from CODEOWNERS automatically; do not @-spam.

**8. Print summary**
branch · PR url · files changed · test status · count of TODO(trd) + open items.

## Permission posture (if ever run unattended)
Reads/writes under `$HOME/git/<svc>/**`, `$HOME/context/**`,
`/tmp/**`; Bash `make *`, `git *` (NO force-push), `gh *`, `python3 *`; reading
`~/.secrets/github_pat` for the step-7 auth fallback (value never printed);
Atlassian + code-review-graph + Claude-in-Chrome MCP (Step 1.5 diagram pass) — pre-approved. The push CHECKPOINT (rule 4) still
applies: if no human is present to confirm, STOP at step 6 and leave the branch
local. Never open a PR without confirmation.
