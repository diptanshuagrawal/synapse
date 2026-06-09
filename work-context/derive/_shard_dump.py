"""One-off: shard state/pending_cluster_finalize.json into /tmp/clabel/batch_<i>.json
for the parallel labeling workflow. Balances batches by approximate payload size
so no single agent gets an oversized slice.
"""
import json
import os
import glob

from derive.finalize_refresh import PENDING_PATH

OUT = "/tmp/clabel"
TARGET_BYTES = 160_000  # ~per-batch payload budget


def main():
    os.makedirs(OUT, exist_ok=True)
    for f in glob.glob(f"{OUT}/batch_*.json") + glob.glob(f"{OUT}/verdicts_*.json"):
        os.remove(f)

    d = json.loads(PENDING_PATH.read_text())
    clusters = d["clusters"]
    # greedy size-balanced bin packing into sequential batches
    batches = []
    cur, cur_bytes = [], 0
    for c in clusters:
        b = len(json.dumps(c))
        if cur and cur_bytes + b > TARGET_BYTES:
            batches.append(cur)
            cur, cur_bytes = [], 0
        cur.append(c)
        cur_bytes += b
    if cur:
        batches.append(cur)

    for i, b in enumerate(batches):
        with open(f"{OUT}/batch_{i}.json", "w") as fh:
            json.dump({"batch_index": i, "clusters": b}, fh, indent=2)

    print(json.dumps({
        "n_batches": len(batches),
        "total_clusters": sum(len(b) for b in batches),
        "per_batch_min": min(len(b) for b in batches),
        "per_batch_max": max(len(b) for b in batches),
        "out_dir": OUT,
    }, indent=2))


if __name__ == "__main__":
    main()
