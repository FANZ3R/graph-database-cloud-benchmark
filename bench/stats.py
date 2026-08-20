"""Latency statistics.

Percentiles are computed with the nearest-rank method on the sorted sample
rather than by interpolation, so a reported p95 is always an observation that
actually happened rather than a synthetic value between two measurements.
"""
from __future__ import annotations

import statistics


def percentile(sorted_vals: list[float], q: float) -> float:
    """Nearest-rank percentile. `q` in [0, 1]."""
    if not sorted_vals:
        return float("nan")
    k = max(0, min(len(sorted_vals) - 1, int(round(q * len(sorted_vals) + 0.5)) - 1))
    return sorted_vals[k]


def summarise(samples: list[float], baseline_ms: float = 0.0) -> dict:
    """Reduce a latency sample to the reported statistics.

    `baseline_ms` is that platform's measured network RTT p50. Subtracting it
    gives an estimate of engine cost for platforms that cannot report their own
    execution time. It is only an estimate: when the engine takes ~1 ms and
    network jitter is ~2 ms, the adjusted figure carries a relative error above
    100%. That is precisely why server-reported timings are collected too, and
    why the README presents all three views side by side.
    """
    clean = [v for v in samples if v is not None]
    if not clean:
        return {"n": 0}
    s = sorted(clean)
    out = {
        "n": len(s),
        "min_ms": round(s[0], 3),
        "p50_ms": round(percentile(s, 0.50), 3),
        "p95_ms": round(percentile(s, 0.95), 3),
        "p99_ms": round(percentile(s, 0.99), 3),
        "max_ms": round(s[-1], 3),
        "mean_ms": round(statistics.fmean(s), 3),
        "stdev_ms": round(statistics.stdev(s), 3) if len(s) > 1 else 0.0,
    }
    if baseline_ms:
        out["p50_rtt_adjusted_ms"] = round(max(0.0, out["p50_ms"] - baseline_ms), 3)
        out["p95_rtt_adjusted_ms"] = round(max(0.0, out["p95_ms"] - baseline_ms), 3)
        out["rtt_baseline_ms"] = round(baseline_ms, 3)
    return out