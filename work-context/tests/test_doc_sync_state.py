"""derive/doc_sync_state.py — doc-drift finding state store.

The state machine behind the doc-sync sweep: a stable finding key (so the same
drift never re-posts), owner-mention formatting, the yaml-region locator for the
discovery inventory, and the sqlite record/fetch round-trip (open-only filter).
DB_PATH is redirected to a temp file — never touches the real doc_sync.db.
"""

from __future__ import annotations

import pytest

from derive import doc_sync_state as ds


# ── _finding_key ─────────────────────────────────────────────────────────────

def test_finding_key_stable():
    a = ds._finding_key("123", "trd_drift", "Section A")
    assert a == ds._finding_key("123", "trd_drift", "Section A")
    assert len(a) == 16


def test_finding_key_normalises_anchor():
    # whitespace + case shifts must NOT mint a new key.
    assert ds._finding_key("123", "trd", "Hello  World") == \
           ds._finding_key("123", "trd", "hello world")


def test_finding_key_distinct_on_real_change():
    assert ds._finding_key("123", "trd", "A") != ds._finding_key("123", "trd", "B")
    assert ds._finding_key("123", "trd", "A") != ds._finding_key("999", "trd", "A")


# ── _mention / _owner_name ───────────────────────────────────────────────────

PMAP = {"acc1": {"name": "Alice", "slack_id": "U0ALICE"},
        "acc2": {"name": "Bob", "slack_id": None}}


def test_mention_prefers_slack_id():
    assert ds._mention("acc1", PMAP) == "<@U0ALICE>"


def test_mention_falls_back_to_name():
    assert ds._mention("acc2", PMAP) == "**Bob**"


def test_mention_unknown():
    assert ds._mention("ghost", PMAP) == "**(unknown owner)**"


def test_owner_name():
    assert ds._owner_name("acc1", PMAP) == "Alice"
    assert ds._owner_name("ghost", PMAP) == "ghost"


# ── _inv_region (yaml-bucket locator) ────────────────────────────────────────

def test_inv_region_finds_bucket():
    lines = [
        "monitor:",
        "  - id: 1",
        "needs_confirm:",
        "  - id: 2",
        "  - id: 3",
        "excluded:",
        "  - id: 4",
    ]
    start, end = ds._inv_region(lines, "needs_confirm")
    assert (start, end) == (2, 5)   # the needs_confirm block, up to 'excluded:'


def test_inv_region_missing():
    assert ds._inv_region(["monitor:"], "needs_confirm") == (None, None)


# ── record/fetch round-trip (temp DB) ────────────────────────────────────────

def test_record_and_fetch(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "DB_PATH", str(tmp_path / "doc_sync.db"))
    cols = ("comment_id", "finding_key", "page_id", "page_title", "page_url",
            "comment_url", "check_type", "finding_title", "anchor", "resolution_status")
    with ds._conn() as c:
        for cid, status in [("c1", "open"), ("c2", "resolved")]:
            c.execute(
                f"INSERT INTO doc_sync_comments ({','.join(cols)}) "
                f"VALUES ({','.join('?' * len(cols))})",
                (cid, ds._finding_key("p", "trd", cid), "p", "Page", "url",
                 "curl", "trd", "drift found", cid, status))
    all_rows = ds._fetch()
    assert {r["comment_id"] for r in all_rows} == {"c1", "c2"}
    open_rows = ds._fetch(open_only=True)
    assert {r["comment_id"] for r in open_rows} == {"c1"}   # 'resolved' filtered out
