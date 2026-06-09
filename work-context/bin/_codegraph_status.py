"""Shared status reader for the daily code-graph rebuild (com.example.codegraph).

Imported by bin/cron-status.sh (terminal) + bin/dashboard.py (web) so the
log-parsing lives in ONE place.

The job (bin/run-codegraph.sh, LaunchAgent 18:00 IST daily) git-ff-if-clean +
full `code-review-graph build` for each registered repo, then writes:
  - state/last_codegraph_success.date  (YYYY-MM-DD, only when fail==0)
  - state/codegraph_<YYYYMMDD>.log     (per-run: start/done, ok/fail, per-repo
                                        result + "Full build:" node/edge totals)
This reads the success marker + newest per-day log and returns a status dict.
"""
from __future__ import annotations
import plistlib
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
from derive.sources_config import launchd_prefix  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
PLIST_LABEL = f"{launchd_prefix()}.codegraph"

_TS_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]")
_BUILD_RE = re.compile(r"Full build:\s*([\d,]+) files,\s*([\d,]+) nodes,\s*([\d,]+) edges")
_OK_RE = re.compile(r"([^\s/]+): graph rebuilt")
_FAIL_RE = re.compile(r"ERROR build failed:\s*(\S+)")
_DONE_RE = re.compile(r"refresh done .*ok=(\d+) fail=(\d+)")


def _ts(line: str) -> str | None:
    m = _TS_RE.match(line)
    return m.group(1) if m else None


def read_status(state_dir: Path, plist_dir: Path | None = None) -> dict:
    """Return code-graph rebuild status (success date, last run, per-repo, sched)."""
    out: dict = {"success_date": None, "repos": [], "ok": None, "fail": None,
                 "start": None, "done": None, "running": False,
                 "sched": None, "next": None, "log_date": None}

    sm = state_dir / "last_codegraph_success.date"
    if sm.exists():
        try:
            out["success_date"] = sm.read_text().strip()
        except Exception:
            pass

    logs = sorted(state_dir.glob("codegraph_*.log"))
    if logs:
        out["log_date"] = logs[-1].stem.replace("codegraph_", "")
        try:
            lines = logs[-1].read_text().splitlines()
        except Exception:
            lines = []
        starts = [i for i, l in enumerate(lines) if "refresh start" in l]
        if starts:
            run = lines[starts[-1]:]
            out["start"] = _ts(run[0])
            pending = None   # totals from a "Full build:" awaiting its repo line
            for l in run:
                mb = _BUILD_RE.search(l)
                if mb:
                    pending = {"files": int(mb.group(1).replace(",", "")),
                               "nodes": int(mb.group(2).replace(",", "")),
                               "edges": int(mb.group(3).replace(",", ""))}
                    continue
                m_ok = _OK_RE.search(l)
                if m_ok:
                    out["repos"].append({"name": m_ok.group(1), "ok": True,
                                         "totals": pending})
                    pending = None
                    continue
                m_fail = _FAIL_RE.search(l)
                if m_fail:
                    out["repos"].append({"name": Path(m_fail.group(1)).name,
                                         "ok": False, "totals": None})
                    continue
                m_done = _DONE_RE.search(l)
                if m_done:
                    out["ok"] = int(m_done.group(1))
                    out["fail"] = int(m_done.group(2))
                    out["done"] = _ts(l)
            out["running"] = out["done"] is None

    if plist_dir is None:
        plist_dir = Path.home() / "Library/LaunchAgents"
    p = plist_dir / f"{PLIST_LABEL}.plist"
    if p.exists():
        try:
            with p.open("rb") as f:
                data = plistlib.load(f)
            sci = data.get("StartCalendarInterval")
            if isinstance(sci, dict):
                sci = [sci]
            if sci:
                hours = sorted({e["Hour"] for e in sci if "Hour" in e})
                mins = sorted({e["Minute"] for e in sci if "Minute" in e}) or [0]
                out["sched"] = ", ".join(f"{h:02d}:{m:02d}"
                                         for h in hours for m in mins) + " IST"
                now = datetime.now(IST)
                now_min = now.hour * 60 + now.minute
                fires = sorted(h * 60 + m for h in hours for m in mins)
                upcoming = [x for x in fires if x > now_min]
                tgt = upcoming[0] if upcoming else fires[0] + 1440
                d = tgt - now_min
                out["next"] = f"~{d // 60}h {d % 60}m" if d >= 60 else f"~{d}m"
        except Exception:
            pass
    return out
