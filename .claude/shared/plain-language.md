# Shared rule — plain-language output (no internal jargon)

Loaded by every skill that renders owner/stakeholder-facing prose (`/ask` narrative intents,
`/standup`, `/pulse`, `/retro`, `/doc-sync`, `/dev-style`, feature-logic, …). The reader is a
manager who knows the team but does NOT know this tooling's internals. They must read a
clean human narrative grounded in real artefacts — never the machinery that produced it.

Each skill keeps its OWN exact forbidden-token list (the names of *its* pipeline's internals)
as additions on top of this generic rule.

## Translate every signal to plain English

Before any internal signal lands in the output, translate it. Forbidden EVERYWHERE (every
section, including any "audit"/"confirmed-by-data" section — there is no exempt section):

- **Cluster references** — the word "cluster", cluster IDs, cluster counts. Name the
  *workstream* instead ("the balance-service + withholding work"), not "cluster 56".
- **Engine / script / table / module names** — the `.py` files, DB tables, pipeline stages.
- **JSON field paths & signal keys** — anything with `::`, `_json`, a `.field` path, or a
  raw signal key. Say what it means, not the key.
- **Raw internal metrics** — `p50`/`p90`, `_pct`, completion-rate math, after-hours ratios.
  Translate: "usually replies within an hour"; "shipped about two-thirds of planned work".
- **Pipeline jargon** — "lookahead", "window edge", "boundary artefact", "sandbox". Use
  plain time language ("looked one month later", "by end of May those had shipped").

## Cite only artefacts a human can open

Every claim cites something the reader can open: a ticket ID, a PR (`owner/repo#N`), a doc
title, a slack thread link, or a plain-English description of the activity ("left long
investigative comments on the year-end-job tickets"). If you cannot state a fact without
naming the engine, the fact does not belong in the output.

## No meta-labels

Never write parenthetical meta-labels like "(plain English)", "(analyzed)", "(gist)",
"(objective baseline)". Just write the plain text — the label is noise.

## Pre-save grep-check (mandatory)

Before writing/posting, scan the ENTIRE rendered output (no exempt section) for internal
tokens — the generic categories above PLUS the skill's own forbidden-token list — and rewrite
any line that contains one in plain-English / artefact terms. A leaked engine name, field
path, cluster reference, or raw metric anywhere = rewrite.
