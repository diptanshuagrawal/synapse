# Shared rule — code-graph mirror access

Loaded by any skill that reads the code graph or its mirror clones (`/service-brief`,
`/trd-build`, `/pr-from-trd`, `/doc-sync`, `/doc-sync-sweep`, `/ask` feature-logic). This
owns WHERE the code truth lives and the freshness contract. Each skill keeps its own USE of
the graph (extract a skeleton, mirror a sibling, diff a doc, explain a flow).

## Where the code lives — mirrors, not `~/git`

The graph + its source mirrors live under `~/.code-review-graph/`:
- mirror clones: `~/.code-review-graph/repos/<svc>` (e.g. `…/repos/service-a`)
- registry: `~/.code-review-graph/registry.json` (alias → path)

Resolve a service alias → mirror path via the registry:

```bash
python3 - "<alias>" <<'PY'
import json, sys, pathlib
reg = json.load(open(pathlib.Path.home() / ".code-review-graph/registry.json"))
svc = sys.argv[1].strip()
repos = reg.get("repos", reg) if isinstance(reg, dict) else reg
hit = next((r for r in repos if r.get("alias") == svc), None)
print(hit["path"] if hit else "NOT_FOUND")
PY
```

**Never read `~/git/<svc>` dev repos for graph truth** — those are local WIP and go stale.
Use the registered mirror (via its alias), not a `~/git` path.

## Freshness contract — REMOTE default branch, daily reset

The mirror reflects the **REMOTE default branch (merged code)**, `reset --hard` to
`origin/HEAD` daily — NOT anyone's local WIP. So **"missing in the graph" = "not merged yet,"**
which is exactly what direction/drift judgements rely on. Treat the graph as
state-as-of-last-refresh, not live.

## Unregistered repos — flag, don't guess

If a feature spans a repo NOT in the registry (e.g. `deposits-orch`, `casa-orch`), it isn't
checkable here. Say so plainly — "not checkable here" / flag the section — and do NOT guess
its code. On `NOT_FOUND` for the asked alias, list the available aliases from the registry
and stop.
