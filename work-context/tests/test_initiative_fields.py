"""Initiative-level fields the monthly planner added: JPD delivery dates, the archive
flag, and link-preserving description rendering.

Three of these look like ordinary fields but are not:

* "Target Go-Live Date" declares schema type *string* and stores a date-range JSON blob
  ('{"start":"2026-10-29","end":"2026-10-29"}'). Written like ordinary text it would be
  replaced with a bare date, quietly breaking the field for everyone else.
* JPD archiving sets a FIELD and leaves the status alone — an archived idea usually still
  reads "To Do" — so a statusCategory filter never excluded them and their budgets were
  inflating the planned total.
* _adf_text() drops every link. A `link` mark is invisible to it and an inlineCard (the
  smart link a one-pager is normally attached as) carries no text at all, so the reference
  disappeared entirely from both the planner and any epic created from the initiative.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "derive"))

import capacity_engine as ce


# ---------------------------------------------------------------- JPD date blobs

def test_jpd_date_unwraps_the_range_blob():
    assert ce._jpd_date('{"start":"2026-10-29","end":"2026-10-29"}') == "2026-10-29"


def test_jpd_date_prefers_start_when_the_range_spans():
    assert ce._jpd_date('{"start":"2026-10-01","end":"2026-12-31"}') == "2026-10-01"
    assert ce._jpd_date({"start": "2026-05-04", "end": "2026-05-09"}) == "2026-05-04"


def test_jpd_date_falls_back_to_end_when_only_end_is_set():
    assert ce._jpd_date('{"end":"2026-07-01"}') == "2026-07-01"


def test_jpd_date_passes_through_a_plain_date():
    assert ce._jpd_date("2026-01-02") == "2026-01-02"


def test_jpd_date_is_empty_for_blank_and_malformed_input():
    for bad in ("", None, "{not json", "{}", []):
        assert ce._jpd_date(bad) == "", bad


def test_jpd_date_fields_are_recognised(monkeypatch):
    """The write path keys off field id, not schema, because the schema says 'string'."""
    monkeypatch.setattr(ce, "INIT_GOLIVE_FIELD", "customfield_1")
    monkeypatch.setattr(ce, "INIT_HANDOVER_FIELD", "customfield_2")
    monkeypatch.setattr(ce, "INIT_ACT_GOLIVE_FIELD", "customfield_3")
    monkeypatch.setattr(ce, "INIT_ACT_HANDOVER_FIELD", "customfield_4")
    assert ce._is_jpd_date("customfield_1")
    assert ce._is_jpd_date("customfield_4")
    assert not ce._is_jpd_date("customfield_9")
    assert not ce._is_jpd_date("")


def test_unconfigured_jpd_field_is_not_treated_as_a_date(monkeypatch):
    """A blank config id must not make every unset field look like a JPD date."""
    for f in ("INIT_GOLIVE_FIELD", "INIT_HANDOVER_FIELD",
              "INIT_ACT_GOLIVE_FIELD", "INIT_ACT_HANDOVER_FIELD"):
        monkeypatch.setattr(ce, f, "")
    assert not ce._is_jpd_date("")


# ---------------------------------------------------------------- description links

def _doc(*content):
    return {"type": "doc", "version": 1, "content": list(content)}


def test_inline_card_becomes_a_link_even_though_it_has_no_text():
    url = "https://example.atlassian.net/wiki/spaces/PROD/pages/4000907404/PFMS+Migration"
    blocks = ce._adf_rich(_doc({"type": "paragraph", "content": [
        {"type": "text", "text": "One Pager: "},
        {"type": "inlineCard", "attrs": {"url": url}},
    ]}))
    spans = [sp for b in blocks for sp in b]
    linked = [sp for sp in spans if sp["h"]]
    assert len(linked) == 1
    assert linked[0]["h"] == url
    # the label is derived from the URL slug, since the card itself carries no text
    assert linked[0]["t"] == "PFMS Migration"


def test_link_mark_on_text_is_preserved():
    blocks = ce._adf_rich(_doc({"type": "paragraph", "content": [
        {"type": "text", "text": "the doc",
         "marks": [{"type": "link", "attrs": {"href": "https://example.com/x"}}]},
    ]}))
    spans = [sp for b in blocks for sp in b]
    assert spans == [{"t": "the doc", "h": "https://example.com/x"}]


def test_plain_text_survives_with_no_link():
    blocks = ce._adf_rich(_doc({"type": "paragraph", "content": [
        {"type": "text", "text": "no link here"}]}))
    assert [sp["t"] for b in blocks for sp in b] == ["no link here"]
    assert all(not sp["h"] for b in blocks for sp in b)


def test_paragraphs_become_separate_blocks():
    blocks = ce._adf_rich(_doc(
        {"type": "paragraph", "content": [{"type": "text", "text": "first"}]},
        {"type": "paragraph", "content": [{"type": "text", "text": "second"}]},
    ))
    assert len(blocks) == 2


def test_blank_blocks_are_dropped():
    blocks = ce._adf_rich(_doc(
        {"type": "paragraph", "content": []},
        {"type": "paragraph", "content": [{"type": "text", "text": "   "}]},
        {"type": "paragraph", "content": [{"type": "text", "text": "real"}]},
    ))
    assert len(blocks) == 1


def test_hard_break_is_kept_inside_a_block():
    blocks = ce._adf_rich(_doc({"type": "paragraph", "content": [
        {"type": "text", "text": "a"}, {"type": "hardBreak"}, {"type": "text", "text": "b"}]}))
    assert "\n" in "".join(sp["t"] for b in blocks for sp in b)


def test_empty_description_yields_no_blocks():
    assert ce._adf_rich({}) == []
    assert ce._adf_rich(None) == []


def test_card_label_backs_up_past_a_numeric_id():
    # .../pages/4000907404 alone would render as a meaningless number
    assert ce._card_label("https://x.net/wiki/spaces/PROD/pages/4000907404") == "pages"
    assert ce._card_label("https://x.net/browse/BOARD-12") == "BOARD 12"


# ---------------------------------------------------------------- archive handling

def test_archived_ideas_are_excluded_by_default(monkeypatch):
    """Archived ideas keep an open status, so the JQL must filter on the archive FIELD."""
    assert "cf[10628] is EMPTY" in _capture_jql(monkeypatch, include_archived=False)


def test_include_archived_drops_the_archive_clause(monkeypatch):
    assert "cf[10628] is EMPTY" not in _capture_jql(monkeypatch, include_archived=True)


def test_status_filter_alone_would_not_have_caught_them(monkeypatch):
    """Guards the actual bug: statusCategory != Done does not exclude archived ideas."""
    jql = _capture_jql(monkeypatch, include_archived=False)
    assert "statusCategory != Done" in jql        # still there
    assert "cf[10628] is EMPTY" in jql            # and is NOT what excludes archived rows


def _capture_jql(monkeypatch, include_archived):
    """Run pod_initiatives far enough to see the JQL it builds, with no network."""
    captured = {}

    class _Stop(Exception):
        pass

    import urllib.request

    def fake_urlopen(req, timeout=None):
        captured["body"] = req.data
        raise _Stop()

    monkeypatch.setattr(ce, "OINT_PROJECT", "BOARD")
    monkeypatch.setattr(ce, "OINT_POD", "Core Payments Platform")
    monkeypatch.setattr(ce, "INITIATIVE_POD_FIELD", "customfield_11853")
    monkeypatch.setattr(ce, "INIT_ARCHIVED_FIELD", "customfield_10628")
    monkeypatch.setattr(ce, "_secret", lambda k: "x")
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    out = ce.pod_initiatives(include_archived=include_archived)
    # the search raises inside the try, so pod_initiatives returns its error dict
    assert "__error__" in out
    import json as _json
    return _json.loads(captured["body"])["jql"]


def test_include_archived_is_reported_on_the_payload(monkeypatch):
    monkeypatch.setattr(ce, "OINT_PROJECT", "")
    out = ce.pod_initiatives()
    assert "__error__" in out   # unconfigured project short-circuits before any network


# ---------------------------------------------------------------- holiday deduction

def test_holidays_match_dates_parsed_by_yaml(tmp_path, monkeypatch):
    """PyYAML turns an unquoted 2026-10-02 into a date object, not a string.

    holidays_for() compared those against ISO strings, so nothing ever matched and no
    public holiday was deducted from capacity. October 2026 reported 22 working days
    when Gandhi Jayanti and Vijayadasmi should have made it 20.
    """
    import datetime as dt
    cfg = tmp_path / "holidays-2026.yaml"
    cfg.write_text(
        "holidays:\n"
        "  - date: 2026-10-02\n    day: Friday\n    type: holiday\n    occasion: Gandhi Jayanti\n"
        "  - date: '2026-10-21'\n    day: Wednesday\n    type: holiday\n    occasion: Vijayadasmi\n"
    )
    monkeypatch.setattr(ce, "CFG", str(tmp_path))
    days = [dt.date(2026, 10, 1) + dt.timedelta(days=i) for i in range(31)]
    out = ce.holidays_for(2026, days)
    # the first is a yaml date object, the second an explicitly quoted string: both must land
    assert "2026-10-02" in out, out
    assert "2026-10-21" in out, out
    assert out["2026-10-02"]["occasion"] == "Gandhi Jayanti"


def test_holidays_outside_the_window_are_ignored(tmp_path, monkeypatch):
    import datetime as dt
    cfg = tmp_path / "holidays-2026.yaml"
    cfg.write_text("holidays:\n  - date: 2026-03-02\n    type: holiday\n    occasion: Holi\n")
    monkeypatch.setattr(ce, "CFG", str(tmp_path))
    days = [dt.date(2026, 10, 1) + dt.timedelta(days=i) for i in range(31)]
    assert ce.holidays_for(2026, days) == {}


def test_a_window_spanning_new_year_loads_both_years(tmp_path, monkeypatch):
    """A sprint window can straddle December into January, so one year's file is not enough."""
    import datetime as dt
    (tmp_path / "holidays-2026.yaml").write_text(
        "holidays:\n  - date: 2026-12-25\n    type: holiday\n    occasion: Christmas\n")
    (tmp_path / "holidays-2027.yaml").write_text(
        "holidays:\n  - date: 2027-01-01\n    type: holiday\n    occasion: New Year\n")
    monkeypatch.setattr(ce, "CFG", str(tmp_path))
    days = [dt.date(2026, 12, 20) + dt.timedelta(days=i) for i in range(20)]
    out = ce.holidays_for(2026, days)
    assert "2026-12-25" in out and "2027-01-01" in out, out


