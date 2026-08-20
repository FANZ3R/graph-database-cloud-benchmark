"""Bolt/Cypher adapter -- serves CognoDB Cloud, Neo4j AuraDB and Memgraph.

All three speak the Bolt protocol and accept the official Neo4j Python driver,
so they share one implementation and, importantly, one set of *query* strings.
Any latency difference between them is therefore attributable to the engine
and the network, never to a query someone wrote more cleverly for one target.

DDL is a different story. Memgraph accepts Cypher queries but not Neo4j's
index grammar, so index creation and introspection are dialect-specific. That
split is deliberate and narrow: the measured queries stay byte-identical, only
the schema setup diverges.

Load ordering matters
---------------------
Nodes are created first, then the uid index, then edges. Creating the index
before the edge load is not optional: each edge insert resolves two uids, so
without an index that phase degrades to a full label scan per edge -- on this
dataset roughly 4 billion comparisons at 0.5 vCPU -- and the ingest benchmark
would measure the absence of an index rather than write throughput. Building
it after the node load rather than before is the cheaper direction, since bulk
index construction beats incremental maintenance.

Writes are verified, never assumed
----------------------------------
A Cypher CREATE guarded by a MATCH that finds nothing succeeds and creates
nothing: no error, no warning. Counting submitted rows would therefore report
a complete load, and a throughput number, for work that never happened. Every
batch is checked against the server's own counters, and the edge phase is
re-read in a fresh session afterwards -- a load the server acknowledges but
which is not subsequently visible is a different failure from one that never
happened, and the two must not be conflated.
"""
from __future__ import annotations

import time
from typing import Any, Iterable

from neo4j import GraphDatabase

from .base import Adapter

# ---------------------------------------------------------------------------
# Query strings. Identical text for every Bolt platform -- deliberately.
# ---------------------------------------------------------------------------
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
        "MATCH (:User)-[r:FRIEND]->(:User) RETURN count(r) AS c",

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


