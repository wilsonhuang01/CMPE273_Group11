"""Read-only powercap diagnostics when the Docker VM exposes package energy.

Package-wide readings include other VM work and launcher overhead. They cannot
be attributed to an individual process and never participate in qualification.
"""

import time
from pathlib import Path


def snapshot(root=Path("/sys/class/powercap")):
    try:
        domains = {}
        for path in sorted(root.glob("*-rapl:*")):
            if path.name.count(":") != 1:
                continue
            name = (path / "name").read_text()[:128].strip()
            if not name.startswith("package"):
                continue
            value = int((path / "energy_uj").read_text()[:32])
            maximum = int((path / "max_energy_range_uj").read_text()[:32])
            if not 0 <= value < maximum <= 2**64 or len(domains) >= 8:
                raise ValueError("invalid powercap domain bounds")
            domains[path.name] = {"name": name, "energy_uj": value, "maximum_uj": maximum}
        if not domains:
            raise ValueError("package energy counters are not exposed by this Docker VM")
        return {"status": "available", "monotonic": time.monotonic(), "domains": domains}
    except (OSError, ValueError) as error:
        return {"status": "unavailable", "count": None, "reason": str(error)[:512]}


def measurement(before, after):
    for reading in (before, after):
        if reading["status"] != "available":
            return reading
    if before["domains"].keys() != after["domains"].keys():
        return {"status": "unavailable", "count": None, "reason": "powercap domains changed"}
    domains = []
    for key, start in before["domains"].items():
        end = after["domains"][key]
        if start["name"] != end["name"] or start["maximum_uj"] != end["maximum_uj"]:
            return {"status": "unavailable", "count": None, "reason": "powercap identity changed"}
        delta = (end["energy_uj"] - start["energy_uj"]) % start["maximum_uj"]
        domains.append({"id": key, "name": start["name"], "microjoules": delta})
    total = sum(v["microjoules"] for v in domains)
    return {
        "status": "available",
        "count": total,
        "unit": "microjoules",
        "joules": total / 1e6,
        "scope": "package-wide",
        "domains": domains,
        "interval_seconds": after["monotonic"] - before["monotonic"],
        "wrap_policy": "at-most-one-counter-wrap",
    }
