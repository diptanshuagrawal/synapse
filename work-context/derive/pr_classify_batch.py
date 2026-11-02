#!/usr/bin/env python3
"""Headless /pr-quality classify: run state/pending_pr_comments.json through
`claude -p` (Sonnet) in chunks, write state/verdicts.pr_comments.json, then apply
and recompute pr_friction. Owner-run; same taxonomy as config/pr_review_rules.md.

    .venv/bin/python derive/pr_classify_batch.py [--chunk 120] [--since 2026-01-01]
"""
import argparse, json, os, re, subprocess, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"
PENDING = STATE / "pending_pr_comments.json"
VERDICTS = STATE / "verdicts.pr_comments.json"
RULES = (ROOT / "config" / "pr_review_rules.md").read_text()
PY = str(ROOT / ".venv" / "bin" / "python")


def _env():
    return {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}


def _extract(text):
    m = re.search(r"\[[\s\S]*\]", text)
    return json.loads(m.group(0), strict=False) if m else []


def classify(chunk, idx):
    items = [{"event_id": x["event_id"], "subject": x["subject"], "kind": x["kind"],
              "body": x["body_excerpt"]} for x in chunk]
    prompt = (RULES + "\n\nClassify each item below. Return ONLY a JSON array of verdicts "
              "(event_id, category, confidence), one per item, nothing else.\n\n"
              + json.dumps(items, ensure_ascii=False))
    for attempt in range(3):
        try:
            out = subprocess.run(["claude", "-p", "--model", "sonnet", "--output-format", "text"],
                                 input=prompt, capture_output=True, text=True, env=_env(),
                                 timeout=900).stdout
            v = _extract(out)
            if v:
                print(f"chunk {idx}: {len(v)}/{len(chunk)}", flush=True)
                return v
        except Exception as e:
            print(f"chunk {idx} attempt {attempt}: {e}", flush=True)
    print(f"chunk {idx}: FAILED", flush=True)
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunk", type=int, default=120)
    ap.add_argument("--since", default="2026-01-01")
    a = ap.parse_args()
    pending = json.loads(PENDING.read_text())["pending"]
    chunks = [pending[i:i + a.chunk] for i in range(0, len(pending), a.chunk)]
    print(f"{len(pending)} comments in {len(chunks)} chunks", flush=True)
    with ThreadPoolExecutor(3) as ex:
        res = list(ex.map(classify, chunks, range(len(chunks))))
    verdicts = [v for r in res for v in r]
    VERDICTS.write_text(json.dumps({"verdicts": verdicts}, indent=1))
    print(f"verdicts: {len(verdicts)}", flush=True)
    subprocess.run([PY, str(ROOT / "derive" / "apply_pr_classes.py")], cwd=ROOT)
    subprocess.run([PY, str(ROOT / "derive" / "github_metrics.py"), "--since", a.since], cwd=ROOT)
    print("DONE", flush=True)


if __name__ == "__main__":
    sys.exit(main())
