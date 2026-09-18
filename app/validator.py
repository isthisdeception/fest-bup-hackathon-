"""
Independent final schedule validator.

Replays the finished plan against the original request and compiled
constraints, re-verifying every rule from Problem Statement Sections 09
and 11.3. Returns a list of violation strings (empty = valid).

INDEPENDENCE REQUIREMENT: This module imports nothing from optimizer.py
or plan_builder.py and shares no helper code with them. A shared helper
would let a shared bug pass both.

Source: Problem Statement Section 08 (final replay);
        Participant Guide Section 09 (violation classes).
"""

import math
import logging

from app.directives import CompiledConstraints
from app.schemas import HourlyPlanEntry, Scenario

logger = logging.getLogger(__name__)

TOL = 0.01       # Problem Statement Section 11.5
STRICT_TOL = 1e-6  # Internal arithmetic checks


def validate_plan(
    scenario: Scenario,
    constraints: CompiledConstraints,
    plan: list[HourlyPlanEntry],
    total_grid_kwh: float,
    total_cost_bdt: float,
    peak_grid_kwh: float,
) -> list[str]:
    """Validate the finished plan against all rules.

    Returns human-readable violation strings. Empty list means valid.
    """
    violations: list[str] = []

    # ── Structure (Section 11.3) ──────────────────────────────────────

    # 1. Exactly 24 entries
    if len(plan) != 24:
        violations.append(f"[V1] Expected 24 entries, got {len(plan)}")
        return violations  # can't proceed without 24 entries

    # 2. Hours are exactly {0..23}
    hour_values = [e.hour for e in plan]
    hour_set = set(hour_values)
    if hour_set != set(range(24)):
        missing = set(range(24)) - hour_set
        extra = hour_set - set(range(24))
        violations.append(
            f"[V2] Hour set mismatch: missing={sorted(missing)}, "
            f"extra={sorted(extra)}"
        )

    # 3. Sorted ascending by hour
    if hour_values != sorted(hour_values):
        violations.append("[V3] Entries not sorted ascending by hour")

    # 4. Every numeric field is finite
    for e in plan:
        for field, val in [
            ("grid_kwh", e.grid_kwh),
            ("solar_used_kwh", e.solar_used_kwh),
            ("battery_kwh", e.battery_kwh),
            ("battery_energy_after_kwh", e.battery_energy_after_kwh),
        ]:
            if not math.isfinite(val):
                violations.append(
                    f"[V4] h{e.hour} {field}={val} is not finite"
                )

    # 5. Non-negative values
    for e in plan:
        for field, val in [
            ("grid_kwh", e.grid_kwh),
            ("solar_used_kwh", e.solar_used_kwh),
            ("battery_kwh", e.battery_kwh),
            ("battery_energy_after_kwh", e.battery_energy_after_kwh),
        ]:
            if val < -TOL:
                violations.append(
                    f"[V5] h{e.hour} {field}={val:.6f} is negative"
                )

    # 6. battery_action valid
    valid_actions = {"charge", "discharge", "idle"}
    for e in plan:
        if e.battery_action not in valid_actions:
            violations.append(
                f"[V6] h{e.hour} battery_action={e.battery_action!r} invalid"
            )

    # 7. idle implies battery_kwh ~= 0
    for e in plan:
        if e.battery_action == "idle" and abs(e.battery_kwh) > TOL:
            violations.append(
                f"[V7] h{e.hour} idle but battery_kwh={e.battery_kwh:.6f}"
            )

    # ── Energy (Sections 9.4, 9.5) ───────────────────────────────────

    # Build index for O(1) lookup
    by_hour = {e.hour: e for e in plan}

    # 8. solar_used <= effective_solar
    for h in range(24):
        e = by_hour.get(h)
        if e is None:
            continue
        if e.solar_used_kwh > constraints.effective_solar[h] + TOL:
            violations.append(
                f"[V8] h{h} solar_used={e.solar_used_kwh:.6f} > "
                f"effective_solar={constraints.effective_solar[h]:.6f}"
            )

    # 9. Energy balance: grid + solar + discharge == demand + charge
    for h in range(24):
        e = by_hour.get(h)
        if e is None:
            continue
        charge = e.battery_kwh if e.battery_action == "charge" else 0.0
        discharge = e.battery_kwh if e.battery_action == "discharge" else 0.0
        lhs = e.grid_kwh + e.solar_used_kwh + discharge
        rhs = scenario.demand[h] + charge
        if abs(lhs - rhs) > TOL:
            violations.append(
                f"[V9] h{h} balance: grid({e.grid_kwh:.6f}) + "
                f"solar({e.solar_used_kwh:.6f}) + discharge({discharge:.6f}) "
                f"= {lhs:.6f} != demand({scenario.demand[h]:.6f}) + "
                f"charge({charge:.6f}) = {rhs:.6f}"
            )

    # ── Battery (Sections 9.1, 9.2, 9.3, 9.6) ───────────────────────

    # 10. Replay battery state from initial
    E = scenario.initial_energy_kwh
    for h in range(24):
        e = by_hour.get(h)
        if e is None:
            continue
        charge = e.battery_kwh if e.battery_action == "charge" else 0.0
        discharge = e.battery_kwh if e.battery_action == "discharge" else 0.0
        E = E + charge - discharge
        if abs(E - e.battery_energy_after_kwh) > TOL:
            violations.append(
                f"[V10] h{h} battery state: replayed={E:.6f} != "
                f"reported={e.battery_energy_after_kwh:.6f}"
            )

    # 11. battery_energy_after >= reserve
    for h in range(24):
        e = by_hour.get(h)
        if e is None:
            continue
        if e.battery_energy_after_kwh < constraints.reserve[h] - TOL:
            violations.append(
                f"[V11] h{h} battery_energy_after={e.battery_energy_after_kwh:.6f} < "
                f"reserve={constraints.reserve[h]:.6f}"
            )

    # 12. battery_energy_after <= capacity
    for h in range(24):
        e = by_hour.get(h)
        if e is None:
            continue
        if e.battery_energy_after_kwh > scenario.capacity_kwh + TOL:
            violations.append(
                f"[V12] h{h} battery_energy_after={e.battery_energy_after_kwh:.6f} > "
                f"capacity={scenario.capacity_kwh:.6f}"
            )

    # 13. Charge/discharge within rate limits
    for h in range(24):
        e = by_hour.get(h)
        if e is None:
            continue
        charge = e.battery_kwh if e.battery_action == "charge" else 0.0
        discharge = e.battery_kwh if e.battery_action == "discharge" else 0.0
        if charge > scenario.max_charge_kwh_per_hour + TOL:
            violations.append(
                f"[V13] h{h} charge={charge:.6f} > "
                f"max_charge={scenario.max_charge_kwh_per_hour:.6f}"
            )
        if discharge > scenario.max_discharge_kwh_per_hour + TOL:
            violations.append(
                f"[V13] h{h} discharge={discharge:.6f} > "
                f"max_discharge={scenario.max_discharge_kwh_per_hour:.6f}"
            )

    # 14. End-of-day neutrality
    last = by_hour.get(23)
    if last is not None:
        if abs(last.battery_energy_after_kwh - scenario.initial_energy_kwh) > TOL:
            violations.append(
                f"[V14] end-of-day: E[23]={last.battery_energy_after_kwh:.6f} != "
                f"E0={scenario.initial_energy_kwh:.6f}"
            )

    # ── Directive-specific (Section 5.3) ──────────────────────────────

    # 15. charge <= charge_ub (catches violated no_charge_window)
    for h in range(24):
        e = by_hour.get(h)
        if e is None:
            continue
        charge = e.battery_kwh if e.battery_action == "charge" else 0.0
        if charge > constraints.charge_ub[h] + TOL:
            violations.append(
                f"[V15] h{h} charge={charge:.6f} > "
                f"charge_ub={constraints.charge_ub[h]:.6f}"
            )

    # 16. discharge <= discharge_ub (catches violated no_discharge_window)
    for h in range(24):
        e = by_hour.get(h)
        if e is None:
            continue
        discharge = e.battery_kwh if e.battery_action == "discharge" else 0.0
        if discharge > constraints.discharge_ub[h] + TOL:
            violations.append(
                f"[V16] h{h} discharge={discharge:.6f} > "
                f"discharge_ub={constraints.discharge_ub[h]:.6f}"
            )

    # 17. grid_kwh <= grid_ub (catches violated max_grid_window)
    for h in range(24):
        e = by_hour.get(h)
        if e is None:
            continue
        if e.grid_kwh > constraints.grid_ub[h] + TOL:
            violations.append(
                f"[V17] h{h} grid_kwh={e.grid_kwh:.6f} > "
                f"grid_ub={constraints.grid_ub[h]:.6f}"
            )

    # ── Totals (Section 11.3) ─────────────────────────────────────────

    # 18. sum(grid_kwh) == total_grid_kwh
    recomputed_grid = sum(e.grid_kwh for e in plan)
    if abs(recomputed_grid - total_grid_kwh) > TOL:
        violations.append(
            f"[V18] total_grid: reported={total_grid_kwh:.6f} != "
            f"recomputed={recomputed_grid:.6f}"
        )

    # 19. sum(grid_kwh * tariff) == total_cost_bdt
    recomputed_cost = sum(
        by_hour[h].grid_kwh * scenario.tariff[h]
        for h in range(24)
        if h in by_hour
    )
    if abs(recomputed_cost - total_cost_bdt) > TOL:
        violations.append(
            f"[V19] total_cost: reported={total_cost_bdt:.6f} != "
            f"recomputed={recomputed_cost:.6f}"
        )

    # 20. max(grid_kwh) == peak_grid_kwh
    recomputed_peak = max(e.grid_kwh for e in plan)
    if abs(recomputed_peak - peak_grid_kwh) > TOL:
        violations.append(
            f"[V20] peak_grid: reported={peak_grid_kwh:.6f} != "
            f"recomputed={recomputed_peak:.6f}"
        )

    return violations


