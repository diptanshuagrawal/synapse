"""Deterministic guard for ticketize candidates — proves the two invariants are
enforced by code (people.yaml + events.db), not by a model following a prose rule.

Regression for 2026-06-24: a DETECT pass confabulated a reporter's human name and
pasted an evidence link from an unrelated thread; both shipped to a ticket. These
tests lock the checks that now block exactly those two failure modes.
"""
import importlib.util
import sqlite3
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "bin" / "ticketize_validate.py"
_spec = importlib.util.spec_from_file_location("ticketize_validate", SRC)
tv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tv)

# people.yaml ground truth (all scopes): U1=Alice, U2=Bob, U3=Carol
BY_ID = {
    "U1": {"name": "Alice Example", "canon": "alice"},
    "U2": {"name": "Bob Example", "canon": "bob"},
    "U3": {"name": "Carol Example", "canon": "carol"},
}
BY_CANON = {v["canon"]: {"name": v["name"], "sid": k} for k, v in BY_ID.items()}

# evidence thread CHAN/1700000000.000100 authored by U1 (root) + U2 (reply) only.
THREAD_TS = "1700000000.000100"
EV = f"https://x.slack.com/archives/CHAN/p{THREAD_TS.replace('.', '')}"


def _db(path):
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE events (id TEXT, source TEXT, actor TEXT, subject TEXT, "
        "channel_id TEXT, thread_ts TEXT)"
    )
    conn.executemany(
        "INSERT INTO events (id,source,actor,subject,channel_id,thread_ts) VALUES (?,?,?,?,?,?)",
        [
            (f"slack:CHAN:{THREAD_TS}", "slack", "U1", f"slack:CHAN:{THREAD_TS}", "CHAN", None),
            (f"slack:CHAN:{THREAD_TS}:1700000000.000200", "slack", "U2", f"slack:CHAN:{THREAD_TS}", "CHAN", THREAD_TS),
        ],
    )
    conn.commit()
    return conn


def test_clean_candidate_passes(tmp_path):
    conn = _db(tmp_path / "e.db")
    cands = [{"label": "E1", "decision": "pending", "reporter": "U1 (Alice Example)", "evidence": EV}]
    assert tv.validate_candidates(cands, BY_ID, BY_CANON, conn) == []


def test_confabulated_reporter_name_is_caught(tmp_path):
    conn = _db(tmp_path / "e.db")
    cands = [{"label": "E1", "decision": "pending", "reporter": "U1 (Wrong Person)", "evidence": EV}]
    viol = tv.validate_candidates(cands, BY_ID, BY_CANON, conn)
    assert any("invented name" in v for v in viol), viol


def test_unknown_reporter_id_is_caught(tmp_path):
    conn = _db(tmp_path / "e.db")
    cands = [{"label": "E1", "decision": "pending", "reporter": "U999 (Ghost)", "evidence": EV}]
    viol = tv.validate_candidates(cands, BY_ID, BY_CANON, conn)
    assert any("not in people.yaml" in v for v in viol), viol


def test_mis_grounded_evidence_link_is_caught(tmp_path):
    # Carol (U3) is a real person, name matches — but she is NOT in the evidence thread.
    conn = _db(tmp_path / "e.db")
    cands = [{"label": "E1", "decision": "pending", "reporter": "U3 (Carol Example)", "evidence": EV}]
    viol = tv.validate_candidates(cands, BY_ID, BY_CANON, conn)
    assert any("NOT an author" in v for v in viol), viol


def test_nonslack_evidence_skips_thread_check(tmp_path):
    # A GitHub PR evidence link can't be thread-checked — name still verified, no false positive.
    conn = _db(tmp_path / "e.db")
    cands = [{"label": "E1", "decision": "pending", "reporter": "U3 (Carol Example)",
              "evidence": "https://github.com/org/repo/pull/123"}]
    assert tv.validate_candidates(cands, BY_ID, BY_CANON, conn) == []


def test_created_candidate_not_gated(tmp_path):
    # Already-created rows are left alone even if attribution looks off.
    conn = _db(tmp_path / "e.db")
    cands = [{"label": "E1", "decision": "created", "reporter": "U1 (Wrong Person)", "evidence": EV}]
    assert tv.validate_candidates(cands, BY_ID, BY_CANON, conn) == []


def test_canonical_reporter_resolves(tmp_path):
    conn = _db(tmp_path / "e.db")
    cands = [{"label": "E1", "decision": "pending", "reporter": "alice", "evidence": EV}]
    assert tv.validate_candidates(cands, BY_ID, BY_CANON, conn) == []
