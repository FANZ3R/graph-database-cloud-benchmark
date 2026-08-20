"""Workload runner: warm-up, measurement, percentiles, concurrency sweep.

Three things this module does that a naive harness would not:

1. Verifies semantic equivalence before trusting timings. Every platform runs
   the same logical query with the same parameters; if row counts disagree,
   the translations are not equivalent and the comparison is void. Detecting
   that after publishing would be worse than not publishing.

2. Records server-reported execution time alongside wall-clock. With CognoDB
   243 ms away and the Docker platforms on localhost, wall-clock latency is
   dominated by geography. Server-side timings come from inside each engine
   and exclude the network entirely. Not every platform reports them --
   Memgraph and CognoDB do not -- so the RTT-adjusted figure is the fallback,
   and its agreement with server-reported times on platforms that expose both
   is what licenses using it on the platforms that do not.

3. Warms up before measuring, and reports the cold sample separately rather
   than discarding it. A cold first query is a real user experience, just a
   different one from steady state.

Concurrency errors are recorded by exception type and message, not merely
counted. A failure rate without a cause cannot be attributed to the database
rather than the client, and reporting it as a platform defect when it is a
driver threading issue would be a false finding.
"""
from __future__ import annotations

import json
import random
import statistics
import time
from concurrent.futures import ThreadPoolExecutor

from . import config, dataset, stats, workloads
from .adapters import build

MIXED_READ_WORKLOADS = ["point_lookup", "hop1", "hop2"]
MIXED_WRITE_RATIO = 0.1          # 10% writes / 90% reads
SWEEP_SECONDS = 15


# ---------------------------------------------------------------------------
# Semantic equivalence
# ---------------------------------------------------------------------------
def verify_semantics(adapters: dict, sampler) -> dict:
    """Assert every platform returns the same row count for the same query.

    This is the guardrail on the AQL translations in particular: if ArangoDB's
    subquery-then-limit produces a different cardinality than Cypher's
    RETURN DISTINCT ... LIMIT, the two are not the same question and no timing
    comparison between them is meaningful.
    """
    report = {}
    for wl in workloads.CATALOGUE:
        if workloads.CATALOGUE[wl].is_write:
            continue
        params_list = sampler.sequence(wl, 5)
        per_platform = {}
        for key, ad in adapters.items():
            counts = []
            for p in params_list:
                try:
                    counts.append(ad.execute(wl, p))
                except Exception as e:
                    counts.append(f"ERROR: {type(e).__name__}")
            per_platform[key] = counts
        distinct = {tuple(map(str, v)) for v in per_platform.values()}
        report[wl] = {
            "params": params_list,
            "row_counts": per_platform,
            "agree": len(distinct) == 1,
        }
        flag = "OK  " if report[wl]["agree"] else "MISMATCH"
        print(f"  {flag} {wl}")
        if not report[wl]["agree"]:
            for k, v in per_platform.items():
                print(f"         {k:<12} {v}")
    return report


# ---------------------------------------------------------------------------
# Single-client latency
# ---------------------------------------------------------------------------
def measure_workload(adapter, workload: str, sampler, baseline_ms: float) -> dict:
    n = config.READ_ITERATIONS
    warm = config.WARMUP_ITERATIONS
    params = sampler.sequence(workload, warm + n)

    # Cold sample: the very first execution, before any cache is populated.
    t0 = time.perf_counter()
    cold_rows, cold_server = adapter.execute_timed(workload, params[0])
    cold_ms = (time.perf_counter() - t0) * 1000.0

    for p in params[1:warm]:
        adapter.execute(workload, p)

    wall, server, rows = [], [], []
    for p in params[warm:]:
        t0 = time.perf_counter()
        r, s = adapter.execute_timed(workload, p)
        wall.append((time.perf_counter() - t0) * 1000.0)
        rows.append(r)
        if s is not None:
            server.append(s)

    out = {
        "workload": workload,
        "category": workloads.CATALOGUE[workload].category,
        "iterations": len(wall),
        "warmup_iterations": warm,
        "cold_ms": round(cold_ms, 3),
        "cold_rows": cold_rows,
        "cold_server_ms": round(cold_server, 3) if cold_server is not None else None,
        "rows_mean": round(statistics.fmean(rows), 1) if rows else 0,
        "rows_min": min(rows) if rows else 0,
        "rows_max": max(rows) if rows else 0,
        "wall_clock": stats.summarise(wall, baseline_ms),
    }
    if server:
        out["server_reported"] = stats.summarise(server)
        out["server_time_available"] = True
    else:
        out["server_time_available"] = False
        out["server_note"] = "platform does not report server-side execution time"
    return out


