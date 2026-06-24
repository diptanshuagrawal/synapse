"""derive/slack_discover_usergroups.classify — subteam bucketing.

classify() is the pure core of the two-layer user-group discovery: given the
raw usergroups + the owner uid + the team roster, it sorts each group into
manager / team / ambiguous / configured (or drops it). No IO — exercised with
synthetic data. Short fake ids (S0…/U0… with ≤5-char tails) dodge the publish
leak gate's {8,9}-char platform-id pattern.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from derive import slack_discover_usergroups as ug  # noqa: E402

OWNER = "U0OWNER"
# roster = owner + 3 direct reports
TEAM = {OWNER: "Owner", "U0REP1": "Rep One", "U0REP2": "Rep Two", "U0REP3": "Rep Three"}


def _g(sid, users, name="grp", handle=None):
    return {"id": sid, "handle": handle or sid, "name": name, "users": users}


def test_classify_buckets_each_group_by_owner_and_report_membership():
    groups = [
        _g("S0MGR", [OWNER, "U0REP1"]),               # owner + <2 reports  → manager
        _g("S0TEAM", ["U0REP1", "U0REP2", "U0REP3"]),  # no owner + >=2 reps → team
        _g("S0AMBI", [OWNER, "U0REP1", "U0REP2"]),     # owner + >=2 reports → ambiguous
        _g("S0CONF", [OWNER, "U0REP1"]),               # already configured  → configured
        _g("S0SKIP", [OWNER, "U0REP1"]),               # skiplisted          → dropped
        _g("S0NONE", ["U0X1", "U0X2"]),                # no owner, no reports → dropped
        _g("C0CHAN", [OWNER, "U0REP1"]),               # not an S-id          → dropped
        _g("S0MTY", []),                               # no members           → dropped
    ]
    out = ug.classify(
        groups, OWNER, TEAM,
        existing={"S0CONF": True},   # True → configured as a MANAGER layer
        skiplist={"S0SKIP"},
    )

    assert [r["id"] for r in out["manager"]] == ["S0MGR"]
    assert [r["id"] for r in out["team"]] == ["S0TEAM"]
    assert [r["id"] for r in out["ambiguous"]] == ["S0AMBI"]
    assert [r["id"] for r in out["configured"]] == ["S0CONF"]
    assert out["configured"][0]["layer"] == "manager"

    # dropped groups appear in no bucket
    placed = {r["id"] for b in out.values() for r in b}
    assert placed == {"S0MGR", "S0TEAM", "S0AMBI", "S0CONF"}
    for dropped in ("S0SKIP", "S0NONE", "C0CHAN", "S0MTY"):
        assert dropped not in placed

    # report attribution is carried for display
    team_rec = out["team"][0]
    assert team_rec["reports"] == 3
    assert sorted(team_rec["report_names"]) == ["Rep One", "Rep Three", "Rep Two"]
    assert team_rec["owner_in"] is False


def test_classify_team_threshold_is_reports_min_for_team():
    # exactly REPORTS_MIN_FOR_TEAM reports (no owner) → team; one fewer → dropped.
    reps = list(TEAM)[1:1 + ug.REPORTS_MIN_FOR_TEAM]
    out = ug.classify([_g("S0EDGE", reps)], OWNER, TEAM, existing={}, skiplist=set())
    assert [r["id"] for r in out["team"]] == ["S0EDGE"]

    out2 = ug.classify([_g("S0LOW", reps[:1])], OWNER, TEAM, existing={}, skiplist=set())
    assert all(not b for b in out2.values())  # one report, owner absent → no bucket
