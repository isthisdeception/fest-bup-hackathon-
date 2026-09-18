"""Step 12 validation: validator correctness + mutation tests."""
import copy
import sys

from app.directives import Directive, compile_constraints
from app.optimizer import optimize
from app.plan_builder import build_plan
from app.validator import validate_plan, build_safe_plan
from app.schemas import HourlyPlanEntry
from tests.fixtures import (
    flat_tariff_no_solar,
    two_tariff_arbitrage,
    no_charge_window_cheap,
    no_discharge_window_expensive,
    max_grid_cap,
    min_reserve,
    solar_reduction_scenario,
    negative_tariff,
)

errors = []

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


def build_fixture(fixture_fn):
    """Build a full plan from a fixture."""
    sc, dirs = fixture_fn()
    cc = compile_constraints(sc, dirs)
    opt = optimize(sc, cc)
    plan, tg, tc, pk = build_plan(sc, cc, opt)
    return sc, cc, plan, tg, tc, pk


# ── Part 1: All fixtures validate with zero violations ──

print("=== Part 1: Positive path (all fixtures valid) ===")
for name, fn in FIXTURES:
    sc, cc, plan, tg, tc, pk = build_fixture(fn)
    violations = validate_plan(sc, cc, plan, tg, tc, pk)
    ok = len(violations) == 0
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {len(violations)} violations")
    if not ok:
        for v in violations:
            print(f"  {v}")
        errors.append(f"{name}: {len(violations)} violations")


# ── Part 2: Mutation tests ──

print("\n=== Part 2: Mutation tests ===")
sc_base, cc_base, plan_base, tg_base, tc_base, pk_base = build_fixture(two_tariff_arbitrage)


def mutate_plan(plan, tg, tc, pk, mutator_fn):
    """Deep copy plan and apply mutation."""
    plan_copy = [HourlyPlanEntry(**e.model_dump()) for e in plan]
    tg_c, tc_c, pk_c = tg, tc, pk
    result = mutator_fn(plan_copy, tg_c, tc_c, pk_c)
    if result:
        return result
    return plan_copy, tg_c, tc_c, pk_c


def check_mutation(label, violation_prefix, mutator_fn):
    """Apply mutation and verify the validator catches it."""
    result = mutate_plan(plan_base, tg_base, tc_base, pk_base, mutator_fn)
    plan_m, tg_m, tc_m, pk_m = result
    violations = validate_plan(sc_base, cc_base, plan_m, tg_m, tc_m, pk_m)
    caught = any(violation_prefix in v for v in violations)
    ok = len(violations) > 0 and caught
    print(f"[{'PASS' if ok else 'FAIL'}] Mutation: {label} -> "
          f"{len(violations)} violations, caught={caught}")
    if not ok:
        errors.append(f"Mutation {label}: caught={caught}, violations={violations[:2]}")


# M1: Add 1.0 to one hour's grid_kwh -> total mismatch AND balance violation
def m1(plan, tg, tc, pk):
    p = plan[5]
    plan[5] = HourlyPlanEntry(hour=p.hour, grid_kwh=p.grid_kwh + 1.0,
                              solar_used_kwh=p.solar_used_kwh,
                              battery_action=p.battery_action,
                              battery_kwh=p.battery_kwh,
                              battery_energy_after_kwh=p.battery_energy_after_kwh)
    return plan, tg, tc, pk

check_mutation("grid_kwh +1 -> balance+total", "[V9]", m1)

# M2: solar_used above effective solar -> V8
def m2(plan, tg, tc, pk):
    p = plan[10]
    plan[10] = HourlyPlanEntry(hour=p.hour, grid_kwh=p.grid_kwh,
                               solar_used_kwh=cc_base.effective_solar[10] + 5.0,
                               battery_action=p.battery_action,
                               battery_kwh=p.battery_kwh,
                               battery_energy_after_kwh=p.battery_energy_after_kwh)
    return plan, tg, tc, pk

check_mutation("solar_used > effective -> V8", "[V8]", m2)

