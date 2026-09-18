"""
Plan builder: convert LP solution to hourly_plan and totals.

Converts an OptimizeResult (separate c/d vectors) into the exact 24-entry
hourly_plan with single battery_action per hour, plus total_grid_kwh,
total_cost_bdt, peak_grid_kwh.

The order of operations guarantees that reported totals match a
recalculation from hourly_plan exactly (Problem Statement Section 11.3;
Participant Guide Section 09).

Source: Problem Statement Sections 9.x, 10.3, 11.3, 11.5.
"""

import logging
import math

from app.directives import CompiledConstraints
from app.optimizer import OptimizeResult
from app.schemas import HourlyPlanEntry, Scenario

logger = logging.getLogger(__name__)

N = 24
EPS = 1e-6
ROUND_DIGITS = 6


def build_plan(
    scenario: Scenario,
    constraints: CompiledConstraints,
    opt_result: OptimizeResult,
) -> tuple[list[HourlyPlanEntry], float, float, float]:
    """Build hourly_plan and totals from an LP solution.

    Returns (plan, total_grid_kwh, total_cost_bdt, peak_grid_kwh).
    Operations proceed in the exact order specified by Step 11.
    """
    # ── 1. Net the battery ──────────────────────────────────────────────
    actions: list[str] = []
    battery_kwh: list[float] = []

    for h in range(N):
        net = opt_result.c[h] - opt_result.d[h]
        if net > EPS:
            actions.append("charge")
            battery_kwh.append(net)
        elif net < -EPS:
            actions.append("discharge")
            battery_kwh.append(-net)
        else:
            actions.append("idle")
            battery_kwh.append(0.0)

    # ── 2. Clamp tiny negatives ─────────────────────────────────────────
    solar_used = list(opt_result.s)
    for h in range(N):
        solar_used[h] = _clamp_nonneg(solar_used[h], f"solar_used[{h}]")
        battery_kwh[h] = _clamp_nonneg(battery_kwh[h], f"battery_kwh[{h}]")

    # ── 3. Round free variables ─────────────────────────────────────────
    for h in range(N):
        solar_used[h] = round(solar_used[h], ROUND_DIGITS)
        battery_kwh[h] = round(battery_kwh[h], ROUND_DIGITS)

    # ── 4. Re-derive grid_kwh from energy balance ───────────────────────
    grid_kwh: list[float] = []
    for h in range(N):
        charge_h = battery_kwh[h] if actions[h] == "charge" else 0.0
        discharge_h = battery_kwh[h] if actions[h] == "discharge" else 0.0

        g = scenario.demand[h] + charge_h - solar_used[h] - discharge_h
        g = round(g, ROUND_DIGITS)

        # Guard A: solar overshoot -> curtail solar
        if g < 0:
            overshoot = -g
            solar_used[h] = round(solar_used[h] - overshoot, ROUND_DIGITS)
            solar_used[h] = max(0.0, solar_used[h])
            g = 0.0

        # Guard B: grid above constraint cap -> log (don't truncate)
        if g > constraints.grid_ub[h] + EPS:
            logger.error(
                "grid_kwh[%d]=%.6f exceeds grid_ub=%.6f (will be caught by validator)",
                h, g, constraints.grid_ub[h],
            )

        grid_kwh.append(g)

    # ── 5. Re-derive battery state by accumulation ──────────────────────
    battery_energy_after: list[float] = []
    E = scenario.initial_energy_kwh
    for h in range(N):
        if actions[h] == "charge":
            E += battery_kwh[h]
        elif actions[h] == "discharge":
            E -= battery_kwh[h]
        battery_energy_after.append(round(E, ROUND_DIGITS))

    # ── 6. Neutrality snap ──────────────────────────────────────────────
    residual = E - scenario.initial_energy_kwh
    if abs(residual) > 0 and abs(residual) <= 1e-4:
        # Find last non-idle hour and adjust
        last_active = -1
        for h in range(N - 1, -1, -1):
            if actions[h] != "idle":
                last_active = h
                break

        if last_active >= 0:
            # Adjust the battery_kwh of the last active hour
            if actions[last_active] == "charge":
                battery_kwh[last_active] = round(
                    battery_kwh[last_active] - residual, ROUND_DIGITS
                )
            else:  # discharge
                battery_kwh[last_active] = round(
                    battery_kwh[last_active] + residual, ROUND_DIGITS
                )

            # If adjustment makes battery_kwh zero or negative, switch to idle
            if battery_kwh[last_active] <= 0:
                actions[last_active] = "idle"
                battery_kwh[last_active] = 0.0

            # Re-derive grid_kwh for the adjusted hour
            charge_h = battery_kwh[last_active] if actions[last_active] == "charge" else 0.0
            discharge_h = battery_kwh[last_active] if actions[last_active] == "discharge" else 0.0
            grid_kwh[last_active] = round(
                scenario.demand[last_active] + charge_h
                - solar_used[last_active] - discharge_h,
                ROUND_DIGITS,
            )
            if grid_kwh[last_active] < 0:
                overshoot = -grid_kwh[last_active]
                solar_used[last_active] = round(
                    solar_used[last_active] - overshoot, ROUND_DIGITS
                )
                solar_used[last_active] = max(0.0, solar_used[last_active])
                grid_kwh[last_active] = 0.0

            # Re-derive battery state from last_active onward
            E_rebuild = scenario.initial_energy_kwh
            for h2 in range(N):
                if actions[h2] == "charge":
                    E_rebuild += battery_kwh[h2]
                elif actions[h2] == "discharge":
                    E_rebuild -= battery_kwh[h2]
                if h2 >= last_active:
                    battery_energy_after[h2] = round(E_rebuild, ROUND_DIGITS)
    elif abs(residual) > 1e-4:
        logger.error(
            "Neutrality residual %.9f exceeds 1e-4; not patching", residual
        )

    # ── 7. Compute totals from the finished plan ────────────────────────
    total_grid_kwh = round(sum(grid_kwh), ROUND_DIGITS)
    total_cost_bdt = round(
        sum(grid_kwh[h] * scenario.tariff[h] for h in range(N)),
        ROUND_DIGITS,
    )
    peak_grid_kwh = max(grid_kwh)

    # ── 8. Finite assertion ─────────────────────────────────────────────
    for h in range(N):
        _assert_finite(grid_kwh[h], f"grid_kwh[{h}]")
        _assert_finite(solar_used[h], f"solar_used[{h}]")
        _assert_finite(battery_kwh[h], f"battery_kwh[{h}]")
        _assert_finite(battery_energy_after[h], f"battery_energy_after[{h}]")
    _assert_finite(total_grid_kwh, "total_grid_kwh")
    _assert_finite(total_cost_bdt, "total_cost_bdt")
    _assert_finite(peak_grid_kwh, "peak_grid_kwh")

    # ── 9. Build HourlyPlanEntry objects ────────────────────────────────
    plan = []
    for h in range(N):
        plan.append(HourlyPlanEntry(
            hour=h,
            grid_kwh=grid_kwh[h],
            solar_used_kwh=solar_used[h],
            battery_action=actions[h],
            battery_kwh=battery_kwh[h],
            battery_energy_after_kwh=battery_energy_after[h],
        ))

    return plan, total_grid_kwh, total_cost_bdt, peak_grid_kwh


# ─── Helpers ───────────────────────────────────────────────────────────────

def _clamp_nonneg(v: float, name: str) -> float:
    """Clamp tiny negatives to 0; raise on real negatives."""
    if v < 0:
        if v > -1e-9:
            return 0.0
        raise ValueError(f"{name} is negative: {v}")
    return v


def _assert_finite(v: float, name: str) -> None:
    """Assert a value is finite."""
    if not math.isfinite(v):
        raise ValueError(f"{name} is not finite: {v}")
