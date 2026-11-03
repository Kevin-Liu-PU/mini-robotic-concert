"""Read-only environment/clock probe. No third-party imports or devices."""
import argparse
import importlib.metadata
import json
from pathlib import Path
import platform
import subprocess
import sys
import time


def probe(samples=50):
    packages = {}
    for name in ("numpy", "ur-rtde"):
        try: packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError: packages[name] = None
    clock = time.get_clock_info("perf_counter")
    comparisons = []
    for _ in range(3):
        before = time.perf_counter_ns()
        child = subprocess.run([sys.executable, "-B", "-c", "import time; print(time.perf_counter_ns())"],
                               capture_output=True, text=True, check=True, timeout=10)
        after = time.perf_counter_ns()
        stamp = int(child.stdout.strip())
        comparisons.append(dict(before_ns=before, child_ns=stamp, after_ns=after,
                                bracket_matches=before <= stamp <= after))
    waits = []
    for _ in range(samples):
        before = time.perf_counter_ns()
        time.sleep(.005)
        waits.append((time.perf_counter_ns() - before) / 1e6)
    return dict(kind="ENVIRONMENT_AND_SOFTWARE_CLOCK_ONLY", python=sys.version,
                executable=sys.executable, system=platform.system(), release=platform.release(),
                process_architecture=platform.machine(), package_versions=packages,
                clock=dict(implementation=clock.implementation, monotonic=clock.monotonic,
                           adjustable=clock.adjustable, resolution_s=clock.resolution),
                same_host_child_clock_samples=comparisons,
                shared_clock_bracket_ok=all(x["bracket_matches"] for x in comparisons),
                requested_sleep_ms=5, sleep_samples=samples,
                observed_sleep_ms=dict(min=min(waits), max=max(waits), mean=sum(waits)/len(waits)),
                hardware_imported=False, robot_connected=False,
                note="This is neither a physical latency measurement nor a hard realtime guarantee.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--wait-samples", type=int, default=50)
    args = parser.parse_args()
    if not 1 <= args.wait_samples <= 2000: parser.error("wait samples must be 1..2000")
    result = probe(args.wait_samples)
    output = json.dumps(result, indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(output, encoding="utf-8")
    print(output)
    return 0 if result["shared_clock_bracket_ok"] else 2


if __name__ == "__main__": raise SystemExit(main())
