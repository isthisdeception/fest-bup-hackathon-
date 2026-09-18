"""
Two-phase linear program optimizer for 24-hour energy scheduling.

Phase 1: minimize total grid cost (sum of tariff * grid_kwh).
Phase 2: minimize peak grid import subject to Phase 1 optimal cost.

Uses scipy.optimize.linprog with the HiGHS solver.

Variable layout (96 continuous, +1 in phase 2):
  x = [g[0..23], s[0..23], c[0..23], d[0..23]]
  G(h)=h, S(h)=24+h, C(h)=48+h, D(h)=72+h
  Phase 2: p at index 96.

Source: Problem Statement Sections 5.2, 5.3, 9.x;
        Participant Guide Section 07 categories 2-3.
"""

import logging
import time
from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import linprog

from app.directives import (
    CompiledConstraints,
    Directive,
    compile_constraints,
)
from app.schemas import Scenario

logger = logging.getLogger(__name__)

N = 24  # hours


@dataclass
class OptimizeResult:
    """Result of the optimization engine."""
    g: list[float] = field(default_factory=list)    # grid_kwh per hour
    s: list[float] = field(default_factory=list)    # solar_used_kwh per hour
    c: list[float] = field(default_factory=list)    # charge per hour
    d: list[float] = field(default_factory=list)    # discharge per hour
    optimal_cost: float = 0.0
    phase2_applied: bool = False
    relaxations: list[str] = field(default_factory=list)
    status: str = "optimal"


def optimize(
    scenario: Scenario,
    constraints: CompiledConstraints,
) -> OptimizeResult:
    """Run the two-phase LP optimizer.

    Phase 1: minimize grid cost.
    Phase 2: minimize peak grid subject to cost <= optimal + 1e-6.
    Includes infeasibility relaxation ladder.
    """
    start = time.perf_counter()

    result = _solve_phase1(scenario, constraints)

    if result.status == "infeasible":
        result = _relaxation_ladder(scenario, constraints)

    # Phase 2: minimize peak
    if result.status == "optimal":
        result = _solve_phase2(scenario, constraints, result)

    elapsed = (time.perf_counter() - start) * 1000
    logger.info(
        "Optimizer: status=%s cost=%.2f phase2=%s relaxations=%d (%.1f ms)",
        result.status, result.optimal_cost, result.phase2_applied,
        len(result.relaxations), elapsed,
    )
    return result


def warmup_solve() -> bool:
    """Trivial solve to warm up scipy/HiGHS imports. Returns True on success."""
    try:
        c = [1.0] * 4
        bounds = [(0, 10)] * 4
        A_eq = [[1, 1, 0, 0]]
        b_eq = [5.0]
        res = linprog(c, A_eq=A_eq, b_eq=b_eq, bounds=bounds, method="highs")
        return res.success
    except Exception as exc:
        logger.warning("Warmup solve failed: %s", exc)
        return False


# ─── Phase 1: Minimize cost ───────────────────────────────────────────────

def _solve_phase1(
    scenario: Scenario,
    constraints: CompiledConstraints,
) -> OptimizeResult:
    """Phase 1 LP: minimize sum(tariff[h] * g[h])."""
    n_vars = 4 * N  # 96

    # Objective: minimize sum(tariff * g)
    c_obj = np.zeros(n_vars)
    for h in range(N):
        c_obj[h] = scenario.tariff[h]  # G(h) = h

    # Bounds
    bounds = _build_bounds(scenario, constraints)

    # Equality constraints (25 rows)
    A_eq, b_eq = _build_equalities(scenario)

    # Inequality constraints (48 rows)
    A_ub, b_ub = _build_inequalities(scenario, constraints)

    res = linprog(c_obj, A_ub=A_ub, b_ub=b_ub, A_eq=A_eq, b_eq=b_eq,
                  bounds=bounds, method="highs")

    if not res.success:
        return OptimizeResult(status="infeasible")

    return _extract_result(res)


