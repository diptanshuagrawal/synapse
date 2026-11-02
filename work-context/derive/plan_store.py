#!/usr/bin/env python3
"""Durable storage for the monthly planner's own state.

The stack rank and the per-month draft used to live only in one browser's localStorage.
That is a single point of failure for a quarter of planning: clear the site data, open it
in another browser, or hit any bug that writes the key once, and the whole thing is gone
with no way to tell what it used to be.

So it is written here, and EVERY save keeps the version it replaced. Nothing overwrites
silently: if the order changes, the previous one is still on disk and can be read back.
"""
import datetime as dt
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Overridable so a test can drive a REAL server without writing to the real plan. I
# learned this the hard way: a browser probe against the live board replaced a 123-item
# rank with its own fixture, and only the version history got it back.
PLAN_FILE = os.environ.get("SYNAPSE_PLAN_FILE") or os.path.join(ROOT, "state", "monthly_plan.json")
HIST_DIR = os.environ.get("SYNAPSE_PLAN_HISTORY") or os.path.join(
    ROOT, "state", "monthly_plan_history")
KEEP = 60               # versions kept; a save a day for two months
SCHEMA = 2              # v2: columns, estimates, heads and dragged starts too


# Everything here is something the planner TYPED or DRAGGED. View toggles that only say
# what you are looking at right now - show-all, archived, Tech-DRI, which panels are
# expanded - stay in the browser, because they are not the plan.
_LISTS = ("order", "months", "cols")
_MAPS = ("plan", "est", "heads", "schStart")
# What a record written before any of this existed can speak for. Anything else in such a
# record is absent, not empty, and a browser adopting it must leave its own alone.
_OLD_FIELDS = ["months", "order", "plan", "pods"]


def _empty():
    doc = {"v": SCHEMA, "pods": None, "saved": "", "savedBy": "", "fields": [], "rev": 0}
    for k in _LISTS:
        doc[k] = []
    for k in _MAPS:
        doc[k] = {}
    return doc


def read_plan(path=None):
    """The stored plan, or an empty one. Never raises: the planner must still open."""
    try:
        with open(path or PLAN_FILE) as fh:
            doc = json.load(fh) or {}
    except Exception:
        return _empty()
    if not isinstance(doc, dict) or doc.get("v") not in (1, SCHEMA):
        return _empty()
    base = _empty()
    base.update({k: doc.get(k, base[k]) for k in base})
    # A v1 record carries the rank and the month plan but nothing about columns or
    # estimates. Say so, so a browser adopting it does not read "no columns" out of a
    # record that never had any and blank its own.
    base["v"] = doc.get("v", SCHEMA)
    # Which fields the writer actually sent. Versions are too blunt for this: a browser
    # running yesterday's page sends only the rank, and the record must not then claim
    # "no columns" on its behalf and blank a view that was fine.
    f = doc.get("fields")
    base["fields"] = sorted(f) if isinstance(f, list) else list(_OLD_FIELDS)
    return base


def history(path=None, hist=None):
    """What the plan looked like before each save, newest first."""
    d = hist or HIST_DIR
    try:
        names = sorted(os.listdir(d), reverse=True)
    except Exception:
        return []
    out = []
    for n in names:
        if not n.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, n)) as fh:
                doc = json.load(fh)
        except Exception:
            continue
        out.append({"file": n, "saved": doc.get("saved", ""),
                    "order": len(doc.get("order") or []),
                    "planned": len(doc.get("plan") or {})})
    return out


def restore(name, path=None, hist=None):
    """Make a kept version current again. The version it replaces is kept too, so an undo
    of the undo is possible."""
    f = os.path.join(hist or HIST_DIR, os.path.basename(name))
    try:
        with open(f) as fh:
            doc = json.load(fh)
    except Exception:
        return {"__error__": f"no such version: {name}"}
    # Restored AS IT WAS, including which shape it is. Stamping it with the current
    # schema would claim it carries fields it never had - and a browser adopting it would
    # then read "no columns" out of a record written before columns existed.
    return write_plan(doc, path=path, hist=hist, by="restore:" + os.path.basename(name),
                      keep_version=doc.get("v"))