class BoltAdapter(Adapter):

    def __init__(self, platform):
        super().__init__(platform)
        self._driver = None
        self._uri = _pick(platform.env, "_URI")
        self._user = _pick(platform.env, "_USER")
        self._pwd = _pick(platform.env, "_PASSWORD")
        # Memgraph's default Docker image runs without auth; passing a
        # (None, None) tuple upsets the driver, so send auth=None instead.
        self._auth = (self._user, self._pwd) if self._user else None
        # Memgraph speaks Bolt and Cypher but NOT Neo4j's DDL grammar.
        self._dialect = "memgraph" if platform.key == "memgraph" else "neo4j"

    # ---- lifecycle -------------------------------------------------------
    def connect(self) -> None:
        self._driver = GraphDatabase.driver(
            self._uri, auth=self._auth,
            connection_timeout=30, max_connection_pool_size=64,
            connection_acquisition_timeout=120,
        )
        self._driver.verify_connectivity()

    def close(self) -> None:
        if self._driver:
            self._driver.close()
            self._driver = None

    def ping(self) -> float:
        t0 = time.perf_counter()
        with self._driver.session() as s:
            s.run("RETURN 1").consume()
        return (time.perf_counter() - t0) * 1000.0

    # ---- schema / data ---------------------------------------------------
    def reset(self) -> None:
        with self._driver.session() as s:
            # Chunked delete: a single DETACH DELETE over 150k relationships
            # exceeds the transaction memory budget on a 512 MB instance.
            while True:
                rec = s.run(
                    "MATCH (n) WITH n LIMIT 10000 DETACH DELETE n "
                    "RETURN count(n) AS c"
                ).single()
                if not rec or rec["c"] == 0:
                    break
            for stmt in self._drop_index_statements(s):
                try:
                    s.run(stmt).consume()
                except Exception:
                    pass

    def _drop_index_statements(self, session) -> list[str]:
        if self._dialect == "memgraph":
            return ["DROP INDEX ON :User(uid)", "DROP INDEX ON :User(region)"]
        try:
            rows = session.run("SHOW INDEXES YIELD name RETURN name").data()
            return [f"DROP INDEX {r['name']} IF EXISTS" for r in rows
                    if not r["name"].startswith("__")]
        except Exception:
            return []

    def create_indexes(self) -> list[str]:
        """Create benchmark indexes using each engine's own DDL grammar.

        Memgraph 2.18 supports only single-property label indexes -- no
        composite. The filtered_lookup workload therefore runs against a
        single-property index on `region` there, versus a composite
        (region, age) index elsewhere. That is a genuine capability
        difference, reported as such rather than equalised by crippling the
        other platforms.
        """
        if self._dialect == "memgraph":
            stmts = [
                ("CREATE INDEX ON :User(uid)",
                 "label-property index on :User(uid)"),
                ("CREATE INDEX ON :User(region)",
                 "label-property index on :User(region)"),
            ]
        else:
            stmts = [
                ("CREATE INDEX user_uid IF NOT EXISTS FOR (u:User) ON (u.uid)",
                 "range index on :User(uid)"),
                ("CREATE INDEX user_region IF NOT EXISTS FOR (u:User) ON (u.region)",
                 "range index on :User(region)"),
                ("CREATE INDEX user_region_age IF NOT EXISTS "
                 "FOR (u:User) ON (u.region, u.age)",
                 "composite index on :User(region, age)"),
            ]

        created = []
        with self._driver.session() as s:
            for stmt, desc in stmts:
                try:
                    s.run(stmt).consume()
                    created.append(desc)
                except Exception as e:
                    msg = str(e).lower()
                    if "already" in msg or "exist" in msg:
                        created.append(f"{desc} (already present)")
                    else:
                        created.append(
                            f"{desc} — NOT CREATED ({type(e).__name__}: {e})")
            try:
                s.run("CALL db.awaitIndexes(300)").consume()
            except Exception:
                time.sleep(3)     # Memgraph builds indexes synchronously

        if self._dialect == "memgraph":
            created.append(
                "NOTE: Memgraph 2.18 has no composite index support; "
                "filtered_lookup uses the single-property region index.")
        return created

    def load_nodes(self, batches: Iterable[list[dict]]) -> int:
        return self._load(NODE_LOAD, batches, "nodes")

    def load_edges(self, batches: Iterable[list[dict]]) -> int:
        n = self._load(EDGE_LOAD, batches, "relationships")
        # Read back in a fresh session. A load the server reports succeeding
        # but which is not subsequently visible is a different failure from
        # one that never happened, and the two must not be confused.
        with self._driver.session() as s:
            visible = s.run(
                "MATCH (:User)-[r:FRIEND]->(:User) RETURN count(r) AS c"
            ).single()["c"]
        if visible != n:
            raise RuntimeError(
                f"Server reported creating {n:,} relationships but only "
                f"{visible:,} are visible afterwards. Aborting rather than "
                f"reporting throughput for writes that did not persist."
            )
        return n

    def _load(self, query: str, batches: Iterable[list[dict]], kind: str) -> int:
        """Return the count the SERVER reports creating, not rows submitted.

        Each batch runs in its own short-lived session. Holding one session
        open across an entire multi-second remote load is a plausible failure
        mode on a managed free tier, and short sessions are also what the
        driver documentation recommends for unit-of-work isolation.
        """
        total = 0
        for i, batch in enumerate(batches):
            with self._driver.session() as s:
                c = s.run(query, rows=batch).consume().counters
                made = (c.relationships_created if kind == "relationships"
                        else c.nodes_created)
                total += made
                if made != len(batch):
                    raise RuntimeError(
                        f"Batch {i}: submitted {len(batch):,} rows, server "
                        f"reports creating {made:,} {kind}. Load aborted."
                    )
        return total

    # ---- queries ---------------------------------------------------------
    def execute(self, workload: str, params: dict[str, Any]) -> int:
        with self._driver.session() as s:
            return _drain(s, workload, params)[0]

    def execute_timed(self, workload: str, params: dict[str, Any]):
        with self._driver.session() as s:
            return _drain(s, workload, params)

    def worker(self):
        return _BoltWorker(self._driver)

    # ---- observability ---------------------------------------------------
    def footprint(self) -> dict[str, Any]:
        out: dict[str, Any] = {"observable": True, "source": "Bolt introspection"}
        with self._driver.session() as s:
            for label, q in (
                ("nodes", "MATCH (n:User) RETURN count(n) AS c"),
                ("relationships",
                 "MATCH (:User)-[r:FRIEND]->(:User) RETURN count(r) AS c"),
            ):
                try:
                    out[label] = s.run(q).single()["c"]
                except Exception:
                    out[label] = None
        out["note"] = ("On-disk store size is not exposed over Bolt on managed "
                       "tiers; read it from the vendor console instead.")
        return out


class _BoltWorker:
    """Independent session for one concurrent client."""

    def __init__(self, driver):
        self._session = driver.session()

    def execute(self, workload: str, params: dict[str, Any]) -> int:
        return _drain(self._session, workload, params)[0]

    def close(self):
        self._session.close()


def _drain(session, workload: str, params: dict[str, Any]) -> tuple[int, float | None]:
    """Run a workload, fully consume it, and report server-side execution time.

    Consuming matters: the Bolt driver streams lazily, so returning early would
    time the round-trip to the first record rather than the query.

    `result_available_after` + `result_consumed_after` are measured inside the
    database and exclude network transit. With CognoDB 243 ms away and Memgraph
    on localhost, wall-clock latency is dominated by geography; these two
    fields are what make the engines comparable at all.
    """
    result = session.run(QUERIES[workload], **params)
    rows = sum(1 for _ in result)
    summary = result.consume()
    avail = summary.result_available_after
    consumed = summary.result_consumed_after
    server_ms = None
    if avail is not None and consumed is not None:
        server_ms = float(avail) + float(consumed)
    return rows, server_ms


def _pick(env: dict, suffix: str) -> str:
    for k, v in env.items():
        if k.endswith(suffix):
            return v
    return ""