# M3: Change battery_energy_after_kwh -> V10
def m3(plan, tg, tc, pk):
    p = plan[12]
    plan[12] = HourlyPlanEntry(hour=p.hour, grid_kwh=p.grid_kwh,
                               solar_used_kwh=p.solar_used_kwh,
                               battery_action=p.battery_action,
                               battery_kwh=p.battery_kwh,
                               battery_energy_after_kwh=p.battery_energy_after_kwh + 50.0)
    return plan, tg, tc, pk

check_mutation("battery_energy_after changed -> V10", "[V10]", m3)

# M4: idle with battery_kwh=5 -> V7
def m4(plan, tg, tc, pk):
    p = plan[0]
    plan[0] = HourlyPlanEntry(hour=p.hour, grid_kwh=p.grid_kwh,
                              solar_used_kwh=p.solar_used_kwh,
                              battery_action="idle",
                              battery_kwh=5.0,
                              battery_energy_after_kwh=p.battery_energy_after_kwh)
    return plan, tg, tc, pk

check_mutation("idle with kwh=5 -> V7", "[V7]", m4)

# M5: charge above rate limit -> V13
def m5(plan, tg, tc, pk):
    p = plan[0]
    plan[0] = HourlyPlanEntry(hour=p.hour, grid_kwh=p.grid_kwh,
                              solar_used_kwh=p.solar_used_kwh,
                              battery_action="charge",
                              battery_kwh=sc_base.max_charge_kwh_per_hour + 10.0,
                              battery_energy_after_kwh=p.battery_energy_after_kwh)
    return plan, tg, tc, pk

check_mutation("charge > rate limit -> V13", "[V13]", m5)

# M6: charge in no_charge_window -> V15
sc_nc, dirs_nc = no_charge_window_cheap()
cc_nc = compile_constraints(sc_nc, dirs_nc)
opt_nc = optimize(sc_nc, cc_nc)
plan_nc, tg_nc, tc_nc, pk_nc = build_plan(sc_nc, cc_nc, opt_nc)

def m6(plan, tg, tc, pk):
    # Force charge in hour 2 (which is in the no_charge window 0-5)
    p = plan[2]
    plan[2] = HourlyPlanEntry(hour=p.hour, grid_kwh=p.grid_kwh,
                              solar_used_kwh=p.solar_used_kwh,
                              battery_action="charge",
                              battery_kwh=10.0,
                              battery_energy_after_kwh=p.battery_energy_after_kwh)
    return plan, tg, tc, pk

plan_nc_m = [HourlyPlanEntry(**e.model_dump()) for e in plan_nc]
result_m6 = m6(plan_nc_m, tg_nc, tc_nc, pk_nc)
v6 = validate_plan(sc_nc, cc_nc, *result_m6)
caught_v15 = any("[V15]" in v for v in v6)
print(f"[{'PASS' if caught_v15 else 'FAIL'}] Mutation: charge in no_charge -> V15 "
      f"({len(v6)} violations)")
if not caught_v15:
    errors.append(f"M6: {v6[:2]}")

# M7: discharge in no_discharge_window -> V16
sc_nd, dirs_nd = no_discharge_window_expensive()
cc_nd = compile_constraints(sc_nd, dirs_nd)
opt_nd = optimize(sc_nd, cc_nd)
plan_nd, tg_nd, tc_nd, pk_nd = build_plan(sc_nd, cc_nd, opt_nd)

plan_nd_m = [HourlyPlanEntry(**e.model_dump()) for e in plan_nd]
p = plan_nd_m[19]
plan_nd_m[19] = HourlyPlanEntry(hour=p.hour, grid_kwh=p.grid_kwh,
                                solar_used_kwh=p.solar_used_kwh,
                                battery_action="discharge",
                                battery_kwh=10.0,
                                battery_energy_after_kwh=p.battery_energy_after_kwh)
v7 = validate_plan(sc_nd, cc_nd, plan_nd_m, tg_nd, tc_nd, pk_nd)
caught_v16 = any("[V16]" in v for v in v7)
print(f"[{'PASS' if caught_v16 else 'FAIL'}] Mutation: discharge in no_discharge -> V16 "
      f"({len(v7)} violations)")
if not caught_v16:
    errors.append(f"M7: {v7[:2]}")

