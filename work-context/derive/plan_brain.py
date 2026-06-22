#!/usr/bin/env python3
"""plan_brain.py — LLM tactical-analysis layer for the sprint planner.

Takes the computed capacity + the owner's initiative skeleton (with free-text
comments) and asks Claude to DERIVE a day-by-day allocation: which initiative
each person works on each available day, honouring the comments
("X is ~done", "solve Y during on-call", etc.), flagging
over-capacity / slips, and explaining the key calls.

Deterministic data (capacity, ticket SP) is computed elsewhere; this module is
the reasoning step. Owner opted into a per-click Claude call (the planner's
"Generate plan" button) — that's why a script calls the API here.
"""
import os, sys, json
import anthropic

SECRETS = os.path.expanduser("~/.secrets")
MODEL = "claude-opus-4-8"

SYS = """You are a sprint-planning analyst for an engineering pod. You are given:
- the sprint window and per-person CAPACITY (each person's day-by-day availability
  status, net working days, and effective story-point capacity), and
- a skeleton of INITIATIVES the engineering manager wants worked on, each with a
  type (fixed = must finish this sprint; continuous = ongoing, fills spare days),
  assignee(s), a reviewer, a priority (P1>P2>P3), an effort in story points (for
  fixed work), and a free-text COMMENT that may carry tactical instructions.

Your job: produce a realistic day-by-day plan — for every person, what they work
on each day of the sprint — and surface the risks.

Rules:
- Day status codes in the input: "" = available, "W" = WFH (still a WORKING day),
  "O" = on-call, "L" = leave, "H" = holiday, "WE" = weekend.
- A person can only do initiative work on available ("") or WFH ("W") days, UNLESS
  a comment explicitly says otherwise (e.g. "solve during on-call") — then you may
  place that initiative on the named person's on-call days.
- Never place work on leave, holiday, or weekend days.
- READ THE COMMENTS and act on them. Examples of the kind of reasoning expected:
  * "X — ~done, mostly in review" => treat residual effort as near-zero; don't
    consume many days for it even if its ticket SP looks large.
  * "Y — solve during on-call by <person>" => place Y on that person's on-call days
    specifically.
  * "inherits a leaver's tickets" => that work really does land on the assignee.
- Convert fixed effort to days using the person's efficiency: days ≈ round(SP / eff).
- Allocate fixed initiatives first, highest priority first. Continuous initiatives
  fill whatever available days remain. If a person has more committed work than
  available days, that's an over-capacity situation: place what fits (highest
  priority first) and report the rest as a slip.
- A reviewer spends reviewCost SP per initiative they review — account for it as a
  light load on the reviewer, not a full day per initiative.

Output (via the structured tool) for EVERY person, one cell per day of the sprint,
in the same date order as the input `days`. Each cell: the date, a `kind`
(work|leave|oncall|wfh|holiday|weekend|idle), and a short `label` (the initiative
name for work cells — keep it under ~24 chars; empty for non-work cells, or a brief
note like "On-call + <task>" when work shares an on-call day).
Then `signals`: callouts for over-capacity people, slips (what won't fit + why),
unassigned people, missing data — each with a severity level.
Then `rationale`: 2-5 sentences on the key decisions and how you applied the comments."""

SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["people", "signals", "rationale"],
    "properties": {
        "people": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["name", "cells"],
            "properties": {
                "name": {"type": "string"},
                "cells": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["date", "kind", "label"],
                    "properties": {
                        "date": {"type": "string"},
                        "kind": {"type": "string", "enum": ["work", "leave", "oncall", "wfh", "holiday", "weekend", "idle"]},
                        "label": {"type": "string"},
                    }}}}}},
        "signals": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["level", "text"],
            "properties": {
                "level": {"type": "string", "enum": ["danger", "warn", "ok", "info"]},
                "text": {"type": "string"},
            }}},
        "rationale": {"type": "string"},
    },
}


def _client():
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        p = os.path.join(SECRETS, "anthropic_api_key")
        if os.path.exists(p):
            key = open(p).read().strip()
    return anthropic.Anthropic(api_key=key)


def analyze(payload):
    client = _client()
    user = ("Plan this sprint. Here is the capacity + initiative skeleton as JSON.\n\n"
            + json.dumps(payload, indent=2))
    resp = client.messages.create(
        model=MODEL,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        output_config={"effort": "high", "format": {"type": "json_schema", "schema": SCHEMA}},
        system=SYS,
        messages=[{"role": "user", "content": user}],
    )
    text = next((b.text for b in resp.content if b.type == "text"), "")
    return json.loads(text)


if __name__ == "__main__":
    data = json.load(sys.stdin)
    print(json.dumps(analyze(data), indent=2))
