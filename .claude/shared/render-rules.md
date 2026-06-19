# Shared render rules — links, citations, item descriptions

Loaded by skills that render owner-facing digests/narratives (`/standup`, `/ask`).
These are the GENERIC rules common to every such output. Skill-specific machinery
(e.g. standup's `# PR INDEX` / gather `link=` field, `/ask`'s pipeline-jargon
grep-check) stays in the skill that owns it — this file holds only what's shared.

## 1. URL conventions (use as inline markdown links)

- **slack** `CH:ts` → `[label](https://example.slack.com/archives/{CH}/p{ts_no_dot})`
- **Jira** `EX-NNNN` → `[EX-NNNN](https://your-org.atlassian.net/browse/{EX-NNNN})`
- **Confluence** `page:NNNN` → use the REAL link, NOT `/wiki/pages/{NNNN}` (that 404s).
  Take `_links.base + _links.webui` from a Confluence search/get
  (`…/wiki/spaces/<KEY>/pages/{NNNN}/<slug>`) or the short `_links.base + _links.tinyui`
  (`…/wiki/x/<tiny>`). Fetch the page's webui link before emitting — the space KEY
  varies per page (PROD, …); don't assume one.
  - Deep-link a SECTION by appending a heading anchor: take the heading's visible
    text, replace every space with a hyphen, keep numbers/periods/case. E.g.
    "4. Hook Fire Order" → `#4.-Hook-Fire-Order`, "3.1 charge_attempts" →
    `#3.1-charge_attempts`. The API doesn't expose heading ids — build from the text.
- **GitHub PR** `owner/repo#N` → `[#N description](https://github.com/{owner/repo}/pull/{N})`

## 2. Never a bare ID — describe, then link

- Lead every item with a plain-English phrase of WHAT it is (reworded from the real
  summary — for Jira, the `issue_created` title, NOT a `status_change` transition
  string), then the ID as a trailing clickable link.
- **EVERY link carries a plain-English descriptor — PRs included, not just tickets.**
  A bare `[#850]` is a regression; so is a terse title-slug like `[#850 order-type
  check]` — still unreadable in the Slack render. Describe the PR the way you'd
  describe a ticket: a short clause saying what it changes, reworded from the PR
  **title + body**, not a slug. Examples — `[#865](url) adds CIB & RIB
  internet-banking channel types to the product enum`, `[#867](url) CI gate that
  pushes images to ECR on non-prod builds`.

## 3. Thread references must be SELF-SUMMARIZING — the link is proof, not the content

A reader must understand the item WITHOUT clicking. Any line that points at a slack
thread (blockers especially, but also asks, decisions, ops/prod-watch, up-next
commitments) must state, in plain words: (a) WHAT the issue/ask actually is, and
(b) WHY it matters here — what's blocked, who's being chased + their latest response,
or what decision is owed. A one-line preview is rarely enough; OPEN the thread
(`slack_read_thread` on the row's ch + ts) and distil one clause of real context.
The link still goes LAST; it's evidence, never a substitute for the summary.

✘ "Recurring reporting-impacting issue — chasing the infra owner, no response yet ([thread])"
✔ "Recurring data lag in the `account_balance` table (a TB-diff mismatch) is skewing
reporting; escalated to the infra owner, who ack'd ('delayed by a resource issue') with
no fix ETA ([thread])".

## 4. Pre-save link check (mandatory)

Before posting/replying, scan the rendered output:
- Every line mentioning a slack thread/ask/message ("flagged in", "asked", "thread",
  "in #channel", a teammate quote) MUST carry a `[thread](…)` link. If a referenced
  row truly has no link (rare), append `(no linkable ts)` so the gap is explicit — a
  bare slack reference with no link is a bug, not an option.
- Every link — `[#N]`, `[EX-NNNN]`, Confluence, build — has an inline descriptor next
  to it (not just the number). A bare `[#N]` with no label is a bug.
- Every `([thread])` line is SELF-SUMMARIZING (what + why, §3) — if you can't tell what
  the issue/ask is without clicking, it's not done: open the thread and add the context.
