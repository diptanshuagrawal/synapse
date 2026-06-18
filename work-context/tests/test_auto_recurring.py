"""derive/auto_recurring.py — recurring-template cluster detection.

_classify_cluster_by_content inspects a cluster's member bodies and labels it as
a recurring template (channel-join, subteam pings, standup nudges, bot
notifications, or a shared body-prefix) when ≥80% of members match — so noise
clusters get auto-labelled instead of burning a chat-labeling slot. Pure
function over (subject, actor, body) tuples.
"""

from __future__ import annotations

import pytest

from derive import auto_recurring as ar


def _members(*bodies):
    return [(f"s{i}", "U0X", b) for i, b in enumerate(bodies)]


def test_no_members():
    label, dbg = ar._classify_cluster_by_content(1, [])
    assert label is None and dbg["reason"] == "no_members"


def test_channel_join_template():
    m = _members(*["<@U0ALICE> has joined the channel"] * 5)
    label, _ = ar._classify_cluster_by_content(1, m)
    assert label and "channel-membership" in label


def test_standup_nudge_template():
    m = _members(*["please join standup folks"] * 4)
    label, _ = ar._classify_cluster_by_content(1, m)
    assert label and "standup" in label.lower()


def test_order_resolution_bot_template():
    m = _members(*["Issue Resolved: order 123"] * 5)
    label, _ = ar._classify_cluster_by_content(1, m)
    assert label and "bot notifications" in label


def test_shared_prefix_template():
    m = _members(*["Release branch for today is ready"] * 5)
    label, _ = ar._classify_cluster_by_content(1, m)
    assert label and label.startswith("Recurring template:")


def test_below_threshold_not_flagged():
    # only 2/5 are channel-join (<80%) → no template label.
    m = _members(
        "<@U0A> has joined the channel",
        "<@U0B> has joined the channel",
        "real discussion about the payout bug",
        "more substantive design talk",
        "another genuine thread",
    )
    label, dbg = ar._classify_cluster_by_content(1, m)
    assert label is None
    assert dbg["regex_hits"]["channel_join"] == 2


def test_mixed_real_content_not_flagged():
    m = _members("designing the ledger schema", "fixing payout rounding", "TRD review")
    assert ar._classify_cluster_by_content(1, m)[0] is None


# ── thin-label regex ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("label,thin", [
    ("Insufficient content", True),
    ("Sparse cluster", True),
    ("Untriaged", True),
    ("Payout withholding rollout", False),
])
def test_thin_label_pattern(label, thin):
    assert bool(ar._THIN_LABEL_PATTERNS.search(label)) is thin
