"""FalkorDB adapter.

FalkorDB is a Redis module that stores graphs as sparse adjacency matrices and
evaluates traversals with GraphBLAS linear algebra. It speaks a Cypher subset
over the Redis protocol rather than Bolt, so it needs its own client -- but the
query text is kept as close to the Bolt version as the dialect allows, and
every divergence is recorded in DIALECT_NOTES for the README.
"""
from __future__ import annotations

import time
from typing import Any, Iterable

from falkordb import FalkorDB

from .base import Adapter

GRAPH_NAME = "bench"

# Divergences from the Bolt query set, reported in the README rather than
# quietly smoothed over.
DIALECT_NOTES = [
    "FalkorDB accepts a composite CREATE INDEX statement without error but "
    "does not materialise a true composite index -- db.indexes() shows the "
    "properties registered individually. The actual index set is read back "
    "after creation and reported, rather than trusting the statement.",
    "Index introspection uses `CALL db.indexes()` rather than `SHOW INDEXES`.",
    "Reset drops the whole graph key rather than issuing a chunked DETACH "
    "DELETE, because the graph is a single Redis key. This is faster than the "
    "Bolt path and is NOT included in any reported timing.",
]

QUERIES: dict[str, str] = {
    "point_lookup":
        "MATCH (u:User {uid: $uid}) RETURN u.uid AS uid, u.age AS age",

    "filtered_lookup":
        "MATCH (u:User) WHERE u.region = $region AND u.age >= $min_age "
        "RETURN u.uid AS uid LIMIT $limit",

    "hop1":
        "MATCH (:User {uid: $uid})-[:FRIEND]->(n:User) "
        "RETURN DISTINCT n.uid AS uid LIMIT $limit",

    "hop2":
        "MATCH (:User {uid: $uid})-[:FRIEND]->(:User)-[:FRIEND]->(n:User) "
        "RETURN DISTINCT n.uid AS uid LIMIT $limit",

    "hop3":
        "MATCH (:User {uid: $uid})-[:FRIEND]->(:User)-[:FRIEND]->(:User)"
        "-[:FRIEND]->(n:User) "
        "RETURN DISTINCT n.uid AS uid LIMIT $limit",

    "agg_region":
        "MATCH (u:User) RETURN u.region AS region, count(*) AS c "
        "ORDER BY c DESC LIMIT $limit",

    "agg_rel_count":
        "MATCH ()-[r:FRIEND]->() RETURN count(r) AS c",

    "write_edge":
        "MATCH (a:User {uid: $src}), (b:User {uid: $dst}) "
        "MERGE (a)-[:FRIEND]->(b) RETURN 1 AS ok",
}

NODE_LOAD = (
    "UNWIND $rows AS r "
    "CREATE (u:User {uid: r.uid, gender: r.gender, region: r.region, age: r.age})"
)

EDGE_LOAD = (
    "UNWIND $rows AS r "
    "MATCH (a:User {uid: r.src}) "
    "MATCH (b:User {uid: r.dst}) "
    "CREATE (a)-[:FRIEND]->(b)"
)


