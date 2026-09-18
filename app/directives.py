"""
Directive domain model and constraint compilation.

This module owns the internal representation of interpreted operator
directives and the pure function that compiles them into per-hour
constraint arrays consumed by both the optimizer and the independent
validator.

Source: Problem Statement Section 5.3 (directive effects);
        Participant Guide Section 07 cat. 2 (directive application scoring).
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

from app.schemas import (
    ALLOWED_DIRECTIVE_TYPES,
    DirectiveInterpretationEntry,
    Scenario,
    build_max_grid_window_adjustment,
    build_minimum_battery_reserve_adjustment,
    build_no_charge_window_adjustment,
    build_no_discharge_window_adjustment,
    build_solar_reduction_adjustment,
)

logger = logging.getLogger(__name__)

# Valid source tags for diagnostics (never appears in API response)
VALID_SOURCES = frozenset({"llm", "llm_repaired", "downgraded_no_op", "test_injected"})


class DirectiveCompilationError(Exception):
    """A directive is structurally invalid for compilation.

    Should be impossible after guardrails, but the pipeline (Step 13)
    catches this, drops the directive, logs, and re-compiles.
    """


# ─── Directive dataclass ──────────────────────────────────────────────────

@dataclass(frozen=True)
class Directive:
    """One validated operator directive ready for compilation.

    Immutable so that the same directive list always yields the same
    compiled constraints.
    """

    note_index: int
    directive_type: str
    hours: tuple[int, ...]
    factor: Optional[float] = None               # solar_reduction only
    minimum_energy_kwh: Optional[float] = None    # minimum_battery_reserve only
    max_grid_kwh: Optional[float] = None          # max_grid_window only
    explanation: str = ""
    source: str = "llm"


# ─── Compiled constraints ─────────────────────────────────────────────────

@dataclass(frozen=True)
class CompiledConstraints:
    """Per-hour constraint arrays produced by compile_constraints.

    All lists are exactly 24 elements, indexed by hour 0..23.
    """

    effective_solar: list[float] = field(default_factory=list)
    reserve: list[float] = field(default_factory=list)
    charge_ub: list[float] = field(default_factory=list)
    discharge_ub: list[float] = field(default_factory=list)
    grid_ub: list[float] = field(default_factory=list)
    applied_directives: tuple[Directive, ...] = ()


# ─── Compilation ───────────────────────────────────────────────────────────

def compile_constraints(
    scenario: Scenario,
    directives: list[Directive],
) -> CompiledConstraints:
    """Compile directives + scenario into per-hour constraint arrays.

    Pure function: does not mutate scenario or directives.
    Uses the most-restrictive combination rule for overlapping directives
    (conflict C7 in the execution plan).
    """
    # Initialize from scenario defaults
    effective_solar = list(scenario.solar)                    # copy
    reserve = [scenario.minimum_energy_kwh] * 24
    charge_ub = [scenario.max_charge_kwh_per_hour] * 24
    discharge_ub = [scenario.max_discharge_kwh_per_hour] * 24
    # Conflict C12: grid_ub = demand + max_charge (energy-balance-implied bound
    # that keeps the LP bounded for any tariff sign)
    grid_ub = [
        scenario.demand[h] + scenario.max_charge_kwh_per_hour
        for h in range(24)
    ]

    # Fold in each directive
    for d in directives:
        if d.directive_type == "no_op":
            continue

        if d.directive_type == "solar_reduction":
            if d.factor is None:
                raise DirectiveCompilationError(
                    f"solar_reduction directive (note {d.note_index}) has factor=None"
                )
            for h in d.hours:
                effective_solar[h] *= d.factor  # multiplicative (most-restrictive)

        elif d.directive_type == "minimum_battery_reserve":
            if d.minimum_energy_kwh is None:
                raise DirectiveCompilationError(
                    f"minimum_battery_reserve directive (note {d.note_index}) "
                    f"has minimum_energy_kwh=None"
                )
            for h in d.hours:
                reserve[h] = max(reserve[h], d.minimum_energy_kwh)  # max (most-restrictive)

        elif d.directive_type == "no_charge_window":
            for h in d.hours:
                charge_ub[h] = 0.0  # union of blocked hours

        elif d.directive_type == "no_discharge_window":
            for h in d.hours:
                discharge_ub[h] = 0.0  # union of blocked hours

        elif d.directive_type == "max_grid_window":
            if d.max_grid_kwh is None:
                raise DirectiveCompilationError(
                    f"max_grid_window directive (note {d.note_index}) "
                    f"has max_grid_kwh=None"
                )
            for h in d.hours:
                grid_ub[h] = min(grid_ub[h], d.max_grid_kwh)  # min (most-restrictive)

        else:
            # Should never happen after guardrails
            raise DirectiveCompilationError(
                f"Unknown directive_type {d.directive_type!r} (note {d.note_index})"
            )

    # Clamp effective_solar to guard against float noise on 0-solar hours
    for h in range(24):
        effective_solar[h] = max(0.0, effective_solar[h])

    return CompiledConstraints(
        effective_solar=effective_solar,
        reserve=reserve,
        charge_ub=charge_ub,
        discharge_ub=discharge_ub,
        grid_ub=grid_ub,
        applied_directives=tuple(directives),
    )


# ─── API-facing interpretation entries ─────────────────────────────────────

def interpretation_entries(
    directives: list[Directive],
) -> list[DirectiveInterpretationEntry]:
    """Convert directives into the API response's directive_interpretation.

    - Sorted ascending by note_index.
    - `applies` is derived from type, never taken from the model.
    - `structured_adjustment` is built by the Step 05 builders.
    """
    sorted_directives = sorted(directives, key=lambda d: d.note_index)
    entries = []

    for d in sorted_directives:
        applies = d.directive_type != "no_op"

        # Build structured_adjustment from the type-specific builder
        if d.directive_type == "solar_reduction":
            adjustment = build_solar_reduction_adjustment(
                hours=list(d.hours), factor=d.factor,  # type: ignore[arg-type]
            )
        elif d.directive_type == "minimum_battery_reserve":
            adjustment = build_minimum_battery_reserve_adjustment(
                hours=list(d.hours),
                minimum_energy_kwh=d.minimum_energy_kwh,  # type: ignore[arg-type]
            )
        elif d.directive_type == "no_charge_window":
            adjustment = build_no_charge_window_adjustment(hours=list(d.hours))
        elif d.directive_type == "no_discharge_window":
            adjustment = build_no_discharge_window_adjustment(hours=list(d.hours))
        elif d.directive_type == "max_grid_window":
            adjustment = build_max_grid_window_adjustment(
                hours=list(d.hours),
                max_grid_kwh=d.max_grid_kwh,  # type: ignore[arg-type]
            )
        else:
            # no_op
            adjustment = None

        entries.append(DirectiveInterpretationEntry(
            note_index=d.note_index,
            applies=applies,
            directive_type=d.directive_type,
            structured_adjustment=adjustment,
            explanation=d.explanation,
        ))

    return entries


# ─── Deterministic plan_summary ────────────────────────────────────────────

def summarize(
    directives: list[Directive],
    scenario: Scenario,
    total_grid_kwh: float,
    total_cost_bdt: float,
    peak_grid_kwh: float,
) -> str:
    """Produce a deterministic plan_summary from compiled facts.

    Built from data only, never LLM-generated. Kept under ~400 chars.
    Source: Problem Statement Section 10.1 (required field);
            Participant Guide Section 04 (wording is not judged).
    """
    applied = [d for d in directives if d.directive_type != "no_op"]
    no_ops = [d for d in directives if d.directive_type == "no_op"]

    parts = []

    # Directive summary
    if applied:
        directive_descs = []
        for d in sorted(applied, key=lambda x: x.note_index):
            hour_str = _format_hours(d.hours)
            if d.directive_type == "solar_reduction":
                directive_descs.append(
                    f"{d.directive_type} on {hour_str} (factor {d.factor:.2f})"
                )
            elif d.directive_type == "minimum_battery_reserve":
                directive_descs.append(
                    f"{d.directive_type} on {hour_str} "
                    f"(min {d.minimum_energy_kwh:.1f} kWh)"
                )
            elif d.directive_type == "max_grid_window":
                directive_descs.append(
                    f"{d.directive_type} on {hour_str} "
                    f"(max {d.max_grid_kwh:.1f} kWh)"
                )
            else:
                directive_descs.append(f"{d.directive_type} on {hour_str}")

        parts.append(
            f"Applied {len(applied)} operator directive(s): "
            + ", ".join(directive_descs)
        )
    else:
        parts.append("No operator directives affected the schedule")

    if no_ops:
        parts.append(f"{len(no_ops)} note(s) had no schedule impact")

    # Battery return
    parts.append(
        f"Battery returns to {scenario.initial_energy_kwh:.1f} kWh"
    )

    # Totals
    parts.append(
        f"Total grid {total_grid_kwh:.2f} kWh at {total_cost_bdt:.2f} BDT, "
        f"peak {peak_grid_kwh:.2f} kWh"
    )

    summary = ". ".join(parts) + "."

    # Truncate to ~400 chars if needed
    if len(summary) > 400:
        summary = summary[:397] + "..."

    return summary


def _format_hours(hours: tuple[int, ...]) -> str:
    """Format hour tuple as compact ranges, e.g. (10,11,12,14) -> 'hours 10-12,14'."""
    if not hours:
        return "no hours"

    sorted_h = sorted(hours)
    ranges = []
    start = sorted_h[0]
    end = sorted_h[0]

    for h in sorted_h[1:]:
        if h == end + 1:
            end = h
        else:
            ranges.append(f"{start}-{end}" if start != end else str(start))
            start = end = h
    ranges.append(f"{start}-{end}" if start != end else str(start))

    return "hours " + ",".join(ranges)
