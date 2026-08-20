"""Connectivity smoke test and network-RTT baseline.

Run this before anything else:  python -m bench.smoke

Two jobs:

1. Prove every configured platform is reachable and the credentials work.

2. Measure the *network* round-trip to each platform, separately from query
   execution. This matters enormously here: the CognoDB instance is in
   us-east4 (Northern Virginia) while the client is in India, so a ~250 ms
   RTT will dominate any query that executes in single-digit milliseconds.
   Comparing a remote managed service against a localhost container without
   isolating that term would be measuring geography, not databases.

   The baseline is the p50 of a trivially cheap query (RETURN 1). Every
   latency in the final report is presented twice: raw wall-clock, and
   RTT-adjusted (raw - baseline).
"""
from __future__ import annotations

import json
import statistics
import sys
import time

from . import config, envinfo

PING_ITERATIONS = 50


def _ms(fn) -> float:
    t0 = time.perf_counter()
    fn()
    return (time.perf_counter() - t0) * 1000.0


def probe_bolt(p) -> dict:
    from neo4j import GraphDatabase

    uri = p.env.get(f"{p.key.upper()}_URI") or _first(p.env, "_URI")
    user = _first(p.env, "_USER")
    pwd = _first(p.env, "_PASSWORD")
    auth = (user, pwd) if user else None

    driver = GraphDatabase.driver(uri, auth=auth, connection_timeout=30)
    try:
        driver.verify_connectivity()
        with driver.session() as s:
            for _ in range(5):                      # discard connection warm-up
                s.run("RETURN 1").consume()
            samples = [_ms(lambda: s.run("RETURN 1").consume())
                       for _ in range(PING_ITERATIONS)]
            try:
                ver = s.run(
                    "CALL dbms.components() YIELD name, versions "
                    "RETURN name + ' ' + versions[0] AS v"
                ).single()["v"]
            except Exception:
                ver = "version query unsupported"
        return {"ok": True, "server": ver, "samples": samples}
    finally:
        driver.close()


def probe_falkor(p) -> dict:
    from falkordb import FalkorDB

    db = FalkorDB(
        host=p.env.get("FALKORDB_HOST", "localhost"),
        port=int(p.env.get("FALKORDB_PORT", 6379)),
        password=p.env.get("FALKORDB_PASSWORD") or None,
    )
    g = db.select_graph("smoke")
    for _ in range(5):
        g.query("RETURN 1")
    samples = [_ms(lambda: g.query("RETURN 1")) for _ in range(PING_ITERATIONS)]
    return {"ok": True, "server": "FalkorDB", "samples": samples}


def probe_arango(p) -> dict:
    from arango import ArangoClient

    client = ArangoClient(hosts=p.env["ARANGO_URL"])
    sys_db = client.db("_system", username=p.env["ARANGO_USER"],
                       password=p.env["ARANGO_PASSWORD"])
    ver = sys_db.version()
    for _ in range(5):
        list(sys_db.aql.execute("RETURN 1"))
    samples = [_ms(lambda: list(sys_db.aql.execute("RETURN 1")))
               for _ in range(PING_ITERATIONS)]
    return {"ok": True, "server": f"ArangoDB {ver}", "samples": samples}


def _first(env: dict, suffix: str) -> str:
    for k, v in env.items():
        if k.endswith(suffix):
            return v
    return ""


PROBES = {"bolt": probe_bolt, "falkor": probe_falkor, "arango": probe_arango}


def main() -> int:
    env = envinfo.collect()
    print(f"client fingerprint {env['fingerprint']}  "
          f"({env['os']}, {env['cpu']}, {env['logical_cores']} cores)\n")

    baselines, failures = {}, []
    for p in config.PLATFORMS.values():
        if not p.configured:
            print(f"  SKIP  {p.display_name:<32} (env vars not set)")
            continue
        try:
            r = PROBES[p.adapter](p)
            s = sorted(r["samples"])
            p50, p95 = s[len(s) // 2], s[int(len(s) * 0.95)]
            baselines[p.key] = {
                "rtt_p50_ms": round(p50, 3),
                "rtt_p95_ms": round(p95, 3),
                "rtt_stdev_ms": round(statistics.stdev(s), 3),
                "deployment": p.deployment,
                "server": r["server"],
            }
            print(f"  OK    {p.display_name:<32} RTT p50={p50:7.2f}ms "
                  f"p95={p95:7.2f}ms  [{r['server']}]")
        except Exception as e:
            failures.append((p.display_name, str(e)))
            print(f"  FAIL  {p.display_name:<32} {type(e).__name__}: {e}")

    out = config.RAW_DIR / "rtt_baseline.json"
    out.write_text(json.dumps(
        {"environment": env, "baselines": baselines}, indent=2))
    print(f"\nbaseline written -> {out}")

    if baselines:
        remote = [b for b in baselines.values() if b["deployment"] == "managed"]
        local = [b for b in baselines.values() if b["deployment"] == "docker"]
        if remote and local:
            gap = min(r["rtt_p50_ms"] for r in remote) - max(
                l["rtt_p50_ms"] for l in local)
            print(f"\nremote-vs-local RTT gap: {gap:.1f} ms — every managed-platform "
                  f"latency carries this as a floor.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
