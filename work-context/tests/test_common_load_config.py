"""Config-loader graceful degrade.

people.yaml / projects.yaml are gitignored (real identities) and therefore
absent on a fresh clone or CI. A missing identity/project map is not fatal:
_resolve_person already returns None for unmapped handles, so the loaders must
degrade to an empty list rather than raise FileNotFoundError and crash every
enrich_refs caller. These pin that contract so the server-side gate (which runs
without the local config files) stays green.
"""

from __future__ import annotations

import pytest

from ingest import common


@pytest.fixture(autouse=True)
def _reset_config_cache(monkeypatch):
    # The loaders memoize into module globals; reset so each test re-reads.
    monkeypatch.setattr(common, "_people_config", None, raising=False)
    monkeypatch.setattr(common, "_projects_config", None, raising=False)


def test_load_people_missing_file_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(common, "CONFIG_DIR", tmp_path)  # empty dir, no people.yaml
    assert common._load_people() == []


def test_load_projects_missing_file_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(common, "CONFIG_DIR", tmp_path)  # empty dir, no projects.yaml
    assert common._load_projects() == []


def test_load_people_empty_file_returns_empty(monkeypatch, tmp_path):
    # A comment-only / empty yaml parses to None; must not AttributeError.
    (tmp_path / "people.yaml").write_text("# no people yet\n")
    monkeypatch.setattr(common, "CONFIG_DIR", tmp_path)
    assert common._load_people() == []


def test_load_projects_empty_file_returns_empty(monkeypatch, tmp_path):
    (tmp_path / "projects.yaml").write_text("")
    monkeypatch.setattr(common, "CONFIG_DIR", tmp_path)
    assert common._load_projects() == []


def test_load_people_reads_present_file(monkeypatch, tmp_path):
    (tmp_path / "people.yaml").write_text(
        "people:\n  - canonical: alice\n    github: alice-gh\n"
    )
    monkeypatch.setattr(common, "CONFIG_DIR", tmp_path)
    people = common._load_people()
    assert people == [{"canonical": "alice", "github": "alice-gh"}]


def test_missing_people_resolves_to_none(monkeypatch, tmp_path):
    # The downstream contract: no map → handle is known-but-unmapped → None,
    # not a crash. This is the exact path enrich_refs hits during upsert.
    monkeypatch.setattr(common, "CONFIG_DIR", tmp_path)
    assert common._resolve_person("alice-gh", "github") is None


def test_load_projects_reads_present_file(monkeypatch, tmp_path):
    (tmp_path / "projects.yaml").write_text(
        "projects:\n  - slug: payments\n    keywords: [upi, txn]\n"
    )
    monkeypatch.setattr(common, "CONFIG_DIR", tmp_path)
    projects = common._load_projects()
    assert projects == [{"slug": "payments", "keywords": ["upi", "txn"]}]
