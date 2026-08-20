"""Data loading and ingest measurement.

Load order is nodes -> indexes -> index-readiness poll -> edges. Building the
uid index before the edge phase is mandatory: each edge insert resolves two
uids, so without an index that phase degrades to a full scan per edge and the
measurement becomes a study of missing indexes rather than write throughput.
A failed index build aborts the load loudly instead of silently producing a
number that measures the wrong thing.

The readiness poll exists because index population is asynchronous on at least
one platform under test, with no queryable readiness signal. Sleeping a fixed
interval and hoping is not a measurement strategy; see `_await_index_ready`.

Batch size is identical on every platform, which is a deliberate fairness
choice with a known cost: each batch is one round trip, so a platform 243 ms
away pays 243 ms per batch that a localhost container does not. Tuning batch
size per platform would produce better absolute numbers for the remote
platforms but would no longer be the same experiment. The round-trip tax is
reported separately in the analysis instead.
"""
from __future__ import annotations

import csv
import json
import sys
import time

from . import config, dataset

BATCH_NODES = 5_000
BATCH_EDGES = 10_000


def _progress(batches, total: int, label: str):
    """Wrap a batch iterator with a live progress line.

    Silent multi-minute loads make it impossible to distinguish slow from
    hung -- a distinction that matters, because a missing index turns a
    30-second load into an effectively infinite one.
    """
    done = 0
    t0 = time.perf_counter()
    for batch in batches:
        yield batch
        done += len(batch)
        el = time.perf_counter() - t0
        rate = done / el if el else 0
        print(f"\r    {label}: {done:>7,}/{total:,}  "
              f"{done / total * 100:5.1f}%  {rate:>8,.0f}/s", end="", flush=True)
    print()


def _await_index_ready(adapter, probe_uid: int, timeout: float = 120.0) -> float:
    """Block until an indexed lookup for a known uid actually resolves.

    CognoDB v0.9.11 supports neither `db.awaitIndexes()` nor a populated
    `state`/`populationPercent` in `SHOW INDEXES`, so index readiness cannot be
    queried directly. If the planner selects a not-yet-populated index, the
    lookup returns zero rows instead of falling back to a label scan -- which
    makes the subsequent edge load silently create nothing, with no error.

    Polling for an observable effect is the only reliable signal available.
    Returns seconds waited, recorded separately from load timings so it never
    inflates or deflates a throughput figure.
    """
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout:
        try:
            if adapter.execute("point_lookup", {"uid": probe_uid}) > 0:
                return time.perf_counter() - t0
        except Exception:
            pass
        time.sleep(1.0)
    raise RuntimeError(
        f"Indexed lookup for uid={probe_uid} still returns no rows after "
        f"{timeout:.0f}s. The edge load would silently create nothing."
    )


