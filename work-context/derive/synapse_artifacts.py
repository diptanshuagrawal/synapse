"""Per-competency supporting artifacts for a developer in a review cycle.

Each competency gets a few measured items (PRs, review quality, docs, sprint delivery, on-call, releases,
peer ratings) drawn from data Synapse already holds. Used by the Reviews dev page and the PDF export.
Read-only. Reuses synapse_prq (PR friction + review quality) and synapse_sprint (cached sprint numbers).
"""
import json
import os
import sqlite3
import statistics

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "index", "events.db")
DESIGN_WORDS = ("trd", "design", "contract", "solutioning", "architecture", "rfc", "proposal", "approach")
# MSF parameters (Darwinbox peer form) mapped onto the review competencies.
PEER_MAP = {"Team Collaboration": "collaboration", "Customer Centricity": "customer", "Business Outcomes": "customer",
            "Execution Excellence": "execution", "Ownership": "operational"}
RL = ["", "DNME", "SME", "ME", "EE", "OP"]


def _person(canon):
    with open(os.path.join(ROOT, "config", "people.yaml")) as f:
        for p in yaml.safe_load(f).get("people", []):
            if p.get("canonical") == canon:
                return p
    return {}


def _team_roles():
    with open(os.path.join(ROOT, "config", "people.yaml")) as f:
        return {p["canonical"]: p.get("role", "") for p in yaml.safe_load(f).get("people", [])
                if p.get("canonical") and p.get("scope") == "team"}


def _item(label, value, note="", links=None):
    return {"label": label, "value": value, "note": note, "links": links or []}


