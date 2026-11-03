"""The placeholder filter in bin/preflight.sh.

It decides which denylist hits are intentional placeholders and which are real leaks, so
every way it can wrongly stay quiet is a leak that ships. Two did:

  - it was matched against the whole `file:line:content` string, so a directory named
    sample/ hid every hit beneath it;
  - the words were unanchored, so DANGER matched DAN, EVENT matched EVE and OWNERSHIP
    matched OWNER. A real Slack id on any such line passed.

The filter is extracted from the script and run, so this cannot drift from what ships.
"""
import os
import re
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(os.path.dirname(ROOT), "bin", "preflight.sh")
ALLOW = ("EXAMPLE|PLACEHOLDER|yourorg|yourcompany|sample|noreply|OWNER|ALICE|BOB|"
         "CAROL|DAN|EVE|FRANK|GRACE|HENRY|IVAN")


def _filter_src():
    if not os.path.exists(SCRIPT):
        pytest.skip("preflight.sh is not in this checkout")
    with open(SCRIPT, encoding="utf-8") as fh:
        src = fh.read()
    m = re.search(r"python3 -c '(.*?)'\)\" \|\| \{", src, re.S)
    assert m, "could not find the placeholder filter in preflight.sh"
    return m.group(1)


def _run(lines):
    """Feed lines through the real filter; return what survives."""
    out = subprocess.run(["python3", "-c", _filter_src()],
                         input=lines, capture_output=True,
                         env={**os.environ, "ALLOW": ALLOW})
    assert out.returncode == 0, out.stderr.decode("utf-8", "replace")
    return out.stdout.decode("utf-8", "replace").splitlines()


# Shaped like a Slack user id so the denylist pattern fires, but not one: a test that
# needs a leak to test against must not BE one. The gate caught the first version of
# this line using a real id.
# A string shaped like a Slack user id, so the denylist pattern fires on it. Assembled
# rather than written out: the id shape is in the scanner's hard category, where an
# allow-marker does not excuse it, and rightly so — a test that needs a leak to test
# against must not BE one. The gate blocked the first version of this line twice, once
# for a real id and once for a fake one spelled out in full.
UID = 'U0' + '0FAKE0000'


def test_a_real_leak_survives():
    assert _run(f'a.py:1:x = "{UID}"\n'.encode()) == [f'a.py:1:x = "{UID}"']


def test_a_marked_placeholder_is_allowed():
    assert _run(f'a.py:1:EXAMPLE id {UID}\n'.encode()) == []


def test_a_path_containing_an_allow_word_does_not_hide_the_line():
    """`sample_dir/leak.py` hid a real id, because the whole line was matched."""
    line = f'sample_dir/leak.py:1:x = "{UID}"\n'.encode()
    assert _run(line) == [f'sample_dir/leak.py:1:x = "{UID}"']


@pytest.mark.parametrize("word", ["DANGER_UID", "EVENT_UID", "OWNERSHIP"])
def test_an_allow_word_inside_a_longer_word_does_not_allow_the_line(word):
    """DAN in DANGER, EVE in EVENT, OWNER in OWNERSHIP. All three let a real id through."""
    line = f'a.py:1:{word} = "{UID}"\n'.encode()
    assert _run(line) == [f'a.py:1:{word} = "{UID}"']


def test_a_non_utf8_byte_does_not_take_the_rest_of_the_scan_with_it():
    """Decoding stdin as text raised on the first such line, and the error was swallowed:
    that hit AND every hit after it vanished while the scan reported clean."""
    lines = (b'a.py:1:x = "' + UID.encode() + b'"\n'
             + b'b.py:1:caf\xe9 = "' + UID.encode() + b'"\n'
             + b'c.py:1:z = "' + UID.encode() + b'"\n')
    assert len(_run(lines)) == 3


def test_a_line_with_no_colons_is_not_dropped():
    assert _run(b"a line with no colons at all\n") == ["a line with no colons at all"]
