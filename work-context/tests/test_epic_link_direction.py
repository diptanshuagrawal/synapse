"""initiative <-> epic link direction.

A reversed link still resolves, so the planner renders the epic either way and nothing
looks broken — but Jira Product Discovery only treats "epic implements initiative" as the
idea's delivery epic. That silence is why this needs a test rather than a code comment.

Stored link semantics (confirmed against the live instance):
    inwardIssue <type.outward> outwardIssue
so for 'Polaris work item link' (outward='implements') the EPIC must be the inwardIssue.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "derive"))

import capacity_engine as ce


class _Recorder:
    """Stands in for _jira: no network, just records what would be sent."""

    def __init__(self, links=None):
        self.calls = []
        self.links = links or []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET" and "issuelinks" in path:
            return {"fields": {"issuelinks": self.links}}
        return {}


def _link_call(rec):
    return next(b for m, p, b in rec.calls if m == "POST" and p == "/rest/api/3/issueLink")


def test_epic_is_the_inward_issue(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(ce, "_jira", rec)
    monkeypatch.setattr(ce, "INITIATIVE_LINK_TYPE", "Polaris work item link")
    ce._link_initiative_epic("OINT-1", "BOARD-2")
    body = _link_call(rec)
    assert body["inwardIssue"]["key"] == "BOARD-2", \
        "the EPIC must be inwardIssue, else JPD reads it as 'is implemented by'"
    assert body["outwardIssue"]["key"] == "OINT-1"
    assert body["type"]["name"] == "Polaris work item link"


def test_link_is_idempotent(monkeypatch):
    """An already-linked pair must not create a second link."""
    rec = _Recorder(links=[{"id": "1", "inwardIssue": {"key": "BOARD-2"}}])
    monkeypatch.setattr(ce, "_jira", rec)
    r = ce._link_initiative_epic("OINT-1", "BOARD-2")
    assert r == {"already": True}
    assert not [c for c in rec.calls if c[0] == "POST"]


def test_audit_flags_only_reversed_links(monkeypatch):
    """From the initiative's side the epic sits in inwardIssue when correct."""
    good = {"id": "10", "type": {"name": "Polaris work item link"},
            "inwardIssue": {"key": "BOARD-9"}}
    bad = {"id": "11", "type": {"name": "Polaris work item link"},
           "outwardIssue": {"key": "BOARD-8"}}
    other_type = {"id": "12", "type": {"name": "Blocks"}, "outwardIssue": {"key": "BOARD-7"}}
    rows = {"OINT-A": [good], "OINT-B": [bad], "OINT-C": [other_type]}

    def fake_jira(method, path, body=None):
        key = path.split("/issue/")[1].split("?")[0]
        return {"fields": {"issuelinks": rows[key]}}

    monkeypatch.setattr(ce, "_jira", fake_jira)
    monkeypatch.setattr(ce, "JIRA_PROJECT", "BOARD")
    monkeypatch.setattr(ce, "INITIATIVE_LINK_TYPE", "Polaris work item link")
    monkeypatch.setattr(ce, "pod_initiatives", lambda pods=None: {"initiatives": [
        {"key": k, "epic": {"key": "BOARD-x"}} for k in rows]})

    r = ce.audit_epic_links(fix=False)
    assert r["checked"] == 3
    assert [x["initiative"] for x in r["reversed"]] == ["OINT-B"], r["reversed"]
    assert r["fixed"] == [] and r["broken"] == [], "report mode must write nothing"


def test_audit_report_mode_never_writes(monkeypatch):
    seen = []

    def fake_jira(method, path, body=None):
        seen.append(method)
        return {"fields": {"issuelinks": [
            {"id": "11", "type": {"name": "Polaris work item link"},
             "outwardIssue": {"key": "BOARD-8"}}]}}

    monkeypatch.setattr(ce, "_jira", fake_jira)
    monkeypatch.setattr(ce, "JIRA_PROJECT", "BOARD")
    monkeypatch.setattr(ce, "INITIATIVE_LINK_TYPE", "Polaris work item link")
    monkeypatch.setattr(ce, "pod_initiatives",
                        lambda pods=None: {"initiatives": [{"key": "OINT-B", "epic": {"key": "BOARD-8"}}]})
    ce.audit_epic_links(fix=False)
    assert set(seen) == {"GET"}, f"report mode issued {seen}"


def test_audit_repair_deletes_then_recreates_correctly(monkeypatch):
    calls = []

    def fake_jira(method, path, body=None):
        calls.append((method, path, body))
        return {"fields": {"issuelinks": [
            {"id": "11", "type": {"name": "Polaris work item link"},
             "outwardIssue": {"key": "BOARD-8"}}]}}

    monkeypatch.setattr(ce, "_jira", fake_jira)
    monkeypatch.setattr(ce, "JIRA_PROJECT", "BOARD")
    monkeypatch.setattr(ce, "INITIATIVE_LINK_TYPE", "Polaris work item link")
    monkeypatch.setattr(ce, "pod_initiatives",
                        lambda pods=None: {"initiatives": [{"key": "OINT-B", "epic": {"key": "BOARD-8"}}]})
    monkeypatch.setattr(ce, "_link_initiative_epic",
                        lambda i, e: calls.append(("RELINK", i, e)) or {"already": False})

    r = ce.audit_epic_links(fix=True)
    assert [x["initiative"] for x in r["fixed"]] == ["OINT-B"]
    assert ("DELETE", "/rest/api/3/issueLink/11", None) in calls
    # the recreate must come after the delete, and use the right pair
    assert ("RELINK", "OINT-B", "BOARD-8") in calls
    assert calls.index(("DELETE", "/rest/api/3/issueLink/11", None)) \
        < calls.index(("RELINK", "OINT-B", "BOARD-8"))


def test_failed_recreate_is_reported_not_swallowed(monkeypatch):
    """Delete succeeded, recreate failed -> the pair is now unlinked. Say so loudly."""
    def fake_jira(method, path, body=None):
        return {"fields": {"issuelinks": [
            {"id": "11", "type": {"name": "Polaris work item link"},
             "outwardIssue": {"key": "BOARD-8"}}]}}

    def boom(i, e):
        raise RuntimeError("403")

    monkeypatch.setattr(ce, "_jira", fake_jira)
    monkeypatch.setattr(ce, "JIRA_PROJECT", "BOARD")
    monkeypatch.setattr(ce, "INITIATIVE_LINK_TYPE", "Polaris work item link")
    monkeypatch.setattr(ce, "pod_initiatives",
                        lambda pods=None: {"initiatives": [{"key": "OINT-B", "epic": {"key": "BOARD-8"}}]})
    monkeypatch.setattr(ce, "_link_initiative_epic", boom)

    r = ce.audit_epic_links(fix=True)
    assert r["fixed"] == []
    assert len(r["broken"]) == 1
    b = r["broken"][0]
    assert b["initiative"] == "OINT-B" and b["epic"] == "BOARD-8"
    assert "link now missing" in b["error"]
