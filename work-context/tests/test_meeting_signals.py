"""derive/meetings/signals.owner_facing_todos — the owner-facing To-do filter
that feeds Steno's "My action items" view and the /ask "what do I need to do"
path. Guards attribution honesty: teammate-owned work is NOT an owner to-do, and
an unresolved owner never gets items guessed onto it.
"""

from __future__ import annotations

import json

from derive.meetings import signals as sg

OWNER = "owner-handle"          # fake canonical handle (no real-ID leak)
MATE = "teammate-handle"


def _seed(tmp_path, monkeypatch):
    state = tmp_path / "meeting_signals.json"
    monkeypatch.setattr(sg, "STATE", state)
    data = {
        "actions": [
            {"id": "a-own", "assignee": OWNER, "action": "owner does X",
             "subject": "meeting:2026-07-20:sync", "status": "open"},
            {"id": "a-un", "assignee": "(unassigned)", "action": "someone does Y",
             "subject": "meeting:2026-07-20:sync", "status": "open"},
            {"id": "a-mate", "assignee": MATE, "action": "mate does Z",
             "subject": "meeting:2026-07-20:sync", "status": "open"},
            {"id": "a-done", "assignee": OWNER, "action": "already handled",
             "subject": "meeting:2026-07-20:sync", "status": "done"},
        ],
        "commitments": [
            {"id": "c-own", "person": OWNER, "promise": "owner promised P",
             "subject": "meeting:2026-07-19:plan", "status": "open"},
            {"id": "c-mate", "person": MATE, "promise": "mate promised Q",
             "subject": "meeting:2026-07-19:plan", "status": "open"},
        ],
        "asks": [
            {"id": "k-1", "person": MATE, "ask": "can you decide R?",
             "subject": "meeting:2026-07-18:review", "status": "open"},
        ],
        "untracked": [
            {"id": "u-1", "person": MATE, "work": "flaky test cleanup",
             "subject": "meeting:2026-07-18:review", "status": "open"},
        ],
    }
    state.write_text(json.dumps(data))
    return state


def test_owner_facing_includes_owner_unassigned_and_asks(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    ids = {i["id"] for i in sg.owner_facing_todos(OWNER)}
    # owner action, unassigned action, owner commitment, the ask
    assert ids == {"a-own", "a-un", "c-own", "k-1"}


def test_owner_facing_excludes_teammate_and_resolved(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    ids = {i["id"] for i in sg.owner_facing_todos(OWNER)}
    assert "a-mate" not in ids      # teammate-assigned action
    assert "c-mate" not in ids      # teammate commitment
    assert "a-done" not in ids      # resolved
    assert "u-1" not in ids         # untracked is not an owner to-do


def test_unresolved_owner_never_guesses(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    # No owner handle → only unassigned actions + asks; never the owner's/teammate's.
    ids = {i["id"] for i in sg.owner_facing_todos(None)}
    assert ids == {"a-un", "k-1"}


def test_normalized_shape_and_untracked(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    rows = {i["id"]: i for i in sg.owner_facing_todos(OWNER)}
    r = rows["a-own"]
    assert r["kind"] == "action" and r["text"] == "owner does X"
    assert r["who"] == OWNER and r["subject"] == "meeting:2026-07-20:sync"
    assert rows["c-own"]["kind"] == "commitment"
    assert rows["k-1"]["kind"] == "ask"
    unt = sg.owner_untracked()
    assert [u["id"] for u in unt] == ["u-1"] and unt[0]["kind"] == "untracked"
