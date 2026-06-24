"""derive/people_autofix_apply.py — append-only, idempotent people.yaml writer.

apply() may only ADD org/external entries; it refuses team scope and entries
with no resolvable key, skips identities already in the roster (idempotent), and
never rewrites existing file bytes. All against a tmp people.yaml.
"""

from __future__ import annotations

import yaml

from derive import people_autofix_apply as pa

EXISTING = """people:
- email: known@example.com
  name: Known Person
  scope: org
  jira_id: 712020:known-id
- name: existing-bot
  scope: external
  github: org-existing
  git_names:
  - org-existing
"""


def _write(tmp_path):
    p = tmp_path / "people.yaml"
    p.write_text(EXISTING)
    return p


def _people(path):
    return yaml.safe_load(path.read_text())["people"]


def test_appends_new_org_and_external(tmp_path):
    path = _write(tmp_path)
    n0 = len(_people(path))
    res = pa.apply([
        {"scope": "org", "email": "jane.doe@example.com", "name": "Jane Doe",
         "github": "org-jane", "jira_id": "000000aa11bb22cc33dd44ee",
         "git_names": ["Jane Doe", "org-jane"]},
        {"scope": "external", "name": "tech-bot", "github": "tech-bot",
         "git_names": ["tech-bot"]},
    ], path)
    assert res["n_applied"] == 2 and res["n_skipped"] == 0
    people = _people(path)
    assert len(people) == n0 + 2
    by_email = {p.get("email"): p for p in people}
    assert by_email["jane.doe@example.com"]["scope"] == "org"
    # jira_id with a ':' must round-trip intact
    assert by_email["jane.doe@example.com"]["jira_id"] == "000000aa11bb22cc33dd44ee"
    assert any(p.get("github") == "tech-bot" and p["scope"] == "external" for p in people)


def test_skips_duplicate_identities(tmp_path):
    path = _write(tmp_path)
    res = pa.apply([
        {"scope": "org", "email": "known@example.com", "name": "Dup By Email"},
        {"scope": "external", "name": "x", "github": "org-existing"},
        {"scope": "org", "name": "By Account", "jira_id": "712020:known-id"},
    ], path)
    assert res["n_applied"] == 0 and res["n_skipped"] == 3
    assert all("already mapped" in s["reason"] for s in res["skipped"])
    assert len(_people(path)) == 2          # untouched


def test_refuses_team_and_unresolvable(tmp_path):
    path = _write(tmp_path)
    res = pa.apply([
        {"scope": "team", "email": "newteam@example.com", "name": "No Auto Team"},
        {"scope": "org", "name": "Name Only"},   # no resolvable key
    ], path)
    assert res["n_applied"] == 0 and res["n_skipped"] == 2
    reasons = " ".join(s["reason"] for s in res["skipped"])
    assert "team is never auto-applied" in reasons
    assert "no resolvable key" in reasons


def test_idempotent_rerun(tmp_path):
    path = _write(tmp_path)
    entry = {"scope": "org", "email": "a.b@example.com", "name": "A B",
             "github": "org-ab", "git_names": ["A B"]}
    assert pa.apply([entry], path)["n_applied"] == 1
    assert pa.apply([entry], path)["n_applied"] == 0       # second run adds nothing
    assert len(_people(path)) == 3


def test_dedup_within_one_batch(tmp_path):
    path = _write(tmp_path)
    e = {"scope": "org", "email": "c.d@example.com", "name": "C D", "github": "org-cd"}
    res = pa.apply([e, dict(e)], path)
    assert res["n_applied"] == 1 and res["n_skipped"] == 1


def test_existing_bytes_untouched(tmp_path):
    path = _write(tmp_path)
    pa.apply([{"scope": "org", "email": "z@example.com", "name": "Z", "github": "org-z"}], path)
    assert path.read_text().startswith(EXISTING)   # pure append


def test_quotes_problematic_scalars(tmp_path):
    path = _write(tmp_path)
    # a name with ': ' would break a plain YAML scalar — must be quoted + round-trip
    pa.apply([{"scope": "org", "email": "q@example.com",
               "name": "Weird: Name", "github": "org-q"}], path)
    got = {p.get("email"): p for p in _people(path)}["q@example.com"]
    assert got["name"] == "Weird: Name"


def test_embedded_newline_does_not_corrupt_yaml(tmp_path):
    # a name with a newline must be quoted so the file still parses + round-trips.
    path = _write(tmp_path)
    pa.apply([{"scope": "org", "email": "nl@example.com",
               "name": "Line1\nLine2", "github": "org-nl"}], path)
    people = _people(path)                      # would raise if YAML corrupted
    assert {p.get("email"): p for p in people}["nl@example.com"]["name"] == "Line1\nLine2"
