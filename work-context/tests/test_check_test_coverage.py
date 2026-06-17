"""bin/check_test_coverage.py — the test-coverage gate's own logic.

Dogfooding: the gate that demands every in-scope module have a test is itself
an in-scope module, so it gets one. Covers the pure mapping/matching helpers
(scope filter, source→import-token derivation, test import extraction, dotted
prefix expansion). The git-driven _run_gate is exercised end-to-end by the
pre-push hook, not here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Gate lives in bin/, imported by basename (mirrors how the hook runs it).
_BIN = Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

import check_test_coverage as gate  # noqa: E402


# ── _is_in_scope ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path,expected", [
    ("work-context/ingest/common.py", True),
    ("work-context/derive/jira_metrics.py", True),
    ("work-context/derive/service_derive/go_extractor.py", True),
    ("work-context/bin/_run_health.py", True),
    ("work-context/tests/test_common_cursors.py", False),   # tests excluded
    ("work-context/ingest/__init__.py", False),             # __init__ excluded
    ("work-context/README.md", False),                      # non-.py excluded
    ("work-context/config/people.yaml", False),
    ("work-context/ingest/run-jira.sh", False),             # shell excluded
])
def test_is_in_scope(path, expected):
    assert gate._is_in_scope(path) is expected


# ── _module_tokens ───────────────────────────────────────────────────────────

def test_module_tokens_package_file():
    toks = gate._module_tokens("work-context/derive/jira_metrics.py")
    assert "derive.jira_metrics" in toks and "jira_metrics" in toks


def test_module_tokens_nested_package():
    toks = gate._module_tokens("work-context/derive/service_derive/go_extractor.py")
    assert "derive.service_derive.go_extractor" in toks


def test_module_tokens_bin_is_basename_only():
    # bin/ modules are imported bare (tests put bin/ on sys.path), so no
    # dotted 'bin.x' token — only the basename.
    toks = gate._module_tokens("work-context/bin/_run_health.py")
    assert toks == {"_run_health"}


# ── _test_provides (import extraction) ───────────────────────────────────────

def test_provides_from_pkg_import_name():
    prov = gate._test_provides("from ingest import common\n")
    assert "ingest.common" in prov and "common" in prov and "ingest" in prov


def test_provides_from_dotted_import():
    prov = gate._test_provides("from derive.jira_metrics import compute_done_credits\n")
    assert "derive.jira_metrics" in prov


def test_provides_plain_import():
    prov = gate._test_provides("import _run_health\n")
    assert "_run_health" in prov


def test_provides_dotted_import_adds_prefixes():
    prov = gate._test_provides("import derive.service_derive.go_extractor\n")
    assert "derive.service_derive.go_extractor" in prov
    assert "derive.service_derive" in prov


# ── matching: a test "covers" a source iff token overlap ─────────────────────

def test_source_matches_its_test():
    src = gate._module_tokens("work-context/ingest/common.py")
    prov = gate._test_provides("from ingest import common\n")
    assert src & prov  # non-empty overlap == covered


def test_unrelated_source_does_not_match():
    src = gate._module_tokens("work-context/derive/jira_metrics.py")
    prov = gate._test_provides("from ingest import common\n")
    assert not (src & prov)


# ── _prefixes ────────────────────────────────────────────────────────────────

def test_prefixes():
    assert gate._prefixes("a.b.c") == {"a", "a.b", "a.b.c", "c"}
