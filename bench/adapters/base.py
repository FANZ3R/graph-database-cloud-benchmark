"""Adapter interface.

Every platform implements the same *logical* operations. Query syntax differs
(Cypher vs AQL) but the semantics of each workload must be identical -- that
equivalence is asserted by `bench/workloads.py` and spot-checked by
`verify_semantics()`, which compares result cardinalities across platforms.
"""
from __future__ import annotations

import abc
from typing import Any, Iterable


class Adapter(abc.ABC):
    """One instance per platform. Not thread-safe; use `worker()` for concurrency."""

    #: workload name -> human-readable description of what the query must do
    key: str

    def __init__(self, platform):
        self.platform = platform
        self.key = platform.key

    # ---- lifecycle -------------------------------------------------------
    @abc.abstractmethod
    def connect(self) -> None: ...

    @abc.abstractmethod
    def close(self) -> None: ...

    @abc.abstractmethod
    def ping(self) -> float:
        """Round-trip a trivial no-op query. Returns latency in ms.

        Used both as a liveness check and as the network-RTT baseline that the
        analysis subtracts when comparing managed (remote) vs Docker (local).
        """

    # ---- schema / data ---------------------------------------------------
    @abc.abstractmethod
    def reset(self) -> None:
        """Drop all data and indexes so a load starts from a clean slate."""

    @abc.abstractmethod
    def create_indexes(self) -> list[str]:
        """Create the benchmark indexes. Returns human-readable descriptions
        for the README ('which properties are indexed on each platform')."""

    @abc.abstractmethod
    def load_nodes(self, batches: Iterable[list[dict]]) -> int: ...

    @abc.abstractmethod
    def load_edges(self, batches: Iterable[list[dict]]) -> int: ...

    # ---- queries ---------------------------------------------------------
    @abc.abstractmethod
    def execute(self, workload: str, params: dict[str, Any]) -> int:
        """Run one workload iteration. Returns a result cardinality (row count
        or aggregate) so semantic equivalence can be cross-checked."""

    @abc.abstractmethod
    def worker(self):
        """Return a fresh, independent connection handle for a concurrent
        client. Must be usable from its own thread."""

    # ---- observability ---------------------------------------------------
    def footprint(self) -> dict[str, Any]:
        """Whatever the platform exposes. Default: nothing observable."""
        return {"observable": False, "note": "not observable on this platform"}
