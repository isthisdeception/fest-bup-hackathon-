"""
Request and response schemas for the GridWise API.

Request models use Pydantic v2 for structural validation.
Domain validation errors are raised as DomainValidationError
(not ValueError) so the route handler can distinguish 400 vs 422.
"""

import math
import logging
from dataclasses import dataclass, field
from typing import Optional

from pydantic import BaseModel, field_validator, model_validator

from app.config import settings

logger = logging.getLogger(__name__)

# ─── Constants shared across modules ────────────────────────────────────────

ALLOWED_DIRECTIVE_TYPES: frozenset[str] = frozenset({
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
})

ALLOWED_BATTERY_ACTIONS: frozenset[str] = frozenset({"charge", "discharge", "idle"})


# ─── Custom exception for domain validation ────────────────────────────────

class DomainValidationError(Exception):
    """Well-formed but domain-invalid request (maps to HTTP 422)."""

    def __init__(self, errors: list[dict]):
        self.errors = errors
        super().__init__(str(errors))


# ─── Request models ────────────────────────────────────────────────────────

def _check_finite(v: float, field_name: str) -> float:
    """Reject NaN and Infinity."""
    if not math.isfinite(v):
        raise ValueError(f"{field_name} must be finite, got {v}")
    return v


class HourEntry(BaseModel):
    """One hour of the 24-hour scenario."""

    hour: int
    demand_kwh: float
    solar_kwh: float
    tariff_bdt_per_kwh: float

    @field_validator("hour")
    @classmethod
    def _hour_range(cls, v: int) -> int:
        if not (0 <= v <= 23):
            raise ValueError(f"hour must be 0..23, got {v}")
        return v

    @field_validator("demand_kwh")
    @classmethod
    def _demand_finite(cls, v: float) -> float:
        return _check_finite(v, "demand_kwh")

    @field_validator("solar_kwh")
    @classmethod
    def _solar_finite(cls, v: float) -> float:
        return _check_finite(v, "solar_kwh")

    @field_validator("tariff_bdt_per_kwh")
    @classmethod
    def _tariff_finite(cls, v: float) -> float:
        # Negative tariffs are allowed (conflict C12 handles via LP bound)
        return _check_finite(v, "tariff_bdt_per_kwh")

    @model_validator(mode="after")
    def _hour_domain_checks(self) -> "HourEntry":
        errors = []
        if self.demand_kwh < 0:
            errors.append({
                "field": "demand_kwh",
                "msg": f"demand_kwh must be >= 0, got {self.demand_kwh}",
            })
        if self.solar_kwh < 0:
            errors.append({
                "field": "solar_kwh",
                "msg": f"solar_kwh must be >= 0, got {self.solar_kwh}",
            })
        if errors:
            raise DomainValidationError(errors)
        return self


class BatterySpec(BaseModel):
    """Battery parameters for the scenario."""

    capacity_kwh: float
    initial_energy_kwh: float
    minimum_energy_kwh: float
    max_charge_kwh_per_hour: float
    max_discharge_kwh_per_hour: float

    @field_validator("capacity_kwh")
    @classmethod
    def _capacity_finite(cls, v: float) -> float:
        return _check_finite(v, "capacity_kwh")

    @field_validator("initial_energy_kwh")
    @classmethod
    def _initial_finite(cls, v: float) -> float:
        return _check_finite(v, "initial_energy_kwh")

    @field_validator("minimum_energy_kwh")
    @classmethod
    def _min_finite(cls, v: float) -> float:
        return _check_finite(v, "minimum_energy_kwh")

    @field_validator("max_charge_kwh_per_hour")
    @classmethod
    def _charge_finite(cls, v: float) -> float:
        return _check_finite(v, "max_charge_kwh_per_hour")

    @field_validator("max_discharge_kwh_per_hour")
    @classmethod
    def _discharge_finite(cls, v: float) -> float:
        return _check_finite(v, "max_discharge_kwh_per_hour")

    @model_validator(mode="after")
    def _battery_domain_checks(self) -> "BatterySpec":
        """Domain checks that require cross-field comparison."""
        tol = settings.numeric_tolerance
        errors = []
        if self.capacity_kwh <= 0:
            errors.append({
                "field": "capacity_kwh",
                "msg": f"capacity_kwh must be > 0, got {self.capacity_kwh}",
            })
        if self.initial_energy_kwh < 0:
            errors.append({
                "field": "initial_energy_kwh",
                "msg": f"initial_energy_kwh must be >= 0, got {self.initial_energy_kwh}",
            })
        if self.minimum_energy_kwh < 0:
            errors.append({
                "field": "minimum_energy_kwh",
                "msg": f"minimum_energy_kwh must be >= 0, got {self.minimum_energy_kwh}",
            })
        if self.max_charge_kwh_per_hour < 0:
            errors.append({
                "field": "max_charge_kwh_per_hour",
                "msg": f"max_charge_kwh_per_hour must be >= 0, got {self.max_charge_kwh_per_hour}",
            })
        if self.max_discharge_kwh_per_hour < 0:
            errors.append({
                "field": "max_discharge_kwh_per_hour",
                "msg": f"max_discharge_kwh_per_hour must be >= 0, got {self.max_discharge_kwh_per_hour}",
            })
        if self.minimum_energy_kwh > self.capacity_kwh:
            errors.append({
                "field": "minimum_energy_kwh",
                "msg": f"minimum_energy_kwh ({self.minimum_energy_kwh}) "
                       f"must be <= capacity_kwh ({self.capacity_kwh})",
            })
        if self.initial_energy_kwh < self.minimum_energy_kwh - tol:
            errors.append({
                "field": "initial_energy_kwh",
                "msg": f"initial_energy_kwh ({self.initial_energy_kwh}) "
                       f"must be >= minimum_energy_kwh ({self.minimum_energy_kwh})",
            })
        if self.initial_energy_kwh > self.capacity_kwh + tol:
            errors.append({
                "field": "initial_energy_kwh",
                "msg": f"initial_energy_kwh ({self.initial_energy_kwh}) "
                       f"must be <= capacity_kwh ({self.capacity_kwh})",
            })
        if errors:
            raise DomainValidationError(errors)
        return self


