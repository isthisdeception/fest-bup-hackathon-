"""Step 10 validation: 10 offline optimizer tests."""
import sys
import time

from app.directives import compile_constraints
from app.optimizer import optimize
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


def check(num, cond, msg=""):
    label = f"[{'PASS' if cond else 'FAIL'}] Test {num:02d}"
    if msg:
        label += f": {msg}"
    print(label)
    if not cond:
        errors.append(f"Test {num}: {msg}")


# ── 1. Flat tariff, no solar, no directives ──
sc, dirs = flat_tariff_no_solar()
cc = compile_constraints(sc, dirs)
r1 = optimize(sc, cc)
expected_cost = sum(sc.demand) * sc.tariff[0]  # 2400 * 10 = 24000
check(1, abs(r1.optimal_cost - expected_cost) < TOL and
      all(abs(c) < TOL for c in r1.c) and
      all(abs(d) < TOL for d in r1.d),
      f"cost={r1.optimal_cost} expected={expected_cost} sum(c)={sum(r1.c):.6f}")

# ── 2. Two-tariff arbitrage ──
sc2, dirs2 = two_tariff_arbitrage()
cc2 = compile_constraints(sc2, dirs2)
r2 = optimize(sc2, cc2)
sum_c = sum(r2.c)
sum_d = sum(r2.d)
check(2, abs(sum_c - sum_d) < TOL and
      sum_c > TOL and  # some arbitrage happened
      r2.optimal_cost < expected_cost,  # cheaper than flat
      f"sum(c)={sum_c:.4f} sum(d)={sum_d:.4f} cost={r2.optimal_cost:.2f}")

# ── 3. No-charge window forces c[h]==0 in cheap hours 0-5 ──
sc3, dirs3 = no_charge_window_cheap()
cc3 = compile_constraints(sc3, dirs3)
r3 = optimize(sc3, cc3)
no_charge_ok = all(abs(r3.c[h]) < TOL for h in range(6))
check(3, no_charge_ok,
      f"c[0:6]={[round(r3.c[h], 6) for h in range(6)]}")

# ── 4. No-discharge window forces d[h]==0 in expensive hours 18-21 ──
sc4, dirs4 = no_discharge_window_expensive()
cc4 = compile_constraints(sc4, dirs4)
r4 = optimize(sc4, cc4)
no_discharge_ok = all(abs(r4.d[h]) < TOL for h in range(18, 22))
check(4, no_discharge_ok,
      f"d[18:22]={[round(r4.d[h], 6) for h in range(18, 22)]}")

# ── 5. Max grid cap of 80 yields g[h] <= 80 + tol in hours 10-14 ──
sc5, dirs5 = max_grid_cap()
cc5 = compile_constraints(sc5, dirs5)
r5 = optimize(sc5, cc5)
grid_cap_ok = all(r5.g[h] <= 80.0 + TOL for h in range(10, 15))
check(5, grid_cap_ok,
      f"g[10:15]={[round(r5.g[h], 4) for h in range(10, 15)]}")

# ── 6. Minimum battery reserve of 80 yields E[h] >= 80 - tol ──
sc6, dirs6 = min_reserve()
cc6 = compile_constraints(sc6, dirs6)
r6 = optimize(sc6, cc6)
# Compute E[h] = E0 + sum_{k=0..h}(c[k] - d[k])
E0 = sc6.initial_energy_kwh
battery_state = []
cumul = 0.0
for h in range(24):
    cumul += r6.c[h] - r6.d[h]
    battery_state.append(E0 + cumul)
reserve_ok = all(battery_state[h] >= 80.0 - TOL for h in range(10, 15))
check(6, reserve_ok,
      f"E[10:15]={[round(battery_state[h], 4) for h in range(10, 15)]}")

# ── 7. Solar reduction factor 0.2 yields s[h] <= 0.2 * solar + tol ──
sc7, dirs7 = solar_reduction_scenario()
cc7 = compile_constraints(sc7, dirs7)
r7 = optimize(sc7, cc7)
solar_ok = all(
    r7.s[h] <= 0.2 * sc7.solar[h] + TOL
    for h in range(8, 15)
)
check(7, solar_ok,
      f"s[8:15]={[round(r7.s[h], 4) for h in range(8, 15)]}, "
      f"limit={[0.2*sc7.solar[h] for h in range(8, 15)]}")

# ── 8. Negative tariff: no unbounded result ──
sc8, dirs8 = negative_tariff()
cc8 = compile_constraints(sc8, dirs8)
r8 = optimize(sc8, cc8)
neg_ok = (r8.status == "optimal" and
          all(r8.g[h] <= sc8.demand[h] + sc8.max_charge_kwh_per_hour + TOL
              for h in range(24)))
check(8, neg_ok,
      f"status={r8.status} g[12]={r8.g[12]:.4f} "
      f"bound={sc8.demand[12] + sc8.max_charge_kwh_per_hour}")

# ── 9. Infeasible directives trigger relaxation ladder ──
sc9, dirs9 = infeasible_directives()
cc9 = compile_constraints(sc9, dirs9)
r9 = optimize(sc9, cc9)
infeasible_ok = (len(r9.relaxations) > 0 and
                 r9.status in ("optimal", "fallback_trivial"))
check(9, infeasible_ok,
      f"status={r9.status} relaxations={r9.relaxations[:2]}")

# ── 10. Solve wall-time under 50 ms ──
sc10, dirs10 = flat_tariff_no_solar()
cc10 = compile_constraints(sc10, dirs10)
start = time.perf_counter()
_ = optimize(sc10, cc10)
elapsed_ms = (time.perf_counter() - start) * 1000
check(10, elapsed_ms < 50.0,
      f"elapsed={elapsed_ms:.1f} ms")


# ── Summary ──
if errors:
    print(f"\nFAILED ({len(errors)} issues):")
    for e in errors:
        print(f"  - {e}")
    sys.exit(1)
else:
    print("\nAll 10 optimizer tests PASSED")
