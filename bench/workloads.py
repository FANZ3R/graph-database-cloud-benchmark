"""Workload catalogue.

This module defines what each benchmark measures in *logical* terms. Each
adapter translates these into its own dialect, but the semantics defined here
are binding: the harness cross-checks that every platform returns the same
result cardinality for the same parameters before it trusts any timing.

Why fixed-depth chained patterns rather than variable-length paths
-----------------------------------------------------------------
Cypher's `-[:FRIEND*2]->` and ArangoDB's `FOR v IN 2..2 OUTBOUND` look
equivalent but differ in their default uniqueness semantics -- Cypher enforces
relationship-isomorphism within a path, while AQL's behaviour is governed by
`uniqueEdges`/`uniqueVertices` options that default differently. Rather than
hope they line up, every traversal is written as an explicit chain with
uniqueness options stated outright, and `verify_semantics()` proves the
cardinalities match before any number is published.

Why every traversal carries a LIMIT
-----------------------------------
CognoDB's free tier enforces a hard server-side cap of 50,000 result rows. A
3-hop expansion from a high-degree node in a social graph blows through that
easily. If CognoDB errored while other platforms happily streamed 800k rows,
we would be comparing a failed query against a successful one. So the same
LIMIT is applied everywhere -- it is a fairness device, not a performance
optimisation, and it is disclosed in the README.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from . import config


@dataclass(frozen=True)
class Workload:
    name: str
    category: str          # matches the assignment's metric table
    description: str
    is_write: bool = False


CATALOGUE: dict[str, Workload] = {
    "point_lookup": Workload(
        "point_lookup", "Lookups",
        "Fetch a single User by its indexed `uid`. Exercises the primary index "
        "and nothing else.",
    ),
    "filtered_lookup": Workload(
        "filtered_lookup", "Lookups",
        "Fetch Users matching an indexed `region` and a range predicate on "
        "`age`, capped by LIMIT. Exercises a secondary/composite index.",
    ),
    "hop1": Workload(
        "hop1", "Traversals",
        "Distinct uids of direct FRIEND targets of a start node.",
    ),
    "hop2": Workload(
        "hop2", "Traversals",
        "Distinct uids reachable by exactly two outbound FRIEND edges.",
    ),
    "hop3": Workload(
        "hop3", "Traversals",
        "Distinct uids reachable by exactly three outbound FRIEND edges.",
    ),
    "agg_region": Workload(
        "agg_region", "Aggregations",
        "Group all Users by `region` and count, ordered by count descending, "
        "top 20. A full label scan plus grouping.",
    ),
    "agg_rel_count": Workload(
        "agg_rel_count", "Aggregations",
        "Total count of FRIEND relationships. A full relationship-type scan.",
    ),
    "write_edge": Workload(
        "write_edge", "Mixed workload",
        "Idempotently create one FRIEND edge between two existing Users. Used "
        "only in the mixed read/write concurrency sweep.",
        is_write=True,
    ),
}

READ_WORKLOADS = [w for w in CATALOGUE.values() if not w.is_write]
TRAVERSALS = ["hop1", "hop2", "hop3"]


class ParamSampler:
    """Generates query parameters deterministically.

    Every platform receives the *identical* parameter sequence for a given
    seed, so no platform gets an easier set of start nodes than another. Start
    nodes are drawn from nodes with at least one outbound edge -- sampling
    uniformly from all nodes would pick mostly leaves, making multi-hop
    traversals trivially cheap and the benchmark meaningless.
    """

    def __init__(self, node_uids: list[int], source_uids: list[int],
                 regions: list[str], seed: int | None = None):
        self.node_uids = node_uids
        self.source_uids = source_uids or node_uids
        self.regions = regions
        self.seed = seed if seed is not None else config.SAMPLE_SEED

    def sequence(self, workload: str, n: int) -> list[dict]:
        rng = random.Random(f"{self.seed}:{workload}")
        limit = config.TRAVERSAL_LIMIT
        out = []
        for _ in range(n):
            if workload == "point_lookup":
                out.append({"uid": rng.choice(self.node_uids)})
            elif workload == "filtered_lookup":
                out.append({
                    "region": rng.choice(self.regions),
                    "min_age": rng.choice([18, 21, 25, 30, 35, 40]),
                    "limit": limit,
                })
            elif workload in TRAVERSALS:
                out.append({"uid": rng.choice(self.source_uids), "limit": limit})
            elif workload == "write_edge":
                out.append({
                    "src": rng.choice(self.source_uids),
                    "dst": rng.choice(self.node_uids),
                })
            else:                                    # aggregations take no params
                out.append({"limit": 20})
        return out


def build_sampler(seed: int | None = None) -> ParamSampler:
    """Read the generated CSVs to derive the parameter pools."""
    import csv
    from . import dataset

    uids, regions = [], set()
    with open(dataset.NODES_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            uids.append(int(row["uid"]))
            regions.add(row["region"])

    sources = set()
    with open(dataset.EDGES_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            sources.add(int(row["src"]))

    return ParamSampler(uids, sorted(sources), sorted(regions), seed)