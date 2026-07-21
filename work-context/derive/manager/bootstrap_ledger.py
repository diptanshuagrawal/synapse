#!/usr/bin/env python3
"""One-time seed for the manager ledger (prd/ai-manager.md P1).

Deterministic, no LLM, additive-only: reads existing configs/derived artifacts,
writes ONLY under management/ledger/, and refuses to overwrite any file that
already exists (owner edits are authoritative). Safe to re-run anytime.

Seeds:
  goals.yaml         from work-context/derived/initiatives-out.json (team initiatives)
  people/<slug>.md   one per people.yaml scope:team entry (owner excluded)
  risks.yaml, commitments.yaml, watchlist.yaml, decisions.md   empty scaffolds
  briefs/            created empty

Usage: python3 work-context/derive/manager/bootstrap_ledger.py [--owner <slug>] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # repo root (~/context)
LEDGER = ROOT / "management" / "ledger"
PEOPLE_YAML = ROOT / "work-context" / "config" / "people.yaml"
INITIATIVES = ROOT / "work-context" / "derived" / "initiatives-out.json"
PERSON_TEMPLATE = LEDGER / "templates" / "person.md"

sys.path.insert(0, str(ROOT / "work-context" / "derive"))

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml required (use the pipeline venv python)", file=sys.stderr)
    sys.exit(1)


def detect_owner_slug(people: list[dict]) -> str | None:
    """Match sources_config org.owner_email against people.yaml emails."""
    try:
        import sources_config
        owner_email = sources_config.owner_email()
    except Exception:
        return None
    for p in people:
        if (p.get("email") or "").lower() == owner_email.lower():
            return p.get("canonical")
    return None


def kebab(s: str) -> str:
    return "-".join("".join(c if c.isalnum() else " " for c in s.lower()).split())


def seed_goals(today: str) -> str:
    goals = []
    if INITIATIVES.exists():
        data = json.loads(INITIATIVES.read_text())
        for init in data.get("initiatives", []):
            name = (init.get("name") or "").strip()
            if not name:
                continue
            epic = init.get("epic") or ""
            goals.append(
                {
                    "id": f"goal-{kebab(name)[:40]}",
                    "created": today,
                    "last_reviewed": today,
                    "status": "active",
                    "title": name,
                    "project": "",
                    "target": "",
                    "trajectory": "SEEDED from initiatives-out.json — owner: describe what on-track looks like",
                    "health": "unknown",
                    "health_reason": "seeded, not yet reviewed",
                    "receipts": [r for r in [epic, init.get("epicSummary", "")] if r][:1] or [],
                    "sp": init.get("sp"),
                }
            )
    header = (
        "# Manager ledger — goals. Schema: management/ledger/README.md\n"
        "# Seeded by bootstrap_ledger.py; /manager + owner edits maintain it (owner wins).\n"
    )
    return header + yaml.safe_dump({"goals": goals}, sort_keys=False, allow_unicode=True)


def seed_person(template: str, name: str, slug: str, today: str) -> str:
    return (
        template.replace("<Name> (<people.yaml slug>)", f"{name} ({slug})")
        .replace("**Updated:** YYYY-MM-DD", f"**Updated:** {today}")
        .replace(
            "- **Current focus:** <epic/slug + one line>",
            "- **Current focus:** (seeded — fill from first /manager review)",
        )
        .replace(
            "- **Load:** <light | normal | heavy> — <why, with receipts>",
            "- **Load:** unknown — not yet reviewed",
        )
    )


SCAFFOLDS = {
    "risks.yaml": "# Manager ledger — risks. Schema: management/ledger/README.md\nrisks: []\n",
    "commitments.yaml": "# Manager ledger — commitments. Schema: management/ledger/README.md\ncommitments: []\n",
    "watchlist.yaml": "# Manager ledger — watchlist. Schema: management/ledger/README.md\nwatchlist: []\n",
    "decisions.md": "# Decision log\n<!-- newest first; format in management/ledger/README.md -->\n",
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--owner", help="canonical slug to exclude from people/ seeding")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    today = date.today().isoformat()
    people_cfg = yaml.safe_load(PEOPLE_YAML.read_text()).get("people", [])
    team = [p for p in people_cfg if p.get("scope") == "team" and p.get("canonical")]
    owner = args.owner or detect_owner_slug(people_cfg)
    if owner is None:
        print("WARN: owner slug not resolved (sources_config unavailable?) — "
              "seeding a people file for EVERY team member incl. you; pass --owner to exclude.")

    template = PERSON_TEMPLATE.read_text()

    planned: list[tuple[Path, str]] = [(LEDGER / "goals.yaml", seed_goals(today))]
    planned += [(LEDGER / fn, body) for fn, body in SCAFFOLDS.items()]
    for p in team:
        slug = p["canonical"]
        if slug == owner:
            continue
        planned.append(
            (LEDGER / "people" / f"{slug}.md", seed_person(template, p.get("name", slug), slug, today))
        )

    written, skipped = [], []
    for path, body in planned:
        if path.exists():
            skipped.append(path)
            continue
        if not args.dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
        written.append(path)
    if not args.dry_run:
        (LEDGER / "briefs").mkdir(exist_ok=True)

    verb = "would write" if args.dry_run else "wrote"
    print(f"{verb} {len(written)} file(s); skipped {len(skipped)} existing (never overwritten)")
    for p in written:
        print(f"  + {p.relative_to(ROOT)}")
    for p in skipped:
        print(f"  = {p.relative_to(ROOT)} (exists)")
    print("\nNext: open a /manager session and do the first 'still true?' review of the seeds.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
