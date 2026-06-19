# Shared rule — ground claims in the source, not the title

Loaded by skills that make impact/contribution claims (`/retro`, `/ask` narrative intents,
`/standup`). The principle: a title or summary is a pointer, not evidence. Open the actual
artefact and pull the real content. Skill-specific framing (stakeholder Highs, person
narrative, standup enrichment) stays in the skill.

## Read the source

Before asserting what something delivered or what went wrong, open the underlying
artefact — the slack thread, the ticket body/comments, the PR description, the doc, the
incident thread — and read it. Do NOT paraphrase from the title, the cluster label, or a
one-line preview. Those are summaries of summaries; the meaning lives in the raw bodies.

## Pull the measured numbers + named facts

Extract the concrete, verifiable facts the source actually contains:
- numbers — RPS, p95/p99 latency, success rate, % rollout, ₹ revenue, accounts onboarded,
  downtime saved, cost reduction
- dates — go-live, beta cut, deadline committed
- named decisions — who approved / decided / rolled back, post-rollout observations,
  specific actions ("disabled X job", "deployed beta-only PR #N", "rectified ₹X TB diff")

These belong in the impact/claim line. A claim with a number from the source beats a
generic adjective every time.

## Never invent; say when it's silent

If the source carries no measured impact, say so plainly — do NOT fabricate a number or
infer one. "Shipped; no rollout metrics posted yet" is honest; an invented RPS is a bug.