def _solve_phase2(
    scenario: Scenario,
    constraints: CompiledConstraints,
    phase1_result: OptimizeResult,
) -> OptimizeResult:
    """Phase 2 LP: minimize peak grid subject to cost <= optimal + 1e-6."""
    try:
        n_vars = 4 * N + 1  # 97 (variable p at index 96)
        optimal_cost = phase1_result.optimal_cost

        # Objective: minimize p (the peak variable)
        c_obj = np.zeros(n_vars)
        c_obj[96] = 1.0

        # Bounds: same as phase 1 + p >= 0 (no upper bound)
        bounds_base = _build_bounds(scenario, constraints)
        bounds = bounds_base + [(0, None)]  # p

        # Equality constraints (same 25 rows, extended to 97 columns)
        A_eq_base, b_eq = _build_equalities(scenario)
        A_eq = np.zeros((A_eq_base.shape[0], n_vars))
        A_eq[:, :4*N] = A_eq_base

        # Inequality constraints: base 48 + 24 peak rows + 1 cost cap
        A_ub_base, b_ub_base = _build_inequalities(scenario, constraints)
        n_ub = A_ub_base.shape[0] + N + 1

        A_ub = np.zeros((n_ub, n_vars))
        b_ub = np.zeros(n_ub)

        # Copy base inequalities
        A_ub[:A_ub_base.shape[0], :4*N] = A_ub_base
        b_ub[:A_ub_base.shape[0]] = b_ub_base

        # Peak constraints: g[h] - p <= 0 for all h
        for h in range(N):
            row = A_ub_base.shape[0] + h
            A_ub[row, h] = 1.0          # g[h]
            A_ub[row, 96] = -1.0        # -p
            b_ub[row] = 0.0

        # Cost cap: sum(tariff * g) <= optimal_cost + 1e-6
        cost_row = A_ub_base.shape[0] + N
        for h in range(N):
            A_ub[cost_row, h] = scenario.tariff[h]
        b_ub[cost_row] = optimal_cost + 1e-6

        res = linprog(c_obj, A_ub=A_ub, b_ub=b_ub, A_eq=A_eq, b_eq=b_eq,
                      bounds=bounds, method="highs")

        if not res.success:
            logger.warning("Phase 2 failed (status=%d), using phase 1 solution", res.status)
            return phase1_result

        # Extract and verify cost
        x = res.x
        recomputed_cost = sum(scenario.tariff[h] * x[h] for h in range(N))
        if abs(recomputed_cost - optimal_cost) > 1e-4:
            logger.warning(
                "Phase 2 cost drift: %.6f vs optimal %.6f, discarding phase 2",
                recomputed_cost, optimal_cost,
            )
            return phase1_result

        result = _extract_result_from_x(x[:4*N], recomputed_cost)
        result.phase2_applied = True
        result.relaxations = phase1_result.relaxations
        return result

    except Exception as exc:
        logger.warning("Phase 2 exception: %s, using phase 1 solution", exc)
        return phase1_result


# ─── Constraint builders ──────────────────────────────────────────────────

def _build_bounds(
    scenario: Scenario,
    constraints: CompiledConstraints,
) -> list[tuple[float, float]]:
    """Build variable bounds for linprog."""
    bounds = []
    for h in range(N):
        bounds.append((0.0, constraints.grid_ub[h]))                     # g[h]
    for h in range(N):
        bounds.append((0.0, max(0.0, constraints.effective_solar[h])))    # s[h]
    for h in range(N):
        bounds.append((0.0, constraints.charge_ub[h]))                   # c[h]
    for h in range(N):
        bounds.append((0.0, constraints.discharge_ub[h]))                # d[h]
    return bounds


