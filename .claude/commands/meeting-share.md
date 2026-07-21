---
description: >
  Produce a shareable, PII-redacted export of a meeting note or MoM. Deterministic
  local redaction (no model, no cloud): masks email / phone / account & card
  numbers / PAN / Aadhaar / IFSC / long IDs; team names optional. Owner-reviewed —
  NOTHING is sent anywhere. The redacted file is written for you to share by hand.
argument-hint: "[date-or-slug filter] [names]  (add 'names' to also mask team names)"
---

# /meeting-share — redacted, shareable meeting export

Part of the meeting-intelligence pipeline (prd/meeting-intelligence.md, P5).
Local + deterministic. This command NEVER sends anything: it writes a redacted
copy and shows you what was masked, and YOU decide whether/where to share it
(sending is a separate, explicit, permission-required action).

## STEP 1 — Resolve the target

From `$ARGUMENTS` (date `YYYY-MM-DD` or slug substring; the word `names`
anywhere means "also mask team names"), find the meeting under
`management/meetings/`. Prefer the **MoM** (`<date>-<slug>.mom.md`) — it is the
formal shareable doc — falling back to the private note (`<date>-<slug>.md`).

- If only the private note exists, say so plainly: it is the CANDID version
  (said-vs-done framing, unattributed speculation). Redaction masks identifiers
  but does NOT sanitize candor — recommend generating a MoM first (Steno "MoM"
  button / `/meeting-notes`) for anything going outside the immediate team.
- List candidates if the filter is ambiguous; ask which one. Never guess.

## STEP 2 — Redact (deterministic)

    python3 work-context/derive/meetings/redact.py \
      "management/meetings/<stem>.mom.md" [--mask-names] --report-json

- Add `--mask-names` only when the invocation included `names`.
- Writes `<stem>.share.md` next to the source and prints `{out, masked}`.
- What is masked: email, phone, account/card numbers, PAN, Aadhaar, IFSC, and
  long ID runs. What is KEPT (shareable, no personal identifier): Jira keys,
  `[mm:ss]` offsets, ISO dates, money amounts, URLs.

## STEP 3 — Report + hand back (no send)

- Show the masked-item counts (e.g. "masked: 2 emails, 1 phone, 3 accounts").
- Link the redacted file: `management/meetings/<stem>.share.md`.
- Tell the owner to REVIEW it before sharing — redaction is a safety net, not a
  guarantee; skim for anything sensitive the patterns missed (proper nouns,
  amounts tied to a person, free-text that re-identifies).
- Do NOT post, email, or upload it anywhere. If the owner then asks to send it
  to a specific place, that is a separate explicit step (and any external send
  requires confirming the destination first).