class OptimizeRequest(BaseModel):
    """POST /optimize-energy request body."""

    scenario_id: str
    operator_notes: list[str]
    hours: list[HourEntry]
    battery: BatterySpec

    @field_validator("scenario_id")
    @classmethod
    def _scenario_id_nonempty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("scenario_id must be non-empty")
        return v

    @field_validator("hours")
    @classmethod
    def _exactly_24_hours(cls, v: list[HourEntry]) -> list[HourEntry]:
        if len(v) != 24:
            raise ValueError(f"hours must have exactly 24 entries, got {len(v)}")
        return v

    @field_validator("operator_notes")
    @classmethod
    def _notes_nonempty(cls, v: list[str]) -> list[str]:
        if len(v) < 1:
            raise ValueError("operator_notes must have at least 1 entry")
        for i, note in enumerate(v):
            if not isinstance(note, str) or not note.strip():
                raise ValueError(f"operator_notes[{i}] must be a non-empty string")
        return v

    @model_validator(mode="after")
    def _request_domain_checks(self) -> "OptimizeRequest":
        """Domain checks that involve cross-field or set-level validation."""
        errors = []

        # Note count upper bound (conflict C9)
        if settings.strict_note_count and len(self.operator_notes) > 3:
            errors.append({
                "field": "operator_notes",
                "msg": f"operator_notes has {len(self.operator_notes)} entries, "
                       "maximum is 3 when STRICT_NOTE_COUNT is enabled",
            })
        elif len(self.operator_notes) > 3:
            logger.warning(
                "operator_notes has %d entries (>3); accepting per lenient config",
                len(self.operator_notes),
            )

        # Hour set completeness: must be exactly {0..23}
        hour_values = [h.hour for h in self.hours]
        hour_set = set(hour_values)
        if len(hour_set) != 24 or hour_set != set(range(24)):
            missing = set(range(24)) - hour_set
            duplicates = [h for h in hour_values if hour_values.count(h) > 1]
            errors.append({
                "field": "hours",
                "msg": f"hours must cover exactly {{0..23}}; "
                       f"missing={sorted(missing)}, duplicates={sorted(set(duplicates))}",
            })

        if errors:
            raise DomainValidationError(errors)
        return self


def parse_request(payload: dict) -> OptimizeRequest:
    """Parse and validate a request payload with explicit error classification.

    - JSON structure / type errors -> raises pydantic.ValidationError (-> 400)
    - Domain errors -> raises DomainValidationError (-> 422)

    The route handler calls this instead of relying on FastAPI's automatic
    body binding, giving full control over 400 vs 422 vs 500.
    """
    try:
        return OptimizeRequest.model_validate(payload)
    except DomainValidationError:
        # Re-raise domain errors as-is (they must NOT be caught by Pydantic)
        raise
    except Exception:
        # Pydantic ValidationError or anything else -> re-raise unchanged
        raise


# ─── Scenario: dense normalized representation ─────────────────────────────

