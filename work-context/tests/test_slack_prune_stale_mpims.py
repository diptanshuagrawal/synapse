"""derive/slack_prune_stale_mpims.py — staleness + yaml-block surgery.

The prune pass removes long-quiet MPIM channels from slack_channels.yaml.
_days_since (age), _last_event_iso (newest event on the seed), and
_remove_yaml_block (comment/row-preserving block deletion) are the pieces. Pure
except the seed-driven _last_event_iso.
"""

from __future__ import annotations

import pytest

from derive import slack_prune_stale_mpims as p


# ── _days_since ──────────────────────────────────────────────────────────────

def test_days_since_recent_is_small():
    from datetime import datetime, timezone
    now = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    assert p._days_since(now) < 1.0


def test_days_since_bad_input():
    assert p._days_since("not-a-date") == 999.0


# ── _last_event_iso (seed) ───────────────────────────────────────────────────

def test_last_event_iso(seeded_db):
    # seed's C0A thread: parent 07:00, reply 07:05 → max is the reply.
    assert p._last_event_iso(seeded_db, "C0A") == "2026-06-03T07:05:00Z"
    assert p._last_event_iso(seeded_db, "C0NONE") is None


# ── _remove_yaml_block ───────────────────────────────────────────────────────

YAML = """channels:
  - id: C0KEEP
    name: keep-me
  - id: C0DROP
    name: drop-me
    ingest_mode: full
  - id: C0KEEP2
    name: keep-too
"""


def test_remove_yaml_block_drops_only_target():
    out, changed = p._remove_yaml_block(YAML, "C0DROP")
    assert changed is True
    assert "C0DROP" not in out and "drop-me" not in out
    assert "C0KEEP" in out and "C0KEEP2" in out      # neighbours preserved


def test_remove_yaml_block_missing_is_noop():
    out, changed = p._remove_yaml_block(YAML, "C0ABSENT")
    assert changed is False and out == YAML
