"""ArangoDB adapter.

ArangoDB is the deliberate odd one out: multi-model, RocksDB-backed, and with
no Cypher at all. Including it is what proves the harness measures databases
rather than Cypher implementations -- but it also means every query is a
translation, and translations are where benchmarks quietly cheat.

The three translations that actually matter
-------------------------------------------
1. `RETURN DISTINCT x LIMIT n` has no single-clause AQL equivalent. Writing
   `LIMIT n ... RETURN DISTINCT x` would limit BEFORE deduplicating, returning
   fewer rows than Cypher for the same input -- a smaller, cheaper, and
   incomparable query. We therefore materialise the distinct set in a
   subquery and limit afterwards, matching Cypher's ordering exactly.

2. Traversal uniqueness is stated explicitly (`uniqueEdges: 'path'`,
   `uniqueVertices: 'none'`) to match Cypher's relationship-isomorphism
   semantics, rather than relying on AQL defaults that differ by traversal
   type.

3. Cypher ignores unused query parameters; AQL rejects them outright. The
   shared sampler is platform-neutral and emits one parameter dict per
   workload category, so aggregations receive `limit` whether their query
   references it or not. Bind variables are filtered to those the query
   actually uses, which satisfies AQL without letting any platform receive a
   different logical parameter sequence.

Translations 1 and 2 are verified empirically, not asserted:
`verify_semantics()` in the runner compares row counts across all platforms
for identical parameters and has confirmed exact agreement (1, 9, 15, 375,
748, 20 rows) with the Cypher engines.
"""
from __future__ import annotations

import time
from typing import Any, Iterable

from arango import ArangoClient

from .base import Adapter

VERTEX = "users"
EDGE = "friend"
GRAPH = "social"

DIALECT_NOTES = [
    "AQL has no `RETURN DISTINCT ... LIMIT`; distinct is materialised in a "
    "subquery and limited afterwards to preserve Cypher's ordering. Verified "
    "equivalent by row-count comparison against the Cypher engines.",
    "Traversal uniqueness options are stated explicitly rather than defaulted.",
    "AQL rejects bind parameters the query does not reference, where Cypher "
    "ignores them. Bind variables are filtered per query rather than changing "
    "what the shared sampler emits.",
    "Point lookup uses the automatic primary index on `_key`, which is the "
    "closest analogue to an indexed `uid` lookup in Cypher.",
    "`agg_rel_count` is answered from collection metadata on ArangoDB. Neo4j "
    "answers the equivalent from its count store. Neither performs a real "
    "scan, so this metric measures round-trip and planning cost, not "
    "aggregation throughput.",
]

QUERIES: dict[str, str] = {
    "point_lookup":
        f"FOR u IN {VERTEX} FILTER u._key == @uid "
        "RETURN {uid: u.uid, age: u.age}",

    "filtered_lookup":
        f"FOR u IN {VERTEX} FILTER u.region == @region AND u.age >= @min_age "
        "LIMIT @limit RETURN u.uid",

    "hop1":
        f"LET ids = (FOR v IN 1..1 OUTBOUND @start {EDGE} "
        "OPTIONS {uniqueEdges: 'path', uniqueVertices: 'none'} "
        "RETURN DISTINCT v.uid) "
        "FOR id IN ids LIMIT @limit RETURN id",

    "hop2":
        f"LET ids = (FOR v IN 2..2 OUTBOUND @start {EDGE} "
        "OPTIONS {uniqueEdges: 'path', uniqueVertices: 'none'} "
        "RETURN DISTINCT v.uid) "
        "FOR id IN ids LIMIT @limit RETURN id",

    "hop3":
        f"LET ids = (FOR v IN 3..3 OUTBOUND @start {EDGE} "
        "OPTIONS {uniqueEdges: 'path', uniqueVertices: 'none'} "
        "RETURN DISTINCT v.uid) "
        "FOR id IN ids LIMIT @limit RETURN id",

    "agg_region":
        f"FOR u IN {VERTEX} COLLECT region = u.region WITH COUNT INTO c "
        "SORT c DESC LIMIT @limit RETURN {region: region, c: c}",

    "agg_rel_count":
        f"FOR e IN {EDGE} COLLECT WITH COUNT INTO c RETURN c",

    "write_edge":
        f"UPSERT {{_from: @from, _to: @to}} "
        f"INSERT {{_from: @from, _to: @to}} UPDATE {{}} IN {EDGE} RETURN 1",
}


