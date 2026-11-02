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
HELD_BACK = {"synapse_reviews", "txn_watch", "oncall_closure"}


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
