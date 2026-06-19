# Shared rule — saving rendered output to markdown

Loaded by skills that persist a durable artefact under `management/` (`/ask`, `/pulse`, …).
The chat reply is the live preview; the file is the durable copy the owner re-reads and greps
later (1:1 prep, retro source, audit trail). Each skill keeps its own `management/` subdir +
filename scheme; this chunk owns the format + safety rules common to all.

EXCEPTIONS — skills that deliberately diverge, do NOT wire them to this chunk:
- `/standup` writes NO md files (owner decision 2026-06-12; digest is delivered, not archived).
- `/pr-report` intentionally OVERWRITES same-day (idempotent) and uses a `work-context/management/`
  base — its never-overwrite behaviour is the opposite of this chunk's.
- `/retro` uses a stakeholder-doc header style of its own.

## Location

Write under `management/<domain>/` (e.g. `management/retros/`, `management/narratives/`,
`management/pulse/`, `management/pr-quality/`, `management/queries/`). Paths are relative to
repo root (`$HOME/context`). `mkdir -p` the parent first. Use the **Write tool** — never
`cat > file` via Bash (file-writing policy).

## Mandatory file header (first lines)

```markdown
# <Concise title>

**Intent:** <what this is>          ← `/ask` uses `**Intent:**` (the intent name); other
                                       skills may relabel this slot `**Scope:**`. Keep ONE
                                       label per skill; match what the skill's existing files use.
**Generated:** <YYYY-MM-DD HH:MM IST>
**Window:** <since> → <until>   (only when the output has a time window)
**Question:** "<verbatim user question>"   (for free-text query intents)

---

<rendered content — exactly as it appears in chat>
```

Dates are ISO short (`YYYY-MM-DD`); timestamps in IST.

## Never overwrite

If the target path already exists, append `-2`, `-3`, … before `.md`. Older runs are the
evidence trail — don't clobber them.

## End the chat reply with the path

After writing, the chat reply MUST end with:

```
**Saved to:** `<absolute path>`
```

so the owner can open the file directly.
