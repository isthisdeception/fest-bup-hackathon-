"""
Public sample cases harness.

Two modes:
  --mode offline  : inject reference interpretations, compare optimizer costs
  --mode http     : POST to live endpoint, compare interpretations & schedules end to end (Step 19)

Hard rule: this file lives only under tests/. No production module may
import from tests/. No SAMPLE- IDs or reference data in app/.
"""

import argparse
import json
import os
import sys
import time

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

SAMPLE_FILE = os.path.join(
    os.path.dirname(__file__), "data",
    "BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json",
)

TOL = 0.01  # Problem Statement Section 11.5


def load_cases():
    """Load the public sample cases file."""
    with open(SAMPLE_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert "_meta" in data, "Missing _meta key"
    assert "cases" in data, "Missing cases key"
    cases = data["cases"]
    print(f"Loaded {len(cases)} cases from {os.path.basename(SAMPLE_FILE)}")
    return cases


def build_directives_from_interpretation(interp_list):
    """Convert reference directive_interpretation into Directive objects."""
    from app.directives import Directive

    directives = []
    for item in interp_list:
        dtype = item["directive_type"]
        adj = item.get("structured_adjustment") or {}

        hours = tuple(adj.get("hours", []))
        factor = adj.get("factor")
        min_energy = adj.get("minimum_energy_kwh")
        max_grid = adj.get("max_grid_kwh")

        directives.append(Directive(
            note_index=item["note_index"],
            directive_type=dtype,
            hours=hours,
            factor=factor,
            minimum_energy_kwh=min_energy,
            max_grid_kwh=max_grid,
            explanation=item.get("explanation", ""),
            source="test_injected",
        ))

    return directives


def build_scenario_from_input(inp):
    """Convert a case's input dict into a Scenario."""
    from app.schemas import parse_request, to_scenario
    req = parse_request(inp)
    return to_scenario(req)


def run_offline(cases):
    """Run offline mode: inject reference interpretations, compare costs."""
    from app.directives import compile_constraints
    from app.optimizer import optimize
    from app.plan_builder import build_plan
    from app.validator import validate_plan

    results = []
    total_passed = 0

    print()
    print(f"{'Case':<12} {'Status':<10} {'Cost Match':<12} {'Valid':<8} "
          f"{'Our Cost':>12} {'Ref Cost':>12} {'Our Peak':>10} {'Ref Peak':>10} "
          f"{'Time ms':>8}")
    print("-" * 100)

    for case in cases:
        case_id = case["id"]
        inp = case["input"]
        expected = case["expected_output"]

        ref_cost = expected["total_cost_bdt"]
        ref_grid = expected["total_grid_kwh"]
        ref_peak = expected["peak_grid_kwh"]
        ref_interp = expected["directive_interpretation"]

        t0 = time.perf_counter()

        try:
            scenario = build_scenario_from_input(inp)
            directives = build_directives_from_interpretation(ref_interp)
            constraints = compile_constraints(scenario, directives)
            opt = optimize(scenario, constraints)
            plan, total_grid, total_cost, peak = build_plan(
                scenario, constraints, opt
            )
            violations = validate_plan(
                scenario, constraints, plan, total_grid, total_cost, peak
            )

            elapsed = (time.perf_counter() - t0) * 1000

            cost_match = abs(total_cost - ref_cost) <= TOL
            valid = len(violations) == 0
            passed = cost_match and valid

            status = "PASS" if passed else "FAIL"
            if passed:
                total_passed += 1

            results.append({
                "case_id": case_id,
                "status": status,
                "cost_match": cost_match,
                "valid": valid,
                "our_cost": total_cost,
                "ref_cost": ref_cost,
                "our_peak": peak,
                "ref_peak": ref_peak,
                "elapsed_ms": elapsed,
                "violations": violations,
            })

            print(f"{case_id:<12} {status:<10} {'YES' if cost_match else 'NO':<12} "
                  f"{'YES' if valid else 'NO':<8} "
                  f"{total_cost:>12.2f} {ref_cost:>12.2f} "
                  f"{peak:>10.2f} {ref_peak:>10.2f} "
                  f"{elapsed:>8.1f}")

            if not cost_match:
                print(f"  COST DIFF: {abs(total_cost - ref_cost):.6f}")
            if violations:
                for v in violations[:3]:
                    print(f"  VIOLATION: {v}")

        except Exception as exc:
            elapsed = (time.perf_counter() - t0) * 1000
            print(f"{case_id:<12} {'ERROR':<10} {'---':<12} {'---':<8} "
                  f"{'---':>12} {ref_cost:>12.2f} "
                  f"{'---':>10} {ref_peak:>10.2f} "
                  f"{elapsed:>8.1f}")
            print(f"  ERROR: {exc}")
            results.append({
                "case_id": case_id, "status": "ERROR",
                "error": str(exc),
            })

    print("-" * 100)
    print(f"\nPASSED {total_passed}/{len(cases)}")

    return total_passed == len(cases)


def _compare_interpretations(actual_list, expected_list):
    """Compare returned directive_interpretation against expected ground truth.

    Returns (passed, list_of_diffs).
    """
    if len(actual_list) != len(expected_list):
        return False, [f"Count mismatch: got {len(actual_list)}, expected {len(expected_list)}"]

    diffs = []
    for i, (act, exp) in enumerate(zip(actual_list, expected_list)):
        prefix = f"Note {i}: "
        if act.get("applies") != exp.get("applies"):
            diffs.append(f"{prefix}applies got {act.get('applies')} expected {exp.get('applies')}")

        if act.get("directive_type") != exp.get("directive_type"):
            diffs.append(f"{prefix}type got {act.get('directive_type')} expected {exp.get('directive_type')}")

        act_adj = act.get("structured_adjustment")
        exp_adj = exp.get("structured_adjustment")

        if exp.get("directive_type") == "no_op":
            if act_adj is not None:
                diffs.append(f"{prefix}no_op expected null structured_adjustment, got {act_adj}")
        else:
            if not isinstance(act_adj, dict):
                diffs.append(f"{prefix}expected structured_adjustment dict, got {type(act_adj)}")
                continue

            # Compare hours
            act_hours = act_adj.get("hours", [])
            exp_hours = exp_adj.get("hours", []) if isinstance(exp_adj, dict) else []
            if list(act_hours) != list(exp_hours):
                diffs.append(f"{prefix}hours got {act_hours} expected {exp_hours}")

            # Compare numeric fields
            dtype = exp.get("directive_type")
            if dtype == "solar_reduction":
                act_f = act_adj.get("factor")
                exp_f = exp_adj.get("factor")
                if act_f is None or abs(float(act_f) - float(exp_f)) > TOL:
                    diffs.append(f"{prefix}factor got {act_f} expected {exp_f}")
            elif dtype == "minimum_battery_reserve":
                act_r = act_adj.get("minimum_energy_kwh")
                exp_r = exp_adj.get("minimum_energy_kwh")
                if act_r is None or abs(float(act_r) - float(exp_r)) > TOL:
                    diffs.append(f"{prefix}reserve got {act_r} expected {exp_r}")
            elif dtype == "max_grid_window":
                act_g = act_adj.get("max_grid_kwh")
                exp_g = exp_adj.get("max_grid_kwh")
                if act_g is None or abs(float(act_g) - float(exp_g)) > TOL:
                    diffs.append(f"{prefix}max_grid got {act_g} expected {exp_g}")

    return (len(diffs) == 0), diffs


def run_http(cases, base_url, delay=2.0):
    """Run HTTP mode: POST to live endpoint and assert full end-to-end correctness."""
    import httpx
    from app.directives import compile_constraints
    from app.schemas import HourlyPlanEntry
    from app.validator import validate_plan

    print(f"\nHTTP mode against {base_url} (pacing delay: {delay}s)")
    print(f"{'Case':<12} {'HTTP':<5} {'Interp':<8} {'Replay':<8} {'Totals':<8} "
          f"{'Quality':<9} {'Our Cost':>10} {'Ref Cost':>10} {'Latency s':>10}")
    print("-" * 90)

    interp_passed = 0
    validity_passed = 0
    quality_ratios = []
    latencies = []

    client = httpx.Client(timeout=35.0)

    for idx, case in enumerate(cases):
        if idx > 0 and delay > 0:
            time.sleep(delay)

        case_id = case["id"]
        inp = case["input"]
        expected = case["expected_output"]
        ref_cost = expected["total_cost_bdt"]
        ref_peak = expected["peak_grid_kwh"]

        t0 = time.perf_counter()
        try:
            resp = client.post(
                f"{base_url}/optimize-energy",
                json=inp,
            )
            elapsed_s = time.perf_counter() - t0
            latencies.append(elapsed_s)

            if resp.status_code != 200:
                print(f"{case_id:<12} {resp.status_code:<5} {'FAIL':<8} {'FAIL':<8} {'FAIL':<8} "
                      f"{'0.00':<9} {'---':>10} {ref_cost:>10.2f} {elapsed_s:>10.2f}")
                print(f"  Response error: {resp.text[:250]}")
                continue

            body = resp.json()

            # 1. Top-level fields & scenario_id check
            required_keys = {
                "scenario_id", "directive_interpretation", "hourly_plan",
                "total_grid_kwh", "total_cost_bdt", "peak_grid_kwh", "plan_summary"
            }
            if not required_keys.issubset(body.keys()):
                print(f"{case_id:<12} 200   FAIL (missing keys) ...")
                continue

            assert body["scenario_id"] == inp["scenario_id"], "scenario_id mismatch"

            # 2. Interpretation comparison
            act_interp = body.get("directive_interpretation", [])
            exp_interp = expected.get("directive_interpretation", [])
            interp_ok, interp_diffs = _compare_interpretations(act_interp, exp_interp)
            if interp_ok:
                interp_passed += 1

            # 3. Ground-truth replay validation
            scenario = build_scenario_from_input(inp)
            true_directives = build_directives_from_interpretation(exp_interp)
            true_constraints = compile_constraints(scenario, true_directives)

            plan_entries = [HourlyPlanEntry(**h) for h in body["hourly_plan"]]
            our_grid = body.get("total_grid_kwh", 0.0)
            our_cost = body.get("total_cost_bdt", 0.0)
            our_peak = body.get("peak_grid_kwh", 0.0)

            violations = validate_plan(
                scenario, true_constraints, plan_entries, our_grid, our_cost, our_peak
            )
            replay_ok = (len(violations) == 0)
            if replay_ok:
                validity_passed += 1

            # 4. Totals recomputation
            computed_grid = sum(h.grid_kwh for h in plan_entries)
            computed_cost = sum(h.grid_kwh * scenario.tariff[h.hour] for h in plan_entries)
            computed_peak = max(h.grid_kwh for h in plan_entries)

            totals_ok = (
                abs(computed_grid - our_grid) <= TOL
                and abs(computed_cost - our_cost) <= TOL
                and abs(computed_peak - our_peak) <= TOL
            )

            # 5. Quality ratio: min(1, ref_cost / our_cost)
            if our_cost <= 0:
                q_ratio = 1.0
            elif abs(our_cost - ref_cost) <= TOL:
                q_ratio = 1.0
            else:
                q_ratio = min(1.0, ref_cost / our_cost)
            quality_ratios.append(q_ratio)

            print(f"{case_id:<12} 200   {'PASS' if interp_ok else 'FAIL':<8} "
                  f"{'PASS' if replay_ok else 'FAIL':<8} "
                  f"{'PASS' if totals_ok else 'FAIL':<8} "
                  f"{q_ratio:<9.4f} {our_cost:>10.2f} {ref_cost:>10.2f} {elapsed_s:>10.2f}")

            if not interp_ok:
                for d in interp_diffs:
                    print(f"  INTERP DIFF: {d}")
            if violations:
                for v in violations[:2]:
                    print(f"  REPLAY VIOLATION: {v}")

        except Exception as exc:
            elapsed_s = time.perf_counter() - t0
            latencies.append(elapsed_s)
            print(f"{case_id:<12} ERR   {'FAIL':<8} {'FAIL':<8} {'FAIL':<8} "
                  f"{'0.00':<9} {'---':>10} {ref_cost:>10.2f} {elapsed_s:>10.2f}")
            print(f"  ERROR: {exc}")

    client.close()

    total_cases = len(cases)
    avg_q = sum(quality_ratios) / len(quality_ratios) if quality_ratios else 0.0
    sorted_lat = sorted(latencies)
    p50 = sorted_lat[int(len(sorted_lat) * 0.50)] if sorted_lat else 0.0
    p95 = sorted_lat[int(len(sorted_lat) * 0.95)] if sorted_lat else 0.0

    print("-" * 90)
    print("\n--- AGGREGATE SUMMARY ---")
    print(f"Interpretation : {interp_passed}/{total_cases}")
    print(f"Validity (true): {validity_passed}/{total_cases}")
    print(f"Avg Quality    : {avg_q:.4f}")
    print(f"Latency p50    : {p50:.2f}s")
    print(f"Latency p95    : {p95:.2f}s")
    print("-------------------------")

    return (interp_passed == total_cases) and (validity_passed == total_cases) and (abs(avg_q - 1.0) <= 0.001)


def main():
    parser = argparse.ArgumentParser(description="Public sample cases harness")
    parser.add_argument("--mode", choices=["offline", "http"], default="offline")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--delay", type=float, default=2.0, help="Delay in seconds between HTTP requests")
    args = parser.parse_args()

    cases = load_cases()

    if args.mode == "offline":
        success = run_offline(cases)
    else:
        success = run_http(cases, args.base_url, delay=args.delay)

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
