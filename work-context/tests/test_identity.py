"""derive/identity_signals.py + identity_reconcile.py — identity self-heal.

Every ingest emits observed identity pairs; identity_reconcile back-fills
people.yaml from them. Cover the pure normalisation/ordering, the signal
upsert (dedup + n_obs), and the entry-value extraction the reconciler matches
on. The full reconcile() write-back is left to integration (it edits the live
people.yaml).
"""

from __future__ import annotations

import sqlite3

import pytest

from derive import identity_signals as isig
from derive import identity_reconcile as irec


# ── _normalize / _ordered (pure) ─────────────────────────────────────────────

def test_normalize_email_lowercased_and_stripped():
    assert isig._normalize("email", "  Alice@X.COM ") == "alice@x.com"


def test_normalize_non_email_preserves_case():
    assert isig._normalize("github", " Alice-GH ") == "Alice-GH"


def test_ordered_collapses_pair_direction():
    a = isig._ordered("github", "z", "email", "a@x")
    b = isig._ordered("email", "a@x", "github", "z")
    assert a == b   # (a,b) and (b,a) → same canonical tuple


# ── record_signal (DB upsert) ────────────────────────────────────────────────

@pytest.fixture
def sigconn():
    c = sqlite3.connect(":memory:")
    isig.init(c)
    yield c
    c.close()


def test_record_signal_inserts(sigconn):
    isig.record_signal(sigconn, "jira", "email", "a@x.com", "jira_id", "ACC1")
    sigconn.commit()
    n = sigconn.execute("SELECT COUNT(*) FROM identity_signals").fetchone()[0]
    assert n == 1


def test_record_signal_dedups_and_counts(sigconn):
    for _ in range(3):
        isig.record_signal(sigconn, "jira", "email", "a@x.com", "jira_id", "ACC1")
    sigconn.commit()
    rows = sigconn.execute("SELECT n_obs FROM identity_signals").fetchall()
    assert len(rows) == 1 and rows[0][0] == 3   # one row, observed 3×


def test_record_signal_direction_independent(sigconn):
    isig.record_signal(sigconn, "jira", "email", "a@x.com", "jira_id", "ACC1")
    isig.record_signal(sigconn, "slack", "jira_id", "ACC1", "email", "a@x.com")
    sigconn.commit()
    # both collapse to the same canonical pair → one row.
    assert sigconn.execute("SELECT COUNT(*) FROM identity_signals").fetchone()[0] == 1


def test_record_signal_noops(sigconn):
    isig.record_signal(sigconn, "jira", "email", None, "jira_id", "ACC1")   # missing value
    isig.record_signal(sigconn, "jira", "bogus", "x", "jira_id", "ACC1")    # invalid type
    isig.record_signal(sigconn, "jira", "email", "a@x", "email", "a@x")     # identical
    sigconn.commit()
    assert sigconn.execute("SELECT COUNT(*) FROM identity_signals").fetchone()[0] == 0


# ── collect_entry_values (pure) ──────────────────────────────────────────────

def test_collect_entry_values_maps_fields_to_types():
    entry = {
        "email": "a@x.com", "jira_id": "ACC1", "github": "alice-gh",
        "git_names": ["Alice Example", "A. Example"],
        "github_aliases": ["alice-old"],
    }
    out = irec.collect_entry_values(entry)
    assert out["email"] == {"a@x.com"}
    assert out["jira_id"] == {"ACC1"}
    assert out["git_name"] == {"Alice Example", "A. Example"}
    # github_aliases fold into the github type alongside the primary handle.
    assert out["github"] == {"alice-gh", "alice-old"}
