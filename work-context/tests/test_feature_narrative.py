"""derive/feature_narrative.py — feature lifecycle narrative render.

render() fuses compute_stages (planning→rollout) with the CMR release stream
into a markdown narrative for one feature. Driven on the seed with a built
FeatureArtefacts + an in-test feature_release row. Pure helpers (_date,
_days_between, _outcome_tally) pinned too.
"""

from __future__ import annotations

import pytest

from derive import feature_narrative as fn
from derive.feature_resolve import FeatureArtefacts


def _fa(release_cmrs=None):
    return FeatureArtefacts(
        slug="payments", name="Payments", epics=["EX-2238"], jira=["EX-2301"],
        github=["org/repo#10"], confluence=["page:123456789"], slack=[],
        declared_confluence=[], release_cmrs=release_cmrs or [], mode="slug")


def test_date():
    assert fn._date("2026-06-10T09:00:00Z") == "2026-06-10"
    assert fn._date(None) == "—"


def test_days_between():
    assert fn._days_between("2026-06-01T00:00:00Z", "2026-06-11T00:00:00Z") == 10
    assert fn._days_between(None, "2026-06-11T00:00:00Z") is None
    assert fn._days_between("bad", "worse") is None


def test_outcome_tally():
    stream = [{"outcome": "released"}, {"outcome": "released"}, {"outcome": "rolled_back"}]
    assert fn._outcome_tally(stream) == {"released": 2, "rolled_back": 1}


def test_release_stream_and_render(seeded_db):
    # add a feature_release row so the stream is non-empty.
    seeded_db.execute(
        "INSERT INTO feature_release (cmr_subject, slug, released_at, approved_by, "
        "release_owner, outcome, pr_urls_json, title, url) VALUES "
        "('CMR-1','payments','2026-06-04T00:00:00Z','bob','alice','released',"
        "'[\"org/repo#10\"]','payout deploy','u')")
    seeded_db.commit()
    fa = _fa(release_cmrs=["CMR-1"])
    stream = fn._release_stream(seeded_db, fa)
    assert len(stream) == 1 and stream[0]["outcome"] == "released"
    md = fn.render(seeded_db, fa)
    assert isinstance(md, str) and "Payments" in md


def test_render_no_releases(seeded_db):
    # no release_cmrs → empty stream, but stages still render.
    md = fn.render(seeded_db, _fa())
    assert isinstance(md, str) and md.strip()
