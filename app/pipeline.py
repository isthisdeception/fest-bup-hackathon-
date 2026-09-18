"""
Pipeline orchestrator: validation -> interpretation -> guardrails ->
compilation -> optimization -> plan building -> final validation -> response.

This is the single entry point for the /optimize-energy endpoint.
"""

import logging
import time

from app.directives import (
    compile_constraints,
    interpretation_entries,
    summarize,
)
from app.llm.interpreter import interpret_validated
from app.optimizer import optimize
from app.plan_builder import build_plan
from app.schemas import (
    OptimizeRequest,
    OptimizeResponse,
    to_scenario,
)
from app.validator import validate_plan, build_safe_plan

logger = logging.getLogger(__name__)


# ─── Pipeline entry point ───────────────────────────────────────────────

async def run(req: OptimizeRequest) -> dict:
    """Execute the full optimization pipeline.

    Returns a dict ready for JSONResponse serialization.
    """
    total_start = time.perf_counter()

    # a. Convert to internal scenario
    scenario = to_scenario(req)

    # b. Interpret operator notes with LLM (guardrailed, bounded, repairable)
    interp_start = time.perf_counter()
    directives, diag = interpret_validated(req.operator_notes, scenario)
    interp_ms = (time.perf_counter() - interp_start) * 1000

    # c. Compile constraints (ONCE - shared by optimizer, plan builder, validator)
    constraints = compile_constraints(scenario, directives)

    # d. Optimize
    opt_start = time.perf_counter()
    opt_result = optimize(scenario, constraints)
    opt_ms = (time.perf_counter() - opt_start) * 1000

    # e. Build plan
    plan, total_grid, total_cost, peak_grid = build_plan(
        scenario, constraints, opt_result
    )

    # f. Validate plan
    violations = validate_plan(
        scenario, constraints, plan, total_grid, total_cost, peak_grid
    )

    # g. If violations: fall back to safe plan
    if violations:
        logger.error(
            "Validator violations on %s (%d): %s",
            req.scenario_id, len(violations), violations,
        )
        plan, total_grid, total_cost, peak_grid = build_safe_plan(
            scenario, constraints
        )
        safe_violations = validate_plan(
            scenario, constraints, plan, total_grid, total_cost, peak_grid
        )
        if safe_violations:
            logger.error(
                "Safe plan also invalid on %s: %s — "
                "dropping directive reserves and rebuilding",
                req.scenario_id, safe_violations,
            )
            from app.directives import CompiledConstraints
            relaxed = CompiledConstraints(
                effective_solar=list(constraints.effective_solar),
                reserve=[scenario.minimum_energy_kwh] * 24,
                charge_ub=[scenario.max_charge_kwh_per_hour] * 24,
                discharge_ub=[scenario.max_discharge_kwh_per_hour] * 24,
                grid_ub=list(constraints.grid_ub),
                applied_directives=constraints.applied_directives,
            )
            plan, total_grid, total_cost, peak_grid = build_safe_plan(
                scenario, relaxed
            )
    else:
        logger.info("Validator: 0 violations on %s", req.scenario_id)

    # h. Interpretation entries for the response
    entries = interpretation_entries(directives)

    # i. Deterministic plan summary
    summary = summarize(
        directives, scenario, total_grid, total_cost, peak_grid
    )

    # j. Build response
    response = OptimizeResponse(
        scenario_id=req.scenario_id,  # echoed byte-identically
        directive_interpretation=entries,
        hourly_plan=plan,
        total_grid_kwh=total_grid,
        total_cost_bdt=total_cost,
        peak_grid_kwh=peak_grid,
        plan_summary=summary,
    )

    total_ms = (time.perf_counter() - total_start) * 1000
    logger.info(
        "Pipeline %s: interp_ms=%.1f (attempts=%d, degraded=%s) optimize_ms=%.1f total_ms=%.1f",
        req.scenario_id, interp_ms, diag.get("attempts", 1), diag.get("degraded", False), opt_ms, total_ms,
    )

    return response.model_dump()
