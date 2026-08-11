#!/usr/bin/env python3
"""Expand a compact sprint-plan SPEC into the full plan JSONs the planner renders.

Why: /sprint-plan used to have the chat session hand-type the full `cells`
arrays — one cell per person per day, twice (Plan A + Plan B). That's ~90%
mechanical output. Now the session writes ONE compact spec (spans of work per
person + picks/signals/rationale) and this script materializes both plan files
deterministically from the spec + the dump's day grid and statuses.

Spec (written by the chat session to derived/sprint-plan-spec.json):

    {
      "A": {
        "assignments": {
          "<person name or canonical>": [
            {"from": "2026-07-08", "to": "2026-07-10", "label": "Narration PROJ-2531"},
            {"from": "2026-07-13", "to": "2026-07-17", "label": "Withholding v2",
             "oncall": true}          // optional: place on O days too (comment-driven)
          ]
        },
        "backlogPicks": [...],        // passed through verbatim
        "signals": [...],             // passed through verbatim
        "rationale": "..."            // passed through verbatim
      },
      "B": { same shape }
    }

Expansion rules (mirror .claude/commands/sprint-plan.md):
  - one cell per day in dump `days` order, for EVERY person in the dump;
  - default kind from the dump status: ""->idle, W->wfh, O->oncall, L->leave,
    H->holiday, WE->weekend;
  - a span labels a day only if the day is within [from,to] AND workable:
    status "" or W -> kind work; status O needs "oncall": true on the span
    (renders kind oncall WITH label = shared on-call work);
  - spans never place work on L/H/WE days — those days are skipped with a
    warning, never an error (that's the point: "first week on X" clips to
    working days automatically);
  - overlapping spans on the same day = conflict warning; the later span wins;
  - labels are clipped to 24 chars (schema cap) with a warning.

Outputs: A -> derived/sprint-plan.json, B -> derived/sprint-plan-brain.json
(only the plans present in the spec are written). Prints a per-person
workday/idle summary + all warnings so the session can sanity-check against
capacity and fix the spec if needed.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parent.parent

STATUS_KIND = {"": "idle", "W": "wfh", "O": "oncall", "L": "leave",
               "H": "holiday", "WE": "weekend"}
WORKABLE = {"", "W"}
LABEL_CAP = 24
PLAN_FILES = {"A": "sprint-plan.json", "B": "sprint-plan-brain.json"}
PLAN_TAGS = {"A": "as-specified", "B": "manager rebalance"}


def _d(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _person_index(people: list[dict]) -> dict[str, dict]:
    idx = {}
    for p in people:
        idx[p["name"].lower()] = p
        if p.get("canonical"):
            idx[p["canonical"].lower()] = p
    return idx


def expand_plan(dump: dict, plan_spec: dict, plan_key: str,
                warnings: list[str]) -> dict:
    days = dump["days"]
    people = dump["people"]
    pidx = _person_index(people)

    # resolve assignment spans onto real people
    spans_by_name: dict[str, list[dict]] = {p["name"]: [] for p in people}
    for who, spans in (plan_spec.get("assignments") or {}).items():
        p = pidx.get(who.lower())
        if p is None:
            valid = ", ".join(sorted(q["name"] for q in people))
            raise SystemExit(f"[{plan_key}] unknown person '{who}' — valid: {valid}")
        for s in spans:
            frm = _d(s.get("from") or s["date"])
            to = _d(s.get("to") or s.get("from") or s["date"])
            if to < frm:
                raise SystemExit(f"[{plan_key}] {who}: span to<{frm} ({s})")
            spans_by_name[p["name"]].append(
                {"from": frm, "to": to, "label": str(s.get("label", "")),
                 "oncall": bool(s.get("oncall"))})

    out_people = []
    for p in people:
        cells = []
        n_work = n_idle = 0
        for i, day in enumerate(days):
            dt = _d(day["date"])
            status = p["statuses"][i]
            kind = STATUS_KIND.get(status, "idle")
            label = ""
            hits = [s for s in spans_by_name[p["name"]]
                    if s["from"] <= dt <= s["to"]]
            placeable = [s for s in hits
                         if status in WORKABLE or (status == "O" and s["oncall"])]
            if len(placeable) > 1:
                warnings.append(f"[{plan_key}] {p['name']} {day['date']}: "
                                f"{len(placeable)} overlapping spans — "
                                f"'{placeable[-1]['label']}' wins")
            if placeable:
                s = placeable[-1]
                label = s["label"]
                if len(label) > LABEL_CAP:
                    warnings.append(f"[{plan_key}] {p['name']} {day['date']}: "
                                    f"label clipped to {LABEL_CAP} chars "
                                    f"('{label}')")
                    label = label[:LABEL_CAP]
                kind = "oncall" if status == "O" else "work"
                n_work += 1
            elif hits and status not in WORKABLE:
                # span covers a non-workable day — clip silently-ish
                warnings.append(f"[{plan_key}] {p['name']} {day['date']}: span "
                                f"'{hits[-1]['label']}' skipped ({kind} day)")
            if kind == "idle":
                n_idle += 1
            cells.append({"date": day["date"], "kind": kind, "label": label})
        out_people.append({"name": p["name"], "cells": cells,
                           "_work_days": n_work, "_idle_days": n_idle})

    return {
        "_generated": f"{date.today().isoformat()} ({PLAN_TAGS[plan_key]})",
        "people": [{k: v for k, v in pp.items() if not k.startswith("_")}
                   for pp in out_people],
        "backlogPicks": plan_spec.get("backlogPicks", []),
        "signals": plan_spec.get("signals", []),
        "rationale": plan_spec.get("rationale", ""),
        "_summary": {pp["name"]: {"work": pp["_work_days"],
                                  "idle": pp["_idle_days"]}
                     for pp in out_people},
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--spec", default="derived/sprint-plan-spec.json")
    ap.add_argument("--dump", default="derived/sprint-dump.json")
    ap.add_argument("--out-dir", default="derived")
    args = ap.parse_args()

    spec_p = _PKG_ROOT / args.spec if not Path(args.spec).is_absolute() else Path(args.spec)
    dump_p = _PKG_ROOT / args.dump if not Path(args.dump).is_absolute() else Path(args.dump)
    if not spec_p.exists():
        raise SystemExit(f"spec not found: {spec_p} — write the compact spec first")
    dump = json.load(open(dump_p))
    spec = json.load(open(spec_p))

    warnings: list[str] = []
    written = {}
    for key in ("A", "B"):
        if key not in spec:
            continue
        plan = expand_plan(dump, spec[key], key, warnings)
        summary = plan.pop("_summary")
        out = (_PKG_ROOT / args.out_dir / PLAN_FILES[key]
               if not Path(args.out_dir).is_absolute()
               else Path(args.out_dir) / PLAN_FILES[key])
        out.write_text(json.dumps(plan, indent=2))
        written[key] = {"file": str(out), "people": len(plan["people"]),
                        "picks": len(plan["backlogPicks"]),
                        "signals": len(plan["signals"]),
                        "workdays": summary}
    if not written:
        raise SystemExit("spec has neither 'A' nor 'B' — nothing to write")

    print(json.dumps({"written": written, "warnings": warnings}, indent=2))
    if warnings:
        print(f"\n{len(warnings)} warning(s) above — review before telling the "
              f"owner to Load plan.", file=sys.stderr)


if __name__ == "__main__":
    main()
