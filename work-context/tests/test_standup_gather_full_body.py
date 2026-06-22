"""Guard: standup_gather's detection scans must read the FULL message body.

Regression for 2026-06-22: the owner @-ask scan tested ASK_RE against a
`substr(body, 1, 260)` slice. A real EM ping (`@tech-managers … Action Items …
close by Monday`) opened with a subteam handle + a long cc-list of @mentions +
a tracker URL, pushing the ask keywords past char 260 — so the regex never saw
them and the ask was silently dropped from "Your queue". The same truncation hid
leave notes and trailing `cc: <@owner>` mentions.

Invariant: any query whose rows feed LEAVE_RE / ASK_RE / NOISE_RE or a
`<@uid>` / `<!subteam^…>` substring check selects the full `body` column, never
`substr(body, …)`. Display trimming happens in Python (`snip = …[:N]`).
"""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "bin" / "standup_gather.py"


def _source():
    return SRC.read_text()


def test_leave_scan_selects_full_body():
    src = _source()
    # The LEAVE_RE scan query (ORDER BY ts over the slack window) must select full body.
    assert "SELECT actor,ts,body,subject FROM events" in src, \
        "leave-signal scan must SELECT full body (LEAVE_RE runs on it), not substr(body,…)"


def test_member_and_owner_ask_scans_select_full_body():
    src = _source()
    # slack_recent (member @-asks) and slack_owner_recent (owner @-asks) share this prefix.
    assert "SELECT ts,channel_id,thread_ts,actor,body,subject FROM events" in src, \
        "member/owner @-ask scans must SELECT full body (ASK_RE + mention checks run on it)"


def test_no_band_aid_slice():
    """The interim 2000-char slice must be gone — detection uses full body, not a bigger cut.

    (The positive assertions above lock the three detection queries to full `body`; if a
    truncation is reintroduced there, the exact SELECT strings change and they fail. The
    display-only `slack_auth` echo and the github PR-index desc may still use substr — they
    feed no matcher.)
    """
    src = _source()
    assert "substr(body,1,2000)" not in src, \
        "the 2000-char band-aid is still present — use full body, not a bigger slice"