def _build_equalities(scenario: Scenario) -> tuple[np.ndarray, np.ndarray]:
    """Build 25 equality constraint rows.

    24 energy balance rows: g[h] + s[h] + d[h] - c[h] = demand[h]
    1 neutrality row:       sum(c) - sum(d) = 0
    """
    n_vars = 4 * N
    n_eq = N + 1
    A_eq = np.zeros((n_eq, n_vars))
    b_eq = np.zeros(n_eq)

    # Energy balance: g[h] + s[h] + d[h] - c[h] = demand[h]
    for h in range(N):
        A_eq[h, h] = 1.0           # g[h]
        A_eq[h, N + h] = 1.0       # s[h]
        A_eq[h, 2*N + h] = -1.0    # -c[h]
        A_eq[h, 3*N + h] = 1.0     # d[h]
        b_eq[h] = scenario.demand[h]

    # End-of-day neutrality: sum(c) - sum(d) = 0
    for h in range(N):
        A_eq[N, 2*N + h] = 1.0     # +c[h]
        A_eq[N, 3*N + h] = -1.0    # -d[h]
    b_eq[N] = 0.0

    return A_eq, b_eq


def _build_inequalities(
    scenario: Scenario,
    constraints: CompiledConstraints,
) -> tuple[np.ndarray, np.ndarray]:
    """Build 48 inequality constraint rows.

    24 capacity ceiling:   NET(h) <= capacity - E0
    24 active minimum:    -NET(h) <= E0 - reserve[h]

    where NET(h) = sum_{k=0..h} (c[k] - d[k])
    """
    n_vars = 4 * N
    n_ub = 2 * N  # 48
    A_ub = np.zeros((n_ub, n_vars))
    b_ub = np.zeros(n_ub)

    E0 = scenario.initial_energy_kwh
    cap = scenario.capacity_kwh

    for h in range(N):
        # Capacity ceiling: sum_{k=0..h} (c[k] - d[k]) <= cap - E0
        for k in range(h + 1):
            A_ub[h, 2*N + k] = 1.0     # +c[k]
            A_ub[h, 3*N + k] = -1.0    # -d[k]
        b_ub[h] = cap - E0

        # Active minimum floor: -sum_{k=0..h} (c[k] - d[k]) <= E0 - reserve[h]
        row = N + h
        for k in range(h + 1):
            A_ub[row, 2*N + k] = -1.0   # -c[k]
            A_ub[row, 3*N + k] = 1.0    # +d[k]
        b_ub[row] = E0 - constraints.reserve[h]

    return A_ub, b_ub


# ─── Result extraction ────────────────────────────────────────────────────

def _extract_result(res) -> OptimizeResult:
    """Extract OptimizeResult from linprog result."""
    return _extract_result_from_x(res.x, res.fun)


def _extract_result_from_x(x: np.ndarray, cost: float) -> OptimizeResult:
    """Extract OptimizeResult from solution vector and cost."""
    g = [max(0.0, float(x[h])) for h in range(N)]
    s = [max(0.0, float(x[N + h])) for h in range(N)]
    c = [max(0.0, float(x[2*N + h])) for h in range(N)]
    d = [max(0.0, float(x[3*N + h])) for h in range(N)]
    return OptimizeResult(
        g=g, s=s, c=c, d=d,
        optimal_cost=float(cost),
        status="optimal",
    )


# ─── Infeasibility relaxation ladder ──────────────────────────────────────