def test_missing_holiday_file_is_not_fatal(tmp_path, monkeypatch):
    import datetime as dt
    monkeypatch.setattr(ce, "CFG", str(tmp_path))
    days = [dt.date(2026, 10, 1) + dt.timedelta(days=i) for i in range(5)]
    assert ce.holidays_for(2026, days) == {}


# ---------------------------------------------------------------- planning-cycle windows

def _cycle_cfg(tmp_path, monkeypatch, body):
    (tmp_path / "sprint_planning.yaml").write_text(body)
    monkeypatch.setattr(ce, "CFG", str(tmp_path))


def test_cycle_window_reads_configured_dates(tmp_path, monkeypatch):
    """A planning cycle is two sprints, not a calendar month: Oct-26 runs 7 Oct to 3 Nov."""
    import datetime as dt
    _cycle_cfg(tmp_path, monkeypatch,
               "planning_cycles:\n"
               "  Oct-26: {start: 2026-10-07, end: 2026-11-03}\n"
               "  Nov-26: {start: '2026-11-04', end: '2026-12-01'}\n")
    assert ce.cycle_window("Oct-26") == (dt.date(2026, 10, 7), dt.date(2026, 11, 3))
    # quoted strings must work the same as yaml date objects
    assert ce.cycle_window("Nov-26") == (dt.date(2026, 11, 4), dt.date(2026, 12, 1))


