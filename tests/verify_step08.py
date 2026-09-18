"""Step 08 verification: directive model and constraint compilation."""
import sys

# ── 1. Module imports cleanly ──
from app.directives import (
    Directive, CompiledConstraints, compile_constraints,
    interpretation_entries, summarize, DirectiveCompilationError,
)
from app.schemas import Scenario
print("[PASS] Module imports cleanly")

errors = []

# Helper: build a simple 24-hour scenario
def make_scenario(
    solar=None, demand=None, min_energy=20.0, max_charge=50.0, max_discharge=50.0,
    capacity=200.0, initial=100.0,
):
    return Scenario(
        scenario_id="TEST",
        operator_notes=["test"],
        demand=demand or [100.0] * 24,
        solar=solar or [10.0] * 24,
        tariff=[5.0] * 24,
        capacity_kwh=capacity,
        initial_energy_kwh=initial,
        minimum_energy_kwh=min_energy,
        max_charge_kwh_per_hour=max_charge,
        max_discharge_kwh_per_hour=max_discharge,
    )


# ── 2. solar_reduction on hours [12,13] with factor=0.25 ──
sc = make_scenario(solar=[10.0]*24, min_energy=20.0, max_charge=50.0, max_discharge=50.0)
d = Directive(note_index=0, directive_type="solar_reduction", hours=(12, 13), factor=0.25, explanation="test")
cc = compile_constraints(sc, [d])
if abs(cc.effective_solar[12] - 2.5) > 1e-9:
    errors.append(f"solar_reduction h12: expected 2.5, got {cc.effective_solar[12]}")
if abs(cc.effective_solar[11] - 10.0) > 1e-9:
    errors.append(f"solar_reduction h11: expected 10.0, got {cc.effective_solar[11]}")
print(f"[{'PASS' if not errors else 'FAIL'}] solar_reduction: h12={cc.effective_solar[12]}, h11={cc.effective_solar[11]}")

# ── 3. Two overlapping solar_reduction (0.5 * 0.5 = 0.25 on h12, solar=10 -> 2.5) ──
d1 = Directive(note_index=0, directive_type="solar_reduction", hours=(12,), factor=0.5, explanation="first")
d2 = Directive(note_index=1, directive_type="solar_reduction", hours=(12,), factor=0.5, explanation="second")
cc2 = compile_constraints(sc, [d1, d2])
if abs(cc2.effective_solar[12] - 2.5) > 1e-9:
    errors.append(f"overlap solar h12: expected 2.5, got {cc2.effective_solar[12]}")
print(f"[{'PASS' if abs(cc2.effective_solar[12] - 2.5) < 1e-9 else 'FAIL'}] overlapping solar_reduction: h12={cc2.effective_solar[12]}")

# ── 4. minimum_battery_reserve of 100 with base min 40 ──
sc3 = make_scenario(min_energy=40.0)
d3 = Directive(note_index=0, directive_type="minimum_battery_reserve", hours=(10, 11, 12), minimum_energy_kwh=100.0, explanation="test")
cc3 = compile_constraints(sc3, [d3])
if abs(cc3.reserve[10] - 100.0) > 1e-9:
    errors.append(f"reserve h10: expected 100.0, got {cc3.reserve[10]}")
if abs(cc3.reserve[9] - 40.0) > 1e-9:
    errors.append(f"reserve h9: expected 40.0, got {cc3.reserve[9]}")
print(f"[{'PASS' if abs(cc3.reserve[10] - 100.0) < 1e-9 and abs(cc3.reserve[9] - 40.0) < 1e-9 else 'FAIL'}] min_battery_reserve: h10={cc3.reserve[10]}, h9={cc3.reserve[9]}")

