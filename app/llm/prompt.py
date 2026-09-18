"""
LLM Prompt Design, Structured Output Schema, and User Payload Builder.

Exposes:
  - `PROMPT_VERSION`: String constant "v1" for cache keying
  - `OUTPUT_JSON_SCHEMA`: Flat strict JSON schema for LLM interpretation output
  - `SYSTEM_PROMPT`: Complete system prompt containing directive rules, normalization, and few-shots
  - `EXAMPLES`: Locally authored few-shot examples
  - `build_user_payload(notes, scenario) -> str`: Compact serialized context (battery + notes)
"""

from __future__ import annotations

import json
from typing import Any

PROMPT_VERSION: str = "v1"

# ─── Output JSON Schema ───────────────────────────────────────────────────────
# Flat per-item fields with explicit nulls to maximize strict schema compatibility
# across Gemini, OpenAI, Groq, OpenRouter, and Ollama.
# Note: `applies` is omitted by design; it is deterministically derived in Step 08.

OUTPUT_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "interpretations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "note_index": {"type": "integer"},
                    "directive_type": {
                        "type": "string",
                        "enum": [
                            "solar_reduction",
                            "minimum_battery_reserve",
                            "no_charge_window",
                            "no_discharge_window",
                            "max_grid_window",
                            "no_op",
                        ],
                    },
                    "hours": {
                        "type": "array",
                        "items": {"type": "integer"},
                    },
                    "factor": {"type": ["number", "null"]},
                    "minimum_energy_kwh": {"type": ["number", "null"]},
                    "max_grid_kwh": {"type": ["number", "null"]},
                    "explanation": {"type": "string"},
                },
                "required": [
                    "note_index",
                    "directive_type",
                    "hours",
                    "factor",
                    "minimum_energy_kwh",
                    "max_grid_kwh",
                    "explanation",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["interpretations"],
    "additionalProperties": False,
}

# ─── Locally Authored Few-Shot Examples ───────────────────────────────────────
# Freshly written examples covering each directive type and percentage conversion.
# Zero overlap with the public sample pack.

EXAMPLES: list[dict[str, Any]] = [
    {
        "note": "Inverter servicing will cut usable PV to a quarter of forecast between 09:00 and 11:00.",
        "interpretation": {
            "note_index": 0,
            "directive_type": "solar_reduction",
            "hours": [9, 10],
            "factor": 0.25,
            "minimum_energy_kwh": None,
            "max_grid_kwh": None,
            "explanation": "Usable PV reduced to 25% from 9 AM to 11 AM.",
        },
    },
    {
        "note": "Hold a minimum of 35% of pack capacity from 17:00 through 20:00 for the clinic.",
        "context": "battery capacity = 300 kWh",
        "interpretation": {
            "note_index": 0,
            "directive_type": "minimum_battery_reserve",
            "hours": [17, 18, 19],
            "factor": None,
            "minimum_energy_kwh": 105.0,
            "max_grid_kwh": None,
            "explanation": "Reserve 35% of 300 kWh (105 kWh) during hours 17, 18, 19.",
        },
    },
    {
        "note": "Charger breaker will be locked out from four in the morning until seven.",
        "interpretation": {
            "note_index": 0,
            "directive_type": "no_charge_window",
            "hours": [4, 5, 6],
            "factor": None,
            "minimum_energy_kwh": None,
            "max_grid_kwh": None,
            "explanation": "Battery charging disabled during hours 4, 5, 6.",
        },
    },
    {
        "note": "Relay commissioning means no battery export to loads from 9 AM to 11 AM.",
        "interpretation": {
            "note_index": 0,
            "directive_type": "no_discharge_window",
            "hours": [9, 10],
            "factor": None,
            "minimum_energy_kwh": None,
            "max_grid_kwh": None,
            "explanation": "Battery discharging disabled during hours 9 and 10.",
        },
    },
    {
        "note": "Substation work limits intake to a maximum of 140 kWh per hour between 7 PM and 9 PM.",
        "interpretation": {
            "note_index": 0,
            "directive_type": "max_grid_window",
            "hours": [19, 20],
            "factor": None,
            "minimum_energy_kwh": None,
            "max_grid_kwh": 140.0,
            "explanation": "Grid import capped at 140 kWh per hour during hours 19 and 20.",
        },
    },
    {
        "note": "The admin block will repaint stairwells over the weekend.",
        "interpretation": {
            "note_index": 0,
            "directive_type": "no_op",
            "hours": [],
            "factor": None,
            "minimum_energy_kwh": None,
            "max_grid_kwh": None,
            "explanation": "Unrelated maintenance activity not affecting 24-hour energy dispatch.",
        },
    },
]

# ─── System Prompt ────────────────────────────────────────────────────────────

