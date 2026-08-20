"""Environment fingerprint.

Every result file carries one of these. The report generator refuses to merge
results whose `fingerprint` differs, because the assignment requires all
platforms to be measured from the same client machine -- mixing an office
Linux box with a home Windows/WSL2 box would invalidate the comparison
through CPU, network-path and container-runtime differences alike.

This is a guardrail, not paperwork: it makes an invalid run impossible to
publish by accident.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess
import sys


def _cpu_model() -> str:
    try:
        if sys.platform.startswith("linux"):
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if line.startswith("model name"):
                        return line.split(":", 1)[1].strip()
        elif sys.platform == "darwin":
            return subprocess.check_output(
                ["sysctl", "-n", "machdep.cpu.brand_string"], text=True
            ).strip()
        elif sys.platform.startswith("win"):
            return os.environ.get("PROCESSOR_IDENTIFIER", "unknown")
    except Exception:
        pass
    return platform.processor() or "unknown"


def _total_ram_gb() -> float:
    try:
        if hasattr(os, "sysconf") and "SC_PAGE_SIZE" in os.sysconf_names:
            return round(
                os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1e9, 1
            )
    except Exception:
        pass
    return 0.0


def _in_wsl() -> bool:
    try:
        return "microsoft" in open("/proc/version").read().lower()
    except Exception:
        return False


def _docker_runtime() -> str:
    if not shutil.which("docker"):
        return "docker not on PATH"
    try:
        out = subprocess.check_output(
            ["docker", "info", "--format", "{{.OperatingSystem}}|{{.NCPU}}|{{.MemTotal}}"],
            text=True, stderr=subprocess.DEVNULL, timeout=15,
        ).strip()
        os_name, ncpu, mem = out.split("|")
        return f"{os_name} (ncpu={ncpu}, mem={int(mem)/1e9:.1f}GB)"
    except Exception as e:
        return f"docker info failed: {e}"


def collect() -> dict:
    info = {
        "hostname": socket.gethostname(),
        "os": f"{platform.system()} {platform.release()}",
        "wsl2": _in_wsl(),
        "cpu": _cpu_model(),
        "logical_cores": os.cpu_count(),
        "host_ram_gb": _total_ram_gb(),
        "python": sys.version.split()[0],
        "docker": _docker_runtime(),
    }
    # Fingerprint deliberately excludes hostname-independent noise but includes
    # everything that could shift timings.
    material = json.dumps(
        {k: info[k] for k in ("hostname", "os", "wsl2", "cpu", "logical_cores", "docker")},
        sort_keys=True,
    )
    info["fingerprint"] = hashlib.sha256(material.encode()).hexdigest()[:16]
    return info


if __name__ == "__main__":
    print(json.dumps(collect(), indent=2))
