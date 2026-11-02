"""Synapse /pr-friction: PR friction (authoring) + PR review quality (reviewing).

Two lenses over the same merged team PRs in a window:
  - Friction: how much correction a dev's PRs needed (pr_friction score, comments received).
  - Review quality: what kind of comments a dev gives on others' PRs (pr_comment_class).
Each lens has a developer view (consolidated) and a PR view (per PR, drill-down).

Read-only over index/events.db. Hot-reloaded by synapse_pages._fresh().
"""
import json
import statistics
from datetime import date

# Taxonomy (config/pr_review_rules.md) folded into four reader-facing buckets.
BUCKET = {
    "correctness": "bug", "business-logic": "bug", "security": "bug",
    "design": "design", "test-gap": "design",
    "question": "question",
    "naming": "nit", "nit": "nit", "praise": "nit",
}
ECHO = {"", "approved", "approved.", "approving", "approving.", "approving these changes.",
        "requested changes", "requested changes."}
WINDOWS = {
    "h1": ("2026-04-01", "2026-09-30", "Mid-year (Apr to Sep)"),
    "90": (None, None, "Last 90 days"),
    "30": (None, None, "Last 30 days"),
    "ytd": ("2026-01-01", None, "This year"),
}


def _window(key):
    since, until, label = WINDOWS.get(key, WINDOWS["h1"])
    if key in ("90", "30"):
        from datetime import timedelta
        since = (date.today() - timedelta(days=int(key))).isoformat()
    return since, (until + "T23:59:59Z") if until else None, label


def report(conn, people, lookup, win="h1"):
    """people: [{canonical,name,role}], lookup: login(lower) -> canonical."""
    since, until, label = _window(win)
    team = {p["canonical"]: p for p in people if p.get("scope") == "team" and p.get("role")}

    def who(login):
        c = lookup.get((login or "").lower())
        return c if c in team else None

    q = ("SELECT m.subject, m.repo, m.number, m.additions, m.deletions, m.merged_at, "
         "f.score, f.dominant_category dc, f.mechanical_json mj "
         "FROM pr_meta m JOIN pr_friction f ON f.subject=m.subject "
         "WHERE m.state='merged' AND m.merged_at >= ?" + (" AND m.merged_at <= ?" if until else ""))
    args = [since] + ([until] if until else [])
    prs = {}
    for r in conn.execute(q, args):
        a = conn.execute("SELECT actor, title FROM events WHERE subject=? AND event_type IN "
                         "('pr_opened','pr_merged') ORDER BY (event_type='pr_opened') DESC LIMIT 1",
                         (r["subject"],)).fetchone()
        author = who(a["actor"] if a else None)
        if not author:
            continue
        mech = json.loads(r["mj"] or "{}")
        prs[r["subject"]] = {
            "subject": r["subject"], "repo": (r["repo"] or "").split("/")[-1], "number": r["number"],
            "title": (a["title"] if a else "") or "", "author": author,
            "url": f"https://github.com/{r['repo']}/pull/{r['number']}",
            "merged": (r["merged_at"] or "")[:10], "score": round(r["score"] or 0, 1),
            "cause": r["dc"] or "clean", "loc": (r["additions"] or 0) + (r["deletions"] or 0),
            "cr": mech.get("changes_requested", 0), "rounds": mech.get("review_rounds", 0),
            "rework": mech.get("rework_commits", 0), "ttm": mech.get("ttm_hours"),
            "recv": {"bug": 0, "design": 0, "question": 0, "nit": 0}, "bot": 0, "comments": [],
        }
    if not prs:
        return {"window": label, "prs": [], "devs": [], "reviewers": [], "fresh": _fresh(conn)}

    subs = list(prs)
    ph = ",".join("?" * len(subs))
    # Classified comments on these PRs (reviewers' side and authors' side at once).
    for r in conn.execute(
            f"SELECT c.subject, c.source, c.category, e.actor, e.ts, e.body, e.url FROM pr_comment_class c "
            f"JOIN events e ON e.id=c.event_id WHERE c.subject IN ({ph})", subs):
        p = prs[r["subject"]]
        b = BUCKET.get(r["category"], "question")
        human = r["source"] == "human"
        rv = who(r["actor"]) if human else "ai"
        if human:
            p["recv"][b] += 1
        else:
            p["bot"] += 1
        body = " ".join((r["body"] or "").split())
        p["comments"].append({"by": rv or r["actor"], "bucket": b, "cat": r["category"],
                              "ts": (r["ts"] or "")[:10], "text": body[:320], "url": r["url"]})
    for p in prs.values():
        p["unreviewed"] = p["rounds"] == 0 and not any(c["by"] != p["author"] for c in p["comments"] if c["by"] != "ai")
    # Approvals with no text, per reviewer (rubber-stamp signal), by PR.
    silent = {}
    for r in conn.execute(
            f"SELECT subject, actor, body FROM events WHERE source='github' AND event_type='review' "
            f"AND subject IN ({ph})", subs):
        rv = who(r["actor"])
        if rv and rv != prs[r["subject"]]["author"] and (r["body"] or "").strip().lower() in ECHO:
            silent.setdefault(rv, set()).add(r["subject"])

    # Developer view: authoring friction.
    devs = []
    for c, p in team.items():
        allp = [x for x in prs.values() if x["author"] == c]
        if not allp:
            continue
        # A PR merged with no human review scores 0, which is not "clean": keep it out of the median.
        mine = [x for x in allp if not x["unreviewed"]] or allp
        sc = [x["score"] for x in mine]
        recv = {k: sum(x["recv"][k] for x in mine) for k in ("bug", "design", "question", "nit")}
        devs.append({"id": c, "name": p["name"], "role": p.get("role", ""), "prs": len(allp),
                     "reviewed": len(allp) - sum(x["unreviewed"] for x in allp),
                     "unreviewed": sum(x["unreviewed"] for x in allp),
                     "median": round(statistics.median(sc), 1),
                     "clean": sum(1 for s in sc if s < 10), "high": sum(1 for s in sc if s >= 25),
                     "loc": int(statistics.median([x["loc"] for x in mine])),
                     "recv": recv, "bot": sum(x["bot"] for x in mine)})
    devs.sort(key=lambda d: d["median"])

    # Developer view: review quality (comments given on others' PRs).
    rev = {}
    for p in prs.values():
        for cm in p["comments"]:
            if cm["by"] == p["author"]:
                continue
            key = cm["by"] if cm["by"] in team or cm["by"] == "ai" else None
            if not key:
                continue
            d = rev.setdefault(key, {"given": {"bug": 0, "design": 0, "question": 0, "nit": 0},
                                     "prs": set(), "authors": set()})
            d["given"][cm["bucket"]] += 1
            d["prs"].add(p["subject"])
            d["authors"].add(p["author"])
    for c, s in silent.items():
        d = rev.setdefault(c, {"given": {"bug": 0, "design": 0, "question": 0, "nit": 0},
                               "prs": set(), "authors": set()})
        d["prs"] |= s
        d["authors"] |= {prs[x]["author"] for x in s}
    reviewers = []
    for c, d in rev.items():
        n = sum(d["given"].values())
        reviewers.append({"id": c, "name": "AI reviewers (Claude, MatterAI)" if c == "ai" else team[c]["name"],
                          "role": "" if c == "ai" else team[c].get("role", ""), "ai": c == "ai",
                          "comments": n, "given": d["given"], "prs": len(d["prs"]),
                          "authors": len(d["authors"] - {c}), "silent": len(silent.get(c, ())),
                          "substantive": round(100 * (d["given"]["bug"] + d["given"]["design"]) / n) if n else 0})
    reviewers.sort(key=lambda r: (r["ai"], -(r["given"]["bug"] + r["given"]["design"])))

    out = sorted(prs.values(), key=lambda x: -x["score"])
    return {"window": label, "since": since, "prs": out, "devs": devs, "reviewers": reviewers,
            "fresh": _fresh(conn, subs), "trend": trend(conn, team, who),
            "rq": review_quality(conn, team, lookup, since, until)}


