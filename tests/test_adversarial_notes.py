"""
Step 20: Adversarial paraphrase, time-expression normalization, distractor,
and extreme-but-valid scenario test suite.

Author: Locally authored - ZERO public sample pack strings.
Tests live HTTP service at --base-url (default: http://127.0.0.1:8000).
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from typing import Any, Optional

sys.path.insert(0, ".")

import httpx

from app.directives import compile_constraints
from app.optimizer import optimize
from app.plan_builder import build_plan
from app.validator import validate_plan
from tests.fixtures import STRESS_FIXTURES, Scenario


@dataclass
class ExpectedNote:
    note_text: str
    directive_type: str
    hours: list[int]
    expected_numeric: Optional[float]  # factor, min_energy, or max_grid; None if window-only/no_op
    applies: bool = True


@dataclass
class InterpretationCase:
    case_id: str
    category: str
    battery_capacity: float
    notes: list[ExpectedNote]


# ─── Locally Authored Interpretation Test Cases ──────────────────────────────

INTERPRETATION_CASES: list[InterpretationCase] = [
    # ── Category 1: Solar reduction paraphrase and percentage direction ─────
    InterpretationCase(
        case_id="SOLAR-01",
        category="solar_reduction",
        battery_capacity=200.0,
        notes=[
            ExpectedNote(
                note_text="PV output will fall to roughly 20% of forecast from 1 PM to 3 PM.",
                directive_type="solar_reduction",
                hours=[13, 14],
                expected_numeric=0.2,
                applies=True,
            )
        ],
    ),
    InterpretationCase(
        case_id="SOLAR-02",
        category="solar_reduction",
        battery_capacity=200.0,
        notes=[
            ExpectedNote(
                note_text="Expect an 80% drop in rooftop generation during the 1-3 PM maintenance slot.",
                directive_type="solar_reduction",
                hours=[13, 14],
                expected_numeric=0.2,
                applies=True,
            )
        ],
    ),
    InterpretationCase(
        case_id="SOLAR-03",
        category="solar_reduction",
        battery_capacity=200.0,
        notes=[
            ExpectedNote(
                note_text="Panel washing from one until three will leave about a fifth of normal output.",
                directive_type="solar_reduction",
                hours=[13, 14],
                expected_numeric=0.2,
                applies=True,
            )
        ],
    ),
    InterpretationCase(
        case_id="SOLAR-04",
        category="solar_reduction",
        battery_capacity=200.0,
        notes=[
            ExpectedNote(
                note_text="Between 13:00 and 15:00 usable solar is only 20 percent of the forecast.",
                directive_type="solar_reduction",
                hours=[13, 14],
                expected_numeric=0.2,
                applies=True,
            )
        ],
    ),
    InterpretationCase(
        case_id="SOLAR-05",
        category="solar_reduction",
        battery_capacity=200.0,
        notes=[
            ExpectedNote(
                note_text="Heavy cloud will halve solar between 10 AM and noon.",
                directive_type="solar_reduction",
                hours=[10, 11],
                expected_numeric=0.5,
                applies=True,
            )
        ],
    ),
    InterpretationCase(
        case_id="SOLAR-06",
        category="solar_reduction",
        battery_capacity=200.0,
        notes=[
            ExpectedNote(
                note_text="Array is fully offline for rewiring from 9 AM until 11 AM.",
                directive_type="solar_reduction",
                hours=[9, 10],
                expected_numeric=0.0,
                applies=True,
            )
        ],
    ),
    InterpretationCase(
        case_id="SOLAR-07",
        category="solar_reduction",
        battery_capacity=200.0,
        notes=[
            ExpectedNote(
                note_text="Solar will run at three quarters of forecast from 08:00 to 10:00.",
                directive_type="solar_reduction",
                hours=[8, 9],
                expected_numeric=0.75,
                applies=True,
            )
        ],
    ),

    # ── Category 2: Time-expression normalization ────────────────────────────
    InterpretationCase(
        case_id="TIME-01",
        category="no_charge_window",
        battery_capacity=200.0,
        notes=[
            ExpectedNote(
                note_text="No battery charging from midnight until 3 AM.",
                directive_type="no_charge_window",
                hours=[0, 1, 2],
                expected_numeric=None,
                applies=True,
            )
        ],
    ),
    InterpretationCase(
        case_id="TIME-02",
        category="no_charge_window",
        battery_capacity=200.0,
        notes=[
            ExpectedNote(
                note_text="Charging unavailable from 10 PM to midnight.",
                directive_type="no_charge_window",
                hours=[22, 23],
                expected_numeric=None,
                applies=True,
            )
        ],
    ),
    InterpretationCase(
        case_id="TIME-03",
        category="no_charge_window",
        battery_capacity=200.0,
        notes=[
            ExpectedNote(
                note_text="Do not charge the battery between 14:00 and 16:00.",
                directive_type="no_charge_window",
                hours=[14, 15],
                expected_numeric=None,
                applies=True,
            )
        ],
    ),
    InterpretationCase(
        case_id="TIME-04",
        category="no_charge_window",
        battery_capacity=200.0,
        notes=[
            ExpectedNote(
                note_text="Charger offline during the 11 AM hour only.",
                directive_type="no_charge_window",
                hours=[11],
                expected_numeric=None,
                applies=True,
            )
        ],
    ),
    InterpretationCase(
        case_id="TIME-05",
        category="no_discharge_window",
        battery_capacity=200.0,
        notes=[
            ExpectedNote(
                note_text="No discharging from 6 PM through 9 PM.",
                directive_type="no_discharge_window",
                hours=[18, 19, 20],
                expected_numeric=None,
                applies=True,
            )
        ],
    ),
    InterpretationCase(
        case_id="TIME-06",
        category="no_discharge_window",
        battery_capacity=200.0,
        notes=[
            ExpectedNote(
                note_text="Battery export to loads is blocked from noon to 2 PM.",
                directive_type="no_discharge_window",
                hours=[12, 13],
                expected_numeric=None,
                applies=True,
            )
        ],
    ),

    # ── Category 3: Reserve, absolute and relative ───────────────────────────
    InterpretationCase(
        case_id="RES-01",
        category="minimum_battery_reserve",
        battery_capacity=250.0,
        notes=[
            ExpectedNote(
                note_text="Keep at least 120 kWh in the battery from 6 PM until 9 PM.",
                directive_type="minimum_battery_reserve",
                hours=[18, 19, 20],
                expected_numeric=120.0,
                applies=True,
            )
        ],
    ),
    InterpretationCase(
        case_id="RES-02",
        category="minimum_battery_reserve",
        battery_capacity=250.0,
        notes=[
            ExpectedNote(
                note_text="Maintain a minimum of 40% of pack capacity from 17:00 to 20:00.",
                directive_type="minimum_battery_reserve",
                hours=[17, 18, 19],
                expected_numeric=100.0,  # 40% of 250 kWh
                applies=True,
            )
        ],
    ),
    InterpretationCase(
        case_id="RES-03",
        category="minimum_battery_reserve",
        battery_capacity=200.0,
        notes=[
            ExpectedNote(
                note_text="Hold half the battery in reserve between 7 PM and 10 PM.",
                directive_type="minimum_battery_reserve",
                hours=[19, 20, 21],
                expected_numeric=100.0,  # 50% of 200 kWh
                applies=True,
            )
        ],
    ),
    InterpretationCase(
        case_id="RES-04",
        category="minimum_battery_reserve",
        battery_capacity=200.0,
        notes=[
            ExpectedNote(
                note_text="Emergency backup needs no less than 75 kWh stored during the 8 PM hour.",
                directive_type="minimum_battery_reserve",
                hours=[20],
                expected_numeric=75.0,
                applies=True,
            )
        ],
    ),

    # ── Category 4: Grid cap ─────────────────────────────────────────────────
    InterpretationCase(
        case_id="GRID-01",
        category="max_grid_window",
        battery_capacity=200.0,
        notes=[
            ExpectedNote(
                note_text="Grid import must not exceed 155 kWh in any hour from 6 PM until 9 PM.",
                directive_type="max_grid_window",
                hours=[18, 19, 20],
                expected_numeric=155.0,
                applies=True,
            )
        ],
    ),
    InterpretationCase(
        case_id="GRID-02",
        category="max_grid_window",
        battery_capacity=200.0,
        notes=[
            ExpectedNote(
                note_text="Feeder limit caps intake at 180 kWh per hour between 7 PM and 9 PM.",
                directive_type="max_grid_window",
                hours=[19, 20],
                expected_numeric=180.0,
                applies=True,
            )
        ],
    ),
    InterpretationCase(
        case_id="GRID-03",
        category="max_grid_window",
        battery_capacity=200.0,
        notes=[
            ExpectedNote(
                note_text="Keep purchased power at or below 200 kWh hourly from 5 PM to 8 PM.",
                directive_type="max_grid_window",
                hours=[17, 18, 19],
                expected_numeric=200.0,
                applies=True,
            )
        ],
    ),

    # ── Category 5: Distractors (must be no_op) ──────────────────────────────
    InterpretationCase(
        case_id="DIST-01",
        category="distractor",
        battery_capacity=200.0,
        notes=[
            ExpectedNote(
                note_text="The registrar will publish exam routines next Tuesday.",
                directive_type="no_op",
                hours=[],
                expected_numeric=None,
                applies=False,
            )
        ],
    ),
    InterpretationCase(
        case_id="DIST-02",
        category="distractor",
        battery_capacity=200.0,
        notes=[
            ExpectedNote(
                note_text="Hostel maintenance is scheduled for the semester break.",
                directive_type="no_op",
                hours=[],
                expected_numeric=None,
                applies=False,
            )
        ],
    ),
    InterpretationCase(
        case_id="DIST-03",
        category="distractor",
        battery_capacity=200.0,
        notes=[
            ExpectedNote(
                note_text="Please submit vehicle passes to the security office.",
                directive_type="no_op",
                hours=[],
                expected_numeric=None,
                applies=False,
            )
        ],
    ),
    InterpretationCase(
        case_id="DIST-04",
        category="distractor",
        battery_capacity=200.0,
        notes=[
            ExpectedNote(
                note_text="Yesterday's solar generation was unusually high.",
                directive_type="no_op",
                hours=[],
                expected_numeric=None,
                applies=False,
            )
        ],
    ),
    InterpretationCase(
        case_id="DIST-05",
        category="distractor",
        battery_capacity=200.0,
        notes=[
            ExpectedNote(
                note_text="Next week the panels will be washed from 1 PM to 3 PM.",
                directive_type="no_op",
                hours=[],
                expected_numeric=None,
                applies=False,
            )
        ],
    ),
    InterpretationCase(
        case_id="DIST-06",
        category="distractor",
        battery_capacity=200.0,
        notes=[
            ExpectedNote(
                note_text="The generator will be load-tested at the Uttara campus.",
                directive_type="no_op",
                hours=[],
                expected_numeric=None,
                applies=False,
            )
        ],
    ),

    # ── Category 6: Multi-directive requests (3 notes in 1 request) ───────────
    InterpretationCase(
        case_id="MULTI-01",
        category="multi_directive",
        battery_capacity=200.0,
        notes=[
            ExpectedNote(
                note_text="Expect an 80% drop in rooftop generation during the 1-3 PM maintenance slot.",
                directive_type="solar_reduction",
                hours=[13, 14],
                expected_numeric=0.2,
                applies=True,
            ),
            ExpectedNote(
                note_text="Do not charge the battery between 14:00 and 16:00.",
                directive_type="no_charge_window",
                hours=[14, 15],
                expected_numeric=None,
                applies=True,
            ),
            ExpectedNote(
                note_text="The registrar will publish exam routines next Tuesday.",
                directive_type="no_op",
                hours=[],
                expected_numeric=None,
                applies=False,
            ),
        ],
    ),
    InterpretationCase(
        case_id="MULTI-02",
        category="multi_directive",
        battery_capacity=250.0,
        notes=[
            ExpectedNote(
                note_text="Keep at least 120 kWh in the battery from 6 PM until 9 PM.",
                directive_type="minimum_battery_reserve",
                hours=[18, 19, 20],
                expected_numeric=120.0,
                applies=True,
            ),
            ExpectedNote(
                note_text="Feeder limit caps intake at 180 kWh per hour between 7 PM and 9 PM.",
                directive_type="max_grid_window",
                hours=[19, 20],
                expected_numeric=180.0,
                applies=True,
            ),
            ExpectedNote(
                note_text="Hostel maintenance is scheduled for the semester break.",
                directive_type="no_op",
                hours=[],
                expected_numeric=None,
                applies=False,
            ),
        ],
    ),
    InterpretationCase(
        case_id="MULTI-03",
        category="multi_directive",
        battery_capacity=200.0,
        notes=[
            ExpectedNote(
                note_text="Charging unavailable from 10 PM to midnight.",
                directive_type="no_charge_window",
                hours=[22, 23],
                expected_numeric=None,
                applies=True,
            ),
            ExpectedNote(
                note_text="Battery export to loads is blocked from noon to 2 PM.",
                directive_type="no_discharge_window",
                hours=[12, 13],
                expected_numeric=None,
                applies=True,
            ),
            ExpectedNote(
                note_text="Please submit vehicle passes to the security office.",
                directive_type="no_op",
                hours=[],
                expected_numeric=None,
                applies=False,
            ),
        ],
    ),
    InterpretationCase(
        case_id="MULTI-04",
        category="multi_directive",
        battery_capacity=200.0,
        notes=[
            ExpectedNote(
                note_text="Keep at least 100 kWh in the battery from 6 PM until 10 PM.",
                directive_type="minimum_battery_reserve",
                hours=[18, 19, 20, 21],
                expected_numeric=100.0,
                applies=True,
            ),
            ExpectedNote(
                note_text="Grid import must not exceed 155 kWh in any hour from 7 PM until 9 PM.",
                directive_type="max_grid_window",
                hours=[19, 20],
                expected_numeric=155.0,
                applies=True,
            ),
            ExpectedNote(
                note_text="Yesterday's solar generation was unusually high.",
                directive_type="no_op",
                hours=[],
                expected_numeric=None,
                applies=False,
            ),
        ],
    ),
    InterpretationCase(
        case_id="MULTI-05",
        category="multi_directive",
        battery_capacity=200.0,
        notes=[
            ExpectedNote(
                note_text="Heavy cloud will halve solar between 10 AM and noon.",
                directive_type="solar_reduction",
                hours=[10, 11],
                expected_numeric=0.5,
                applies=True,
            ),
            ExpectedNote(
                note_text="Keep purchased power at or below 200 kWh hourly from 11 AM to 2 PM.",
                directive_type="max_grid_window",
                hours=[11, 12, 13],
                expected_numeric=200.0,
                applies=True,
            ),
            ExpectedNote(
                note_text="The generator will be load-tested at the Uttara campus.",
                directive_type="no_op",
                hours=[],
                expected_numeric=None,
                applies=False,
            ),
        ],
    ),
]


def make_request_body(case: InterpretationCase) -> dict[str, Any]:
    """Generate a standard 24h scenario JSON payload with given notes & capacity."""
    hours_data = []
    for h in range(24):
        hours_data.append({
            "hour": h,
            "demand_kwh": 100.0,
            "solar_kwh": 40.0 if 8 <= h < 16 else 0.0,
            "tariff_bdt_per_kwh": 5.0 if 0 <= h < 6 else (20.0 if 18 <= h < 22 else 10.0),
        })
    return {
        "scenario_id": f"ADV-{case.case_id}",
        "battery": {
            "capacity_kwh": case.battery_capacity,
            "initial_energy_kwh": case.battery_capacity * 0.5,
            "minimum_energy_kwh": case.battery_capacity * 0.1,
            "max_charge_kwh_per_hour": case.battery_capacity * 0.25,
            "max_discharge_kwh_per_hour": case.battery_capacity * 0.25,
        },
        "operator_notes": [n.note_text for n in case.notes],
        "hours": hours_data,
    }


def compare_directive(expected: ExpectedNote, actual: dict[str, Any]) -> tuple[bool, str]:
    """Compare one interpreted directive against expectation."""
    if actual.get("applies") != expected.applies:
        return False, f"applies mismatch: expected {expected.applies}, got {actual.get('applies')}"

    if actual.get("directive_type") != expected.directive_type:
        return False, f"type mismatch: expected {expected.directive_type}, got {actual.get('directive_type')}"

    adj = actual.get("structured_adjustment")
    if expected.directive_type == "no_op":
        if adj is not None:
            return False, f"expected structured_adjustment=None for no_op, got {adj}"
        return True, "OK"

    if adj is None:
        return False, "structured_adjustment is None for active directive"

    actual_hours = adj.get("hours", [])
    if list(actual_hours) != expected.hours:
        return False, f"hours mismatch: expected {expected.hours}, got {actual_hours}"

    if expected.expected_numeric is not None:
        field_map = {
            "solar_reduction": "factor",
            "minimum_battery_reserve": "minimum_energy_kwh",
            "max_grid_window": "max_grid_kwh",
        }
        val_key = field_map.get(expected.directive_type)
        if val_key:
            actual_val = adj.get(val_key)
            if actual_val is None:
                return False, f"missing numeric field {val_key}"
            if abs(float(actual_val) - expected.expected_numeric) > 0.01:
                return False, f"{val_key} mismatch: expected {expected.expected_numeric}, got {actual_val}"

    return True, "OK"


def run_interpretation_suite(base_url: str, delay: float) -> tuple[int, int, list[str]]:
    """Run all interpretation test cases over live HTTP."""
    print("\n" + "=" * 95)
    print("INTERPRETATION ROBUSTNESS SUITE (Adversarial Paraphrases, Time Expressions, Distractors)")
    print("=" * 95)
    print(f"{'Case ID':<10} {'Category':<22} {'Notes':<6} {'HTTP':<6} {'Result':<8} {'Latency':<8} {'Details'}")
    print("-" * 95)

    client = httpx.Client(timeout=30.0)
    passed_notes = 0
    total_notes = 0
    failures: list[str] = []

    for case in INTERPRETATION_CASES:
        payload = make_request_body(case)
        t0 = time.perf_counter()
        try:
            resp = client.post(f"{base_url}/optimize-energy", json=payload)
            lat = time.perf_counter() - t0
        except Exception as exc:
            lat = time.perf_counter() - t0
            print(f"{case.case_id:<10} {case.category:<22} {len(case.notes):<6} {'ERR':<6} {'FAIL':<8} {lat:6.2f}s {str(exc)}")
            for n in case.notes:
                failures.append(f"{case.case_id}: HTTP connection error {exc}")
                total_notes += 1
            time.sleep(delay)
            continue

        if resp.status_code != 200:
            print(f"{case.case_id:<10} {case.category:<22} {len(case.notes):<6} {resp.status_code:<6} {'FAIL':<8} {lat:6.2f}s Status code {resp.status_code}")
            for n in case.notes:
                failures.append(f"{case.case_id}: HTTP {resp.status_code}")
                total_notes += 1
            time.sleep(delay)
            continue

        data = resp.json()
        interp_list = data.get("directive_interpretation", [])
        case_passed = True
        case_details = []

        for idx, expected in enumerate(case.notes):
            total_notes += 1
            if idx >= len(interp_list):
                case_passed = False
                failures.append(f"{case.case_id} note {idx}: missing in response")
                case_details.append(f"note {idx} missing")
                continue

            ok, reason = compare_directive(expected, interp_list[idx])
            if ok:
                passed_notes += 1
            else:
                case_passed = False
                failures.append(f"{case.case_id} note {idx} ('{expected.note_text}'): {reason}")
                case_details.append(f"n{idx}:{reason}")

        status_str = "PASS" if case_passed else "FAIL"
        detail_str = "All notes matched" if case_passed else "; ".join(case_details)
        print(f"{case.case_id:<10} {case.category:<22} {len(case.notes):<6} {resp.status_code:<6} {status_str:<8} {lat:6.2f}s {detail_str}")

        if delay > 0:
            time.sleep(delay)

    return passed_notes, total_notes, failures


def run_validity_stress_suite() -> tuple[int, int, list[str]]:
    """Run all 19 stress scenarios through the optimizer, plan builder, and validator."""
    print("\n" + "=" * 95)
    print("SCENARIO STRESS FIXTURES SUITE (19 Extreme-but-valid Scenarios)")
    print("=" * 95)
    print(f"{'Fixture Name':<35} {'Opt Status':<12} {'Violations':<12} {'Cost BDT':<15} {'Status'}")
    print("-" * 95)

    passed = 0
    failures: list[str] = []

    for name, fn in STRESS_FIXTURES:
        sc, d = fn()
        cc = compile_constraints(sc, d)
        sol = optimize(sc, cc)
        if sol.status != "optimal":
            failures.append(f"{name}: optimizer status {sol.status}")
            print(f"{name:<35} {sol.status:<12} {'N/A':<12} {'N/A':<15} FAIL")
            continue

        plan, tg, tc, pk = build_plan(sc, cc, sol)
        violations = validate_plan(sc, cc, plan, tg, tc, pk)
        if len(violations) > 0:
            failures.append(f"{name}: {violations}")
            print(f"{name:<35} {sol.status:<12} {len(violations):<12} {tc:12.2f}    FAIL")
        else:
            passed += 1
            print(f"{name:<35} {sol.status:<12} {0:<12} {tc:12.2f}    PASS")

    return passed, len(STRESS_FIXTURES), failures


def main():
    parser = argparse.ArgumentParser(description="Step 20 Adversarial & Stress Test Suite")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Base URL of service")
    parser.add_argument("--delay", type=float, default=2.0, help="Pacing delay between calls")
    args = parser.parse_args()

    # 1. Run Interpretation Suite
    interp_passed, interp_total, interp_failures = run_interpretation_suite(args.base_url, args.delay)
    interp_rate = (interp_passed / interp_total) * 100 if interp_total else 0.0

    # 2. Run Validity Stress Suite
    stress_passed, stress_total, stress_failures = run_validity_stress_suite()
    stress_rate = (stress_passed / stress_total) * 100 if stress_total else 0.0

    # 3. Print Aggregate Summary
    print("\n" + "=" * 50)
    print("STEP 20 TEST SUMMARY")
    print("=" * 50)
    print(f"Interpretation Pass Rate: {interp_passed}/{interp_total} ({interp_rate:.1f}%) [Target >= 90%]")
    print(f"Validity Stress Pass Rate: {stress_passed}/{stress_total} ({stress_rate:.1f}%) [Target 100%]")
    print("=" * 50)

    if interp_failures:
        print("\nKnown Limitations / Residual Interpretation Mismatches:")
        for fail in interp_failures:
            print(f"  - {fail}")

    if stress_failures:
        print("\nValidity Stress Failures:")
        for fail in stress_failures:
            print(f"  - {fail}")
        sys.exit(1)

    if interp_rate < 90.0:
        print(f"\nInterpretation rate {interp_rate:.1f}% below target 90.0%")
        sys.exit(1)

    print("\nAll Step 20 criteria PASSED successfully!")
    sys.exit(0)


if __name__ == "__main__":
    main()