def write_plan(doc, path=None, hist=None, by="", keep_version=None, base_rev=None):
    """Replace the stored plan, keeping the version it replaced.

    `base_rev` is the revision the writer last saw. If the stored plan has moved on since,
    the write is REFUSED rather than applied: two windows are both allowed to write, and
    without this the one that happened to click last would quietly erase the other. The
    refusal carries the current plan so the caller can decide what to do about it.

    An order that arrives EMPTY while one is already stored is refused. That is what a
    browser with cleared storage sends, and accepting it would wipe the real plan with a
    blank one - exactly the failure this file exists to prevent. Clearing on purpose is a
    restore to an empty version, not an accidental save.
    """
    if not isinstance(doc, dict):
        return {"__error__": "plan must be an object"}
    order = doc.get("order")
    for k in _MAPS:
        if doc.get(k) is not None and not isinstance(doc.get(k), dict):
            return {"__error__": f"{k} must be an object"}
    for k in _LISTS:
        if doc.get(k) is not None and not isinstance(doc.get(k), list):
            return {"__error__": f"{k} must be a list"}

    f = path or PLAN_FILE
    h = hist or HIST_DIR
    before = read_plan(f)
    if base_rev is not None and int(base_rev) != int(before.get("rev") or 0):
        return {"__error__": "the plan changed in another window",
                "conflict": True, "rev": before.get("rev") or 0, "plan": before}
    if before.get("order") and not (order or []) and not by.startswith("restore:"):
        return {"__error__": "refusing to replace a saved order with an empty one",
                "kept": len(before["order"])}

    now = dt.datetime.now()
    fresh = _empty()
    for k in _LISTS:
        fresh[k] = [str(x) for x in (doc.get(k) or [])]
    for k in _MAPS:
        v = doc.get(k)
        fresh[k] = v if isinstance(v, dict) else {}
    sent = sorted(k for k in (_LISTS + _MAPS + ("pods",)) if k in doc)
    fresh.update({
        "rev": int(before.get("rev") or 0) + 1,
        "fields": sent or list(_OLD_FIELDS),
        "v": keep_version if keep_version in (1, SCHEMA) else SCHEMA,
        "pods": doc.get("pods"),
        "saved": now.isoformat(timespec="seconds"),
        "savedBy": str(by or doc.get("savedBy") or "")[:60],
    })

    os.makedirs(os.path.dirname(f), exist_ok=True)
    if before.get("saved") or before.get("order"):
        os.makedirs(h, exist_ok=True)
        # Two saves in the same millisecond must not land on the same filename: the
        # second would replace the first, and the version it was keeping would be the one
        # thing lost. The suffix only grows when that actually happens.
        stamp = now.strftime("%Y%m%d-%H%M%S-%f")[:-3]
        name = stamp
        n = 0
        while os.path.exists(os.path.join(h, name + ".json")):
            n += 1
            name = "%s-%d" % (stamp, n)
        tmp = os.path.join(h, name + ".json.tmp")
        with open(tmp, "w") as fh:
            json.dump(before, fh, indent=1, sort_keys=True)
        os.replace(tmp, os.path.join(h, name + ".json"))
        _trim(h)

    tmp = f + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(fresh, fh, indent=1, sort_keys=True)
    os.replace(tmp, f)
    return {"ok": True, "order": len(fresh["order"]), "saved": fresh["saved"],
            "rev": fresh["rev"]}


def _trim(h):
    try:
        names = sorted(n for n in os.listdir(h) if n.endswith(".json"))
    except Exception:
        return
    for n in names[:-KEEP]:
        try:
            os.remove(os.path.join(h, n))
        except OSError:
            pass
