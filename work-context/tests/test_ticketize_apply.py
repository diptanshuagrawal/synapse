"""bin/ticketize_apply.py — assignee resolution (the FAIL-LOUD guarantee).

resolve_assignee must NOT ship a human name to Jira as a bogus accountId: the old
`len(s) >= 16` heuristic swallowed full names (an 18-char full name passed the check).
_looks_like_account_id now requires an id SHAPE (no whitespace). ticketize_apply.py
lives at repo-root bin/ (outside the package), so it's loaded by path. Example account
ids below are synthetic placeholders (EXAMPLE-marked), not real workspace ids.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parent.parent.parent / "bin" / "ticketize_apply.py"
_spec = importlib.util.spec_from_file_location("ticketize_apply", _BIN)
ta = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ta)

RAW_ID = "EXAMPLE:abcd0123ef456789"   # synthetic accountId (colon form, no real id)


# ── _looks_like_account_id ───────────────────────────────────────────────────

@pytest.mark.parametrize("s", [
    RAW_ID,                          # colon form
    "5b10ac8d82e05b22cc7d4ef5",      # 24-char opaque id
    "EXAMPLE-ACCT-0123456789",       # opaque token, no spaces
])
def test_looks_like_account_id_accepts_id_shapes(s):
    assert ta._looks_like_account_id(s)


@pytest.mark.parametrize("s", [
    "Alice Example",   # full name (has spaces) — the bug: spaces => not an id
    "Bob Carol",
    "alice",           # too short
    "alice.e",
    "",
    None,
])
def test_looks_like_account_id_rejects_names_and_short(s):
    assert not ta._looks_like_account_id(s)


# ── resolve_assignee ─────────────────────────────────────────────────────────

def test_override_name_that_doesnt_resolve_fails_loud(monkeypatch):
    # Unresolvable override must NOT fall back to the candidate (owner) silently.
    monkeypatch.setattr(ta, "accountid_for", lambda t: None)
    acct, warn = ta.resolve_assignee("Alice Example", "owner-acct")
    assert acct is None
    assert warn and "Alice Example" in warn


def test_override_resolves_via_roster(monkeypatch):
    monkeypatch.setattr(ta, "accountid_for", lambda t: "acc-42" if t == "alice" else None)
    assert ta.resolve_assignee("alice", "owner-acct") == ("acc-42", None)


def test_override_raw_accountid_passes_through(monkeypatch):
    monkeypatch.setattr(ta, "accountid_for", lambda t: None)
    assert ta.resolve_assignee(RAW_ID, "owner-acct") == (RAW_ID, None)


def test_no_override_uses_candidate(monkeypatch):
    monkeypatch.setattr(ta, "accountid_for", lambda t: "acc-owner" if t == "owner" else None)
    assert ta.resolve_assignee("", "owner") == ("acc-owner", None)


def test_candidate_unresolvable_is_unassigned(monkeypatch):
    monkeypatch.setattr(ta, "accountid_for", lambda t: None)
    acct, warn = ta.resolve_assignee("", "Bob Carol")
    assert acct is None and warn
