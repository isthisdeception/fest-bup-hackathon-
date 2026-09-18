"""
Deterministic guardrail validator for LLM output.

Converts an untrusted parsed-JSON object from the LLM into a validated
list[Directive] covering every note exactly once, repairing what is safely
repairable and downgrading what is not — without ever inventing a directive.

NO-INVENTION STRUCTURAL GUARANTEE:
The Directive dataclass has no fields for demand, solar, tariff, capacity,
initial energy, base minimum, or rate limits.  It is therefore structurally
impossible for an interpretation to modify base scenario parameters.
(Problem Statement Section 08 "No invention".)

Source: Problem Statement Section 08 (guardrails);
        Participant Guide Section 03 (deterministic validation).
"""

import logging
import math
import re
from dataclasses import dataclass, field
from typing import Optional

from app.directives import Directive
from app.schemas import ALLOWED_DIRECTIVE_TYPES, Scenario

logger = logging.getLogger(__name__)

# Control character pattern for explanation sanitization
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]+")

# Default explanations per type
_DEFAULT_EXPLANATIONS = {
    "solar_reduction": "Interpreted as a solar availability reduction for the stated hours.",
    "minimum_battery_reserve": "Interpreted as a minimum battery reserve requirement for the stated hours.",
    "no_charge_window": "Interpreted as a no-charging window for the stated hours.",
    "no_discharge_window": "Interpreted as a no-discharging window for the stated hours.",
    "max_grid_window": "Interpreted as a maximum grid import cap for the stated hours.",
    "no_op": "This note does not affect today's 24-hour energy schedule.",
}


@dataclass
class GuardrailResult:
    """Result of validate_interpretation."""
    directives: list[Directive] = field(default_factory=list)
    repairs: list[str] = field(default_factory=list)
    rejections: list[str] = field(default_factory=list)
    needs_retry: bool = False


def validate_interpretation(
    raw: object,
    scenario: Scenario,
    note_count: int,
) -> GuardrailResult:
    """Validate and normalize untrusted LLM output into Directives.

    Never raises — all failure modes are captured in the result.
    Never invents a directive type.
    Never performs network I/O.
    """
    result = GuardrailResult()

    try:
        items = _extract_items(raw, result)
        if items is None:
            # Fill all indices with no_op
            result.directives = [
                Directive(
                    note_index=i,
                    directive_type="no_op",
                    hours=(),
                    explanation=_DEFAULT_EXPLANATIONS["no_op"],
                    source="downgraded_no_op",
                )
                for i in range(note_count)
            ]
            return result

        # Per-item validation
        seen_indices: set[int] = set()
        valid_directives: list[Directive] = []

        for item in items:
            d = _validate_item(item, scenario, note_count, seen_indices, result)
            if d is not None:
                seen_indices.add(d.note_index)
                valid_directives.append(d)

        # Coverage check — flag missing indices
        all_indices = set(range(note_count))
        missing = all_indices - seen_indices
        if missing:
            result.needs_retry = True
            result.rejections.append(
                f"Missing note_index values: {sorted(missing)}"
            )

        # Fill missing indices with no_op after retry decision
        for idx in sorted(missing):
            valid_directives.append(Directive(
                note_index=idx,
                directive_type="no_op",
                hours=(),
                explanation=_DEFAULT_EXPLANATIONS["no_op"],
                source="downgraded_no_op",
            ))

        # Sort ascending by note_index
        result.directives = sorted(valid_directives, key=lambda d: d.note_index)

    except Exception as exc:
        # Never raise out of validate_interpretation
        logger.error("Guardrail unexpected error: %s", exc)
        result.needs_retry = True
        result.directives = [
            Directive(
                note_index=i,
                directive_type="no_op",
                hours=(),
                explanation=_DEFAULT_EXPLANATIONS["no_op"],
                source="downgraded_no_op",
            )
            for i in range(note_count)
        ]

    return result


# ─── A. Envelope extraction ───────────────────────────────────────────────

_ACCEPTED_KEYS = ("interpretations", "directive_interpretation", "results", "notes")


