"""Build a reproducible sample of the SNAP soc-Pokec social network.

Source : https://snap.stanford.edu/data/soc-Pokec.html
Full   : 1,632,803 nodes / 30,622,564 directed friendship edges

Why sample by BFS and not by random edges
-----------------------------------------
Picking 200k edges uniformly at random from a 30M-edge graph yields a nearly
edgeless scatter: average degree collapses to ~1 and multi-hop traversals
terminate immediately, which would make the 2-hop and 3-hop benchmarks
meaningless. A BFS/snowball ball around a seed node preserves the local
density and degree skew of the real network, so traversal costs are
representative.

Determinism
-----------
The seed node is chosen by sorting candidate node ids and indexing with
SAMPLE_SEED, and BFS explores neighbours in sorted id order. Same seed =>
byte-identical CSVs on any machine.
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import json
import shutil
import sys
import urllib.request
from collections import deque
from pathlib import Path

import numpy as np
import pandas as pd

from . import config

EDGES_URL = "https://snap.stanford.edu/data/soc-pokec-relationships.txt.gz"
PROFILES_URL = "https://snap.stanford.edu/data/soc-pokec-profiles.txt.gz"

EDGES_GZ = config.DATA_DIR / "soc-pokec-relationships.txt.gz"
PROFILES_GZ = config.DATA_DIR / "soc-pokec-profiles.txt.gz"

NODES_CSV = config.DATA_DIR / "nodes.csv"
EDGES_CSV = config.DATA_DIR / "edges.csv"
MANIFEST = config.DATA_DIR / "manifest.json"

# Columns 0,3,4,7 of the profiles TSV: user_id, gender, region, AGE
PROFILE_COLS = {0: "uid", 3: "gender", 4: "region", 7: "age"}


def _download(url: str, dest: Path) -> None:
    if dest.exists() and dest.stat().st_size > 0:
        print(f"  [cached] {dest.name}")
        return
    print(f"  downloading {url} -> {dest.name}")
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f, length=1 << 20)
    tmp.rename(dest)


def _read_edges() -> tuple[np.ndarray, np.ndarray]:
    print("  parsing full edge list (30.6M rows, ~30s)...")
    df = pd.read_csv(
        EDGES_GZ, sep="\t", header=None, names=["src", "dst"], dtype=np.int32
    )
    return df["src"].to_numpy(), df["dst"].to_numpy()


def _build_csr(src: np.ndarray, dst: np.ndarray, n_max: int):
    """Sort edges by source and build offset index for O(deg) neighbour lookup."""
    order = np.argsort(src, kind="stable")
    s, d = src[order], dst[order]
    offsets = np.searchsorted(s, np.arange(n_max + 2), side="left")
    return d, offsets


def _bfs_order(neighbours, offsets, seed: int, budget: int) -> list[int]:
    """Breadth-first node ordering. Neighbours visited in sorted id order so
    the traversal is deterministic."""
    seen = {seed}
    order = [seed]
    q = deque([seed])
    while q and len(order) < budget:
        u = q.popleft()
        nbrs = np.sort(neighbours[offsets[u]: offsets[u + 1]])
        for v in nbrs:
            v = int(v)
            if v not in seen:
                seen.add(v)
                order.append(v)
                q.append(v)
                if len(order) >= budget:
                    break
    return order


def _induced_edge_count(src, dst, keep_mask) -> int:
    return int(np.count_nonzero(keep_mask[src] & keep_mask[dst]))


def build(target_rels: int | None = None, seed: int | None = None) -> dict:
    target_rels = target_rels or config.TARGET_RELATIONSHIPS
    seed = seed if seed is not None else config.SAMPLE_SEED

    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    print("Step 1/5  fetching source files")
    _download(EDGES_URL, EDGES_GZ)
    _download(PROFILES_URL, PROFILES_GZ)

    print("Step 2/5  loading graph")
    src, dst = _read_edges()
    n_max = int(max(src.max(), dst.max()))
    neighbours, offsets = _build_csr(src, dst, n_max)

    # Deterministic seed node: pick from high-degree nodes so BFS doesn't
    # strand in a sparse corner. Rank by degree, index by SAMPLE_SEED.
    deg = np.diff(offsets[: n_max + 2])
    hubs = np.argsort(-deg, kind="stable")[:1000]
    seed_node = int(hubs[seed % len(hubs)])
    print(f"  seed node = {seed_node} (out-degree {int(deg[seed_node])})")

    print("Step 3/5  binary-searching node budget for ~%d edges" % target_rels)
    lo, hi = 1_000, config.MAX_NODES
    best_order, best_edges = None, 0
    for _ in range(12):
        mid = (lo + hi) // 2
        order = _bfs_order(neighbours, offsets, seed_node, mid)
        mask = np.zeros(n_max + 1, dtype=bool)
        mask[np.array(order, dtype=np.int32)] = True
        e = _induced_edge_count(src, dst, mask)
        print(f"    nodes={len(order):>7,}  induced edges={e:>9,}")
        if e >= target_rels:
            best_order, best_edges = order, e
            hi = mid - 1
        else:
            lo = mid + 1
        if len(order) < mid:      # BFS exhausted the component
            best_order, best_edges = order, e
            break
    if best_order is None:
        best_order, best_edges = order, e

    node_ids = np.array(sorted(best_order), dtype=np.int32)
    mask = np.zeros(n_max + 1, dtype=bool)
    mask[node_ids] = True
    keep = mask[src] & mask[dst]
    e_src, e_dst = src[keep], dst[keep]

    # Trim deterministically (sorted, then head) to land exactly on target
    if len(e_src) > target_rels:
        ordr = np.lexsort((e_dst, e_src))[:target_rels]
        e_src, e_dst = e_src[ordr], e_dst[ordr]
        still = np.unique(np.concatenate([e_src, e_dst]))
        node_ids = still

    # Hard ceilings. Neo4j AuraDB Free publishes contradictory limits (product
    # page: 50k nodes / 175k rels; FAQ: 200k / 400k). We size to the
    # conservative figure so the identical dataset loads on every platform --
    # a dataset that fits four platforms and not the fifth is not a benchmark.
    if len(node_ids) > config.MAX_NODES:
        sys.exit(
            f"Sample has {len(node_ids):,} nodes, exceeding MAX_NODES="
            f"{config.MAX_NODES:,}. Lower TARGET_RELATIONSHIPS and rebuild."
        )
    print(f"  final: {len(node_ids):,} nodes / {len(e_src):,} relationships")

    print("Step 4/5  joining profile attributes")
    wanted = set(int(x) for x in node_ids)
    rows = {}
    with gzip.open(PROFILES_GZ, "rt", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            parts = line.rstrip("\n").split("\t")
            try:
                uid = int(parts[0])
            except (ValueError, IndexError):
                continue
            if uid not in wanted:
                continue
            gender = parts[3] if parts[3] not in ("null", "") else ""
            region = parts[4] if parts[4] not in ("null", "") else "unknown"
            try:
                age = int(parts[7])
            except (ValueError, IndexError):
                age = 0
            rows[uid] = (uid, gender, region, age if 0 < age < 120 else 0)

    print("Step 5/5  writing CSVs")
    with open(NODES_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["uid", "gender", "region", "age"])
        for uid in node_ids:
            uid = int(uid)
            w.writerow(rows.get(uid, (uid, "", "unknown", 0)))

    with open(EDGES_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["src", "dst"])
        for a, b in zip(e_src.tolist(), e_dst.tolist()):
            w.writerow([a, b])

    manifest = {
        "source": "SNAP soc-Pokec social network",
        "source_url": "https://snap.stanford.edu/data/soc-Pokec.html",
        "full_graph": {"nodes": 1_632_803, "relationships": 30_622_564},
        "sample_method": "deterministic BFS/snowball from a high-degree seed",
        "sample_seed": seed,
        "seed_node": seed_node,
        "nodes": int(len(node_ids)),
        "relationships": int(len(e_src)),
        "node_properties": ["uid (int)", "gender (str)", "region (str)", "age (int)"],
        "relationship_type": "FRIEND (directed)",
        "size_constraint": (
            "Capped at <=45,000 nodes / 150,000 relationships to fit the most "
            "conservative published Neo4j AuraDB Free limit (50k nodes / 175k "
            "relationships). Neo4j's FAQ states 200k/400k; the product "
            "announcement states 50k/175k. We sized to the smaller figure."
        ),
        "nodes_csv_sha256": _sha(NODES_CSV),
        "edges_csv_sha256": _sha(EDGES_CSV),
    }
    MANIFEST.write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2))
    return manifest


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_manifest() -> dict:
    if not MANIFEST.exists():
        sys.exit("No dataset built. Run:  python -m bench.dataset")
    return json.loads(MANIFEST.read_text())


def iter_node_batches(batch_size: int = 5_000):
    with open(NODES_CSV, newline="", encoding="utf-8") as f:
        batch = []
        for row in csv.DictReader(f):
            batch.append({
                "uid": int(row["uid"]),
                "gender": row["gender"],
                "region": row["region"],
                "age": int(row["age"]),
            })
            if len(batch) >= batch_size:
                yield batch
                batch = []
        if batch:
            yield batch


def iter_edge_batches(batch_size: int = 10_000):
    with open(EDGES_CSV, newline="", encoding="utf-8") as f:
        batch = []
        for row in csv.DictReader(f):
            batch.append({"src": int(row["src"]), "dst": int(row["dst"])})
            if len(batch) >= batch_size:
                yield batch
                batch = []
        if batch:
            yield batch


if __name__ == "__main__":
    build()