# ── 5. max_grid_window of 155 ──
sc4 = make_scenario(demand=[100.0]*24, max_charge=50.0)
d4 = Directive(note_index=0, directive_type="max_grid_window", hours=(5, 6, 7), max_grid_kwh=155.0, explanation="test")
cc4 = compile_constraints(sc4, [d4])
# Inside window: min(100+50, 155) = 150 -> but wait, 155 > 150 so grid_ub stays at 150
# Actually: default grid_ub = demand[h] + max_charge = 100 + 50 = 150
# min(150, 155) = 150, so it stays 150
# Let me check with demand=200: default = 200+50 = 250, min(250,155) = 155
sc4b = make_scenario(demand=[200.0]*24, max_charge=50.0)
cc4b = compile_constraints(sc4b, [d4])
if abs(cc4b.grid_ub[5] - 155.0) > 1e-9:
    errors.append(f"grid_ub h5: expected 155.0, got {cc4b.grid_ub[5]}")
outside_expected = 200.0 + 50.0  # 250
if abs(cc4b.grid_ub[4] - outside_expected) > 1e-9:
    errors.append(f"grid_ub h4: expected {outside_expected}, got {cc4b.grid_ub[4]}")
print(f"[{'PASS' if abs(cc4b.grid_ub[5] - 155.0) < 1e-9 and abs(cc4b.grid_ub[4] - outside_expected) < 1e-9 else 'FAIL'}] max_grid_window: h5={cc4b.grid_ub[5]}, h4={cc4b.grid_ub[4]}")

# ── 6. interpretation_entries on no_op -> applies=False, structured_adjustment=None ──
d_noop = Directive(note_index=0, directive_type="no_op", hours=(), explanation="irrelevant note")
entries = interpretation_entries([d_noop])
if len(entries) != 1:
    errors.append(f"interpretation_entries: expected 1 entry, got {len(entries)}")
elif entries[0].applies is not False:
    errors.append(f"no_op applies: expected False, got {entries[0].applies}")
elif entries[0].structured_adjustment is not None:
    errors.append(f"no_op adjustment: expected None, got {entries[0].structured_adjustment}")
print(f"[{'PASS' if entries and entries[0].applies is False and entries[0].structured_adjustment is None else 'FAIL'}] no_op -> applies=False, adjustment=None")

# ── 7. summarize produces a string ──
summary = summarize(
    directives=[d],
    scenario=sc,
    total_grid_kwh=2692.5,
    total_cost_bdt=38365.0,
    peak_grid_kwh=175.0,
)
if not isinstance(summary, str) or len(summary) == 0:
    errors.append("summarize returned empty or non-string")
if len(summary) > 400:
    errors.append(f"summary too long: {len(summary)} chars")
print(f"[PASS] summarize: {summary[:80]}...")

# ── 8. no_charge_window and no_discharge_window ──
d_nc = Directive(note_index=0, directive_type="no_charge_window", hours=(14, 15), explanation="test")
d_nd = Directive(note_index=1, directive_type="no_discharge_window", hours=(16, 17), explanation="test")
cc5 = compile_constraints(sc, [d_nc, d_nd])
if cc5.charge_ub[14] != 0.0:
    errors.append(f"no_charge h14: expected 0.0, got {cc5.charge_ub[14]}")
if cc5.charge_ub[13] != 50.0:
    errors.append(f"charge h13: expected 50.0, got {cc5.charge_ub[13]}")
if cc5.discharge_ub[16] != 0.0:
    errors.append(f"no_discharge h16: expected 0.0, got {cc5.discharge_ub[16]}")
if cc5.discharge_ub[15] != 50.0:
    errors.append(f"discharge h15: expected 50.0, got {cc5.discharge_ub[15]}")
print(f"[{'PASS' if cc5.charge_ub[14] == 0.0 and cc5.discharge_ub[16] == 0.0 else 'FAIL'}] no_charge/no_discharge windows")

# ── Summary ──
if errors:
    print(f"\nFAILED ({len(errors)} issues):")
    for e in errors:
        print(f"  - {e}")
    sys.exit(1)
else:
    print("\nAll Step 08 checks PASSED")