def _relaxation_ladder(
    scenario: Scenario,
    constraints: CompiledConstraints,
) -> OptimizeResult:
    """Try progressively relaxing directive constraints until feasible.

    Never relaxes physics: energy balance, non-negativity, capacity,
    base minimum_energy_kwh, hourly rate limits, end-of-day neutrality.
    """
    logger.warning(
        "Phase 1 infeasible. Starting relaxation ladder. "
        "effective_solar=%s, reserve=%s, charge_ub=%s, discharge_ub=%s, grid_ub=%s",
        constraints.effective_solar[:4], constraints.reserve[:4],
        constraints.charge_ub[:4], constraints.discharge_ub[:4],
        constraints.grid_ub[:4],
    )

    relaxations: list[str] = []

    # Step 1: Drop directives one at a time, highest note_index first
    directives = list(constraints.applied_directives)
    # Sort by note_index descending for drop order
    sorted_by_idx = sorted(
        [d for d in directives if d.directive_type != "no_op"],
        key=lambda d: d.note_index,
        reverse=True,
    )

    remaining = list(directives)
    for drop_d in sorted_by_idx:
        remaining = [d for d in remaining if d is not drop_d]
        relaxations.append(
            f"Dropped directive note_index={drop_d.note_index} "
            f"type={drop_d.directive_type}"
        )
        new_constraints = compile_constraints(scenario, remaining)
        result = _solve_phase1(scenario, new_constraints)
        if result.status == "optimal":
            result.relaxations = relaxations
            return result

    # Step 2: All directives dropped, relax compiled constraints manually
    # 2a: grid_ub back to physical bound
    relaxations.append("Relaxed grid_ub to physical bound (demand + max_charge)")
    phys_constraints = _relax_grid_ub(scenario, constraints)
    result = _solve_phase1(scenario, phys_constraints)
    if result.status == "optimal":
        result.relaxations = relaxations
        return result

    # 2b: reserve back to base minimum
    relaxations.append("Relaxed reserve to base minimum_energy_kwh")
    phys_constraints = _relax_reserve(scenario, phys_constraints)
    result = _solve_phase1(scenario, phys_constraints)
    if result.status == "optimal":
        result.relaxations = relaxations
        return result

    # 2c: charge/discharge back to rate limits
    relaxations.append("Relaxed charge/discharge to base rate limits")
    phys_constraints = _relax_windows(scenario, phys_constraints)
    result = _solve_phase1(scenario, phys_constraints)
    if result.status == "optimal":
        result.relaxations = relaxations
        return result

    # Step 3: Trivial always-feasible fallback
    logger.error("All relaxations exhausted. Using trivial fallback plan.")
    relaxations.append("Trivial fallback: all-grid, no battery")
    return _trivial_fallback(scenario, constraints, relaxations)


def _relax_grid_ub(
    scenario: Scenario,
    constraints: CompiledConstraints,
) -> CompiledConstraints:
    """Relax grid_ub to the physical bound."""
    return CompiledConstraints(
        effective_solar=list(constraints.effective_solar),
        reserve=list(constraints.reserve),
        charge_ub=list(constraints.charge_ub),
        discharge_ub=list(constraints.discharge_ub),
        grid_ub=[
            scenario.demand[h] + scenario.max_charge_kwh_per_hour
            for h in range(N)
        ],
        applied_directives=constraints.applied_directives,
    )


def _relax_reserve(
    scenario: Scenario,
    constraints: CompiledConstraints,
) -> CompiledConstraints:
    """Relax reserve to base minimum_energy_kwh."""
    return CompiledConstraints(
        effective_solar=list(constraints.effective_solar),
        reserve=[scenario.minimum_energy_kwh] * N,
        charge_ub=list(constraints.charge_ub),
        discharge_ub=list(constraints.discharge_ub),
        grid_ub=list(constraints.grid_ub),
        applied_directives=constraints.applied_directives,
    )


def _relax_windows(
    scenario: Scenario,
    constraints: CompiledConstraints,
) -> CompiledConstraints:
    """Relax charge/discharge to base rate limits."""
    return CompiledConstraints(
        effective_solar=list(constraints.effective_solar),
        reserve=list(constraints.reserve),
        charge_ub=[scenario.max_charge_kwh_per_hour] * N,
        discharge_ub=[scenario.max_discharge_kwh_per_hour] * N,
        grid_ub=list(constraints.grid_ub),
        applied_directives=constraints.applied_directives,
    )


def _trivial_fallback(
    scenario: Scenario,
    constraints: CompiledConstraints,
    relaxations: list[str],
) -> OptimizeResult:
    """Trivial always-feasible plan: use solar, rest from grid, no battery."""
    g = []
    s = []
    for h in range(N):
        solar_use = min(scenario.demand[h], constraints.effective_solar[h])
        grid_use = scenario.demand[h] - solar_use
        g.append(max(0.0, grid_use))
        s.append(max(0.0, solar_use))

    cost = sum(scenario.tariff[h] * g[h] for h in range(N))

    return OptimizeResult(
        g=g, s=s,
        c=[0.0] * N,
        d=[0.0] * N,
        optimal_cost=cost,
        status="fallback_trivial",
        relaxations=relaxations,
    )