def api(qs):
    """Self-contained entry for /api/reviews/pr (no dependency on synapse_pages)."""
    import os, sqlite3, yaml
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "config", "people.yaml")) as f:
        ppl = yaml.safe_load(f).get("people", [])
    people = [{"canonical": p["canonical"], "name": p.get("name", p["canonical"]), "role": p.get("role", ""),
               "scope": p.get("scope", "")} for p in ppl if p.get("canonical")]
    lookup = {}
    for p in ppl:
        c = p.get("canonical")
        if not c:
            continue
        for k in ("canonical", "github", "email", "jira_id", "slack_id", "slack_handle", "name"):
            if p.get(k):
                lookup[str(p[k]).lower().strip()] = c
        for a in (p.get("github_aliases") or []) + (p.get("git_names") or []):
            lookup[str(a).lower().strip()] = c
    conn = sqlite3.connect(f"file:{os.path.join(root, 'index', 'events.db')}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        return report(conn, people, lookup, (qs.get("w") or ["h1"])[0])
    finally:
        conn.close()


TREND_FROM = "2025-10-01"
EARLY, LATE = ("2026-04", "2026-06"), ("2026-07", "2026-09")


def trend(conn, team, who):
    """Per dev, per month since TREND_FROM: friction on reviewed PRs they authored, and the
    mix of review comments they gave on others' PRs. Plus Apr-Jun vs Jul-Sep comparison."""
    authors = {}
    for r in conn.execute("SELECT subject, actor FROM events WHERE source='github' AND "
                          "event_type IN ('pr_opened','pr_merged') AND ts >= ? ORDER BY event_type='pr_merged'",
                          (TREND_FROM,)):
        authors[r["subject"]] = who(r["actor"])
    months, out = set(), {c: {"fr": {}, "rv": {}, "unrev": {}} for c in team}
    for r in conn.execute("SELECT m.subject, substr(m.merged_at,1,7) mo, f.score, f.mechanical_json mj "
                          "FROM pr_meta m JOIN pr_friction f ON f.subject=m.subject "
                          "WHERE m.state='merged' AND m.merged_at >= ?", (TREND_FROM,)):
        a = authors.get(r["subject"])
        if not a:
            continue
        months.add(r["mo"])
        mech = json.loads(r["mj"] or "{}")
        if mech.get("review_rounds", 0) == 0 and mech.get("human_comments", 0) == 0:
            out[a]["unrev"][r["mo"]] = out[a]["unrev"].get(r["mo"], 0) + 1
        else:
            out[a]["fr"].setdefault(r["mo"], []).append(r["score"] or 0)
    for r in conn.execute("SELECT c.subject, c.category, e.actor, substr(e.ts,1,7) mo FROM pr_comment_class c "
                          "JOIN events e ON e.id=c.event_id WHERE c.source='human' AND e.ts >= ?", (TREND_FROM,)):
        rv = who(r["actor"])
        if not rv or authors.get(r["subject"]) in (None, rv):
            continue
        months.add(r["mo"])
        b = out[rv]["rv"].setdefault(r["mo"], {"bug": 0, "design": 0, "question": 0, "nit": 0})
        b[BUCKET.get(r["category"], "question")] += 1

    def med(lst):
        return round(statistics.median(lst), 1) if lst else None

    def span(d, lo, hi):
        return [x for mo, xs in d.items() if lo <= mo <= hi for x in xs]

    def subst(d, lo, hi):
        g = [v for mo, v in d.items() if lo <= mo <= hi]
        n = sum(sum(v.values()) for v in g)
        return (round(100 * sum(v["bug"] + v["design"] for v in g) / n) if n else None), n

    months = sorted(m for m in months if m <= date.today().isoformat()[:7])
    devs = []
    for c, p in team.items():
        o = out[c]
        if not (o["fr"] or o["rv"]):
            continue
        es, en = subst(o["rv"], *EARLY)
        ls, ln = subst(o["rv"], *LATE)
        devs.append({
            "id": c, "name": p["name"], "role": p.get("role", ""),
            "months": [{"m": m, "friction": med(o["fr"].get(m, [])), "prs": len(o["fr"].get(m, [])),
                        "unrev": o["unrev"].get(m, 0),
                        "given": o["rv"].get(m, {"bug": 0, "design": 0, "question": 0, "nit": 0})} for m in months],
            "early": {"friction": med(span(o["fr"], *EARLY)), "subst": es, "comments": en},
            "late": {"friction": med(span(o["fr"], *LATE)), "subst": ls, "comments": ln},
        })
    return {"months": months, "devs": devs, "early": "Apr to Jun", "late": "Jul to Sep"}


def review_quality(conn, team, lookup, since, until):
    """How well each dev REVIEWS: comments they gave on anyone's PR (team or other teams), mix, catches per PR,
    whether the author changed code after a substantive comment, silent approvals, and time to first response."""
    q_until = until or "9999"
    ev = conn.execute(
        "SELECT e.id, e.subject, e.actor, e.ts, e.event_type, e.title, e.body, e.url, c.category FROM events e "
        "LEFT JOIN pr_comment_class c ON c.event_id=e.id "
        "WHERE e.source='github' AND e.event_type IN ('review','comment','issue_comment') AND e.ts >= ? AND e.ts <= ?",
        (since, q_until)).fetchall()
    subs = {r["subject"] for r in ev if lookup.get((r["actor"] or "").lower()) in team}
    if not subs:
        return {"devs": []}
    ph = ",".join("?" * len(subs))
    meta, opened, author, title = {}, {}, {}, {}
    for r in conn.execute(f"SELECT subject, event_type, actor, ts, title FROM events WHERE subject IN ({ph}) "
                          "AND event_type IN ('pr_opened','pr_merged')", list(subs)):
        if r["event_type"] == "pr_opened":
            opened[r["subject"]] = r["ts"]
            author[r["subject"]] = r["actor"]
            title[r["subject"]] = r["title"]
        else:
            meta.setdefault(r["subject"], r["ts"])
            author.setdefault(r["subject"], r["actor"])
            title.setdefault(r["subject"], r["title"])
    commits = {}
    for r in conn.execute(f"SELECT subject, actor, ts FROM events WHERE subject IN ({ph}) AND event_type='commit_in_pr' "
                          "ORDER BY ts", list(subs)):
        commits.setdefault(r["subject"], []).append((r["ts"], r["actor"]))
    # PRs never merged (open or closed) have no opened/merged event: fall back to the closer, then the first committer.
    for r in conn.execute(f"SELECT subject, actor FROM events WHERE subject IN ({ph}) AND event_type='pr_closed'", list(subs)):
        author.setdefault(r["subject"], r["actor"])
    for sub, cs in commits.items():
        author.setdefault(sub, cs[0][1])
    canon = lambda x: lookup.get((x or "").lower()) or x  # noqa: E731
    loc = {r["subject"]: (r["additions"] or 0) + (r["deletions"] or 0)
           for r in conn.execute(f"SELECT subject, additions, deletions FROM pr_meta WHERE subject IN ({ph})", list(subs))}
    out = {}
    for r in ev:
        c = lookup.get((r["actor"] or "").lower())
        if c not in team:
            continue
        sub = r["subject"]
        a = author.get(sub)
        if not a or canon(a) == c or any(canon(x[1]) == c for x in commits.get(sub, [])):
            continue  # own PR, or co-author on it
        acanon = lookup.get(a.lower())
        d = out.setdefault(c, {"given": {"bug": 0, "design": 0, "question": 0, "nit": 0}, "prs": set(), "teamPrs": set(),
                               "authors": set(), "approved": set(), "spoke": set(), "first": {}, "blocked": set(),
                               "subst": 0, "monthly": {}, "catches": [], "loc": 0})
        d["prs"].add(sub)
        d["authors"].add(acanon or a)
        if acanon in team:
            d["teamPrs"].add(sub)
        d["first"][sub] = min(d["first"].get(sub, r["ts"]), r["ts"])
        state = (r["title"] or "").rsplit(": ", 1)[-1] if r["event_type"] == "review" else ""
        body = (r["body"] or "").strip()
        if state == "APPROVED":
            d["approved"].add(sub)
        if state == "CHANGES_REQUESTED":
            d["blocked"].add(sub)
        if body.lower() not in ECHO:
            d["spoke"].add(sub)
        cat = r["category"]
        if not cat:
            continue
        b = BUCKET.get(cat, "question")
        d["given"][b] += 1
        mo = r["ts"][:7]
        d["monthly"].setdefault(mo, {"bug": 0, "design": 0, "question": 0, "nit": 0})[b] += 1
        if b in ("bug", "design"):
            d["subst"] += 1
            d["catches"].append({"bucket": b, "ts": r["ts"][:10], "pr": sub, "title": (title.get(sub) or "")[:90],
                                 "author": acanon or a, "team": acanon in team, "url": r["url"],
                                 "text": " ".join(body.split())[:320]})
    from datetime import datetime

    def hrs(a, b):
        try:
            return (datetime.fromisoformat(b.replace("Z", "+00:00")) - datetime.fromisoformat(a.replace("Z", "+00:00"))).total_seconds() / 3600
        except Exception:
            return None
    devs = []
    for c, d in out.items():
        n = sum(d["given"].values())
        resp = [h for h in (hrs(opened[s], t) for s, t in d["first"].items() if s in opened) if h is not None and h >= 0]
        d["catches"].sort(key=lambda x: (x["bucket"] != "bug", x["ts"]), reverse=False)
        devs.append({"id": c, "name": team[c]["name"], "role": team[c].get("role", ""),
                     "prs": len(d["prs"]), "teamPrs": len(d["teamPrs"]), "otherPrs": len(d["prs"] - d["teamPrs"]),
                     "authors": len(d["authors"]), "comments": n, "given": d["given"],
                     "substantive": round(100 * d["subst"] / n) if n else None,
                     "catchesPerPr": round(d["subst"] / len(d["prs"]), 2) if d["prs"] else 0,
                     "blocked": len(d["blocked"]),
                     "approved": len(d["approved"]), "silent": len(d["approved"] - d["spoke"]),
                     "silentRate": round(100 * len(d["approved"] - d["spoke"]) / len(d["approved"])) if d["approved"] else None,
                     "firstResp": round(statistics.median(resp), 1) if resp else None,
                     "monthly": d["monthly"], "catches": sorted(d["catches"], key=lambda x: x["ts"], reverse=True)[:60]})
    for c, p in team.items():
        if c not in out:
            devs.append({"id": c, "name": p["name"], "role": p.get("role", ""), "prs": 0, "teamPrs": 0, "otherPrs": 0,
                         "authors": 0, "comments": 0, "given": {"bug": 0, "design": 0, "question": 0, "nit": 0},
                         "substantive": None, "catchesPerPr": 0, "blocked": 0, "approved": 0, "silent": 0,
                         "silentRate": None, "firstResp": None, "monthly": {}, "catches": []})
    return {"devs": devs}


def _fresh(conn, subs=None):
    f = {}
    f["github"] = (conn.execute("SELECT max(ts) FROM events WHERE source='github'").fetchone()[0] or "")[:10]
    f["scored"] = (conn.execute("SELECT max(computed_at) FROM pr_friction").fetchone()[0] or "")[:10]
    f["classified"] = (conn.execute("SELECT max(classified_at) FROM pr_comment_class").fetchone()[0] or "")[:10]
    if subs:
        ph = ",".join("?" * len(subs))
        tot = conn.execute(
            f"SELECT count(*) FROM events WHERE source='github' AND event_type IN ('review','comment','issue_comment') "
            f"AND subject IN ({ph}) AND length(trim(coalesce(body,'')))>25 AND actor NOT LIKE '%[bot]'", subs).fetchone()[0]
        done = conn.execute(f"SELECT count(*) FROM pr_comment_class WHERE source='human' AND subject IN ({ph})",
                            subs).fetchone()[0]
        f["coverage"] = round(100 * done / tot) if tot else 0
    return f


CSS = r"""
:root{
  --bg:#f3f6fa; --surface:#ffffff; --surface2:#eef2f8; --border:#e4e9f2;
  --text:#15202e; --muted:#5b6675; --hint:#9aa6b4;
  --accent:#3a55d9; --accent-2:#2c41b8; --accent-soft:rgba(58,85,217,.07);
  --ok:#3b6b3f; --ok-bg:#e9f1e8; --warn:#866113; --warn-bg:#f6efdd; --danger:#b23a4a; --danger-bg:#f8edef;
  --shadow:0 1px 2px rgba(20,32,46,.04),0 2px 6px rgba(20,32,46,.05); --radius:14px;
  --c-bug:#2a78d6; --c-design:#eb6834; --c-question:#1baf7a; --c-nit:#eda100;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font-family:'Hanken Grotesk',-apple-system,system-ui,sans-serif;font-size:14px;line-height:1.45}
.wrap{max-width:1180px;margin:0 auto;padding:6px 24px 60px}
.top{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:12px;flex-wrap:wrap}
h1{font-size:20px;font-weight:700;margin:0;letter-spacing:-.01em}
h2{font-size:15px;font-weight:700;margin:0}
.sub{color:var(--muted);font-size:12.5px;margin:2px 0 0}
.ctl{display:flex;gap:10px;flex-wrap:wrap}
.seg{display:inline-flex;background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:3px}
.seg button{font:inherit;font-size:12.5px;border:0;background:none;padding:6px 12px;border-radius:7px;cursor:pointer;color:var(--muted)}
.seg button.on{background:var(--accent-soft);color:var(--accent-2);font-weight:600}
.fresh{font-size:12px;color:var(--muted);margin:0 0 14px}
.fresh .warn{color:var(--warn);background:var(--warn-bg);padding:1px 7px;border-radius:6px;font-weight:600}
.cards{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));margin-bottom:18px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow);padding:12px 16px}
.card .l{font-size:10px;letter-spacing:.05em;text-transform:uppercase;color:var(--muted)}
.card .v{font-size:22px;font-weight:700;font-family:'IBM Plex Mono',monospace;margin-top:2px}
.card .h{font-size:11.5px;color:var(--hint)}
.panel{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow);padding:16px 18px;margin-bottom:18px}
.ph{display:flex;justify-content:space-between;align-items:baseline;gap:12px;margin-bottom:4px;flex-wrap:wrap}
.how{font-size:12px;color:var(--muted);margin:0 0 12px}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:11.5px;color:var(--muted)}
.legend i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:5px;vertical-align:-1px}
table{width:100%;border-collapse:collapse}
th{text-align:left;font-size:10px;letter-spacing:.05em;text-transform:uppercase;color:var(--muted);font-weight:600;padding:8px;border-bottom:1px solid var(--border);white-space:nowrap;cursor:pointer}
th.n,td.n{text-align:right}
td{padding:9px 8px;border-bottom:1px solid var(--border);vertical-align:middle}
tr.row{cursor:pointer} tr.row:hover td{background:var(--surface2)}
tr.row.sel td{background:var(--accent-soft)}
.nm{font-weight:600} .role{color:var(--hint);font-size:11px;margin-left:6px}
.mono{font-family:'IBM Plex Mono',monospace;font-size:12.5px}
.track{position:relative;height:10px;background:var(--surface2);border-radius:5px;min-width:140px}
.track>i{position:absolute;left:0;top:0;bottom:0;border-radius:5px}
.track .tick{position:absolute;top:-3px;bottom:-3px;width:1px;background:var(--border)}
.stack{display:flex;height:10px;border-radius:5px;overflow:hidden;min-width:160px;background:var(--surface2);gap:2px}
.stack>i{display:block;height:100%}
rect.b-bug{fill:var(--c-bug)} rect.b-design{fill:var(--c-design)} rect.b-question{fill:var(--c-question)} rect.b-nit{fill:var(--c-nit)}
.b-bug{background:var(--c-bug)} .b-design{background:var(--c-design)} .b-question{background:var(--c-question)} .b-nit{background:var(--c-nit)}
.band{font-size:11px;font-weight:600;padding:2px 8px;border-radius:6px;white-space:nowrap}
.band.clean,.band.low{color:var(--ok);background:var(--ok-bg)}
.band.moderate{color:var(--warn);background:var(--warn-bg)}
.band.high,.band.severe{color:var(--danger);background:var(--danger-bg)}
.chip{font-size:10.5px;font-weight:600;padding:1px 7px;border-radius:6px;color:#fff;white-space:nowrap}
a{color:var(--accent-2);text-decoration:none} a:hover{text-decoration:underline}
.drill{background:var(--surface2);padding:14px 16px!important}
.drill h4{margin:0 0 6px;font-size:11px;letter-spacing:.05em;text-transform:uppercase;color:var(--muted)}
.cm{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:8px 10px;margin:6px 0;font-size:12.5px}
.cm .meta{font-size:11px;color:var(--muted);margin-bottom:3px;display:flex;gap:8px;align-items:center}
.facts{display:flex;gap:18px;flex-wrap:wrap;font-size:12px;color:var(--muted);margin-bottom:8px}
.facts b{color:var(--text);font-family:'IBM Plex Mono',monospace}
.filters{display:flex;gap:8px;align-items:center;font-size:12px;color:var(--muted)}
select{font:inherit;font-size:12.5px;border:1px solid var(--border);border-radius:8px;padding:5px 8px;background:var(--surface)}
#tip{position:fixed;pointer-events:none;background:var(--text);color:#fff;font-size:12px;padding:7px 10px;border-radius:8px;max-width:320px;display:none;z-index:50}
svg text{font-family:'IBM Plex Mono',monospace;font-size:10px;fill:var(--muted)}
.empty{color:var(--hint);padding:20px;text-align:center}
.zm-bg{position:fixed;inset:0;background:rgba(21,32,46,.45);display:flex;align-items:center;justify-content:center;z-index:40}
.zm{background:#fff;border-radius:14px;padding:16px 20px 18px;max-width:96vw;max-height:92vh;overflow:auto;box-shadow:0 10px 40px rgba(20,32,46,.25)}
.zm-h{display:flex;justify-content:space-between;align-items:center;gap:16px;margin-bottom:8px}
.zm-h b{font-size:15px}.zm-x{font:inherit;font-size:18px;line-height:1;border:0;background:var(--surface2);border-radius:8px;width:30px;height:30px;cursor:pointer;color:var(--muted)}
.zm table{margin-top:12px}.zm td,.zm th{font-size:12px}
.zoomable{cursor:zoom-in}
.zbtn{font:inherit;font-size:12px;border:1px solid var(--border);border-radius:8px;padding:5px 9px;background:var(--surface);cursor:pointer;color:var(--accent-2)}

.tgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(500px,1fr));gap:14px}
.tcard{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow);padding:14px 16px}
.tcard .hd{display:flex;justify-content:space-between;align-items:baseline;gap:8px}
.delta{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--muted);margin:6px 0 4px}
.delta b{font-family:'IBM Plex Mono',monospace;color:var(--text)}
.up{color:var(--ok);font-weight:600} .down{color:var(--danger);font-weight:600} .flat{color:var(--hint)}
.mini{font-size:10.5px;letter-spacing:.05em;text-transform:uppercase;color:var(--muted);margin:10px 0 2px}

"""

BODY = r"""
<div class="top">
  <div><h2>PR friction and review quality</h2>
    <div class="sub">How much correction each dev's PRs needed, and what kind of review each dev gives. Team PRs merged in the window.</div></div>
  <div class="ctl">
    <div class="seg" id="win"><button data-w="h1" class="on">Mid-year</button><button data-w="90">90d</button><button data-w="30">30d</button><button data-w="ytd">This year</button></div>
    <div class="seg" id="view"><button data-v="dev" class="on">Authors</button><button data-v="rev">Reviewers</button><button data-v="pr">PRs</button><button data-v="trend">Trend</button></div>
  </div>
</div>
<div class="fresh" id="fresh"></div>
<div class="cards" id="cards"></div>
<div id="main"><div class="panel empty">Loading…</div></div>
<div id="tip"></div>
"""

JS = r"""
let W='h1', V='dev', D=null, SEL=null, AUTH='', SORTP='score', DIRP=-1, TSORT='name', DS='score', DD=-1, AS={k:'median',d:1}, RS={k:'substantive',d:-1};

function zoom(title,html,after){ closeZoom();
  const bg=document.createElement('div'); bg.className='zm-bg'; bg.id='zm';
  bg.innerHTML=`<div class="zm"><div class="zm-h"><b>${title}</b><button class="zm-x" title="Close (Esc)">×</button></div>${html}</div>`;
  bg.onclick=e=>{if(e.target===bg||e.target.closest('.zm-x'))closeZoom();};
  document.body.appendChild(bg); placeZoom(bg); after&&after(bg); }
// Inside the Reviews tab the page is an iframe sized to its full height, so position:fixed centres on the
// whole frame. Pin the overlay to the slice of the frame the user can see and freeze the parent's scroll.
function placeZoom(bg){ const fe=window.frameElement; if(!fe) return;
  try{ const r=fe.getBoundingClientRect(), ph=parent.innerHeight, top=Math.max(0,-r.top), h=Math.min(ph, r.bottom)-Math.max(0,r.top);
    Object.assign(bg.style,{position:'absolute',top:top+'px',left:'0',right:'0',bottom:'auto',height:h+'px'});
    document.body.style.position='relative'; bg.querySelector('.zm').style.maxHeight=(h-30)+'px';
    parent.document.documentElement.style.overflow='hidden'; }catch(e){} }
function closeZoom(){const z=document.getElementById('zm'); z&&z.remove(); try{if(window.frameElement)parent.document.documentElement.style.overflow='';}catch(e){} const t=document.getElementById('tip'); if(t)t.style.display='none';}
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeZoom();});
const ZW=()=>Math.max(680,Math.min(1150,innerWidth-110));
const B=[['bug','Bugs and logic'],['design','Design and tests'],['question','Questions'],['nit','Nits and naming']];
const esc=s=>(s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const band=s=>s==0?'clean':s<10?'low':s<25?'moderate':s<50?'high':'severe';
const HX={bug:'#2a78d6',design:'#eb6834',question:'#1baf7a',nit:'#eda100',accent:'#3a55d9',border:'#e4e9f2',danger:'#b23a4a',ok:'#3b6b3f',warn:'#c08a1e',surface:'#ffffff'};
const bcol=s=>s<10?HX.ok:s<25?HX.warn:HX.danger;
const tip=document.getElementById('tip');
function tipOn(e,h){tip.innerHTML=h;tip.style.display='block';tip.style.left=Math.min(e.clientX+14,innerWidth-330)+'px';tip.style.top=(e.clientY+14)+'px';}
function tipOff(){tip.style.display='none';}
document.addEventListener('mousemove',e=>{const t=e.target.closest('[data-tip]'); t?tipOn(e,t.dataset.tip):tipOff();});
const legend=()=>'<div class="legend">'+B.map(([k,l])=>`<span><i class="b-${k}"></i>${l}</span>`).join('')+'</div>';
function stack(o){const n=B.reduce((a,[k])=>a+(o[k]||0),0); if(!n) return '<div class="stack"></div>';
  return '<div class="stack">'+B.filter(([k])=>o[k]).map(([k,l])=>`<i class="b-${k}" style="flex:${o[k]}" data-tip="${l}: ${o[k]} (${Math.round(100*o[k]/n)}%)"></i>`).join('')+'</div>';}
function track(v,max){return `<div class="track" data-tip="Median friction ${v} (${band(v)})"><span class="tick" style="left:${10/max*100}%"></span><span class="tick" style="left:${25/max*100}%"></span><i style="width:${Math.max(2,v/max*100)}%;background:${bcol(v)}"></i></div>`;}
const chip=b=>`<span class="chip b-${b}">${B.find(x=>x[0]==b)[1]}</span>`;
const nameOf=id=>{if(id==='ai')return 'AI reviewer'; const d=[...D.devs,...D.reviewers].find(x=>x.id===id); return d?d.name:id;};
function cmHtml(c){return `<div class="cm"><div class="meta">${chip(c.bucket)}<span>${esc(nameOf(c.by))}</span><span>${c.ts}</span>${c.url?`<a href="${c.url}" target="_blank">open ↗</a>`:''}</div>${esc(c.text)}</div>`;}

async function load(){
  document.getElementById('main').innerHTML='<div class="panel empty">Loading…</div>';
  D=await (await fetch('/api/reviews/pr?w='+W)).json(); SEL=null; render();
}
function render(){
  const f=D.fresh||{}, cov=f.coverage??0;
  document.getElementById('fresh').innerHTML=`${esc(D.window)}. GitHub data to <b>${f.github||'?'}</b>, scores computed <b>${f.scored||'?'}</b>, comments classified <b>${f.classified||'?'}</b>, coverage <b>${cov}%</b> `
    +(cov<80?'<span class="warn">Review categories incomplete for this window</span>':'');
  const P=D.prs.filter(p=>!p.unreviewed), sc=P.map(p=>p.score).sort((a,b)=>a-b), med=sc.length?sc[Math.floor(sc.length/2)]:0;
  const H=D.rq.devs, tc=H.reduce((a,r)=>a+r.comments,0), sub=H.reduce((a,r)=>a+r.given.bug+r.given.design,0);
  document.getElementById('cards').innerHTML=[
    ['PRs merged',D.prs.length,(D.prs.length-P.length)+' with no human review'],['Median friction',med,band(med)],
    ['Clean or low',P.length?Math.round(100*P.filter(p=>p.score<10).length/P.length)+'%':'0%','friction under 10'],
    ['Review comments',tc,'by the team, on any PR'],
    ['Substantive',tc?Math.round(100*sub/tc)+'%':'0%','bugs, logic, design, tests']]
    .map(([l,v,h])=>`<div class="card"><div class="l">${l}</div><div class="v">${v}</div><div class="h">${h}</div></div>`).join('');
  V==='dev'?renderDev():V==='rev'?renderRev():V==='pr'?renderPr():renderTrend();
}

function renderDev(){
  const max=Math.max(30,...D.devs.map(d=>d.median));
  const sv=(o,k)=>k==='name'?o.name:k==='cleanpct'?o.clean/Math.max(1,o.reviewed||o.prs):k==='subst'?o.substantive:o[k];
  const srt=(arr,S)=>[...arr].sort((a,b)=>{ if(a.ai!==b.ai) return a.ai?1:-1; const x=sv(a,S.k),y=sv(b,S.k); return (typeof x==='string'?x.localeCompare(y):x-y)*S.d;});
  const th=(S,k,l,n)=>`<th class="${n?'n':''}" data-tk="${S===AS?'a':'r'}:${k}">${l}${S.k===k?(S.d<0?' ↓':' ↑'):''}</th>`;
  const fr=srt(D.devs,AS).map(d=>`<tr class="row ${SEL==='a:'+d.id?'sel':''}" data-s="a:${d.id}">
    <td><span class="nm">${esc(d.name)}</span><span class="role">${d.role}</span></td>
    <td class="n mono" data-tip="${d.reviewed} reviewed of ${d.prs} merged">${d.reviewed}</td><td class="n mono" data-tip="Merged with no human review" style="color:${d.unreviewed?'var(--warn)':'var(--hint)'}">${d.unreviewed}</td><td>${track(d.median,max)}</td><td class="n mono">${d.median}</td>
    <td class="n mono" data-tip="Reviewed PRs with friction under 10">${Math.round(100*d.clean/Math.max(1,d.reviewed||d.prs))}%</td>
    <td class="n mono" data-tip="PRs with friction 25 or more">${d.high}</td>
    <td>${stack(d.recv)}</td><td class="n mono" data-tip="Median lines changed per PR">${d.loc}</td></tr>`
    +(SEL==='a:'+d.id?`<tr><td colspan="9" class="drill">${devPrs(d.id)}</td></tr>`:'')).join('');
  const rv=srt(D.reviewers,RS).map(r=>`<tr class="row ${SEL==='r:'+r.id?'sel':''}" data-s="r:${r.id}" ${r.ai?'style="opacity:.75"':''}>
    <td><span class="nm">${esc(r.name)}</span><span class="role">${r.role}</span></td>
    <td class="n mono">${r.comments}</td><td>${stack(r.given)}</td>
    <td class="n mono" data-tip="Share of comments that are bugs, logic, design or tests">${r.substantive}%</td>
    <td class="n mono">${r.prs}</td><td class="n mono" data-tip="Different authors reviewed">${r.authors}</td>
    <td class="n mono" data-tip="PRs approved with no written comment">${r.ai?'':r.silent}</td></tr>`
    +(SEL==='r:'+r.id?`<tr><td colspan="7" class="drill">${revCatches(r.id)}</td></tr>`:'')).join('');
  document.getElementById('main').innerHTML=`
  <div class="panel"><div class="ph"><h2>Friction on PRs they authored</h2>${legend()}</div>
    <p class="how">Lower is better. Median friction counts reviewed PRs only (ticks at 10 and 25); unreviewed PRs are listed separately because a PR nobody reviewed always scores 0; the mix shows what reviewers raised on their PRs. Click a dev for their PRs.</p>
    <table><thead><tr>${th(AS,'name','Developer')}${th(AS,'reviewed','Reviewed',1)}${th(AS,'unreviewed','Unreviewed',1)}${th(AS,'median','Median friction')}<th></th>${th(AS,'cleanpct','Clean',1)}${th(AS,'high','High',1)}<th>Comments received</th>${th(AS,'loc','Size',1)}</tr></thead><tbody>${fr||'<tr><td class="empty" colspan="9">No PRs</td></tr>'}</tbody></table></div>
  <div class="panel" style="padding:12px 18px"><span class="how">How well each dev <b>reviews</b> others' PRs is in the <a href="#" id="go-rev">Reviewers</a> view.</span></div>`;
  const gr=document.getElementById('go-rev'); if(gr) gr.onclick=e=>{e.preventDefault(); document.querySelector('#view button[data-v=rev]').click();};
  document.querySelectorAll('th[data-tk]').forEach(t=>t.onclick=()=>{const [w,k]=t.dataset.tk.split(':'); const S=w==='a'?AS:RS; S.d=S.k===k?-S.d:(k==='name'||k==='median'||k==='unreviewed'?1:-1); S.k=k; renderDev();});
  document.querySelectorAll('.dsort').forEach(x=>x.onchange=e=>{DS=e.target.value; DD=-1; renderDev();});
  document.querySelectorAll('.ddir').forEach(b=>b.onclick=()=>{DD=+b.dataset.d; renderDev();});
  bindRows();
}
const caughtN=p=>p.comments.filter(c=>c.by!==p.author&&(c.bucket==='bug'||c.bucket==='design')).length;
function devPrs(id){
  const key={score:p=>p.score, merged:p=>p.merged, size:p=>p.loc, caught:caughtN}[DS];
  const ps=D.prs.filter(p=>p.author===id).sort((a,b)=>{ if(DS==='score'&&a.unreviewed!==b.unreviewed) return a.unreviewed?1:-1;
    const x=key(a),y=key(b); return (typeof x==='string'?x.localeCompare(y):x-y)*DD;});
  const lo=DS==='merged'?'Oldest':'Low', hi=DS==='merged'?'Newest':'High';
  return `<div class="ph"><h4>Their PRs (${ps.length})</h4><div class="filters">Sort <select class="dsort">${[['score','Friction'],['merged','Merged date'],['caught','Issues caught'],['size','Size']].map(([k,l])=>`<option value="${k}" ${k===DS?'selected':''}>${l}</option>`).join('')}</select>
    <div class="seg"><button class="ddir ${DD<0?'on':''}" data-d="-1">${hi} first</button><button class="ddir ${DD>0?'on':''}" data-d="1">${lo} first</button></div></div></div><table>`
    +ps.map(p=>`<tr><td><a href="${p.url}" target="_blank">${p.repo}#${p.number}</a> ${esc(p.title)}</td><td class="mono" style="color:var(--muted)">${p.merged.slice(5)}</td><td>${stack(p.recv)}</td><td class="n mono" data-tip="Bug, logic or design comments">${caughtN(p)}</td><td class="n mono">${p.unreviewed?'':p.score}</td><td>${p.unreviewed?'<span class="band moderate">unreviewed</span>':`<span class="band ${band(p.score)}">${band(p.score)}</span>`}</td><td class="n mono" style="color:var(--hint)">${p.loc}</td></tr>`).join('')+'</table>';
}
function revCatches(id){
  const cs=[]; D.prs.forEach(p=>p.comments.forEach(c=>{if(c.by===id&&p.author!==id&&(c.bucket==='bug'||c.bucket==='design')) cs.push({...c,pr:p});}));
  cs.sort((a,b)=>(a.bucket==='bug'?0:1)-(b.bucket==='bug'?0:1)||b.ts.localeCompare(a.ts));
  if(!cs.length) return '<div class="how">No bug, logic or design comments in this window.</div>';
  return `<h4>Bugs, logic and design comments (${cs.length})</h4>`+cs.slice(0,30).map(c=>`<div class="cm"><div class="meta">${chip(c.bucket)}<a href="${c.pr.url}" target="_blank">${c.pr.repo}#${c.pr.number}</a><span>${esc(nameOf(c.pr.author))}'s PR</span><span>${c.ts}</span>${c.url?`<a href="${c.url}" target="_blank">comment ↗</a>`:''}</div>${esc(c.text)}</div>`).join('');
}

const arr=k=>SORTP===k?(DIRP<0?' ↓':' ↑'):'';
function renderPr(){
  const authors=[...new Set(D.prs.map(p=>p.author))].map(a=>[a,nameOf(a)]).sort((a,b)=>a[1].localeCompare(b[1]));
  let ps=D.prs.filter(p=>!AUTH||p.author===AUTH);
  const caught=p=>p.comments.filter(c=>c.by!==p.author&&(c.bucket==='bug'||c.bucket==='design')).length;
  const key={merged:p=>p.merged, score:p=>p.unreviewed?-1:p.score, size:p=>p.loc, caught:caught, author:p=>nameOf(p.author)}[SORTP];
  ps=[...ps].sort((a,b)=>{ if(SORTP==='score'&&a.unreviewed!==b.unreviewed) return a.unreviewed?1:-1;
    const x=key(a),y=key(b); return (typeof x==='string'?x.localeCompare(y):x-y)*DIRP;});
  const rows=ps.slice(0,150).map(p=>{const subst=p.comments.filter(c=>c.by!==p.author&&(c.bucket==='bug'||c.bucket==='design')).length;
    const revs=new Set(p.comments.filter(c=>c.by!==p.author).map(c=>c.by)).size;
    return `<tr class="row ${SEL==='p:'+p.subject?'sel':''}" data-s="p:${p.subject}">
    <td><a href="${p.url}" target="_blank">${p.repo}#${p.number}</a> <span style="color:var(--muted)">${esc(p.title.slice(0,70))}</span></td>
    <td>${esc(nameOf(p.author))}</td><td class="mono">${p.merged.slice(5)}</td>
    <td class="n mono">${p.unreviewed?'':p.score}</td><td>${p.unreviewed?'<span class="band moderate">unreviewed</span>':`<span class="band ${band(p.score)}">${band(p.score)}</span>`}</td>
    <td>${stack(p.recv)}</td><td class="n mono" data-tip="Bug, logic or design comments from reviewers">${subst}</td>
    <td class="n mono" data-tip="Reviewers who wrote comments">${revs}</td><td class="n mono">${p.loc}</td></tr>`
    +(SEL==='p:'+p.subject?`<tr><td colspan="9" class="drill">${prDrill(p)}</td></tr>`:'');}).join('');
  document.getElementById('main').innerHTML=`
  <div class="panel"><div class="ph"><h2>Friction vs size</h2><div class="filters"><button class="zbtn" id="zsc" title="Open a larger view">⤢ Enlarge</button>Sort <select id="sortp">${[['merged','Merged date'],['score','Friction'],['caught','Issues caught'],['size','Size'],['author','Author']].map(([k,l])=>`<option value="${k}" ${k===SORTP?'selected':''}>${l}</option>`).join('')}</select>
    <div class="seg" id="dirp"><button data-d="-1" class="${DIRP<0?'on':''}">${SORTP==='merged'?'Newest':SORTP==='author'?'Z to A':'High'} first</button><button data-d="1" class="${DIRP>0?'on':''}">${SORTP==='merged'?'Oldest':SORTP==='author'?'A to Z':'Low'} first</button></div>
    Author <select id="auth"><option value="">Everyone</option>${authors.map(([a,n])=>`<option value="${a}" ${a===AUTH?'selected':''}>${esc(n)}</option>`).join('')}</select></div></div>
    <p class="how">Each dot is a reviewed, merged PR (unreviewed PRs are left out). Up means more correction was needed; right means bigger. Dots above the dashed line at 25 are high friction. Click a dot to open its details below.</p>
    <div id="scatter"></div></div>
  <div class="panel"><div class="ph"><h2>PRs and what review caught</h2>${legend()}</div>
    <p class="how">Click a row to see the review: who commented, what they caught, and how much rework followed.</p>
    <table><thead><tr><th>PR</th><th data-sp="author">Author${arr('author')}</th><th data-sp="merged">Merged${arr('merged')}</th><th class="n" data-sp="score">Friction${arr('score')}</th><th></th><th>Comments received</th><th class="n" data-sp="caught">Caught${arr('caught')}</th><th class="n">Reviewers</th><th class="n" data-sp="size">Size${arr('size')}</th></tr></thead><tbody>${rows||'<tr><td class="empty" colspan="9">No PRs</td></tr>'}</tbody></table>
    ${ps.length>150?`<div class="how">Showing 150 of ${ps.length}.</div>`:''}</div>`;
  scatter(ps);
  document.getElementById('zsc').onclick=()=>zoom('Friction vs size'+(AUTH?' · '+esc(nameOf(AUTH)):''),'<div id="zscat"></div>',bg=>scatter(ps,bg.querySelector('#zscat'),ZW(),560));
  document.getElementById('auth').onchange=e=>{AUTH=e.target.value;SEL=null;renderPr();};
  document.querySelectorAll('th[data-sp]').forEach(th=>th.onclick=()=>{const k=th.dataset.sp; DIRP=SORTP===k?-DIRP:(k==='author'?1:-1); SORTP=k; renderPr();});
  document.getElementById('sortp').onchange=e=>{SORTP=e.target.value; DIRP=SORTP==='author'?1:-1; renderPr();};
  document.querySelectorAll('#dirp button').forEach(b=>b.onclick=()=>{DIRP=+b.dataset.d; renderPr();});
  bindRows();
}
function prDrill(p){
  const cs=p.comments.filter(c=>c.by!==p.author);
  const hrs=p.ttm==null?'?':p.ttm<48?Math.round(p.ttm)+' h':Math.round(p.ttm/24)+' d';
  const by={}; cs.forEach(c=>{(by[c.by]=by[c.by]||{bug:0,design:0,question:0,nit:0})[c.bucket]++;});
  return `<div class="facts"><span>Changes requested <b>${p.cr}</b></span><span>Review rounds <b>${p.rounds}</b></span><span>Commits after first review <b>${p.rework}</b></span><span>Time to merge <b>${hrs}</b></span><span>Lines <b>${p.loc}</b></span><span>Main cause <b>${p.cause}</b></span></div>`
    +(Object.keys(by).length?'<h4>Reviewers</h4><table>'+Object.entries(by).map(([k,o])=>`<tr><td style="width:220px">${esc(nameOf(k))}</td><td>${stack(o)}</td><td class="n mono">${Object.values(o).reduce((a,b)=>a+b,0)}</td></tr>`).join('')+'</table>':'')
    +(cs.length?'<h4 style="margin-top:12px">Comments</h4>'+cs.sort((a,b)=>B.findIndex(x=>x[0]==a.bucket)-B.findIndex(x=>x[0]==b.bucket)).map(cmHtml).join(''):'<div class="how">No classified review comments on this PR.</div>');
}
function scatter(all,el,w,h){
  const ps=all.filter(p=>!p.unreviewed);
  el=el||document.getElementById('scatter'); w=w||el.clientWidth||1000; h=h||260; const m={l:36,r:12,t:10,b:26};
  if(!ps.length){el.innerHTML='<div class="empty">No reviewed PRs</div>';return;}
  const lx=v=>Math.log10(Math.max(1,v)), xmax=Math.max(3,Math.ceil(lx(Math.max(...ps.map(p=>p.loc))))), ymax=100;
  const X=v=>m.l+lx(v)/xmax*(w-m.l-m.r), Y=v=>h-m.b-v/ymax*(h-m.t-m.b);
  let s=`<svg width="${w}" height="${h}" style="display:block">`;
  [0,25,50,75,100].forEach(v=>s+=`<line x1="${m.l}" x2="${w-m.r}" y1="${Y(v)}" y2="${Y(v)}" stroke="${v==25?HX.danger:HX.border}" ${v==25?'stroke-dasharray="4 4"':''}/><text x="${m.l-6}" y="${Y(v)+3}" text-anchor="end">${v}</text>`);
  for(let i=0;i<=xmax;i++) s+=`<text x="${X(10**i)}" y="${h-8}" text-anchor="middle">${10**i>=1000?(10**i/1000)+'k':10**i} lines</text>`;
  ps.forEach(p=>s+=`<circle cx="${X(p.loc)}" cy="${Y(Math.min(p.score,100))}" r="5" fill="${bcol(p.score)}" fill-opacity=".7" stroke="#fff" stroke-width="2" style="cursor:pointer" data-s="p:${p.subject}" data-tip="${esc(p.repo+'#'+p.number+' · '+nameOf(p.author))}<br>Friction ${p.score} · ${p.loc} lines<br>${esc(p.title.slice(0,80))}"/>`);
  el.innerHTML=s+'</svg>';
  el.querySelectorAll('circle').forEach(c=>c.onclick=()=>{closeZoom();SEL=c.dataset.s;renderPr();const r=document.querySelector('tr.sel');r&&r.scrollIntoView({block:'center'});});
}

const MON=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const ml=m=>MON[+m.slice(5)-1]+(m.slice(5)==='01'||m===D.trend.months[0]?" '"+m.slice(2,4):'');
function arrow(a,b,lowerBetter){
  if(a==null||b==null) return '<span class="flat">not enough data</span>';
  const d=b-a, good=lowerBetter?d<0:d>0, small=Math.abs(d)<(lowerBetter?1.5:5);
  return `<b>${a}</b> → <b>${b}</b> `+(small?'<span class="flat">steady</span>':`<span class="${good?'up':'down'}">${good?'improved':'worse'}</span>`);
}
function trendCharts(d,w=470,h=110){
  const M=D.trend.months, m={l:28,r:6,t:8,b:18}, n=M.length;
  const X=i=>m.l+(i+.5)*(w-m.l-m.r)/n, bw=Math.max(6,(w-m.l-m.r)/n-8);
  const hi=M.indexOf('2026-04'), he=M.indexOf('2026-09');
  const shade=hi>=0?`<rect x="${X(hi)-(w-m.l-m.r)/n/2}" y="${m.t}" width="${(he-hi+1)*(w-m.l-m.r)/n}" height="${h-m.t-m.b}" fill="#eef1fb"/>`:'';
  const axis=M.map((mo,i)=>`<text x="${X(i)}" y="${h-4}" text-anchor="middle">${ml(mo)}</text>`).join('');
  // friction line (lower is better)
  const ymax=Math.max(30,...d.months.map(x=>x.friction||0)), Y=v=>h-m.b-v/ymax*(h-m.t-m.b);
  let f=`<svg width="${w}" height="${h}" style="display:block">${shade}`;
  [10,25].forEach(v=>f+=`<line x1="${m.l}" x2="${w-m.r}" y1="${Y(v)}" y2="${Y(v)}" stroke="${v==25?HX.danger:HX.border}" stroke-dasharray="3 3"/><text x="${m.l-4}" y="${Y(v)+3}" text-anchor="end">${v}</text>`);
  let path='',pen=false;
  d.months.forEach((x,i)=>{ if(x.friction==null){pen=false;return;} path+=(pen?'L':'M')+X(i)+' '+Y(x.friction)+' '; pen=true; });
  f+=`<path d="${path}" fill="none" stroke="${HX.accent}" stroke-width="2"/>`;
  d.months.forEach((x,i)=>{ if(x.friction!=null) f+=`<circle cx="${X(i)}" cy="${Y(x.friction)}" r="4" fill="${bcol(x.friction)}" stroke="#fff" stroke-width="2" data-tip="${ml(x.m)}: median friction ${x.friction}<br>${x.prs} reviewed PRs${x.unrev?', '+x.unrev+' unreviewed':''}"/>`;
    else if(x.unrev) f+=`<text x="${X(i)}" y="${h-m.b-4}" text-anchor="middle" data-tip="${ml(x.m)}: ${x.unrev} PRs, none reviewed">·</text>`; });
  f+=axis+'</svg>';
  // review given, stacked bars
  const tot=x=>B.reduce((a,[k])=>a+(x.given[k]||0),0), rmax=Math.max(5,...d.months.map(tot)), R=v=>v/rmax*(h-m.t-m.b);
  let r=`<svg width="${w}" height="${h}" style="display:block">${shade}<text x="${m.l-4}" y="${m.t+8}" text-anchor="end">${rmax}</text>`;
  d.months.forEach((x,i)=>{ let y=h-m.b; const t=tot(x); if(!t) return;
    const parts=B.filter(([k])=>x.given[k]).map(([k,l])=>`${l}: ${x.given[k]}`).join('<br>');
    B.forEach(([k])=>{ const v=x.given[k]; if(!v) return; const hh=R(v); y-=hh;
      r+=`<rect x="${X(i)-bw/2}" y="${y+1}" width="${bw}" height="${Math.max(1,hh-2)}" rx="2" fill="${HX[k]}" data-tip="${ml(x.m)}: ${t} comments (${Math.round(100*(x.given.bug+x.given.design)/t)}% substantive)<br>${parts}"/>`; }); });
  r+=axis+'</svg>';
  return `<div class="mini">Friction on their PRs, monthly median (lower is better)</div>${f}<div class="mini">Review comments they gave on others' PRs</div>${r}`;
}

function zoomTrend(d){
  const rows=d.months.map(x=>{const t=B.reduce((a,[k])=>a+(x.given[k]||0),0);
    return `<tr><td>${ml(x.m)}</td><td class="n mono">${x.friction??''}</td><td class="n mono">${x.prs}</td><td class="n mono">${x.unrev||''}</td><td class="n mono">${t||''}</td><td class="n mono">${x.given.bug||''}</td><td class="n mono">${x.given.design||''}</td><td class="n mono">${x.given.question||''}</td><td class="n mono">${x.given.nit||''}</td></tr>`;}).join('');
  zoom(`${esc(d.name)} <span class="role">${esc(d.role)}</span>`, trendCharts(d,ZW(),210)
    +`<table><thead><tr><th>Month</th><th class="n">Median friction</th><th class="n">Reviewed PRs</th><th class="n">Unreviewed</th><th class="n">Comments given</th><th class="n">Bugs, logic</th><th class="n">Design, tests</th><th class="n">Questions</th><th class="n">Nits</th></tr></thead><tbody>${rows}</tbody></table>`);
}
function renderTrend(){
  const T=D.trend;
  const imp=d=>(d.early.friction!=null&&d.late.friction!=null)?d.late.friction-d.early.friction:999;
  const last=d=>{const m=[...d.months].reverse().find(x=>x.friction!=null); return m?m.friction:999;};
  const tkey={name:d=>d.name, improve:imp, latest:last, review:d=>-((d.late.comments||0)+(d.early.comments||0))}[TSORT];
  const devs=[...T.devs].sort((a,b)=>{const x=tkey(a),y=tkey(b); return typeof x==='string'?x.localeCompare(y):x-y;});
  const cards=devs.map(d=>`<div class="tcard"><div class="hd"><div><span class="nm">${esc(d.name)}</span><span class="role">${d.role}</span></div></div>
    <div class="delta"><span>Friction ${T.early} vs ${T.late}: ${arrow(d.early.friction,d.late.friction,true)}</span>
    <span>Substantive reviews: ${arrow(d.early.subst,d.late.subst,false)}</span></div>
    <div class="zoomable" data-zoom="${d.id}" title="Click to enlarge">${trendCharts(d)}</div></div>`).join('');
  document.getElementById('main').innerHTML=`<div class="panel" style="padding:12px 18px"><div class="ph"><h2>Trend over time</h2><div class="filters">Sort <select id="tsort">${[['name','Name'],['improve','Most improved friction'],['latest','Lowest current friction'],['review','Most review comments']].map(([k,l])=>`<option value="${k}" ${k===TSORT?'selected':''}>${l}</option>`).join('')}</select></div></div>${legend()}
    <p class="how" style="margin:0">One card per dev from ${ml(T.months[0])} to now. The shaded band is the mid-year review window (Apr to Sep). The top chart should trend down; in the bottom chart, more blue and orange means deeper reviews. Hover any point or bar for numbers. The window buttons above do not apply here.</p></div>
    <div class="tgrid">${cards}</div>`;
  document.getElementById('tsort').onchange=e=>{TSORT=e.target.value;renderTrend();};
  document.querySelectorAll('[data-zoom]').forEach(z=>z.onclick=()=>zoomTrend(T.devs.find(x=>x.id===z.dataset.zoom)));
}

let RQS={k:'catchesPerPr',d:-1}, RQSEL=null;
function rqChart(d,w=470,h=110){
  const M=D.trend.months, m={l:28,r:6,t:8,b:18}, n=M.length, X=i=>m.l+(i+.5)*(w-m.l-m.r)/n, bw=Math.max(6,(w-m.l-m.r)/n-8);
  const hi=M.indexOf('2026-04'), he=M.indexOf('2026-09');
  const shade=hi>=0?`<rect x="${X(hi)-(w-m.l-m.r)/n/2}" y="${m.t}" width="${(he-hi+1)*(w-m.l-m.r)/n}" height="${h-m.t-m.b}" fill="#eef1fb"/>`:'';
  const g=mo=>d.monthly[mo]||{bug:0,design:0,question:0,nit:0}, tot=x=>B.reduce((a,[k])=>a+(x[k]||0),0);
  const rmax=Math.max(5,...M.map(mo=>tot(g(mo)))), R=v=>v/rmax*(h-m.t-m.b);
  let r=`<svg width="${w}" height="${h}" style="display:block">${shade}<text x="${m.l-4}" y="${m.t+8}" text-anchor="end">${rmax}</text>`;
  M.forEach((mo,i)=>{ const x=g(mo), t=tot(x); if(!t) return; let y=h-m.b;
    const tp=`${ml(mo)}: ${t} comments, ${Math.round(100*(x.bug+x.design)/t)}% substantive<br>`+B.filter(([k])=>x[k]).map(([k,l])=>`${l}: ${x[k]}`).join('<br>');
    B.forEach(([k])=>{const v=x[k]; if(!v)return; const hh=R(v); y-=hh; r+=`<rect x="${X(i)-bw/2}" y="${y+1}" width="${bw}" height="${Math.max(1,hh-2)}" rx="2" fill="${HX[k]}" data-tip="${tp}"/>`;}); });
  return r+M.map((mo,i)=>`<text x="${X(i)}" y="${h-4}" text-anchor="middle">${ml(mo)}</text>`).join('')+'</svg>';
}
function rqCatches(d,limit){
  if(!d.catches.length) return '<div class="how">No bug, logic or design comments in this window.</div>';
  return d.catches.slice(0,limit).map(c=>`<div class="cm"><div class="meta">${chip(c.bucket)}<a href="https://github.com/${c.pr.replace('#','/pull/')}" target="_blank">${esc(c.pr.split('/').pop())}</a><span>${esc(nameOf(c.author))}'s PR${c.team?'':' (other team)'}</span><span>${c.ts}</span>${c.url?`<a href="${c.url}" target="_blank">comment ↗</a>`:''}</div>${esc(c.text)}</div>`).join('');
}
function renderRev(){
  const sv=(o,k)=>k==='name'?o.name:(o[k]??-1);
  const devs=[...D.rq.devs].sort((a,b)=>{const x=sv(a,RQS.k),y=sv(b,RQS.k); return (typeof x==='string'?x.localeCompare(y):x-y)*RQS.d;});
  const th=(k,l,tip)=>`<th class="${k==='name'?'':'n'}" data-rq="${k}" ${tip?`data-tip="${tip}"`:''}>${l}${RQS.k===k?(RQS.d<0?' ↓':' ↑'):''}</th>`;
  const pct=v=>v==null?'<span style="color:var(--hint)">n/a</span>':v+'%';
  const rows=devs.map(d=>`<tr class="row ${RQSEL===d.id?'sel':''}" data-rq-row="${d.id}">
    <td><span class="nm">${esc(d.name)}</span><span class="role">${d.role}</span></td>
    <td class="n mono" data-tip="${d.teamPrs} team PRs, ${d.otherPrs} other teams' PRs">${d.prs}</td>
    <td class="n mono">${d.authors}</td><td class="n mono">${d.comments}</td><td>${stack(d.given)}</td>
    <td class="n mono">${pct(d.substantive)}</td><td class="n mono"><b>${d.catchesPerPr}</b></td>
    <td class="n mono">${d.blocked}</td>
    <td class="n mono" style="color:${(d.silentRate??0)>=70?'var(--danger)':(d.silentRate??0)>=40?'var(--warn)':'inherit'}">${pct(d.silentRate)}</td>
    <td class="n mono">${d.firstResp==null?'n/a':d.firstResp<48?Math.round(d.firstResp)+' h':Math.round(d.firstResp/24)+' d'}</td></tr>`
    +(RQSEL===d.id?`<tr><td colspan="10" class="drill"><div class="ph"><h4>Monthly review comments given</h4>${legend()}</div>
      <div class="zoomable" data-zoomr="${d.id}" title="Click to enlarge">${rqChart(d)}</div>
      <h4 style="margin-top:12px">Strongest catches: bugs, logic and design (${d.catches.length})</h4>${rqCatches(d,12)}
      ${d.catches.length>12?`<button class="zbtn" data-allr="${d.id}">Show all ${d.catches.length}</button>`:''}</td></tr>`:'')).join('');
  document.getElementById('main').innerHTML=`<div class="panel"><div class="ph"><h2>How well each dev reviews</h2>${legend()}</div>
    <p class="how">Comments they gave on anyone's PR in the window, team or other teams, excluding their own PRs and PRs they co-authored.
    <b>Catches per PR</b> = bug, logic and design comments per PR reviewed (the main depth signal). <b>Changes requested</b> = PRs they blocked until fixed.
    <b>Silent approvals</b> = PRs they approved without writing any comment on them (high means rubber-stamping). <b>First response</b> = median time from PR opened to their first review.
    Click a dev for their monthly trend and strongest catches.</p>
    <table><thead><tr>${th('name','Reviewer')}${th('prs','PRs reviewed')}${th('authors','Authors')}${th('comments','Comments')}<th>Mix</th>${th('substantive','Substantive')}${th('catchesPerPr','Catches / PR','Bug, logic and design comments per PR reviewed')}${th('blocked','Changes requested')}${th('silentRate','Silent approvals')}${th('firstResp','First response')}</tr></thead><tbody>${rows}</tbody></table></div>`;
  document.querySelectorAll('th[data-rq]').forEach(t=>t.onclick=()=>{const k=t.dataset.rq; RQS.d=RQS.k===k?-RQS.d:(k==='name'||k==='silentRate'||k==='firstResp'?1:-1); RQS.k=k; renderRev();});
  document.querySelectorAll('tr[data-rq-row]').forEach(r=>r.onclick=e=>{if(e.target.closest('a'))return; RQSEL=RQSEL===r.dataset.rqRow?null:r.dataset.rqRow; renderRev();});
  document.querySelectorAll('[data-zoomr]').forEach(z=>z.onclick=()=>{const d=D.rq.devs.find(x=>x.id===z.dataset.zoomr);
    zoom(`${esc(d.name)} · reviews given`, `<div style="margin-bottom:6px">${legend()}</div>`+rqChart(d,ZW(),260)+`<h4 style="margin:14px 0 4px">Strongest catches (${d.catches.length})</h4>`+rqCatches(d,60));});
  document.querySelectorAll('[data-allr]').forEach(b=>b.onclick=()=>{const d=D.rq.devs.find(x=>x.id===b.dataset.allr); zoom(`${esc(d.name)} · all catches`, rqCatches(d,60));});
}
function bindRows(){
  document.querySelectorAll('tr.row').forEach(r=>r.onclick=e=>{if(e.target.closest('a'))return; SEL=SEL===r.dataset.s?null:r.dataset.s; render();});
}
document.querySelectorAll('#win button').forEach(b=>b.onclick=()=>{document.querySelectorAll('#win button').forEach(x=>x.classList.toggle('on',x===b));W=b.dataset.w;load();});
document.querySelectorAll('#view button').forEach(b=>b.onclick=()=>{document.querySelectorAll('#view button').forEach(x=>x.classList.toggle('on',x===b));V=b.dataset.v;SEL=null;render();});
load();
"""


def page():
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>PR friction · Synapse</title>
<style>@import url('https://fonts.googleapis.com/css2?family=Hanken+Grotesk:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap');{CSS}</style>
</head><body><div class="wrap">{BODY}</div><script>{JS}</script></body></html>"""
