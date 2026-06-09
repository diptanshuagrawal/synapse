#!/usr/bin/env python3
"""ticketize_state.py — deterministic fingerprint + state I/O for /ticketize.

NO LLM, NO network. All judgement (which gap is ticketable) is the skill's job.
This script only: (1) computes a stable fingerprint per candidate so the same
work-gap is never re-proposed or double-created across runs, and (2) reads/writes
the durable state file. Dates are passed in explicitly (never `now`) so runs are
reproducible.

State file: work-context/state/ticket_candidates.json
  { "version": 1,
    "candidates": {
       "<fp>": {"person","summary","link","first_seen","status","jira_key","last_update"}
    } }
  status ∈ proposed | created | rejected

Usage:
  # DETECT: enrich seed candidates with fingerprint + any prior status (read-only)
  python3 bin/ticketize_state.py annotate --date YYYY-MM-DD  < seeds.json   > enriched.json

  # APPLY: persist decisions (upsert). created rows must carry jira_key.
  python3 bin/ticketize_state.py commit   --date YYYY-MM-DD  < decided.json

Candidate JSON shape (array):
  [{"person":"bob-example","summary":"...","link":"https://...",
    "decision":"approve|reject|pending","jira_key":"EX-1234"}, ...]
"""
import sys, os, json, re, hashlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "work-context/state/ticket_candidates.json")


def norm(s):
    """Normalize a summary for fingerprinting: lowercase, alnum-only, collapse ws."""
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def fp(person, summary, link):
    raw = f"{(person or '').strip().lower()}|{norm(summary)}|{(link or '').strip()}"
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def load_state():
    if not os.path.exists(STATE):
        return {"version": 1, "candidates": {}}
    with open(STATE) as f:
        return json.load(f)


def save_state(st):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, STATE)


def get_arg(flag, default=None):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("annotate", "commit"):
        print(__doc__)
        sys.exit(1)
    mode = sys.argv[1]
    date = get_arg("--date")
    if not date:
        print("error: --date YYYY-MM-DD required", file=sys.stderr)
        sys.exit(1)

    try:
        cands = json.load(sys.stdin)
    except Exception as e:
        print(f"error: stdin must be a JSON array of candidates ({e})", file=sys.stderr)
        sys.exit(1)
    if not isinstance(cands, list):
        print("error: expected a JSON array", file=sys.stderr)
        sys.exit(1)

    st = load_state()
    known = st["candidates"]

    if mode == "annotate":
        # read-only: add fingerprint + prior status so the skill can skip dupes.
        out = []
        for c in cands:
            f = fp(c.get("person"), c.get("summary"), c.get("link"))
            prior = known.get(f)
            c["fingerprint"] = f
            c["prior_status"] = prior["status"] if prior else "new"
            c["prior_jira_key"] = prior.get("jira_key") if prior else None
            out.append(c)
        json.dump(out, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
        return

    # commit: upsert decisions into state.
    created = rejected = proposed = 0
    for c in cands:
        f = c.get("fingerprint") or fp(c.get("person"), c.get("summary"), c.get("link"))
        decision = (c.get("decision") or "pending").lower()
        row = known.get(f, {
            "person": c.get("person"), "summary": c.get("summary"),
            "link": c.get("link"), "first_seen": date,
        })
        row["last_update"] = date
        if decision == "approve" and c.get("jira_key"):
            # idempotent: never overwrite an existing created key
            if row.get("status") == "created" and row.get("jira_key"):
                pass
            else:
                row["status"] = "created"
                row["jira_key"] = c["jira_key"]
                created += 1
        elif decision == "reject":
            row["status"] = "rejected"
            rejected += 1
        else:
            row.setdefault("status", "proposed")
            proposed += 1
        known[f] = row
    save_state(st)
    print(f"committed: {created} created, {rejected} rejected, {proposed} proposed "
          f"({len(known)} total tracked) -> {STATE}", file=sys.stderr)


if __name__ == "__main__":
    main()
