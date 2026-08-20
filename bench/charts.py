"""Charts.

Latency plots are log-scale because the platforms span five orders of
magnitude (0.06 ms to 1,479 ms). On a linear axis the three sub-millisecond
platforms collapse onto the baseline and the chart shows nothing except that
Virginia is far away.
"""
from __future__ import annotations

import json

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from . import config

ORDER = ["cognodb", "neo4j_aura", "memgraph", "falkordb", "arangodb"]
COLORS = {"cognodb": "#00b8a9", "neo4j_aura": "#4a7fd4", "memgraph": "#e07a3f",
          "falkordb": "#c94f7c", "arangodb": "#6aa84f"}
WORKLOADS = ["point_lookup", "filtered_lookup", "hop1", "hop2", "hop3",
             "agg_region", "agg_rel_count"]


def _load():
    return json.loads((config.RAW_DIR / "workloads.json").read_text())


def _present(res):
    return [k for k in ORDER if k in res and not res[k].get("failed")]


def latency_grouped(data, view="wall_clock", stat="p50_ms", fname=None):
    res = data["results"]
    keys = _present(res)
    fig, ax = plt.subplots(figsize=(13, 6))
    width = 0.8 / len(keys)

    for i, k in enumerate(keys):
        vals, xs = [], []
        for j, w in enumerate(WORKLOADS):
            wl = res[k]["workloads"].get(w, {})
            v = wl.get(view, {}).get(stat)
            if v:
                vals.append(v)
                xs.append(j + i * width - 0.4 + width / 2)
        ax.bar(xs, vals, width, label=res[k]["display_name"],
               color=COLORS.get(k), edgecolor="white", linewidth=0.5)

    ax.set_yscale("log")
    ax.set_xticks(range(len(WORKLOADS)))
    ax.set_xticklabels(WORKLOADS, rotation=20, ha="right")
    ax.set_ylabel(f"{stat.replace('_ms','').upper()} latency (ms, log scale)")
    ax.set_title(f"Query latency — {view.replace('_',' ')} ({stat.replace('_ms','')})")
    ax.legend(fontsize=9)
    ax.grid(axis="y", alpha=0.3, which="both")
    fig.tight_layout()
    out = config.CHART_DIR / (fname or f"latency_{view}_{stat}.png")
    fig.savefig(out, dpi=140)
    plt.close(fig)
    print(f"  {out}")


def rtt_decomposition(data):
    """Show how much of each managed platform's latency is network."""
    res = data["results"]
    keys = [k for k in _present(res) if res[k]["deployment"] == "managed"]
    if not keys:
        return
    fig, ax = plt.subplots(figsize=(11, 5))
    width = 0.8 / len(keys)
    for i, k in enumerate(keys):
        base = res[k]["rtt_baseline_ms"]
        net, eng, xs = [], [], []
        for j, w in enumerate(WORKLOADS):
            p50 = res[k]["workloads"].get(w, {}).get("wall_clock", {}).get("p50_ms")
            if not p50:
                continue
            xs.append(j + i * width - 0.4 + width / 2)
            net.append(min(base, p50))
            eng.append(max(0.0, p50 - base))
        ax.bar(xs, net, width, color=COLORS.get(k), alpha=0.35,
               label=f"{res[k]['display_name']} — network")
        ax.bar(xs, eng, width, bottom=net, color=COLORS.get(k),
               label=f"{res[k]['display_name']} — above baseline")
    ax.set_xticks(range(len(WORKLOADS)))
    ax.set_xticklabels(WORKLOADS, rotation=20, ha="right")
    ax.set_ylabel("p50 latency (ms)")
    ax.set_title("Managed platforms: how much of the latency is geography?")
    ax.legend(fontsize=8)
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    out = config.CHART_DIR / "rtt_decomposition.png"
    fig.savefig(out, dpi=140)
    plt.close(fig)
    print(f"  {out}")


def concurrency(data):
    res = data["results"]
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 5))
    for k in _present(res):
        sweep = res[k].get("concurrency") or []
        if not sweep:
            continue
        x = [s["clients"] for s in sweep]
        ax1.plot(x, [s["qps"] for s in sweep], marker="o",
                 color=COLORS.get(k), label=res[k]["display_name"])
        ax2.plot(x, [s["latency"].get("p50_ms", 0) for s in sweep], marker="o",
                 color=COLORS.get(k), label=res[k]["display_name"])
    for ax, ylab, title in (
        (ax1, "sustained queries/sec", "Throughput vs client concurrency"),
        (ax2, "p50 latency (ms)", "Latency vs client concurrency")):
        ax.set_xscale("log"); ax.set_yscale("log")
        ax.set_xticks([1, 10, 40]); ax.set_xticklabels(["1", "10", "40"])
        ax.set_xlabel("concurrent clients"); ax.set_ylabel(ylab)
        ax.set_title(title); ax.grid(alpha=0.3, which="both"); ax.legend(fontsize=8)
    fig.tight_layout()
    out = config.CHART_DIR / "concurrency.png"
    fig.savefig(out, dpi=140)
    plt.close(fig)
    print(f"  {out}")


def ingest(data):
    ing = json.loads((config.RAW_DIR / "ingest.json").read_text())["results"]
    keys = [k for k in ORDER if k in ing and not ing[k].get("failed")]
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.bar([ing[k].get("platform", k) for k in keys],
           [ing[k]["relationships_per_second"] for k in keys],
           color=[COLORS.get(k) for k in keys], edgecolor="white")
    ax.set_ylabel("relationships / second")
    ax.set_title("Ingest throughput (identical 10,000-row batches)")
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    out = config.CHART_DIR / "ingest.png"
    fig.savefig(out, dpi=140)
    plt.close(fig)
    print(f"  {out}")


def main():
    data = _load()
    print("writing charts:")
    latency_grouped(data, "wall_clock", "p50_ms")
    latency_grouped(data, "wall_clock", "p95_ms")
    latency_grouped(data, "server_reported", "p50_ms",
                    fname="latency_server_p50.png")
    rtt_decomposition(data)
    concurrency(data)
    ingest(data)


if __name__ == "__main__":
    main()