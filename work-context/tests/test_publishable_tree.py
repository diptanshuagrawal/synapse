"""The published tree has to work without the modules that are held back.

Some modules cannot ship: the review tooling names real developers and quotes their
work. They are gitignored, which means the public checkout does not have them — and a
module that imports one of them at the top level takes the whole page server down there
while working perfectly here. That is exactly what happened: synapse_pages.py imported
synapse_reviews unconditionally and the published repo could not start.

This checks the shape rather than the symptom: a held-back module may only be imported
in a way that tolerates its absence.
"""
import ast
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Modules deliberately kept out of the published tree. Keep in step with .gitignore.
HELD_BACK = {"synapse_reviews"}


def _sources():
    for base in (os.path.join(ROOT, "derive"), os.path.join(os.path.dirname(ROOT), "bin")):
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base)):
            if name.endswith(".py"):
                yield os.path.join(base, name)


def _toplevel_imports(path):
    """Imports that run on `import <module>`: module scope only, and not guarded by a
    try/except. An import inside a function or a try block is a choice; one at the top
    is a requirement."""
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), path)
    out = []
    for node in tree.body:                       # module scope only, so nothing nested
        if isinstance(node, ast.Import):
            out += [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out.append(node.module.split(".")[0])
    return out


@pytest.mark.parametrize("path", list(_sources()), ids=os.path.basename)
def test_a_held_back_module_is_never_a_hard_dependency(path):
    if os.path.basename(path)[:-3] in HELD_BACK:
        pytest.skip("a held-back module may depend on its own kind")
    bad = sorted(set(_toplevel_imports(path)) & HELD_BACK)
    assert not bad, (
        f"{os.path.basename(path)} imports {bad} at module scope, so it cannot load in "
        "a checkout that does not have it. Import it inside a try/except or a function."
    )


def test_the_held_back_list_matches_what_is_actually_ignored():
    """A module dropped from .gitignore but left here would make this file lie."""
    with open(os.path.join(os.path.dirname(ROOT), ".gitignore"), encoding="utf-8") as fh:
        ignored = fh.read()
    for mod in HELD_BACK:
        assert f"{mod}.py" in ignored, f"{mod} is listed here but is not gitignored"


# ---- txn_watch's vocabulary is configured, not carried -------------------------------
# It named the services and the gold tables it walks, which said which bank this is and
# kept an otherwise generic tool out of the public tree.

def _txn_watch():
    import importlib.util
    path = os.path.join(os.path.dirname(ROOT), "bin", "txn_watch.py")
    if not os.path.exists(path):
        pytest.skip("txn_watch is not in this checkout")
    spec = importlib.util.spec_from_file_location("txn_watch_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_upstream_names_come_from_config():
    tw = _txn_watch()
    assert tw.systems({}) == tw.SYS_FALLBACK
    named = {"systems": {"labels": {"origin": "A", "ledger": "B", "core": "C"}}}
    assert tw.systems(named) == {"origin": "A", "ledger": "B", "core": "C"}


def test_an_empty_config_is_an_answer_not_a_reason_to_go_and_read_one():
    """`cfg or load_cfg()` sent {} back to the real file, which made the fallbacks
    untestable and would have read config on a machine that has none."""
    tw = _txn_watch()
    assert tw.systems({}) == tw.SYS_FALLBACK
    assert tw.block_keys({}) == {}


def test_the_block_mapping_is_configured():
    tw = _txn_watch()
    cfg = {"systems": {"blocks": {"X some_schema.table": "X_KEY"}}}
    assert tw.block_keys(cfg) == {"X some_schema.table": "X_KEY"}


# The same rule as txn_watch's: nothing the config names may also sit in the source.
# oncall_closure shares that config, so it is checked the same way.
CONFIG_DRIVEN = ("bin/txn_watch.py", "bin/oncall_closure.py")


def test_no_configured_name_is_also_hardcoded_in_the_source():
    """The names that kept this file unpublishable must live only in config.

    The obvious way to write this test is to list the names and assert they are absent.
    That version failed the leak gate on itself, which is the right answer: a test is
    source too. So the names are read from the local config, which is gitignored and is
    where they are supposed to be, and the test skips where there is none.
    """
    cfg_path = os.path.join(ROOT, "config", "txn_watch.yaml")
    paths = [os.path.join(os.path.dirname(ROOT), p) for p in CONFIG_DRIVEN]
    paths = [p for p in paths if os.path.exists(p)]
    if not paths or not os.path.exists(cfg_path):
        pytest.skip("those tools or their local config are not in this checkout")
    import yaml
    with open(cfg_path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}

    systems = cfg.get("systems") or {}
    slack = cfg.get("slack") or {}
    names = [str(v) for v in (systems.get("labels") or {}).values()]
    names += [str(k) for k in (systems.get("blocks") or {})]
    names += [str((cfg.get("oncall_api") or {}).get("base") or ""),
              str(slack.get("reporter_uid") or ""),
              str(slack.get("workspace_host") or "")]
    names = [n for n in names if len(n.strip()) > 3]
    assert names, "the local config names nothing, so this test checked nothing"

    for path in paths:
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        back = sorted(n for n in names if n in src)
        assert not back, (f"{os.path.basename(path)} hardcodes {len(back)} name(s) the "
                          "config already carries")
