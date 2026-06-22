"""Guard: pulse's leave-mention scan must read the FULL message body.

Regression for 2026-06-22 (sibling of the standup_gather fix, commit c316d42):
`_leave_mentions` selected `substr(body, 1, 160)` and then ran `LEAVE_RX.search`
against that 160-char slice. A real leave note routinely opens with a subteam
ping + a cc-list of @mentions before the keyword ("… will be off Thu/Fri"), so
the OOO/leave signal sits past char 160 → the regex never saw it → the leave
mention was silently dropped from the pulse.

Invariant: the query whose rows feed LEAVE_RX selects the full `body` column,
never `substr(body, …)`. Display trimming happens in Python (`excerpt = …[:160]`).
"""
import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "derive" / "pulse.py"


def _source():
    return SRC.read_text()


def test_leave_scan_selects_full_body():
    src = _source()
    assert "substr(body,1,160)" not in src, \
        "_leave_mentions must SELECT full body (LEAVE_RX runs on it), not substr(body,1,160)"
    assert "SELECT ts, body, url FROM events" in src, \
        "_leave_mentions must SELECT the full body column"


def test_display_excerpt_trimmed_in_python():
    """The display excerpt is still trimmed — just in Python, after the match."""
    src = _source()
    assert "body.strip()[:160]" in src, \
        "the display excerpt must be trimmed in Python (body.strip()[:160]), keeping the matcher on full body"


def test_source_parses():
    ast.parse(_source())
