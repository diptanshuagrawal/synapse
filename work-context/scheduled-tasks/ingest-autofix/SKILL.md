---
name: ingest-autofix
description: Daily from 16:00 IST (retries every 30 min until it succeeds once) — resolves the ingest validators' "unmapped actor" attribution warnings by looking each actor up in Atlassian and adding a people.yaml mapping. Tiered-auto: the safe class (one org-domain human → org, clear bot/service → external) is applied automatically; the risky class (merge-into-existing-person, possible teammate, ambiguous) is parked for owner review. Posts a run-summary to #rollup.
---

Resolve ingest attribution warnings and post a run-summary to Slack.

Working dir: __REPO__/work-context. All python via the venv: `__REPO__/work-context/.venv/bin/python`.

WHAT THIS FIXES: the `attribution` WARN raised by github_validate / jira_validate /
confluence_validate — actors doing work whose id isn't in config/people.yaml, so they can't
be scoped. That's the only cleanly auto-fixable ingest warning. Slack reply-drift / orphan
replies and the topic_brief pipeline-coverage WARNs are NOT auto-fixable here — STEP 5 just
REPORTS them so they stay visible (never silently "fixed").

config/people.yaml is gitignored (real names/emails never reach the public repo), so editing
it is leak-safe. #rollup is the owner's internal channel — real names are fine there (standup
posts them too).

## RUN-ONCE GATE (idempotent — this routine retries every 30 min until it succeeds once today)
Before ANY work, run this and obey it:

    MARK=__REPO__/work-context/state/last_routine_ingest_autofix_success.date
    TODAY=$(TZ=Asia/Kolkata date +%F)
    if [ -f "$MARK" ] && [ "$(cat "$MARK")" = "$TODAY" ]; then echo "GATE: ingest-autofix already succeeded today ($TODAY) — idle"; else echo "GATE: not done today — proceed"; fi

If it prints "already succeeded today" → STOP NOW: do nothing, end the run. Only proceed if it prints "not done today".

## STEP 1 — Detect every unmapped actor
    cd __REPO__/work-context && .venv/bin/python derive/unmapped_actors.py --json

Parse the JSON: `by_source.{github,jira,confluence}` each a list of `{actor, count, samples}`.
- If `n_unmapped_total == 0`: there is nothing to resolve. SKIP to STEP 5 (gather report-only
  warnings), post a "nothing to fix" summary in STEP 6, then stamp success. Do NOT run apply.
- Otherwise continue.