def _extract_items(raw: object, result: GuardrailResult) -> Optional[list]:
    """Extract the interpretations array from the raw LLM output."""

    # If it's already a list, accept directly
    if isinstance(raw, list):
        return raw

    if not isinstance(raw, dict):
        result.needs_retry = True
        result.rejections.append(
            f"Raw output is {type(raw).__name__}, expected dict or list"
        )
        return None

    # Try accepted keys in priority order
    for key in _ACCEPTED_KEYS:
        if key in raw and isinstance(raw[key], list):
            return raw[key]

    # Check if any key holds a list
    for key in _ACCEPTED_KEYS:
        if key in raw:
            result.needs_retry = True
            result.rejections.append(
                f"Key '{key}' is {type(raw[key]).__name__}, expected list"
            )
            return None

    result.needs_retry = True
    result.rejections.append(
        f"No recognized array key found in {sorted(raw.keys())[:5]}"
    )
    return None


# ─── B-H. Per-item validation ─────────────────────────────────────────────

def _validate_item(
    item: object,
    scenario: Scenario,
    note_count: int,
    seen_indices: set[int],
    result: GuardrailResult,
) -> Optional[Directive]:
    """Validate a single interpretation item. Returns Directive or None."""

    # Must be a dict
    if not isinstance(item, dict):
        result.rejections.append(f"Non-dict item dropped: {type(item).__name__}")
        return None

    # B.3 — note_index
    note_index = _coerce_int(item.get("note_index"), result, "note_index")
    if note_index is None:
        result.rejections.append("Item dropped: note_index not coercible to int")
        return None
    if not (0 <= note_index < note_count):
        result.rejections.append(
            f"note_index {note_index} out of range [0, {note_count}), dropped"
        )
        return None

    # B.4 — duplicate note_index
    if note_index in seen_indices:
        result.rejections.append(
            f"Duplicate note_index {note_index}, keeping first occurrence"
        )
        return None

    # B.5 — directive_type
    raw_type = item.get("directive_type", "")
    downgrade = False
    if isinstance(raw_type, str):
        cleaned_type = raw_type.strip().lower()
        if cleaned_type != raw_type:
            result.repairs.append(
                f"note {note_index}: directive_type normalized "
                f"from {raw_type!r} to {cleaned_type!r}"
            )
        directive_type = cleaned_type
    else:
        directive_type = str(raw_type).strip().lower()
        result.repairs.append(
            f"note {note_index}: directive_type coerced from "
            f"{type(raw_type).__name__} to string"
        )

    if directive_type not in ALLOWED_DIRECTIVE_TYPES:
        downgrade = True
        result.rejections.append(
            f"note {note_index}: unknown directive_type {directive_type!r}, "
            f"downgraded to no_op"
        )

    # If downgrading, return no_op immediately
    if downgrade:
        return _make_no_op(note_index, item, result)

    # C. Hours validation (required for non-no_op)
    if directive_type == "no_op":
        # no_op: discard hours and numerics
        explanation = _sanitize_explanation(
            item.get("explanation", ""), directive_type
        )
        return Directive(
            note_index=note_index,
            directive_type="no_op",
            hours=(),
            explanation=explanation,
            source="llm",
        )

    # Non-no_op: validate hours
    hours = _validate_hours(item, note_index, result)
    if hours is None:
        # Downgrade
        return _make_no_op(note_index, item, result)

    # D. Numeric validation per type
    if directive_type == "solar_reduction":
        factor = _validate_factor(item, note_index, result)
        if factor is None:
            return _make_no_op(note_index, item, result)
        explanation = _sanitize_explanation(
            item.get("explanation", ""), directive_type
        )
        return Directive(
            note_index=note_index,
            directive_type="solar_reduction",
            hours=tuple(hours),
            factor=factor,
            explanation=explanation,
            source="llm",
        )

    elif directive_type == "minimum_battery_reserve":
        min_energy = _validate_minimum_energy(
            item, note_index, scenario.capacity_kwh, result
        )
        if min_energy is None:
            return _make_no_op(note_index, item, result)
        explanation = _sanitize_explanation(
            item.get("explanation", ""), directive_type
        )
        return Directive(
            note_index=note_index,
            directive_type="minimum_battery_reserve",
            hours=tuple(hours),
            minimum_energy_kwh=min_energy,
            explanation=explanation,
            source="llm",
        )

    elif directive_type == "no_charge_window":
        # Only hours needed; strip extra numeric fields
        explanation = _sanitize_explanation(
            item.get("explanation", ""), directive_type
        )
        return Directive(
            note_index=note_index,
            directive_type="no_charge_window",
            hours=tuple(hours),
            explanation=explanation,
            source="llm",
        )

    elif directive_type == "no_discharge_window":
        explanation = _sanitize_explanation(
            item.get("explanation", ""), directive_type
        )
        return Directive(
            note_index=note_index,
            directive_type="no_discharge_window",
            hours=tuple(hours),
            explanation=explanation,
            source="llm",
        )

    elif directive_type == "max_grid_window":
        max_grid = _validate_max_grid(item, note_index, result)
        if max_grid is None:
            return _make_no_op(note_index, item, result)
        explanation = _sanitize_explanation(
            item.get("explanation", ""), directive_type
        )
        return Directive(
            note_index=note_index,
            directive_type="max_grid_window",
            hours=tuple(hours),
            max_grid_kwh=max_grid,
            explanation=explanation,
            source="llm",
        )

    # Should never reach here (all types handled above)
    return _make_no_op(note_index, item, result)