def load_platform(adapter, manifest: dict, reset: bool = True) -> dict:
    result = {
        "platform": adapter.key,
        "batch_size_nodes": BATCH_NODES,
        "batch_size_edges": BATCH_EDGES,
        "reset_performed": reset,
    }

    if reset:
        t0 = time.perf_counter()
        adapter.reset()
        result["reset_seconds"] = round(time.perf_counter() - t0, 3)

    # --- nodes ------------------------------------------------------------
    t0 = time.perf_counter()
    n_nodes = adapter.load_nodes(
        _progress(dataset.iter_node_batches(BATCH_NODES),
                  manifest["nodes"], "nodes"))
    node_s = time.perf_counter() - t0

    # --- indexes ----------------------------------------------------------
    t0 = time.perf_counter()
    indexes = adapter.create_indexes()
    index_s = time.perf_counter() - t0
    for line in indexes:
        print(f"    index: {line}")

    broken = [i for i in indexes if "NOT CREATED" in i]
    if broken:
        raise RuntimeError(
            "Index creation failed — the edge load would degrade to a full "
            "scan per edge and the ingest measurement would be meaningless. "
            f"Failures: {broken}"
        )

    # --- wait for the index to actually resolve ---------------------------
    with open(dataset.NODES_CSV, newline="", encoding="utf-8") as f:
        probe_uid = int(next(csv.DictReader(f))["uid"])
    waited = _await_index_ready(adapter, probe_uid)
    result["index_ready_wait_seconds"] = round(waited, 2)
    print(f"    index ready after {waited:.1f}s (polled, not assumed)")

    # --- edges ------------------------------------------------------------
    t0 = time.perf_counter()
    n_edges = adapter.load_edges(
        _progress(dataset.iter_edge_batches(BATCH_EDGES),
                  manifest["relationships"], "edges"))
    edge_s = time.perf_counter() - t0

    result.update({
        "nodes_loaded": n_nodes,
        "relationships_loaded": n_edges,
        "node_load_seconds": round(node_s, 3),
        "index_build_seconds": round(index_s, 3),
        "edge_load_seconds": round(edge_s, 3),
        "total_load_seconds": round(node_s + index_s + edge_s, 3),
        "nodes_per_second": round(n_nodes / node_s, 1) if node_s else None,
        "relationships_per_second": round(n_edges / edge_s, 1) if edge_s else None,
        "indexes": indexes,
        "footprint": adapter.footprint(),
    })

    # --- verify the load actually landed ----------------------------------
    fp = result["footprint"]
    expected_n, expected_e = manifest["nodes"], manifest["relationships"]
    got_n, got_e = fp.get("nodes"), fp.get("relationships")
    result["load_verified"] = (got_n == expected_n and got_e == expected_e)
    if not result["load_verified"]:
        result["load_discrepancy"] = (
            f"expected {expected_n} nodes / {expected_e} rels, "
            f"platform reports {got_n} / {got_e}"
        )
    return result


def main(keys: list[str] | None = None, reset: bool = True) -> None:
    from .adapters import build

    manifest = dataset.load_manifest()
    print(f"dataset: {manifest['nodes']:,} nodes / "
          f"{manifest['relationships']:,} relationships"
          f"{'' if reset else '   [--no-reset: loading onto existing nodes]'}\n")

    targets = keys or [p.key for p in config.configured_platforms()]

    out = config.RAW_DIR / "ingest.json"
    existing = {}
    if out.exists():
        try:
            existing = json.loads(out.read_text()).get("results", {})
        except Exception:
            existing = {}

    for key in targets:
        p = config.get(key)
        if not p.configured:
            print(f"SKIP {p.display_name} (unset: {', '.join(p.missing_env)})")
            continue
        print(f"=== {p.display_name} ===", flush=True)
        adapter = build(p)
        try:
            adapter.connect()
            r = load_platform(adapter, manifest, reset=reset)
            existing[key] = r
            print(f"  nodes  {r['nodes_loaded']:>7,} in {r['node_load_seconds']:>7.1f}s "
                  f"({r['nodes_per_second']:,.0f}/s)")
            print(f"  index  {r['index_build_seconds']:>7.1f}s build, "
                  f"{r['index_ready_wait_seconds']:.1f}s to become queryable")
            print(f"  edges  {r['relationships_loaded']:>7,} in "
                  f"{r['edge_load_seconds']:>7.1f}s "
                  f"({r['relationships_per_second']:,.0f}/s)")
            print(f"  total  {r['total_load_seconds']:>7.1f}s   "
                  f"verified={r['load_verified']}")
            if not r["load_verified"]:
                print(f"  WARNING: {r['load_discrepancy']}")
        except Exception as e:
            existing[key] = {"platform": key, "failed": True,
                             "error": f"{type(e).__name__}: {e}"}
            print(f"  FAILED  {type(e).__name__}: {e}")
        finally:
            adapter.close()
        print()

    # Results accumulate across invocations so platforms can be loaded one at
    # a time without discarding earlier runs.
    out.write_text(json.dumps(
        {"manifest": manifest, "results": existing}, indent=2))
    print(f"written -> {out}")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    main(args or None, reset="--no-reset" not in sys.argv)