## STEP 2 — Resolve identities (this chat session does the judgment — NO script calls an LLM/API)
First get the Atlassian cloudId once (call the Atlassian `getAccessibleAtlassianResources`
tool; use the org's Atlassian site id). Then for EACH unmapped actor:

a. GROUP github actors that share sample subjects (same PRs) — a display-name form
   (e.g. "Jane Doe") and a login form (e.g. "org-janedoe") for the same person show up as two
   actors with overlapping samples. Resolve the group once and cover BOTH forms in one entry
   (put both in `git_names`, the login in `github`).

b. LOOK UP the identity with the Atlassian `lookupJiraAccountId` tool (searchString = the
   actor string; for github prefer the display-name form). It returns displayName + email +
   accountId. Jira actors are usually already an email or account-id; confluence actors are
   account-ids — look them up the same way to get the name/email.

c. CLASSIFY into exactly one tier:

   TIER A — AUTO-APPLY (write to people.yaml):
     • Resolves to exactly ONE human on the org email domain (the `org.email_domain` value in
       config/sources.yaml) AND no existing people.yaml entry already shares any of their
       identity keys → new entry, `scope: org`.
       Fields: email, name, scope:org; plus jira_id (the accountId) for jira/confluence actors;
       plus github + git_names (all actor forms) for github actors.
     • Clear bot / service / automation account (actor matches /bot|automation|^tech-|-svc$|svc-|\[bot\]/
       or never resolves to a human and is obviously machine) → new entry, `scope: external`,
       carrying the actor under github/git_names (or name if no login form).

   TIER B — PARK FOR REVIEW (do NOT write to people.yaml):
     • The resolved person ALREADY exists in people.yaml under a different key → this is an
       alias/merge (e.g. an alternate github login for someone already on the roster). NEVER
       auto-merge — a wrong merge fuses two people. Park with suggested target entry.
     • A NEW github committer with a high commit count whose samples are mostly the owner's
       CORE service repos → POSSIBLE TEAMMATE. team scope is a human decision — park it
       (suggest org as the likely answer).
     • Doesn't resolve, resolves to MULTIPLE people, or resolves to a human NOT on the org
       email domain → park (ambiguous / external-human needs confirmation).

   Cross-check "already exists" against config/people.yaml: read it and match the resolved
   email / accountId / login / name against existing entries' email, jira_id, github,
   github_aliases, git_names, name.

## STEP 3 — Apply Tier A, park Tier B
- Write the Tier-A entries as a JSON array to `state/ingest_autofix_resolved.json`
  (each: {scope, email?, name, github?, jira_id?, git_names?}). Then apply:

    cd __REPO__/work-context && .venv/bin/python derive/people_autofix_apply.py --in state/ingest_autofix_resolved.json

  It is APPEND-ONLY + idempotent: it refuses team scope, skips identities already mapped, and
  never rewrites existing lines. Capture its JSON (applied / skipped).
- Write the Tier-B items as a JSON array to `state/ingest_autofix_pending.json`
  (each: {source, actor, count, samples, resolved:{name,email,accountId}|null, reason,
  suggested_action, suggested_entry?}). This file IS the owner's review queue — overwrite it
  each run with the CURRENT pending set (resolved ones drop off naturally).

## STEP 4 — Re-validate (confirm Tier A cleared the warning) + refresh cached state
Regenerate the cached validate JSON the same atomic, PID-suffixed way the ingest runners do
(per the concurrent-writer rule — never a bare tmp name):

    cd __REPO__/work-context
    for src in github jira confluence; do
      TMP="state/last_${src}_validate.json.$$.tmp"
      .venv/bin/python derive/${src}_validate.py --json > "$TMP" 2>/dev/null \
        && mv "$TMP" "state/last_${src}_validate.json" || rm -f "$TMP"
    done

Then read each `findings`: the `attribution` finding should now be PASS for any source whose
unmapped actors were all Tier A. If a source still WARNs, it's because its remaining unmapped
actors were all Tier B (parked) — that's expected; note it in the summary, do NOT treat it as
a failure.

## STEP 5 — Gather report-only warnings (NOT fixed here — just surfaced)
Read the cached state files and extract any WARN/FAIL (do not act on them):
- `state/last_slack_validate.json` — reply_drift / orphan_replies per channel.
- `state/last_pipeline_validate.json` — enrichment / participants / v2 coverage.
- `state/last_embedding_validate.json` (if present) or note embedding staleness.
Summarize as counts + the top 2-3 channels/clusters, with a one-line "needs re-backfill /
finalize_refresh — not auto-fixable" note.

## STEP 6 — Post the run-summary to #rollup (channel ID __ROLLUP_CHANNEL__)
Use the slack send-message tool with that channel ID. It renders STANDARD markdown — write
`[text](url)` and `**bold**` directly (not Slack mrkdwn). Keep it tight:
- Header: "Ingest auto-fix — <today YYYY-MM-DD>".
- **Applied** (`•` bullets): each new mapping — "Name (source) → org" / "actor → external".
  If none: "No new mappings.".
- **Parked for review** (N): each Tier-B — "actor (source, N events) → <reason>". Point to
  `state/ingest_autofix_pending.json`. If none: "none".
- **Re-validated**: which of github/jira/confluence attribution are now clean (and any still
  WARN only because of parked items).
- **Report-only** (not auto-fixable): slack drift/orphans (N channels) + pipeline coverage
  (N clusters) + embedding staleness — one line each.
- If `n_unmapped_total` was 0 and nothing parked: a single line
  "All ingest actors already scoped — nothing to fix.".
If the post can't be delivered (bot not in #rollup, channel archived): DO NOT silently fail —
report the error in this run's output and do NOT stamp success (next fire retries). The bot
must be invited to #rollup (__ROLLUP_CHANNEL__).

CRITICAL: this run IS a fresh chat session — the identity judgment (STEP 2) happens HERE, in
chat. NEVER call the Anthropic API from a script and NEVER let a script classify. Scripts
strip auth. The only writes are: people.yaml appends (STEP 3), the cached validate JSON
(STEP 4), the two state JSON files, and the Slack post. Everything else is read-only
(events.db, Atlassian lookups, config).

## RECORD SUCCESS (final step — gates the 30-min retry)
ONLY after this run is CONFIRMED complete — detection ran AND apply ran (or there was nothing
to apply) AND the re-validate step finished AND the summary landed in #rollup — stamp the
marker so the rest of today's fires idle:

    TZ=Asia/Kolkata date +%F > __REPO__/work-context/state/last_routine_ingest_autofix_success.date

A "nothing to fix" run counts as success — stamp it. If detection/apply/validate errored or
the summary could not be delivered, do NOT stamp: leave the marker so the next 30-min fire
retries.