# M8: exceed max_grid_window -> V17
sc_gc, dirs_gc = max_grid_cap()
cc_gc = compile_constraints(sc_gc, dirs_gc)
opt_gc = optimize(sc_gc, cc_gc)
plan_gc, tg_gc, tc_gc, pk_gc = build_plan(sc_gc, cc_gc, opt_gc)

plan_gc_m = [HourlyPlanEntry(**e.model_dump()) for e in plan_gc]
p = plan_gc_m[12]
plan_gc_m[12] = HourlyPlanEntry(hour=p.hour, grid_kwh=100.0,  # cap is 80
                                solar_used_kwh=p.solar_used_kwh,
                                battery_action=p.battery_action,
                                battery_kwh=p.battery_kwh,
                                battery_energy_after_kwh=p.battery_energy_after_kwh)
v8 = validate_plan(sc_gc, cc_gc, plan_gc_m, tg_gc, tc_gc, pk_gc)
caught_v17 = any("[V17]" in v for v in v8)
print(f"[{'PASS' if caught_v17 else 'FAIL'}] Mutation: grid > cap -> V17 "
      f"({len(v8)} violations)")
if not caught_v17:
    errors.append(f"M8: {v8[:2]}")

# M9: drop one hour -> V1+V2
def m9(plan, tg, tc, pk):
    del plan[5]  # remove hour 5
    return plan, tg, tc, pk

check_mutation("drop hour -> V1+V2", "[V1]", m9)

# M10: set final battery_energy to initial-50 -> V10+V14
def m10(plan, tg, tc, pk):
    p = plan[23]
    plan[23] = HourlyPlanEntry(hour=p.hour, grid_kwh=p.grid_kwh,
                               solar_used_kwh=p.solar_used_kwh,
                               battery_action=p.battery_action,
                               battery_kwh=p.battery_kwh,
                               battery_energy_after_kwh=sc_base.initial_energy_kwh - 50.0)
    return plan, tg, tc, pk

check_mutation("final E - 50 -> V10+V14", "[V10]", m10)

# M11: inject NaN -> V4
def m11(plan, tg, tc, pk):
    p = plan[7]
    plan[7] = HourlyPlanEntry(hour=p.hour, grid_kwh=float('nan'),
                              solar_used_kwh=p.solar_used_kwh,
                              battery_action=p.battery_action,
                              battery_kwh=p.battery_kwh,
                              battery_energy_after_kwh=p.battery_energy_after_kwh)
    return plan, tg, tc, pk

check_mutation("NaN grid -> V4", "[V4]", m11)


# ── Part 3: No imports from optimizer or plan_builder ──
print("\n=== Part 3: Independence check ===")
import inspect
source = inspect.getsource(validate_plan)
module_source = open("app/validator.py").read()
no_optimizer = "from app.optimizer" not in module_source and "import app.optimizer" not in module_source
no_plan_builder = "from app.plan_builder" not in module_source and "import app.plan_builder" not in module_source
print(f"[{'PASS' if no_optimizer else 'FAIL'}] No optimizer import")
print(f"[{'PASS' if no_plan_builder else 'FAIL'}] No plan_builder import")
if not no_optimizer:
    errors.append("validator imports optimizer")
if not no_plan_builder:
    errors.append("validator imports plan_builder")


# ── Part 4: Safe plan validates ──
print("\n=== Part 4: Safe plan validation ===")
sc_safe, _ = flat_tariff_no_solar()
cc_safe = compile_constraints(sc_safe, [])
safe_plan, safe_tg, safe_tc, safe_pk = build_safe_plan(sc_safe, cc_safe)
safe_v = validate_plan(sc_safe, cc_safe, safe_plan, safe_tg, safe_tc, safe_pk)
print(f"[{'PASS' if len(safe_v) == 0 else 'FAIL'}] Safe plan: {len(safe_v)} violations")
if safe_v:
    for v in safe_v:
        print(f"  {v}")
    errors.append(f"Safe plan: {safe_v[:2]}")


# ── Summary ──
if errors:
    print(f"\nFAILED ({len(errors)} issues):")
    for e in errors:
        print(f"  - {e}")
    sys.exit(1)
else:
    print("\nAll validator tests PASSED")