SYSTEM_PROMPT: str = """You are a deterministic interpreter for a campus energy management system.
Your ONLY job is to convert each natural-language operator note into exactly one
structured directive. You never perform optimization, never compute a schedule,
and never invent data.

Return JSON only, matching the provided schema. No prose outside the JSON.

SUPPORTED DIRECTIVE TYPES (use no others):
1. solar_reduction        - usable solar output is reduced during specific hours.
                            Set "hours" and "factor".
2. minimum_battery_reserve- the battery must stay at or above an energy level during
                            specific hours. Set "hours" and "minimum_energy_kwh".
3. no_charge_window       - battery charging is unavailable during specific hours.
                            Set "hours" only.
4. no_discharge_window    - battery discharging is unavailable during specific hours.
                            Set "hours" only.
5. max_grid_window        - grid import must not exceed an amount in each of specific
                            hours. Set "hours" and "max_grid_kwh".
6. no_op                  - the note does NOT affect today's 24-hour energy schedule.
                            Set "hours" to [] and all numeric fields to null.

TIME WINDOW RULES:
- Hours are whole-hour integers 0-23 on a 24-hour clock.
- A window is START-INCLUSIVE and END-EXCLUSIVE.
  "1 PM to 3 PM" -> [13, 14]
  "from 6 PM until 10 PM" -> [18, 19, 20, 21]
  "between 13:00 and 15:00" -> [13, 14]
  "from 2 AM until 5 AM" -> [2, 3, 4]
  "from 6 PM through 9 PM" -> [18, 19, 20]
  "from 10 PM to midnight" -> [22, 23]
  "from midnight until 3 AM" -> [0, 1, 2]
- Apply the end-exclusive rule for every phrasing: "to", "until", "till", "through",
  "between X and Y", "X-Y".
- "noon" = 12. "midnight" = 0. "one until three" in an afternoon context = [13, 14].
- A single hour reference such as "at 3 PM", "during the 3 PM hour", or "during the 11 AM hour only" -> [hour], e.g. [11].
- Hours must be unique integers 0-23 in ASCENDING order.
- If a window would wrap past hour 23, keep only hours 0-23.

NUMERIC RULES:
- factor is the FRACTION OF SOLAR THAT REMAINS USABLE, as a decimal 0.0-1.0.
  "drop to 20% of forecast"        -> factor 0.2
  "an 80% drop" / "80% reduction"  -> factor 0.2
  "roughly one-fifth" / "a fifth"  -> factor 0.2
  "about half" / "halve solar"     -> factor 0.5
  "three quarters of forecast"     -> factor 0.75
  "fully offline" / "unavailable"  -> factor 0.0
  Never output a percentage such as 20 or 80. Always output the remaining fraction.
- minimum_energy_kwh is an absolute energy level in kWh.
  If the note states a percentage or fraction of battery capacity, convert it using the
  battery capacity given in the input.
  Example: "keep at least 50% of capacity" with capacity 200 kWh -> 100.
  Example: "hold half the battery in reserve" with capacity 200 kWh -> 100.
  Example: "minimum of 40% of pack capacity" with capacity 250 kWh -> 100.
- max_grid_kwh is an absolute per-hour grid import limit in kWh.
  "must not exceed 155 kWh", "at or below 155 kWh", "capped at 155 kWh" -> 155.

RELEVANCE RULES:
- A note is relevant ONLY if it changes solar availability, battery charging,
  battery discharging, a required battery reserve level, or a grid import limit
  for today's 24-hour horizon.
- Everything else is no_op:
  - Schedules of unrelated events, routines, exam notices, parking passes, hostel maintenance.
  - Notes about other days: "Yesterday's solar was..." (past), "Next week panels will be washed..." (future date).
  - Notes about different campuses or external sites: "at the Uttara campus".
- Never force a relevant-looking interpretation onto an unrelated note.
- Never modify demand, tariff, battery capacity, battery initial energy, the base
  minimum reserve, or the hourly charge/discharge rate limits. Those are inputs,
  not directives.

OUTPUT RULES:
- Return EXACTLY ONE object per operator note.
- note_index is the zero-based index of the note in the input array.
- Include every note exactly once, ordered by note_index ascending.
- Set unused numeric fields to null.
- explanation is one short sentence (under 200 characters).

EXAMPLES:
Example 1:
Note: "Inverter servicing will cut usable PV to a quarter of forecast between 09:00 and 11:00."
Output: {"note_index": 0, "directive_type": "solar_reduction", "hours": [9, 10], "factor": 0.25, "minimum_energy_kwh": null, "max_grid_kwh": null, "explanation": "Usable PV reduced to 25% from 9 AM to 11 AM."}

Example 2 (with battery capacity 300 kWh in payload):
Note: "Hold a minimum of 35% of pack capacity from 17:00 through 20:00 for the clinic."
Output: {"note_index": 0, "directive_type": "minimum_battery_reserve", "hours": [17, 18, 19], "factor": null, "minimum_energy_kwh": 105.0, "max_grid_kwh": null, "explanation": "Battery reserve kept at 105 kWh (35% of 300) between 17:00 and 20:00."}

Example 3:
Note: "Charger breaker will be locked out from four in the morning until seven."
Output: {"note_index": 0, "directive_type": "no_charge_window", "hours": [4, 5, 6], "factor": null, "minimum_energy_kwh": null, "max_grid_kwh": null, "explanation": "No charging permitted between 4 AM and 7 AM."}

Example 4:
Note: "Relay commissioning means no battery export to loads from 9 AM to 11 AM."
Output: {"note_index": 0, "directive_type": "no_discharge_window", "hours": [9, 10], "factor": null, "minimum_energy_kwh": null, "max_grid_kwh": null, "explanation": "No battery discharge permitted between 9 AM and 11 AM."}

Example 5:
Note: "Substation work limits intake to a maximum of 140 kWh per hour between 7 PM and 9 PM."
Output: {"note_index": 0, "directive_type": "max_grid_window", "hours": [19, 20], "factor": null, "minimum_energy_kwh": null, "max_grid_kwh": 140.0, "explanation": "Grid import capped at 140 kWh between 19:00 and 21:00."}

Example 6:
Note: "The admin block will repaint stairwells over the weekend."
Output: {"note_index": 0, "directive_type": "no_op", "hours": [], "factor": null, "minimum_energy_kwh": null, "max_grid_kwh": null, "explanation": "Unrelated maintenance activity not affecting 24-hour energy dispatch."}
"""

