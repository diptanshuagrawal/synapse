#!/usr/bin/env python3
"""Layer 1 driver: refresh deterministic skeletons for all configured Go services.

Loops the Go extractor over every service in `config/services.yaml`, writes each
`derived/services/<svc>.skeleton.json`, and reports which services *drifted*
(structure changed since the last run) so a downstream routine can re-brief only
those — saving LLM tokens on unchanged services.

No LLM. No network beyond what the extractor reads from local mirrors.

Gating:
  - Reads `state/last_codegraph_success.date`. With `--require-fresh`, refuses to
    run unless the graph was rebuilt today (the kafka discovery reads graph.db).
    Without it, runs anyway and just reports graph freshness.

Drift:
  - A per-service structural hash (endpoints + tables + kafka, excluding commit)
    is compared to `state/service_skeleton_hashes.json`. First run = all drift.
  - The drift report is written to `state/service_drift.json` for the routine.

Usage:
    python derive/service_derive/build_skeletons.py
    python derive/service_derive/build_skeletons.py --require-fresh
    python derive/service_derive/build_skeletons.py --services-config config/services.yaml
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import go_extractor as gx  # noqa: E402
import play_extractor as play  # noqa: E402

# language -> (builder fn taking (repo_path: Path, alias: str) -> Skeleton)
_BUILDERS = {
    "go": gx.build_skeleton,
    "java": play.build_play_skeleton,
    "play": play.build_play_skeleton,
}

REPO_ROOT = Path(__file__).resolve().parents[2]  # work-context/
STATE_DIR = REPO_ROOT / "state"
OUT_DIR = REPO_ROOT / "derived" / "services"
HASHES_FILE = STATE_DIR / "service_skeleton_hashes.json"
DRIFT_FILE = STATE_DIR / "service_drift.json"
REGISTRY = Path.home() / ".code-review-graph" / "registry.json"
SUCCESS_DATE = STATE_DIR / "last_codegraph_success.date"


def _load_yaml_services(path: Path) -> list[dict]:
    try:
        import yaml  # PyYAML
        data = yaml.safe_load(path.read_text())
        return list(data.get("services", []))
    except ModuleNotFoundError:
        # Minimal fallback: parse "- alias: x" lines without PyYAML.
        out, cur = [], None
        for line in path.read_text().splitlines():
            s = line.strip()
            if s.startswith("- alias:"):
                if cur:
                    out.append(cur)
                cur = {"alias": s.split(":", 1)[1].strip()}
            elif cur and s.startswith("language:"):
                cur["language"] = s.split(":", 1)[1].strip()
        if cur:
            out.append(cur)
        return out


def _registry_map() -> dict[str, str]:
    reg = json.loads(REGISTRY.read_text())
    repos = reg.get("repos", reg) if isinstance(reg, dict) else reg
    return {r["alias"]: r["path"] for r in repos if r.get("alias")}


def _structural_hash(skel_dict: dict) -> str:
    core = {
        k: skel_dict.get(k)
        for k in ("endpoints", "tables", "kafka_listeners", "kafka_producers")
    }
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _today() -> str:
    return _dt.date.today().isoformat()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--services-config", default=str(REPO_ROOT / "config" / "services.yaml"))
    ap.add_argument(
        "--require-fresh",
        action="store_true",
        help="Refuse to run unless the code-graph was rebuilt today.",
    )
    args = ap.parse_args()

    # Graph-freshness gate.
    graph_date = SUCCESS_DATE.read_text().strip() if SUCCESS_DATE.exists() else None
    graph_fresh = graph_date == _today()
    if args.require_fresh and not graph_fresh:
        print(
            f"ABORT: code-graph not fresh (last success: {graph_date or 'never'}, "
            f"today: {_today()}). Run run-codegraph.sh first.",
            file=sys.stderr,
        )
        return 3

    services = _load_yaml_services(Path(args.services_config))
    svcs = [s for s in services if s.get("language", "go") in _BUILDERS]
    if not svcs:
        print("No supported services configured.", file=sys.stderr)
        return 2

    registry = _registry_map()
    prev_hashes: dict[str, str] = (
        json.loads(HASHES_FILE.read_text()) if HASHES_FILE.exists() else {}
    )
    new_hashes = dict(prev_hashes)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    drifted, errors, summary, brief_skipped = [], [], [], []
    for svc in svcs:
        alias = svc["alias"]
        lang = svc.get("language", "go")
        brief_eligible = svc.get("brief", True)
        path = registry.get(alias)
        if not path or not Path(path).is_dir():
            errors.append(alias)
            print(f"  {alias:18} ERROR: mirror not registered/found")
            continue
        skel = _BUILDERS[lang](Path(path), alias)
        skel_dict = asdict(skel)
        (OUT_DIR / f"{alias}.skeleton.json").write_text(json.dumps(skel_dict, indent=2))
        h = _structural_hash(skel_dict)
        changed = prev_hashes.get(alias) != h
        new_hashes[alias] = h
        if changed and brief_eligible:
            drifted.append(alias)
        elif changed and not brief_eligible:
            brief_skipped.append(alias)
        flag = "DRIFT" if changed else "same"
        if changed and not brief_eligible:
            flag = "DRIFT (brief skipped)"
        summary.append(
            f"  {alias:18} ep={len(skel.endpoints):4} tbl={len(skel.tables):4} "
            f"kl={len(skel.kafka_listeners):2} kp={len(skel.kafka_producers):2} {flag}"
        )

    HASHES_FILE.write_text(json.dumps(new_hashes, indent=2, sort_keys=True))
    DRIFT_FILE.write_text(
        json.dumps(
            {
                "date": _today(),
                "graph_fresh": graph_fresh,
                "graph_success_date": graph_date,
                "all": [s["alias"] for s in svcs],
                "drifted": drifted,
                "brief_skipped": brief_skipped,
                "errors": errors,
            },
            indent=2,
        )
    )

    print(f"code-graph fresh: {graph_fresh} (last success: {graph_date or 'never'})")
    print("\n".join(summary))
    print(f"\ndrifted ({len(drifted)}): {drifted or '—'}")
    print(f"errors  ({len(errors)}): {errors or '—'}")
    print(f"drift report → {DRIFT_FILE.relative_to(REPO_ROOT)}")
    if drifted:
        print("\nNext: run /service-brief <svc> for each drifted service.")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
