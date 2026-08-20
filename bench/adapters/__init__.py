"""Adapter factory."""
from __future__ import annotations

from .base import Adapter


def build(platform) -> Adapter:
    if platform.adapter == "bolt":
        from .bolt import BoltAdapter
        return BoltAdapter(platform)
    if platform.adapter == "falkor":
        from .falkor import FalkorAdapter
        return FalkorAdapter(platform)
    if platform.adapter == "arango":
        from .arango import ArangoAdapter
        return ArangoAdapter(platform)
    raise ValueError(f"Unknown adapter type: {platform.adapter}")


__all__ = ["Adapter", "build"]