# ---------------------------------------------------------------------------
# Concurrency sweep
# ---------------------------------------------------------------------------
def concurrency_sweep(adapter, sampler) -> list[dict]:
    """Sustained mixed read/write throughput at increasing client counts.

    Each worker holds its own connection and loops for a fixed wall-clock
    window rather than a fixed iteration count, so a slow platform is measured
    on throughput rather than being allowed to run longer.

    Errors are grouped by exception type and message. A bare count cannot
    distinguish a database rejecting work from a client driver failing under
    threads, and those are very different findings.
    """
    results = []
    for clients in config.CONCURRENCY_LEVELS:
        pools = [adapter.worker() for _ in range(clients)]
        deadline = time.perf_counter() + SWEEP_SECONDS
        counters = [0] * clients
        errors = [0] * clients
        latencies: list[float] = []
        err_kinds: dict = {}

        def run(i: int):
            w = pools[i]
            rng = random.Random(f"{config.SAMPLE_SEED}:sweep:{i}")
            reads = sampler.sequence("hop1", 200)
            writes = sampler.sequence("write_edge", 200)
            local = []
            while time.perf_counter() < deadline:
                is_write = rng.random() < MIXED_WRITE_RATIO
                wl = "write_edge" if is_write else rng.choice(MIXED_READ_WORKLOADS)
                p = (rng.choice(writes) if is_write else rng.choice(reads))
                t0 = time.perf_counter()
                try:
                    w.execute(wl, p)
                    local.append((time.perf_counter() - t0) * 1000.0)
                    counters[i] += 1
                except Exception as e:
                    errors[i] += 1
                    kind = f"{type(e).__name__}: {str(e)[:120]}"
                    err_kinds[kind] = err_kinds.get(kind, 0) + 1
            return local

        t_start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=clients) as ex:
            for lat in ex.map(run, range(clients)):
                latencies.extend(lat)
        elapsed = time.perf_counter() - t_start

        for w in pools:
            try:
                w.close()
            except Exception:
                pass

        total = sum(counters)
        entry = {
            "clients": clients,
            "duration_seconds": round(elapsed, 2),
            "operations": total,
            "errors": sum(errors),
            "error_rate_pct": round(
                sum(errors) / max(1, total + sum(errors)) * 100, 2),
            "error_kinds": dict(sorted(err_kinds.items(),
                                       key=lambda kv: -kv[1])[:5]),
            "qps": round(total / elapsed, 1) if elapsed else 0,
            "read_write_mix": f"{int((1 - MIXED_WRITE_RATIO) * 100)}/"
                              f"{int(MIXED_WRITE_RATIO * 100)}",
            "latency": stats.summarise(latencies),
        }
        results.append(entry)
        print(f"    {clients:>3} clients  {entry['qps']:>9,.1f} qps  "
              f"p50={entry['latency'].get('p50_ms', 0):>8.2f}ms  "
              f"errors={entry['errors']} ({entry['error_rate_pct']}%)")
        for kind, count in entry["error_kinds"].items():
            print(f"         {count:>6,} x {kind}")
    return results


# ---------------------------------------------------------------------------
def run_platform(key: str, sampler, baselines: dict, sweep: bool = True) -> dict:
    p = config.get(key)
    adapter = build(p)
    adapter.connect()
    baseline = baselines.get(key, {}).get("rtt_p50_ms", 0.0)

    result = {
        "platform": key,
        "display_name": p.display_name,
        "deployment": p.deployment,
        "query_language": p.query_language,
        "specs": {"vcpu": p.vcpu, "ram": p.ram, "disk": p.disk,
                  "source": p.limits_source},
        "rtt_baseline_ms": baseline,
        "workloads": {},
    }
    try:
        for wl in workloads.CATALOGUE:
            if workloads.CATALOGUE[wl].is_write:
                continue
            r = measure_workload(adapter, wl, sampler, baseline)
            result["workloads"][wl] = r
            srv = r.get("server_reported", {}).get("p50_ms")
            srv_s = f"{srv:>8.2f}" if srv is not None else "     n/a"
            print(f"    {wl:<18} wall p50={r['wall_clock']['p50_ms']:>8.2f}ms  "
                  f"p95={r['wall_clock']['p95_ms']:>8.2f}ms  "
                  f"server p50={srv_s}ms  rows~{r['rows_mean']:.0f}")
        if sweep:
            print("  concurrency sweep:")
            result["concurrency"] = concurrency_sweep(adapter, sampler)
        result["footprint"] = adapter.footprint()
    finally:
        adapter.close()
    return result


def main(keys: list[str] | None = None, sweep: bool = True) -> None:
    from . import envinfo

    manifest = dataset.load_manifest()
    sampler = workloads.build_sampler()

    baseline_file = config.RAW_DIR / "rtt_baseline.json"
    baselines = {}
    if baseline_file.exists():
        baselines = json.loads(baseline_file.read_text()).get("baselines", {})

    targets = keys or [p.key for p in config.configured_platforms()]

    out = config.RAW_DIR / "workloads.json"
    payload = {"environment": envinfo.collect(), "manifest": manifest,
               "results": {}}
    if out.exists():
        try:
            prev = json.loads(out.read_text())
            payload["results"] = prev.get("results", {})
            if "semantic_check" in prev:
                payload["semantic_check"] = prev["semantic_check"]
        except Exception:
            pass

    # Semantic check across whatever is currently loaded and reachable.
    if len(targets) > 1:
        print("verifying semantic equivalence across platforms:")
        ads = {}
        try:
            for k in targets:
                a = build(config.get(k))
                a.connect()
                ads[k] = a
            payload["semantic_check"] = verify_semantics(ads, sampler)
        finally:
            for a in ads.values():
                a.close()
        print()

    for key in targets:
        p = config.get(key)
        if not p.configured:
            print(f"SKIP {p.display_name}")
            continue
        print(f"=== {p.display_name} ===", flush=True)
        try:
            payload["results"][key] = run_platform(key, sampler, baselines, sweep)
        except Exception as e:
            payload["results"][key] = {"platform": key, "failed": True,
                                       "error": f"{type(e).__name__}: {e}"}
            print(f"  FAILED  {type(e).__name__}: {e}")
        print()

    out.write_text(json.dumps(payload, indent=2))
    print(f"written -> {out}")


if __name__ == "__main__":
    import sys
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    main(args or None, sweep="--no-sweep" not in sys.argv)