# ─── User Payload Builder ─────────────────────────────────────────────────────

def build_user_payload(
    notes: list[str],
    scenario: Any,
    validation_feedback: str | None = None,
) -> str:
    """Build compact JSON payload containing battery parameters and indexed operator notes.

    Omits hourly rows (demand, solar, tariff) to minimize token consumption and
    latency against the p95 <= 5s target.

    Parameters
    ----------
    notes : list[str]
        List of raw operator note strings.
    scenario : Any
        Scenario object, OptimizeRequest, or dict containing battery specification.
    validation_feedback : str | None
        Optional corrective feedback for repair attempts.

    Returns
    -------
    str
        Compact JSON string payload.
    """
    if hasattr(scenario, "battery") and getattr(scenario, "battery", None) is not None:
        b = scenario.battery
        if hasattr(b, "model_dump"):
            b_dict = b.model_dump()
        elif hasattr(b, "dict"):
            b_dict = b.dict()
        elif isinstance(b, dict):
            b_dict = b
        else:
            b_dict = {
                "capacity_kwh": getattr(b, "capacity_kwh", 0.0),
                "initial_energy_kwh": getattr(b, "initial_energy_kwh", 0.0),
                "minimum_energy_kwh": getattr(b, "minimum_energy_kwh", 0.0),
                "max_charge_kwh_per_hour": getattr(b, "max_charge_kwh_per_hour", 0.0),
                "max_discharge_kwh_per_hour": getattr(b, "max_discharge_kwh_per_hour", 0.0),
            }
    elif isinstance(scenario, dict) and "battery" in scenario:
        b_dict = scenario["battery"]
    elif hasattr(scenario, "capacity_kwh"):
        # Flat scenario object (e.g. app.schemas.Scenario)
        b_dict = {
            "capacity_kwh": getattr(scenario, "capacity_kwh", 0.0),
            "initial_energy_kwh": getattr(scenario, "initial_energy_kwh", 0.0),
            "minimum_energy_kwh": getattr(scenario, "minimum_energy_kwh", 0.0),
            "max_charge_kwh_per_hour": getattr(scenario, "max_charge_kwh_per_hour", 0.0),
            "max_discharge_kwh_per_hour": getattr(scenario, "max_discharge_kwh_per_hour", 0.0),
        }
    elif isinstance(scenario, dict) and "capacity_kwh" in scenario:
        b_dict = scenario
    else:
        b_dict = {}

    compact_battery = {
        "capacity_kwh": float(b_dict.get("capacity_kwh", 0.0)),
        "initial_energy_kwh": float(b_dict.get("initial_energy_kwh", 0.0)),
        "minimum_energy_kwh": float(b_dict.get("minimum_energy_kwh", 0.0)),
        "max_charge_kwh_per_hour": float(b_dict.get("max_charge_kwh_per_hour", 0.0)),
        "max_discharge_kwh_per_hour": float(b_dict.get("max_discharge_kwh_per_hour", 0.0)),
    }

    indexed_notes = [
        {"note_index": idx, "text": note[:2000] if len(note) > 2000 else note}
        for idx, note in enumerate(notes)
    ]

    payload: dict[str, Any] = {
        "battery": compact_battery,
        "operator_notes": indexed_notes,
    }
    if validation_feedback:
        payload["validation_feedback"] = validation_feedback

    return json.dumps(payload, separators=(",", ":"))