def artifacts(canon, since, until, peers=None):
    """canon: people.yaml canonical. since/until: ISO dates. peers: parsed MSF export dict (optional)."""
    import synapse_prq
    import synapse_sprint
    p = _person(canon)
    out = {k: [] for k in ("technical", "execution", "operational", "collaboration", "creativity", "customer",
                           "orgbuilding")}
    win = "h1" if since == "2026-04-01" else "ytd"
    prq = synapse_prq.api({"w": [win]})
    a = next((d for d in prq["devs"] if d["id"] == canon), None)
    r = next((d for d in prq["rq"]["devs"] if d["id"] == canon), None)
    mine = [x for x in prq["prs"] if x["author"] == canon]

    # ── Technical: authored PRs, review depth, docs
    if a:
        repos = sorted({x["repo"] for x in mine})
        out["technical"].append(_item("PRs merged", a["prs"],
                                      f"{a['reviewed']} reviewed by others; median friction {a['median']} "
                                      f"({a['clean']} of {a['reviewed']} clean or low); repos: {', '.join(repos)}"))
        hard = [x for x in mine if not x["unreviewed"] and x["score"] >= 25]
        if hard:
            out["technical"].append(_item("High-friction PRs", len(hard), "needed heavy rework in review",
                                          [{"t": f"{x['repo']}#{x['number']}", "u": x["url"]} for x in hard[:4]]))
    if r and r["comments"]:
        top = [c for c in r["catches"] if c["bucket"] == "bug"][:3]
        out["technical"].append(_item("PR review quality", f"{r['catchesPerPr']} catches per PR",
                                      f"{r['prs']} PRs reviewed, {r['comments']} comments, {r['substantive']}% "
                                      f"on bugs, logic or design",
                                      [{"t": c["pr"].split("/")[-1], "u": c["url"]} for c in top]))

    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    jid = p.get("jira_id")
    pages = {}
    if jid:
        for x in conn.execute("SELECT url, title, event_type, min(ts) ts FROM events WHERE source='confluence' "
                              "AND actor=? AND event_type IN ('page_created','page_updated') AND ts BETWEEN ? AND ? "
                              "GROUP BY url, event_type", (jid, since, until + "T23:59:59Z")):
            d = pages.setdefault(x["url"], {"title": x["title"], "created": False})
            d["created"] |= x["event_type"] == "page_created"
    created = [d | {"u": u} for u, d in pages.items() if d["created"]]
    design = [d for d in pages.values() if any(w in (d["title"] or "").lower() for w in DESIGN_WORDS)]
    if pages:
        out["technical"].append(_item("Docs written or edited", len(pages), f"{len(created)} created by them",
                                      [{"t": d["title"][:60], "u": d["u"]} for d in created[:4]]))

    # ── Creativity: design docs authored, design-level review comments
    if design:
        out["creativity"].append(_item("Design docs, TRDs and contracts", len(design),
                                       "pages with TRD, design, contract or solutioning in the title",
                                       [{"t": d["title"][:60], "u": u} for u, d in pages.items() if d in design][:4]))
    if r and r["given"]["design"]:
        out["creativity"].append(_item("Design and test challenges in reviews", r["given"]["design"],
                                       "review comments that question approach, structure or test strategy"))

    # ── Execution + operational: sprint numbers
    sp = synapse_sprint.api({})
    if sp.get("status") == "ok":
        dv = next((d for d in sp["devs"] if d["id"] == canon), None)
        if dv:
            rows = [(s, x) for s, x in zip(sp["sprints"], dv["sprints"]) if x and since <= s["start"] <= until]
            if rows:
                dl = sum(x["delivered"] or 0 for _, x in rows)
                cap = sum(x["capacitySP"] or 0 for _, x in rows)
                band = sum(1 for _, x in rows if x["vsLevel"] in ("within", "above"))
                out["execution"].append(_item("Sprint delivery", f"{round(dl, 1)} SP",
                                              f"{round(100 * dl / cap) if cap else 'n/a'}% of capacity after leave and "
                                              f"on-call; at or above the level band in {band} of {len(rows)} sprints"))
                fl = [x for _, x in rows if x.get("committedSP") is not None]
                if fl:
                    com = sum(x["committedSP"] for x in fl)
                    add = sum(x["addedSP"] for x in fl)
                    co = sum(x["carriedOutSP"] for x in fl)
                    dp = sum(x["donePlannedSP"] for x in fl)
                    da = sum(x["doneAddedSP"] for x in fl)
                    cyc = [c for x in fl for c in (x.get("cycle") or [])]
                    out["execution"].append(_item(
                        "Predictability", f"{round(100 * dp / com) if com else 'n/a'}% of planned work closed",
                        f"carry-over {round(100 * co / (com + add)) if com + add else 'n/a'}%; "
                        f"median cycle time {statistics.median(cyc) if cyc else 'n/a'} days; "
                        f"{round(100 * da / (dp + da)) if dp + da else 0}% of closed work was added mid-sprint"))
                od = sum(x["oncallDays"] or 0 for _, x in rows)
                osp = sum(x["oncallSP"] or 0 for _, x in rows)
                if od or osp:
                    out["operational"].append(_item("On-call", f"{od} days", f"{round(osp, 1)} SP of on-call tickets closed"))

    # ── Operational + customer: production changes they owned
    keys = {k.lower() for k in [p.get("email"), "@" + (p.get("slack_handle") or "")] if k}
    keys |= {"@" + str(x).lower() for x in [p.get("github")] + (p.get("github_aliases") or []) if x}
    rel = [x for x in conn.execute("SELECT cmr_subject, title, url, outcome, is_feature_release, release_owner "
                                   "FROM feature_release WHERE released_at BETWEEN ? AND ?",
                                   (since, until + "T23:59:59Z")) if (x["release_owner"] or "").lower() in keys]
    rel = list({x["cmr_subject"]: x for x in rel}.values())
    if rel:
        bad = [x for x in rel if any(w in (x["outcome"] or "").lower() for w in ("rollback", "emergency", "fail"))]
        out["operational"].append(_item("Production changes owned", len(rel),
                                        f"{len(bad)} rolled back or emergency" if bad else "none rolled back",
                                        [{"t": (x["title"] or x["cmr_subject"])[:50], "u": x["url"]} for x in bad[:3]]))
        feat = [x for x in rel if x["is_feature_release"]]
        if feat:
            out["customer"].append(_item("Customer-facing releases", len(feat), "feature CMRs they owned",
                                         [{"t": (x["title"] or x["cmr_subject"])[:50], "u": x["url"]} for x in feat[:4]]))

    # ── Collaboration + org-building: who they review
    if r and r["prs"]:
        out["collaboration"].append(_item("Reviews for others", f"{r['prs']} PRs",
                                          f"{r['authors']} authors; {r['otherPrs']} PRs from other teams; "
                                          f"{r['blocked']} changes requested; median first response "
                                          f"{round(r['firstResp'] / 24, 1) if r['firstResp'] else 'n/a'} days"))
        roles = _team_roles()
        jr = [c for c in r["catches"] if roles.get(c["author"]) == "SDE1"]
        juniors = sorted({c["author"] for c in jr})
        if jr:
            out["orgbuilding"].append(_item("Substantive reviews on SDE1 PRs", len(jr),
                                            "bug, logic and design comments that teach: " +
                                            ", ".join(j.split("-")[0].title() for j in juniors)))
    others = [d for u, d in pages.items() if not d["created"]]
    if others:
        out["collaboration"].append(_item("Contributed to others' docs", len(others), "edits on pages others created"))
    if created:
        out["orgbuilding"].append(_item("Knowledge shared in writing", len(created), "Confluence pages they created"))

    # ── Peer ratings (MSF)
    if peers and peers.get("stats"):
        for x in peers["stats"].get("parameters", []):
            k = PEER_MAP.get(x["parameter"])
            if k and x.get("avg"):
                out[k].append(_item(f"Peer rating: {x['parameter']}", f"{x['avg']} ({RL[round(x['avg'])]})",
                                    f"{x['n']} raters; " + ", ".join(f"{n} {g}" for g, n in (x.get("dist") or {}).items())))
    conn.close()
    return out