@dataclass
class Scenario:
    """Dense, hour-indexed representation of a validated request.

    All downstream code uses Scenario, never the raw OptimizeRequest.
    """

    scenario_id: str
    operator_notes: list[str]

    # Dense 24-element arrays indexed by hour (0..23)
    demand: list[float] = field(default_factory=list)
    solar: list[float] = field(default_factory=list)
    tariff: list[float] = field(default_factory=list)

    # Battery parameters
    capacity_kwh: float = 0.0
    initial_energy_kwh: float = 0.0
    minimum_energy_kwh: float = 0.0
    max_charge_kwh_per_hour: float = 0.0
    max_discharge_kwh_per_hour: float = 0.0


def to_scenario(req: OptimizeRequest) -> Scenario:
    """Convert a validated OptimizeRequest into a dense Scenario."""
    # Sort hours by hour index and build dense arrays
    sorted_hours = sorted(req.hours, key=lambda h: h.hour)

    return Scenario(
        scenario_id=req.scenario_id,
        operator_notes=list(req.operator_notes),
        demand=[h.demand_kwh for h in sorted_hours],
        solar=[h.solar_kwh for h in sorted_hours],
        tariff=[h.tariff_bdt_per_kwh for h in sorted_hours],
        capacity_kwh=req.battery.capacity_kwh,
        initial_energy_kwh=req.battery.initial_energy_kwh,
        minimum_energy_kwh=req.battery.minimum_energy_kwh,
        max_charge_kwh_per_hour=req.battery.max_charge_kwh_per_hour,
        max_discharge_kwh_per_hour=req.battery.max_discharge_kwh_per_hour,
    )


# ─── Response models (Problem Statement Section 10) ───────────────────────


class DirectiveInterpretationEntry(BaseModel):
    """One entry in directive_interpretation (Section 10.2).

    Field names are exact; do not rename.
    """

    note_index: int
    applies: bool
    directive_type: str
    structured_adjustment: Optional[dict] = None
    explanation: str

    @field_validator("directive_type")
    @classmethod
    def _type_allowed(cls, v: str) -> str:
        if v not in ALLOWED_DIRECTIVE_TYPES:
            raise ValueError(
                f"directive_type must be one of {sorted(ALLOWED_DIRECTIVE_TYPES)}, got {v!r}"
            )
        return v


class HourlyPlanEntry(BaseModel):
    """One hour in hourly_plan (Section 10.3).

    Field names are exact; do not rename.
    """

    hour: int
    grid_kwh: float
    solar_used_kwh: float
    battery_action: str
    battery_kwh: float
    battery_energy_after_kwh: float

    @field_validator("battery_action")
    @classmethod
    def _action_allowed(cls, v: str) -> str:
        if v not in ALLOWED_BATTERY_ACTIONS:
            raise ValueError(
                f"battery_action must be one of {sorted(ALLOWED_BATTERY_ACTIONS)}, got {v!r}"
            )
        return v


class OptimizeResponse(BaseModel):
    """POST /optimize-energy response (Section 10.1).

    Exactly 7 top-level fields, no extras. Field names are exact.
    """

    scenario_id: str
    directive_interpretation: list[DirectiveInterpretationEntry]
    hourly_plan: list[HourlyPlanEntry]
    total_grid_kwh: float
    total_cost_bdt: float
    peak_grid_kwh: float
    plan_summary: str


# ─── Utility ───────────────────────────────────────────────────────────────

def assert_finite(v: float, name: str = "value") -> float:
    """Assert a float is finite (not NaN or Infinity). Used by plan builder."""
    if not math.isfinite(v):
        raise ValueError(f"{name} must be finite, got {v}")
    return v


# ─── Structured adjustment builders (Problem Statement Section 4.1) ───────

def build_solar_reduction_adjustment(hours: list[int], factor: float) -> dict:
    """Build structured_adjustment for solar_reduction."""
    return {"hours": hours, "factor": factor}


def build_minimum_battery_reserve_adjustment(
    hours: list[int], minimum_energy_kwh: float
) -> dict:
    """Build structured_adjustment for minimum_battery_reserve."""
    return {"hours": hours, "minimum_energy_kwh": minimum_energy_kwh}


def build_no_charge_window_adjustment(hours: list[int]) -> dict:
    """Build structured_adjustment for no_charge_window."""
    return {"hours": hours}


def build_no_discharge_window_adjustment(hours: list[int]) -> dict:
    """Build structured_adjustment for no_discharge_window."""
    return {"hours": hours}


def build_max_grid_window_adjustment(
    hours: list[int], max_grid_kwh: float
) -> dict:
    """Build structured_adjustment for max_grid_window."""
    return {"hours": hours, "max_grid_kwh": max_grid_kwh}
