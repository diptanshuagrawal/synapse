"""derive/person_v4_manifest.py — deterministic render-manifest selectors.

The manifest layer turns v3 + deepread dicts into a stable, prose-free render
spec (the determinism that killed /ask run-to-run variance). These pure
selectors/classifiers are the spec: phrase/regex body scans, jira/design/noise
predicates, shipped+designed ranking, DB bucketing, thread classification, and
the slack permalink. All operate on plain dicts/strings — no DB, no network.
"""

from __future__ import annotations

import pytest

from derive import person_v4_manifest as m


# ── _is_jira ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("subject,expected", [
    ("EX-2629", True), ("ABC-1", True),
    ("org/repo#10", False), ("slack:C:1", False), ("", False),
])
def test_is_jira(subject, expected):
    assert m._is_jira(subject) is expected


# ── _scan_phrases / _scan_regexes ────────────────────────────────────────────

def _body(subject, text, ts="2026-06-10T00:00:00Z", source="slack"):
    return {"subject": subject, "body": text, "ts": ts, "source": source}


def test_scan_phrases_hits_and_orders():
    bodies = [
        _body("s2", "all good, ROLLED BACK the change", ts="2026-06-10T02:00:00Z"),
        _body("s1", "we need to rollback now", ts="2026-06-10T01:00:00Z"),
        _body("s3", "unrelated chatter", ts="2026-06-10T03:00:00Z"),
    ]
    hits = m._scan_phrases(bodies, ["rollback", "rolled back"])
    cites = [h["subject"] for h in hits]
    assert cites == ["s1", "s2"]            # ts-ordered, s3 has no phrase
    assert all("phrase" in h and "snippet" in h for h in hits)


def test_scan_phrases_one_hit_per_body():
    bodies = [_body("s1", "rollback and rolled back both here")]
    assert len(m._scan_phrases(bodies, ["rollback", "rolled back"])) == 1


def test_scan_regexes():
    bodies = [_body("s1", "saved ~₹50,00,000 in fees")]
    hits = m._scan_regexes(bodies, [r"₹[\d,]+"])
    assert hits and hits[0]["match"].startswith("₹")


# ── _design_rank / _is_design_slug ───────────────────────────────────────────

def test_design_rank_priority():
    assert m._design_rank("Payments API Contract") == 0     # contract = top
    assert m._design_rank("Ledger TRD") == 1
    assert m._design_rank("local setup cloner") == 8        # misc = lowest


# ── _is_noise_label ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("label,noise", [
    ("channel-join membership event", True),
    ("standup reminder", True),
    ("Payout withholding rollout", False),
])
def test_is_noise_label(label, noise):
    assert m._is_noise_label(label) is noise


# ── _rank_shipped (jira-only, ranked) ────────────────────────────────────────

def test_rank_shipped_filters_and_ranks():
    items = [
        {"subject": "org/repo#10", "title": "a PR", "role": "AUTHOR"},   # not jira → dropped
        {"subject": "EX-2", "title": "small task", "role": "AUTHOR"},
        {"subject": "EX-1", "title": "big story", "role": "AUTHOR"},
    ]
    tmeta = {
        "EX-1": {"issue_type": "Story", "story_points": 8, "title": "big story"},
        "EX-2": {"issue_type": "Story", "story_points": 3, "title": "small task"},
    }
    out = m._rank_shipped(items, tmeta)
    assert [x["cite"] for x in out] == ["EX-1", "EX-2"]      # higher SP first; PR excluded


# ── _bucket_db ───────────────────────────────────────────────────────────────

def test_bucket_db():
    shipped = [
        {"title": "fix reader-db deadlock"},
        {"title": "add partition to events"},
        {"title": "unrelated UI tweak"},
    ]
    out = m._bucket_db(shipped)
    titles = [s["title"] for s in out]
    assert "fix reader-db deadlock" in titles and "unrelated UI tweak" not in titles


# ── _classify_thread ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("body,kind", [
    ("5xx spike on the gateway, paging oncall", "oncall"),
    ("prod readiness for the go-live", "release"),
    ("RCA for the outage", "incident"),
    ("hey can we sync tomorrow?", "coordination"),
])
def test_classify_thread(body, kind):
    assert m._classify_thread(body) == kind


# ── _slack_url ───────────────────────────────────────────────────────────────

def test_slack_url(monkeypatch):
    monkeypatch.setattr(m, "slack_workspace", lambda: "acme")
    assert m._slack_url("slack:C0A:1700000000.000100") == \
        "https://acme.slack.com/archives/C0A/p1700000000000100"
    assert m._slack_url("EX-1") is None
    assert m._slack_url("slack:bad") is None


# ── _designed (confluence pages ranked, inline-comments dropped) ─────────────

def test_designed_ranks_and_drops_inline():
    deep = {"confluence": [
        {"subject": "page:1", "title": "Inline comment on X", "body_bytes": 999},  # dropped
        {"subject": "page:2", "title": "Setup cloner", "body_bytes": 100},
        {"subject": "page:3", "title": "API Contract", "body_bytes": 50},
    ]}
    out = m._designed({}, deep)
    cites = [p["cite"] for p in out]
    assert "page:1" not in cites
    assert cites[0] == "page:3"   # contract (rank 0) ahead of setup (rank 8)