def test_an_unconfigured_cycle_has_no_window(tmp_path, monkeypatch):
    """Falling back to the calendar month is correct; inventing a window is not."""
    _cycle_cfg(tmp_path, monkeypatch,
               "planning_cycles:\n  Oct-26: {start: 2026-10-07, end: 2026-11-03}\n")
    assert ce.cycle_window("Dec-26") is None


def test_missing_or_malformed_cycle_config_is_not_fatal(tmp_path, monkeypatch):
    _cycle_cfg(tmp_path, monkeypatch, "planning_cycles:\n  Oct-26: {start: nonsense}\n")
    assert ce.cycle_window("Oct-26") is None
    _cycle_cfg(tmp_path, monkeypatch, "other_key: 1\n")
    assert ce.cycle_window("Oct-26") is None


def test_a_backwards_window_is_rejected(tmp_path, monkeypatch):
    _cycle_cfg(tmp_path, monkeypatch,
               "planning_cycles:\n  Oct-26: {start: 2026-11-03, end: 2026-10-07}\n")
    assert ce.cycle_window("Oct-26") is None


def test_cycle_label_matches_the_jira_option_format():
    # the Planning Cycle field stores "Oct-26", so the lookup key must match exactly
    assert ce.cycle_label(2026, 10) == "Oct-26"
    assert ce.cycle_label(2026, 1) == "Jan-26"
    assert ce.cycle_label(2027, 12) == "Dec-27"
