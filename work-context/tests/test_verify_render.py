"""derive/verify_render.py — render-verify gate.

verify() is the Phase 5.5 check: every cite in the manifest must actually appear
in the rendered prose (verbatim jira/PR ids, page id-or-title, flag/caveat
markers-or-evidence). It returns the missing tokens — a non-empty result fails
the render. Pure (manifest dict + text in, list out).
"""

from __future__ import annotations

from derive import verify_render as vr


def _manifest(tokens, titles=None, flags=None):
    return {"verify_manifest": tokens, "cite_titles": titles or {}, "flags": flags or []}


# ── jira / PR ids — verbatim ─────────────────────────────────────────────────

def test_jira_id_present_and_missing():
    man = _manifest(["EX-2301", "org/repo#10"])
    assert vr.verify(man, "shipped EX-2301 via org/repo#10") == []
    assert vr.verify(man, "shipped EX-2301 only") == ["org/repo#10"]


# ── page: id or title ────────────────────────────────────────────────────────

def test_page_matches_by_id():
    man = _manifest(["page:123456789"])
    assert vr.verify(man, "see /pages/123456789/ for details") == []


def test_page_matches_by_title():
    man = _manifest(["page:123456789"], titles={"page:123456789": "Ledger Design"})
    assert vr.verify(man, "per the Ledger Design doc") == []        # title match
    assert vr.verify(man, "no reference at all") == ["page:123456789"]


# ── flag: marker or evidence ─────────────────────────────────────────────────

def test_flag_satisfied_by_evidence_subject():
    man = _manifest(["flag:rollback"],
                    flags=[{"kind": "rollback", "evidence": [{"subject": "EX-2301"}]}])
    assert vr.verify(man, "rolled it back, see EX-2301") == []      # evidence subject present


def test_flag_missing_when_no_marker_or_evidence():
    man = _manifest(["flag:rollback"],
                    flags=[{"kind": "rollback", "evidence": [{"subject": "EX-9"}]}])
    # neither the evidence subject nor a rollback marker appears → missing.
    assert vr.verify(man, "everything went fine") == ["flag:rollback"]
