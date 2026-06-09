#!/usr/bin/env python3
"""
ownership_resolve.py — content-first ownership resolution.

Ownership is resolved from the WORK (a subject's classified `domains`), not
from who posted the message or which channel it landed in. This is the fix for
the cross-team recall hole that mis-attributed the zero-downtime year-end close
to a sister team because of the broadcast-channel author.

Priority of signals (highest first):
  1. CONTENT — `domains` → owning team(s) via `config/domain_team_map.yaml`.
     Dominant-domain team = primary; other domains' teams = co_owners.
  2. CHAT verdict — the LLM's `owned_by_primary` (already content-aware), used
     when the subject has no mappable domains.
  3. IDENTITY tiebreaker — author/channel rules (in ownership_corrections.py),
     used only when both above are empty.

Public API:
  load_map() -> dict
  resolve(domains: list[str], chat_primary, chat_co, m) -> (primary, co_owners, basis)
    basis ∈ {content, chat, none} — tells the caller which signal decided it,
    so the census can surface identity-fallback usage.

This module does NOT write the DB. `ownership_corrections.py` calls it to set
content-first ownership before applying identity fallbacks.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MAP_PATH = _REPO_ROOT / "config" / "domain_team_map.yaml"

sys.path.insert(0, str(_REPO_ROOT))
from derive.sources_config import home_team  # noqa: E402


def load_map() -> dict:
    import yaml
    if not _MAP_PATH.exists():
        return {"default_team": home_team(), "overrides": {}, "review": []}
    return yaml.safe_load(_MAP_PATH.read_text()) or {}


def _team_for_domain(slug: str, m: dict) -> tuple[str, list[str]]:
    """Return (primary_team, co_owner_teams) for a single domain slug."""
    ov = (m.get("overrides") or {}).get(slug)
    if ov:
        return ov.get("primary", m.get("default_team", home_team())), list(ov.get("co", []) or [])
    return m.get("default_team", home_team()), []


def resolve(domains: list[str], chat_primary: str | None,
            chat_co: list[str] | None, m: dict) -> tuple[str | None, list[str], str]:
    """Resolve ownership content-first.

    Returns (primary, co_owners, basis). basis ∈ {"content","chat","none"}.
    """
    domains = [d for d in (domains or []) if d]
    if domains:
        # Tally team weights across domains. First domain is dominant (chat
        # emits domains dominant-first; epic-anchor also fronts the epic slug).
        primary_team, primary_co = _team_for_domain(domains[0], m)
        owners: dict[str, int] = {primary_team: 100}
        co: dict[str, int] = {}
        for c in primary_co:
            co[c] = co.get(c, 0) + 10
        for d in domains[1:]:
            pt, pco = _team_for_domain(d, m)
            if pt != primary_team:
                co[pt] = co.get(pt, 0) + 5
            for c in pco:
                if c != primary_team:
                    co[c] = co.get(c, 0) + 2
        co_list = [t for t in co if t != primary_team]
        return primary_team, co_list, "content"

    # No domains → defer to chat verdict (already content-aware on title/body).
    if chat_primary:
        return chat_primary, list(chat_co or []), "chat"

    # Nothing → identity tiebreaker handled downstream.
    return None, [], "none"


def review_slugs(m: dict) -> list[str]:
    return list(m.get("review", []) or [])


def unmapped_domains(all_domains: set[str], m: dict) -> list[str]:
    """Domains that are neither in overrides nor implicitly default — there is
    no truly 'unmapped' slug (default catches all), so this reports slugs that
    fell to default_team AND are not flagged for review, for visibility."""
    ov = set((m.get("overrides") or {}).keys())
    rev = set(m.get("review") or [])
    return sorted(d for d in all_domains if d not in ov and d not in rev)