# ─── Hours validation ─────────────────────────────────────────────────────

def _validate_hours(
    item: dict,
    note_index: int,
    result: GuardrailResult,
) -> Optional[list[int]]:
    """Validate and normalize the hours field. Returns sorted unique list or None."""
    raw_hours = item.get("hours")
    if not isinstance(raw_hours, list):
        result.rejections.append(
            f"note {note_index}: hours is {type(raw_hours).__name__}, "
            f"downgraded to no_op"
        )
        return None

    valid_hours: list[int] = []
    for elem in raw_hours:
        h = _coerce_int_silent(elem)
        if h is None:
            result.rejections.append(
                f"note {note_index}: non-coercible hour element "
                f"{elem!r}, downgraded to no_op"
            )
            return None
        if 0 <= h <= 23:
            valid_hours.append(h)
        else:
            result.repairs.append(
                f"note {note_index}: out-of-range hour {h} dropped"
            )

    if not valid_hours:
        result.rejections.append(
            f"note {note_index}: no valid hours remain, downgraded to no_op"
        )
        return None

    # De-duplicate and sort
    original_len = len(valid_hours)
    unique_sorted = sorted(set(valid_hours))
    if len(unique_sorted) < original_len:
        result.repairs.append(
            f"note {note_index}: duplicate hours removed "
            f"({original_len} -> {len(unique_sorted)})"
        )
    if valid_hours != unique_sorted and len(unique_sorted) == original_len:
        result.repairs.append(
            f"note {note_index}: hours re-sorted to ascending order"
        )

    return unique_sorted


# ─── Numeric validators ───────────────────────────────────────────────────

def _validate_factor(
    item: dict,
    note_index: int,
    result: GuardrailResult,
) -> Optional[float]:
    """Validate solar_reduction factor. Returns normalized value or None."""
    raw = item.get("factor")
    factor = _coerce_float(raw)
    if factor is None or not math.isfinite(factor):
        result.rejections.append(
            f"note {note_index}: factor {raw!r} is not finite, downgraded"
        )
        return None

    # Percentage-to-fraction normalization
    # Only apply for values >= 2.0; values in (1.0, 2.0) are ambiguous
    if factor >= 2.0 and factor <= 100.0:
        new_factor = factor / 100.0
        result.repairs.append(
            f"note {note_index}: factor {factor} interpreted as percentage, "
            f"normalized to {new_factor}"
        )
        factor = new_factor

    # Clamp near-boundary values
    if abs(factor) < 1e-9:
        factor = 0.0
    if abs(factor - 1.0) < 1e-9:
        factor = 1.0

    # Range check
    if not (0.0 <= factor <= 1.0):
        result.rejections.append(
            f"note {note_index}: factor {factor} outside [0, 1], downgraded"
        )
        return None

    return factor