# ─── Last-resort safe plan builder ─────────────────────────────────────────

def build_safe_plan(
    scenario: Scenario,
    constraints: CompiledConstraints,
) -> tuple[list[HourlyPlanEntry], float, float, float]:
    """Build a trivial always-valid plan: use solar, rest from grid, no battery.

    Valid for any scenario where min_energy <= initial_energy <= capacity
    (guaranteed by request validation). Forfeits optimization quality but
    preserves validity.
    """
    plan = []
    E = scenario.initial_energy_kwh

    for h in range(24):
        solar_use = min(scenario.demand[h], constraints.effective_solar[h])
        grid_use = scenario.demand[h] - solar_use
        grid_use = max(0.0, grid_use)
        solar_use = max(0.0, solar_use)

        plan.append(HourlyPlanEntry(
            hour=h,
            grid_kwh=round(grid_use, 6),
            solar_used_kwh=round(solar_use, 6),
            battery_action="idle",
            battery_kwh=0.0,
            battery_energy_after_kwh=round(E, 6),
        ))

    total_grid = round(sum(e.grid_kwh for e in plan), 6)
    total_cost = round(
        sum(plan[h].grid_kwh * scenario.tariff[h] for h in range(24)), 6
    )
    peak_grid = max(e.grid_kwh for e in plan)

    return plan, total_grid, total_cost, peak_grid
