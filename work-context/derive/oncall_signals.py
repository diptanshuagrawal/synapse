"""oncall_signals.py — SINGLE SOURCE OF TRUTH for on-call identity.

On-call work follows the @oncall HANDLE + the oncall bot ORG-WIDE, NOT alert-channel
NAME heuristics (validated 2026-06-23: on-call incidents span plain domain channels —
lending / recon / liabilities, not alert-named — reached via the @oncall subteam ping).

Consumed by bin/standup_gather.py (on-call ops) AND derive/retro_census.py +
derive/person_census.py (incident detection) so every skill detects on-call work the
SAME way. Config-driven; fail-soft (absent config → empty, never crash).
"""
from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_CHANNELS_YAML = _ROOT / "config" / "slack_channels.yaml"
_SUBTEAMS_YAML = _ROOT / "config" / "team_subteams.yaml"


def oncall_channel_ids() -> set[str]:
    """Channel ids flagged `class: oncall` in slack_channels.yaml (the oncall bot hubs)."""
    import yaml
    try:
        chs = yaml.safe_load(_CHANNELS_YAML.read_text())["channels"]
        return {c["id"] for c in chs if (c.get("class") or "") == "oncall" and c.get("id")}
    except Exception:
        return set()


def oncall_handle_tokens() -> list[str]:
    """`<!subteam^S…` ping-token prefixes for team_subteams handles whose name contains
    'oncall'/'on-call'. A message body containing one of these = someone paged on-call."""
    import yaml
    try:
        st = yaml.safe_load(_SUBTEAMS_YAML.read_text()).get("subteams", [])
        out = []
        for s in st:
            h = (s.get("handle") or "").lower()
            if s.get("id") and ("oncall" in h or "on-call" in h):
                out.append(f"<!subteam^{s['id']}")
        return out
    except Exception:
        return []


def pings_oncall(text: str | None, tokens: list[str] | None = None) -> bool:
    """True if `text` contains any @oncall handle ping token. Pass `tokens` (loaded once
    per run via oncall_handle_tokens()) to avoid re-reading config per call."""
    if not text:
        return False
    toks = tokens if tokens is not None else oncall_handle_tokens()
    return any(t in text for t in toks)
