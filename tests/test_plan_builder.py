"""Step 11 validation: plan builder correctness across all fixtures."""
import json
import math
import sys

from app.directives import compile_constraints
from app.optimizer import optimize
from app.plan_builder import build_plan
from tests.fixtures import (
    flat_tariff_no_solar,
    two_tariff_arbitrage,
    no_charge_window_cheap,
    no_discharge_window_expensive,
    max_grid_cap,
    min_reserve,
    solar_reduction_scenario,
    negative_tariff,
    infeasible_directives,
)

errors = []
TOL = 1e-6

FIXTURES = [
    ("flat_tariff", flat_tariff_no_solar),
    ("two_tariff", two_tariff_arbitrage),
    ("no_charge", no_charge_window_cheap),
    ("no_discharge", no_discharge_window_expensive),
    ("grid_cap", max_grid_cap),
    ("min_reserve", min_reserve),
    ("solar_red", solar_reduction_scenario),
    ("neg_tariff", negative_tariff),
]


def check(cond, msg):
    if not cond:
        errors.append(msg)
        return False
    return True


for name, fixture_fn in FIXTURES:
    sc, dirs = fixture_fn()
    cc = compile_constraints(sc, dirs)
    opt = optimize(sc, cc)
    plan, total_grid, total_cost, peak_grid = build_plan(sc, cc, opt)

    prefix = f"[{name}]"
    ok = True

    # 1. len == 24 and hours 0..23
    if not check(len(plan) == 24, f"{prefix} len={len(plan)} != 24"):
        ok = False
    hours = [e.hour for e in plan]
    if not check(hours == list(range(24)), f"{prefix} hours mismatch"):
        ok = False

    # 2. Energy balance per hour
    for e in plan:
        charge = e.battery_kwh if e.battery_action == "charge" else 0.0
        discharge = e.battery_kwh if e.battery_action == "discharge" else 0.0
        balance = e.grid_kwh + e.solar_used_kwh + discharge - sc.demand[e.hour] - charge
        if not check(abs(balance) <= TOL,
                     f"{prefix} h{e.hour} balance={balance:.9f}"):
            ok = False

    # 3. idle implies battery_kwh == 0
    for e in plan:
        if e.battery_action == "idle":
            if not check(e.battery_kwh == 0.0,
                         f"{prefix} h{e.hour} idle but kwh={e.battery_kwh}"):
                ok = False

    # 4. Non-negative values
    for e in plan:
        for field, val in [("grid_kwh", e.grid_kwh),
                           ("solar_used_kwh", e.solar_used_kwh),
                           ("battery_kwh", e.battery_kwh)]:
            if not check(val >= -TOL,
                         f"{prefix} h{e.hour} {field}={val} < 0"):
                ok = False

    # 5. battery_energy_after[23] == initial
    if not check(abs(plan[23].battery_energy_after_kwh - sc.initial_energy_kwh) <= 1e-9,
                 f"{prefix} neutrality: E[23]={plan[23].battery_energy_after_kwh} "
                 f"!= E0={sc.initial_energy_kwh}"):
        ok = False

    # 6. total_cost matches sum
    recomputed_cost = sum(plan[h].grid_kwh * sc.tariff[h] for h in range(24))
    if not check(abs(total_cost - recomputed_cost) <= TOL,
                 f"{prefix} cost: reported={total_cost} recomputed={recomputed_cost}"):
        ok = False

    # 7. total_grid and peak match
    recomputed_grid = sum(e.grid_kwh for e in plan)
    recomputed_peak = max(e.grid_kwh for e in plan)
    if not check(abs(total_grid - recomputed_grid) <= TOL,
                 f"{prefix} total_grid mismatch"):
        ok = False
    if not check(abs(peak_grid - recomputed_peak) <= TOL,
                 f"{prefix} peak_grid mismatch"):
        ok = False

    # 8. All finite
    for e in plan:
        for val in [e.grid_kwh, e.solar_used_kwh, e.battery_kwh,
                    e.battery_energy_after_kwh]:
            if not check(math.isfinite(val),
                         f"{prefix} h{e.hour} non-finite value {val}"):
                ok = False
    for val, label in [(total_grid, "total_grid"), (total_cost, "total_cost"),
                       (peak_grid, "peak_grid")]:
        if not check(math.isfinite(val), f"{prefix} {label} non-finite"):
            ok = False

    # 9. Cost within 0.01 of LP optimum
    if not check(abs(total_cost - opt.optimal_cost) <= 0.01,
                 f"{prefix} cost drift: plan={total_cost} opt={opt.optimal_cost}"):
        ok = False

    # 10. JSON serializable (no NaN/inf)
    plan_dicts = [e.model_dump() for e in plan]
    try:
        json.dumps(plan_dicts)
        json_ok = True
    except (ValueError, TypeError) as exc:
        json_ok = False
        errors.append(f"{prefix} JSON serialization failed: {exc}")
    check(json_ok, f"{prefix} JSON serialization")

    print(f"[{'PASS' if ok and json_ok else 'FAIL'}] {name}: "
          f"cost={total_cost:.2f} grid={total_grid:.2f} peak={peak_grid:.2f}")


if errors:
    print(f"\nFAILED ({len(errors)} issues):")
    for e in errors:
        print(f"  - {e}")
    sys.exit(1)
else:
    print(f"\nAll plan builder checks PASSED across {len(FIXTURES)} fixtures")
