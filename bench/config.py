"""Platform registry and environment configuration.

Every platform declares its *advertised* resource specs here so the README
fairness table is generated from the same source of truth the benchmark uses.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data"
RESULTS_DIR = ROOT / "results"
RAW_DIR = RESULTS_DIR / "raw"
CHART_DIR = RESULTS_DIR / "charts"
for _d in (DATA_DIR, RAW_DIR, CHART_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


TARGET_RELATIONSHIPS = _int("TARGET_RELATIONSHIPS", 150_000)
MAX_NODES = _int("MAX_NODES", 45_000)
SAMPLE_SEED = _int("SAMPLE_SEED", 20260820)
READ_ITERATIONS = _int("READ_ITERATIONS", 120)
WARMUP_ITERATIONS = _int("WARMUP_ITERATIONS", 30)
MAX_RESULT_ROWS = _int("MAX_RESULT_ROWS", 50_000)
TRAVERSAL_LIMIT = _int("TRAVERSAL_LIMIT", 1_000)

CONCURRENCY_LEVELS = [
    int(x) for x in os.getenv("CONCURRENCY_LEVELS", "1,10,40").split(",") if x.strip()
]


@dataclass
class Platform:
    key: str
    display_name: str
    adapter: str              # "bolt" | "falkor" | "arango"
    deployment: str           # "managed" | "docker"
    engine_notes: str
    query_language: str
    # Advertised / enforced resource limits -- goes straight into the README table
    vcpu: str
    ram: str
    disk: str
    limits_source: str        # how we know / how it's enforced
    env: dict = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        """A platform is runnable only if its required env vars are non-empty."""
        return all(v for k, v in self.env.items() if k.endswith("_REQUIRED") is False)


def _env(*names: str) -> dict:
    return {n: os.getenv(n, "") for n in names}


PLATFORMS: dict[str, Platform] = {
    "cognodb": Platform(
        key="cognodb",
        display_name="CognoDB Cloud (free c0)",
        adapter="bolt",
        deployment="managed",
        engine_notes="Managed Bolt-compatible graph service; official Neo4j driver.",
        query_language="Cypher",
        vcpu="burst to 0.5 vCPU",
        ram="512 MB",
        disk="1 GiB",
        limits_source=(
            "Observed in CognoDB console for instance db-230f88c8 (c0, us-east4, "
            "v0.9.11). NOTE: the assignment brief states 256 MB; the provisioned "
            "instance reports 512 MB. We benchmark against the observed spec. "
            "Also enforced: max 200 connections, up to 500 disk IOPS, and a hard "
            "server-side cap of 50,000 result rows per query."
        ),
        env=_env("COGNODB_URI", "COGNODB_USER", "COGNODB_PASSWORD"),
    ),
    "neo4j_aura": Platform(
        key="neo4j_aura",
        display_name="Neo4j AuraDB Free",
        adapter="bolt",
        deployment="managed",
        engine_notes="Managed Neo4j; native property graph, page-cache + JVM heap.",
        query_language="Cypher",
        vcpu="not disclosed (shared)",
        ram="not disclosed (shared)",
        disk="capped by node/rel quota",
        limits_source="Aura Free does not publish vCPU/RAM; quota-based. See caveats.",
        env=_env("NEO4J_AURA_URI", "NEO4J_AURA_USER", "NEO4J_AURA_PASSWORD"),
    ),
    "memgraph": Platform(
        key="memgraph",
        display_name="Memgraph (Docker, capped)",
        adapter="bolt",
        deployment="docker",
        engine_notes="In-memory C++ engine, Bolt protocol, Cypher-compatible.",
        query_language="Cypher",
        vcpu="0.5",
        ram="512 MB",
        disk="1 GiB",
        limits_source="Enforced via docker cpus=0.5 / mem_limit=512m (cgroups v2).",
        env=_env("MEMGRAPH_URI"),
    ),
    "falkordb": Platform(
        key="falkordb",
        display_name="FalkorDB (Docker, capped)",
        adapter="falkor",
        deployment="docker",
        engine_notes="Redis module; sparse adjacency matrices + GraphBLAS.",
        query_language="Cypher (subset)",
        vcpu="0.5",
        ram="512 MB",
        disk="1 GiB",
        limits_source="Enforced via docker cpus=0.5 / mem_limit=512m (cgroups v2).",
        env=_env("FALKORDB_HOST", "FALKORDB_PORT"),
    ),
    "arangodb": Platform(
        key="arangodb",
        display_name="ArangoDB (Docker, capped)",
        adapter="arango",
        deployment="docker",
        engine_notes="Multi-model (document + graph), RocksDB storage engine.",
        query_language="AQL",
        vcpu="0.5",
        ram="512 MB",
        disk="1 GiB",
        limits_source="Enforced via docker cpus=0.5 / mem_limit=512m (cgroups v2).",
        env=_env("ARANGO_URL", "ARANGO_USER", "ARANGO_PASSWORD", "ARANGO_DB"),
    ),
}

ALL_KEYS = list(PLATFORMS.keys())


def get(key: str) -> Platform:
    if key not in PLATFORMS:
        raise KeyError(f"Unknown platform '{key}'. Known: {', '.join(ALL_KEYS)}")
    return PLATFORMS[key]


def configured_platforms() -> list[Platform]:
    return [p for p in PLATFORMS.values() if p.configured]
