#!/usr/bin/env python3
"""One-shot: filter CQL discovery hits → state/doc_sync_discovered.json.
Filter legs: (b) author in people.yaml scope:team; (c) NOT ops/rca/oncall/perf/setup/tracking/report.
Owned-service leg (a) is best-effort by domain terms already in the CQL; discover-merge dedups vs inventory."""
import json, re, sys, yaml

CQL_FILE = sys.argv[1]
OUT = "state/doc_sync_discovered.json"

team_names = set()
for e in yaml.safe_load(open("config/people.yaml"))["people"]:
    if e.get("scope") == "team":
        team_names.add((e.get("name") or "").strip().lower())
        for a in (e.get("git_names") or []) + (e.get("github_aliases") or []):
            team_names.add(a.strip().lower())

# category exclusions (leg c) — matched on title
EXCLUDE = re.compile(
    r"\b(rca|oncall|on-?call|incident|post-?mortem|load\s*test|rps|autovacuum|"
    r"local setup|onboarding|eta plan|experiment config|rollout plan|derisk|"
    r"analysis report|report|perf(ormance)?|dashboard|runbook|weekly|standup|"
    r"retro|planning|deprecated)\b", re.I)

d = json.load(open(CQL_FILE))
nodes = (d.get("content") or d).get("nodes", [])

cands, skipped_author, skipped_cat = [], 0, 0
for n in nodes:
    title = n.get("title", "")
    author = (n.get("author") or {}).get("displayName", "").strip().lower()
    if author not in team_names:
        skipped_author += 1
        continue
    if EXCLUDE.search(title):
        skipped_cat += 1
        continue
    cands.append({"id": str(n["id"]), "title": title,
                  "author": (n.get("author") or {}).get("displayName", ""),
                  "repo": None})

json.dump({"candidates": cands}, open(OUT, "w"), indent=1)
print(f"nodes={len(nodes)} team_authored_survivors={len(cands)} "
      f"skipped_non_team={skipped_author} skipped_category={skipped_cat}")
for c in cands:
    print(" ", c["id"], "|", c["author"], "|", c["title"])