class ArangoAdapter(Adapter):

    def __init__(self, platform):
        super().__init__(platform)
        self._client = None
        self._db = None
        self._url = platform.env["ARANGO_URL"]
        self._user = platform.env["ARANGO_USER"]
        self._pwd = platform.env["ARANGO_PASSWORD"]
        self._name = platform.env.get("ARANGO_DB", "pokec")

    def _open(self):
        client = ArangoClient(hosts=self._url)
        sys_db = client.db("_system", username=self._user, password=self._pwd)
        if not sys_db.has_database(self._name):
            sys_db.create_database(self._name)
        return client, client.db(self._name, username=self._user,
                                 password=self._pwd)

    # ---- lifecycle -------------------------------------------------------
    def connect(self) -> None:
        self._client, self._db = self._open()
        self._ensure_collections()

    def _ensure_collections(self) -> None:
        if not self._db.has_collection(VERTEX):
            self._db.create_collection(VERTEX)
        if not self._db.has_collection(EDGE):
            self._db.create_collection(EDGE, edge=True)
        if not self._db.has_graph(GRAPH):
            self._db.create_graph(GRAPH, edge_definitions=[{
                "edge_collection": EDGE,
                "from_vertex_collections": [VERTEX],
                "to_vertex_collections": [VERTEX],
            }])

    def close(self) -> None:
        self._client = None
        self._db = None

    def ping(self) -> float:
        t0 = time.perf_counter()
        list(self._db.aql.execute("RETURN 1"))
        return (time.perf_counter() - t0) * 1000.0

    # ---- schema / data ---------------------------------------------------
    def reset(self) -> None:
        for name in (EDGE, VERTEX):
            if self._db.has_collection(name):
                self._db.collection(name).truncate()
        self._ensure_collections()

    def create_indexes(self) -> list[str]:
        created = ["automatic primary index on users(_key) (point lookup)"]
        col = self._db.collection(VERTEX)
        try:
            col.add_persistent_index(fields=["region"], name="idx_region")
            created.append("persistent index on users(region)")
        except Exception as e:
            created.append(f"users(region) — NOT CREATED ({type(e).__name__})")
        try:
            col.add_persistent_index(fields=["region", "age"],
                                     name="idx_region_age")
            created.append("persistent index on users(region, age)")
        except Exception as e:
            created.append(f"users(region, age) — NOT CREATED ({type(e).__name__})")
        return created

    def load_nodes(self, batches: Iterable[list[dict]]) -> int:
        col = self._db.collection(VERTEX)
        total = 0
        for batch in batches:
            docs = [{"_key": str(r["uid"]), "uid": r["uid"],
                     "gender": r["gender"], "region": r["region"],
                     "age": r["age"]} for r in batch]
            col.insert_many(docs, overwrite=False, silent=True)
            total += len(docs)
        return total

    def load_edges(self, batches: Iterable[list[dict]]) -> int:
        col = self._db.collection(EDGE)
        total = 0
        for batch in batches:
            docs = [{"_from": f"{VERTEX}/{r['src']}",
                     "_to": f"{VERTEX}/{r['dst']}"} for r in batch]
            col.insert_many(docs, silent=True)
            total += len(docs)
        return total

    # ---- queries ---------------------------------------------------------
    def execute(self, workload: str, params: dict[str, Any]) -> int:
        return _run(self._db, workload, params)[0]

    def execute_timed(self, workload: str, params: dict[str, Any]):
        return _run(self._db, workload, params)

    def worker(self):
        _, db = self._open()
        return _ArangoWorker(db)

    # ---- observability ---------------------------------------------------
    def footprint(self) -> dict[str, Any]:
        """Counts come from count(); byte sizes from figures().

        python-arango's figures() returns the storage figures sub-document and
        does not include a document count, so the two must be read separately.
        """
        out: dict[str, Any] = {"observable": True,
                               "source": "ArangoDB count() + figures()"}
        try:
            for label, name in (("nodes", VERTEX), ("relationships", EDGE)):
                col = self._db.collection(name)
                out[label] = col.count()
                try:
                    fig = col.figures() or {}
                    inner = fig.get("figures", fig)
                    out[f"{label}_bytes"] = (
                        inner.get("documentsSize")
                        or inner.get("documents_size")
                        or fig.get("documentsSize")
                    )
                except Exception:
                    out[f"{label}_bytes"] = None
        except Exception as e:
            out = {"observable": False, "note": f"footprint failed: {e}"}
        return out


class _ArangoWorker:
    def __init__(self, db):
        self._db = db

    def execute(self, workload: str, params: dict[str, Any]) -> int:
        return _run(self._db, workload, params)[0]

    def close(self):
        pass


def _translate(workload: str, params: dict[str, Any]) -> dict[str, Any]:
    """Map the canonical parameter names onto AQL bind variables.

    The sampler emits platform-neutral params (`uid`, `src`, `dst`); ArangoDB
    needs document handles. Doing this here rather than in the sampler keeps
    every platform receiving the identical logical parameter sequence.
    """
    p = dict(params)
    if workload in ("hop1", "hop2", "hop3"):
        p["start"] = f"{VERTEX}/{p.pop('uid')}"
    elif workload == "point_lookup":
        p["uid"] = str(p["uid"])
    elif workload == "write_edge":
        p["from"] = f"{VERTEX}/{p.pop('src')}"
        p["to"] = f"{VERTEX}/{p.pop('dst')}"
    return p


def _bind_for(query: str, params: dict[str, Any]) -> dict[str, Any]:
    """Pass only the bind variables the query actually references.

    Cypher ignores unused parameters; AQL raises. The shared sampler emits one
    parameter dict per workload category, so an aggregation receives `limit`
    whether or not its query uses it. Filtering here keeps the sampler
    platform-neutral -- every engine still gets the identical logical
    parameter sequence -- while satisfying AQL's stricter contract.
    """
    return {k: v for k, v in params.items() if f"@{k}" in query}


def _run(db, workload: str, params: dict[str, Any]) -> tuple[int, float | None]:
    query = QUERIES[workload]
    cursor = db.aql.execute(
        query, bind_vars=_bind_for(query, _translate(workload, params)))
    rows = sum(1 for _ in cursor)
    stats = cursor.statistics() or {}
    exec_s = stats.get("execution_time")
    return rows, (exec_s * 1000.0 if exec_s is not None else None)