class FalkorAdapter(Adapter):

    def __init__(self, platform):
        super().__init__(platform)
        self._db = None
        self._graph = None
        self._host = platform.env.get("FALKORDB_HOST", "localhost")
        self._port = int(platform.env.get("FALKORDB_PORT", 6379))
        self._pwd = platform.env.get("FALKORDB_PASSWORD") or None

    def _connect_raw(self):
        return FalkorDB(host=self._host, port=self._port, password=self._pwd)

    # ---- lifecycle -------------------------------------------------------
    def connect(self) -> None:
        self._db = self._connect_raw()
        self._graph = self._db.select_graph(GRAPH_NAME)
        self._graph.query("RETURN 1")

    def close(self) -> None:
        self._db = None
        self._graph = None

    def ping(self) -> float:
        t0 = time.perf_counter()
        self._graph.query("RETURN 1")
        return (time.perf_counter() - t0) * 1000.0

    # ---- schema / data ---------------------------------------------------
    def reset(self) -> None:
        try:
            self._graph.delete()
        except Exception:
            pass                       # graph key did not exist
        self._graph = self._db.select_graph(GRAPH_NAME)

    def create_indexes(self) -> list[str]:
        """Create indexes, then read back what actually exists.

        FalkorDB accepts a composite CREATE INDEX statement without error but
        does not materialise a true composite index -- it registers the
        properties individually. Trusting the statement's success would put a
        composite index in the README that the engine never built, so the
        actual index set is read back from db.indexes() and reported instead.
        """
        for stmt in ("CREATE INDEX FOR (u:User) ON (u.uid)",
                     "CREATE INDEX FOR (u:User) ON (u.region)",
                     "CREATE INDEX FOR (u:User) ON (u.region, u.age)"):
            try:
                self._graph.query(stmt)
            except Exception:
                pass          # already-exists is expected; truth comes from readback

        created = []
        try:
            res = self._graph.query("CALL db.indexes()")
            for row in res.result_set:
                label, props, types = row[0], row[1], row[2]
                created.append(f"{label}{props} types={types}")
        except Exception as e:
            created.append(f"index readback FAILED ({type(e).__name__}: {e})")

        if not any("age" in c for c in created):
            created.append(
                "NOTE: no composite (region, age) index materialised; "
                "filtered_lookup's age predicate is evaluated post-scan.")
        return created

    def load_nodes(self, batches: Iterable[list[dict]]) -> int:
        return self._load(NODE_LOAD, batches)

    def load_edges(self, batches: Iterable[list[dict]]) -> int:
        return self._load(EDGE_LOAD, batches)

    def _load(self, query: str, batches: Iterable[list[dict]]) -> int:
        total = 0
        for batch in batches:
            self._graph.query(query, {"rows": batch})
            total += len(batch)
        return total

    # ---- queries ---------------------------------------------------------
    def execute(self, workload: str, params: dict[str, Any]) -> int:
        return _run(self._graph, workload, params)[0]

    def execute_timed(self, workload: str, params: dict[str, Any]):
        """Returns (row_count, server_reported_ms)."""
        return _run(self._graph, workload, params)

    def worker(self):
        db = self._connect_raw()
        return _FalkorWorker(db.select_graph(GRAPH_NAME))

    # ---- observability ---------------------------------------------------
    def footprint(self) -> dict[str, Any]:
        out: dict[str, Any] = {"observable": True, "source": "Redis INFO MEMORY"}
        try:
            conn = self._db.connection
            info = conn.info("memory")
            out["used_memory_bytes"] = info.get("used_memory")
            out["used_memory_human"] = info.get("used_memory_human")
            out["maxmemory_bytes"] = info.get("maxmemory")
        except Exception as e:
            out = {"observable": False, "note": f"INFO MEMORY failed: {e}"}
        for label, q in (("nodes", "MATCH (n:User) RETURN count(n) AS c"),
                         ("relationships",
                          "MATCH ()-[r:FRIEND]->() RETURN count(r) AS c")):
            try:
                out[label] = self._graph.query(q).result_set[0][0]
            except Exception:
                out[label] = None
        return out


class _FalkorWorker:
    def __init__(self, graph):
        self._graph = graph

    def execute(self, workload: str, params: dict[str, Any]) -> int:
        return _run(self._graph, workload, params)[0]

    def close(self):
        pass


def _run(graph, workload: str, params: dict[str, Any]) -> tuple[int, float | None]:
    """Execute and return (row_count, server_reported_ms).

    FalkorDB returns its own internal execution time, which excludes network
    entirely -- the cleanest engine-cost signal available on this platform.
    """
    res = graph.query(QUERIES[workload], dict(params))
    rows = len(res.result_set) if res.result_set is not None else 0
    return rows, getattr(res, "run_time_ms", None)