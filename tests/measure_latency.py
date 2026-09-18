"""
Step 22: Latency Measurement, Cache Benchmarking, and Readiness Verification.

Measures:
- Unique requests (defeating cache): verifies p95 <= 5.0s on fresh inputs.
- Cached requests (hitting cache): verifies p95 < 300ms.
- Startup readiness: time to first healthy /health.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from typing import Any

import httpx

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


def make_payload(scenario_id: str, notes: list[str]) -> dict[str, Any]:
    hours = []
    for h in range(24):
        hours.append({
            "hour": h,
            "demand_kwh": 100.0,
            "solar_kwh": 35.0 if 8 <= h < 16 else 0.0,
            "tariff_bdt_per_kwh": 5.0 if 0 <= h < 6 else (20.0 if 18 <= h < 22 else 10.0),
        })
    return {
        "scenario_id": scenario_id,
        "operator_notes": notes,
        "hours": hours,
        "battery": {
            "capacity_kwh": 200.0,
            "initial_energy_kwh": 100.0,
            "minimum_energy_kwh": 20.0,
            "max_charge_kwh_per_hour": 50.0,
            "max_discharge_kwh_per_hour": 50.0,
        },
    }


UNIQUE_NOTES_BANK = [
    ["Solar generation will drop to 30% from 11 AM to 2 PM."],
    ["No battery charging between midnight and 3 AM."],
    ["Keep at least 110 kWh stored from 6 PM to 9 PM."],
    ["Grid intake capped at 170 kWh per hour between 7 PM and 9 PM."],
    ["Battery discharging blocked from 1 PM until 3 PM."],
    ["PV output will fall to half forecast between 10 AM and noon."],
    ["Charging unavailable between 2 PM and 4 PM."],
    ["Maintain minimum 90 kWh reserve from 5 PM to 8 PM."],
    ["Feeder limit restricts intake to 145 kWh between 6 PM and 8 PM."],
    ["No load export from battery between 8 AM and 10 AM."],
    ["Panel maintenance cuts solar to 25% from 9 AM to 11 AM."],
    ["Charger offline during the 4 AM hour only."],
    ["Hold at least 130 kWh in the pack from 7 PM to 10 PM."],
    ["Grid import must not exceed 165 kWh between 6 PM and 9 PM."],
    ["No discharging from 2 PM through 5 PM."],
    ["Solar output reduced to 40% from 1 PM to 3 PM."],
    ["No battery charging from 10 PM to midnight."],
    ["Keep at least 85 kWh stored from 6 PM until 8 PM."],
    ["Capped grid import at 185 kWh between 5 PM and 7 PM."],
    ["Battery export blocked from 11 AM to 1 PM."],
    ["Heavy overcast reduces solar to 60% between 9 AM and 1 PM."],
    ["Charging unavailable from 1 AM to 4 AM."],
    ["Emergency reserve requires 95 kWh from 7 PM to 9 PM."],
    ["Grid import capped at 155 kWh between 6 PM and 8 PM."],
    ["No battery discharging between 9 AM and noon."],
    ["PV generation drops to 35% from 10 AM to 2 PM."],
    ["No charging between 3 PM and 6 PM."],
    ["Keep at least 105 kWh reserve from 6 PM to 10 PM."],
    ["Import limit of 175 kWh between 7 PM and 9 PM."],
    ["Battery discharging prohibited between 11 AM and 2 PM."],
]


def run_benchmark(base_url: str, n: int, is_unique: bool, delay: float) -> dict[str, float]:
    mode_name = "UNIQUE (Fresh LLM Calls, No-Cache)" if is_unique else "CACHED (Repeated Payload)"
    print("\n" + "=" * 80)
    print(f"BENCHMARK: {mode_name} - N={n}")
    print("=" * 80)

    client = httpx.Client(timeout=35.0)
    latencies: list[float] = []
    successes = 0

    base_cached_payload = make_payload("LATENCY-CACHED", ["Battery charging offline from 10 PM to midnight."])

    for i in range(n):
        if is_unique:
            note = UNIQUE_NOTES_BANK[i % len(UNIQUE_NOTES_BANK)]
            payload = make_payload(f"LATENCY-U-{i+1}", note)
        else:
            payload = base_cached_payload

        t0 = time.perf_counter()
        try:
            resp = client.post(f"{base_url}/optimize-energy", json=payload)
            elapsed = time.perf_counter() - t0
            if resp.status_code == 200:
                latencies.append(elapsed)
                successes += 1
                if is_unique:
                    print(f"  Req {i+1:02d}/{n:02d}: {elapsed:6.2f}s  (HTTP 200)")
                elif (i + 1) % 5 == 0 or i == 0 or i == n - 1:
                    print(f"  Req {i+1:02d}/{n:02d}: {elapsed*1000:6.1f}ms (HTTP 200)")
            else:
                print(f"  Req {i+1:02d}/{n:02d}: HTTP {resp.status_code} ({elapsed:.2f}s)")
        except Exception as exc:
            elapsed = time.perf_counter() - t0
            print(f"  Req {i+1:02d}/{n:02d}: Exception {exc} ({elapsed:.2f}s)")

        if is_unique and delay > 0 and i < n - 1:
            time.sleep(delay)

    if not latencies:
        print("ERROR: No successful requests recorded.")
        return {}

    latencies.sort()
    count = len(latencies)
    min_lat = latencies[0]
    max_lat = latencies[-1]
    p50 = latencies[int(count * 0.50)]
    p90 = latencies[min(int(count * 0.90), count - 1)]
    p95 = latencies[min(int(count * 0.95), count - 1)]

    unit = "ms" if not is_unique else "s"
    mult = 1000.0 if not is_unique else 1.0

    print("-" * 80)
    print(f"Results for {mode_name}:")
    print(f"  Success Rate : {successes}/{n} ({successes/n*100:.1f}%)")
    print(f"  Min Latency  : {min_lat*mult:6.2f} {unit}")
    print(f"  p50 Latency  : {p50*mult:6.2f} {unit}")
    print(f"  p90 Latency  : {p90*mult:6.2f} {unit}")
    print(f"  p95 Latency  : {p95*mult:6.2f} {unit}")
    print(f"  Max Latency  : {max_lat*mult:6.2f} {unit}")
    print("-" * 80)

    return {
        "min": min_lat,
        "p50": p50,
        "p90": p90,
        "p95": p95,
        "max": max_lat,
    }


def check_readiness(base_url: str) -> float:
    print("\n" + "=" * 80)
    print("STARTUP READINESS VERIFICATION (/health)")
    print("=" * 80)
    t0 = time.perf_counter()
    resp = httpx.get(f"{base_url}/health", timeout=5.0)
    elapsed = time.perf_counter() - t0
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    print(f"  GET /health reached in {elapsed*1000:.2f} ms (Target < 60s)")
    print("=" * 80)
    return elapsed


def main():
    parser = argparse.ArgumentParser(description="Step 22 Latency Benchmark")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--n", type=int, default=25)
    parser.add_argument("--unique", action="store_true")
    parser.add_argument("--delay", type=float, default=2.2)
    args = parser.parse_args()

    # If run without flag, runs benchmark for the specified mode
    metrics = run_benchmark(args.base_url, args.n, args.unique, args.delay)
    if args.unique:
        if metrics.get("p95", 99.0) <= 5.0:
            print("\nSUCCESS: Unique p95 is <= 5.0s (Full 3/3 points band)")
        elif metrics.get("p95", 99.0) <= 15.0:
            print("\nACCEPTABLE: Unique p95 is in 5.0s-15.0s band (2/3 points band)")
    else:
        if metrics.get("p95", 99.0) < 0.300:
            print("\nSUCCESS: Cached p95 is < 300ms (Cache verified)")

    check_readiness(args.base_url)


if __name__ == "__main__":
    main()