def _validate_minimum_energy(
    item: dict,
    note_index: int,
    capacity_kwh: float,
    result: GuardrailResult,
) -> Optional[float]:
    """Validate minimum_battery_reserve energy. Returns value or None."""
    raw = item.get("minimum_energy_kwh")
    value = _coerce_float(raw)
    if value is None or not math.isfinite(value):
        result.rejections.append(
            f"note {note_index}: minimum_energy_kwh {raw!r} not finite, downgraded"
        )
        return None
    if value < 0:
        result.rejections.append(
            f"note {note_index}: minimum_energy_kwh {value} is negative, downgraded"
        )
        return None
    if value > capacity_kwh:
        result.repairs.append(
            f"note {note_index}: minimum_energy_kwh {value} exceeds "
            f"capacity {capacity_kwh}, clamped to {capacity_kwh}"
        )
        value = capacity_kwh
    return value


def _validate_max_grid(
    item: dict,
    note_index: int,
    result: GuardrailResult,
) -> Optional[float]:
    """Validate max_grid_window cap. Returns value or None."""
    raw = item.get("max_grid_kwh")
    value = _coerce_float(raw)
    if value is None or not math.isfinite(value):
        result.rejections.append(
            f"note {note_index}: max_grid_kwh {raw!r} not finite, downgraded"
        )
        return None
    if value < 0:
        result.rejections.append(
            f"note {note_index}: max_grid_kwh {value} is negative, downgraded"
        )
        return None
    return value


# ─── Explanation sanitization ─────────────────────────────────────────────

def _sanitize_explanation(raw: object, directive_type: str) -> str:
    """Sanitize explanation: coerce, strip control chars, truncate."""
    if not isinstance(raw, str):
        raw = str(raw) if raw is not None else ""
    text = str(raw).strip()
    # Strip control characters and newlines
    text = _CONTROL_CHARS.sub(" ", text)
    text = text.replace("\n", " ").replace("\r", " ")
    # Collapse whitespace
    text = " ".join(text.split())
    # Truncate
    if len(text) > 300:
        text = text[:297] + "..."
    # Default if empty
    if not text:
        text = _DEFAULT_EXPLANATIONS.get(
            directive_type,
            "No explanation provided.",
        )
    return text


# ─── Downgrade helper ─────────────────────────────────────────────────────

def _make_no_op(
    note_index: int,
    item: dict,
    result: GuardrailResult,
) -> Directive:
    """Create a downgraded no_op Directive."""
    explanation = _sanitize_explanation(
        item.get("explanation", ""),
        "no_op",
    )
    return Directive(
        note_index=note_index,
        directive_type="no_op",
        hours=(),
        explanation=explanation,
        source="downgraded_no_op",
    )


# ─── Coercion helpers ─────────────────────────────────────────────────────

def _coerce_int(
    value: object,
    result: GuardrailResult,
    field_name: str,
) -> Optional[int]:
    """Coerce value to int, recording repairs. Returns None on failure."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if math.isfinite(value) and value == int(value):
            result.repairs.append(
                f"{field_name}: coerced float {value} to int {int(value)}"
            )
            return int(value)
        return None
    if isinstance(value, str):
        try:
            f = float(value)
            if math.isfinite(f) and f == int(f):
                result.repairs.append(
                    f"{field_name}: coerced string {value!r} to int {int(f)}"
                )
                return int(f)
        except (ValueError, OverflowError):
            pass
        return None
    return None


def _coerce_int_silent(value: object) -> Optional[int]:
    """Coerce value to int without recording repairs."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if math.isfinite(value) and value == int(value):
            return int(value)
        return None
    if isinstance(value, str):
        try:
            f = float(value)
            if math.isfinite(f) and f == int(f):
                return int(f)
        except (ValueError, OverflowError):
            pass
        return None
    return None


def _coerce_float(value: object) -> Optional[float]:
    """Coerce value to float. Returns None on failure."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except (ValueError, OverflowError):
            return None
    return None
