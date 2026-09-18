"""
LLM Interpreter: Call model, robust JSON extraction, repair retry, and safe degradation.

TRADE-OFF DOCUMENTATION:
Degrading unparseable or failed LLM output to `no_op` forfeits that case's
interpretation credit but preserves schema validity, energy balance, physical
feasibility, and a successful HTTP 200 response. The alternative — throwing an
unhandled error or returning HTTP 500 — would forfeit the same interpretation credit
AND incur severe reliability and failure-rate penalties (Participant Guide Section 08).
Graceful degradation to safe `no_op` directives is therefore the strictly
score-maximizing failure mode.
"""

from __future__ import annotations

from collections import OrderedDict
import hashlib
import json
import logging
import threading
import time
from typing import Any

from app.config import settings
from app.directives import Directive
from app.guardrails import GuardrailResult, validate_interpretation
from app.llm.client import (
    LLMConfigError,
    LLMError,
    LLMProviderError,
    LLMTimeoutError,
    call_model,
)
from app.llm.prompt import OUTPUT_JSON_SCHEMA, PROMPT_VERSION, SYSTEM_PROMPT, build_user_payload
from app.schemas import Scenario, to_scenario

logger = logging.getLogger(__name__)

# Global module degradation counter
DEGRADATION_COUNT: int = 0

# Interpretation LRU Cache (Step 22)
_INTERPRET_CACHE: OrderedDict[str, list[Directive]] = OrderedDict()
_CACHE_LOCK = threading.Lock()


def _compute_cache_key(notes: list[str], sc: Scenario) -> str:
    """Compute semantically safe sha256 cache key including battery params and model version."""
    parts = (
        settings.llm_provider,
        settings.llm_model,
        PROMPT_VERSION,
        tuple(notes),
        round(float(sc.capacity_kwh), 4),
        round(float(sc.initial_energy_kwh), 4),
        round(float(sc.minimum_energy_kwh), 4),
        round(float(sc.max_charge_kwh_per_hour), 4),
        round(float(sc.max_discharge_kwh_per_hour), 4),
    )
    return hashlib.sha256(repr(parts).encode("utf-8")).hexdigest()


def _record_degradation(reason: str) -> None:
    """Increment module counter and log degradation event."""
    global DEGRADATION_COUNT
    DEGRADATION_COUNT += 1
    logger.warning("LLM interpretation degraded (total_count=%d, reason=%s)", DEGRADATION_COUNT, reason)


def _all_no_op(note_count: int, reason: str = "LLM unavailable or degraded") -> list[Directive]:
    """Generate safe no_op directives for all notes."""
    return [
        Directive(
            note_index=i,
            directive_type="no_op",
            hours=(),
            explanation=f"Safe fallback: {reason}.",
            source="downgraded_no_op",
        )
        for i in range(note_count)
    ]


# ─── Robust JSON Parser ───────────────────────────────────────────────────────

def _extract_balanced(text: str, open_char: str, close_char: str) -> str | None:
    """Find the first open_char and extract up to its balanced close_char.

    Accounts for quotes and backslash escapes so nested braces inside strings
    do not distort depth counting.
    """
    start = text.find(open_char)
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if not in_string:
            if ch == open_char:
                depth += 1
            elif ch == close_char:
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
    return None


def _strip_fences(text: str) -> str:
    """Strip leading and trailing markdown code fences (```json ... ```)."""
    lines = text.strip().splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def parse_model_json_with_mode(text: str) -> tuple[object | None, str]:
    """Parse raw model output into a Python object using multi-stage fallback.

    Returns (parsed_object, mode) where mode is one of:
      'direct', 'fenced', 'outer_object', 'outer_array', 'failed'.
    Never raises.
    """
    if not text or not isinstance(text, str):
        return None, "failed"

    cleaned = text.strip()

    # 1. Direct JSON parse
    try:
        return json.loads(cleaned), "direct"
    except Exception:
        pass

    # 2. Strip Markdown code fences
    fenced = _strip_fences(cleaned)
    if fenced != cleaned:
        try:
            return json.loads(fenced), "fenced"
        except Exception:
            pass

    # 3 & 4. Extract outermost balanced container ({...} or [...])
    start_brace = cleaned.find("{")
    start_bracket = cleaned.find("[")

    if start_brace != -1 and (start_bracket == -1 or start_brace < start_bracket):
        # Object starts first
        obj_str = _extract_balanced(cleaned, "{", "}")
        if obj_str:
            try:
                return json.loads(obj_str), "outer_object"
            except Exception:
                pass
        if start_bracket != -1:
            arr_str = _extract_balanced(cleaned, "[", "]")
            if arr_str:
                try:
                    return json.loads(arr_str), "outer_array"
                except Exception:
                    pass
    elif start_bracket != -1:
        # Array starts first
        arr_str = _extract_balanced(cleaned, "[", "]")
        if arr_str:
            try:
                return json.loads(arr_str), "outer_array"
            except Exception:
                pass
        if start_brace != -1:
            obj_str = _extract_balanced(cleaned, "{", "}")
            if obj_str:
                try:
                    return json.loads(obj_str), "outer_object"
                except Exception:
                    pass

    return None, "failed"


def parse_model_json(text: str) -> object | None:
    """Parse raw model text into a Python object or return None on failure. Never raises."""
    obj, _ = parse_model_json_with_mode(text)
    return obj


# ─── Single Model Invocation ──────────────────────────────────────────────────

def interpret(
    notes: list[str],
    scenario: Any,
    deadline: float | None = None,
    feedback: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
) -> tuple[object | None, dict[str, Any]]:
    """Interpret natural-language operator notes using configured or override LLM.

    Parameters
    ----------
    notes : list[str]
        List of raw operator note strings.
    scenario : Any
        Scenario object, OptimizeRequest, or dict providing battery parameters.
    deadline : float | None
        Monotonic timestamp (`time.monotonic()`) bounding the execution.
    feedback : str | None
        Optional validation rejection reasons to guide repair attempt.
    provider : str | None
        Optional provider override (e.g. backup provider).
    model : str | None
        Optional model override.
    api_key : str | None
        Optional API key override.

    Returns
    -------
    tuple[object | None, dict[str, Any]]
        (parsed_object, diagnostics)
    """
    t0 = time.monotonic()
    user_payload = build_user_payload(notes, scenario, validation_feedback=feedback)

    try:
        raw_text = call_model(
            system_prompt=SYSTEM_PROMPT,
            user_payload=user_payload,
            json_schema=OUTPUT_JSON_SCHEMA,
            deadline=deadline,
            provider=provider,
            model=model,
            api_key=api_key,
        )
    except LLMError:
        raise
    except Exception as exc:
        raise LLMProviderError(f"Unexpected error during interpret call: {exc.__class__.__name__}: {exc}") from exc

    elapsed_ms = round((time.monotonic() - t0) * 1000, 2)
    parsed_obj, parse_mode = parse_model_json_with_mode(raw_text)

    item_count = 0
    if isinstance(parsed_obj, dict):
        items = parsed_obj.get("interpretations")
        if isinstance(items, list):
            item_count = len(items)
    elif isinstance(parsed_obj, list):
        item_count = len(parsed_obj)

    logger.info(
        "LLM interpretation parsed: mode=%s, elapsed_ms=%.2f, items_found=%d",
        parse_mode,
        elapsed_ms,
        item_count,
    )
    logger.debug("Raw model output (first 1200 chars): %s", raw_text[:1200])

    diagnostics = {
        "attempts": 1,
        "elapsed_ms": elapsed_ms,
        "parse_mode": parse_mode,
        "cache_hit": False,
        "provider": provider or settings.llm_provider,
        "model": model or settings.llm_model,
    }

    return parsed_obj, diagnostics


# ─── Validated Pipeline Interpreter ───────────────────────────────────────────

def interpret_validated(
    notes: list[str],
    scenario: Any,
) -> tuple[list[Directive], dict[str, Any]]:
    """Execute bounded LLM interpretation with repair retry and safe degradation.

    Guarantees:
      - Total function: never raises an unhandled exception into the route.
      - Strictly bounded by settings.llm_total_budget_s (15s deadline).
      - At most 2 model calls (initial + single repair attempt).
      - On provider or config failure, degrades safely to valid all-no_op directives.
      - Per-note merge never regresses an already-validated directive.

    Parameters
    ----------
    notes : list[str]
        Raw operator notes from the request.
    scenario : Any
        Scenario object or OptimizeRequest.

    Returns
    -------
    tuple[list[Directive], dict[str, Any]]
        (validated_directives, diagnostics)
    """
    t0 = time.monotonic()
    deadline = t0 + settings.llm_total_budget_s
    note_count = len(notes)

    if note_count == 0:
        return [], {
            "attempts": 0,
            "elapsed_ms": 0.0,
            "degraded": False,
            "repaired": False,
            "repairs": [],
            "rejections": [],
        }

    # Ensure scenario is typed Scenario
    sc = scenario if isinstance(scenario, Scenario) else to_scenario(scenario)

    # ─── Cache Lookup ─────────────────────────────────────────────────────────
    if settings.llm_cache_enabled:
        cache_key = _compute_cache_key(notes, sc)
        with _CACHE_LOCK:
            if cache_key in _INTERPRET_CACHE:
                cached_directives = _INTERPRET_CACHE[cache_key]
                _INTERPRET_CACHE.move_to_end(cache_key)
                logger.info("Interpretation cache HIT: %d directives returned", len(cached_directives))
                return list(cached_directives), {
                    "attempts": 0,
                    "elapsed_ms": round((time.monotonic() - t0) * 1000, 2),
                    "degraded": False,
                    "repaired": False,
                    "cached": True,
                    "repairs": [],
                    "rejections": [],
                    "parse_mode": "cache",
                }

    try:
        # ─── Attempt 1 ────────────────────────────────────────────────────────
        raw_obj: object | None = None
        diag1: dict[str, Any] = {}
        gr1: GuardrailResult | None = None

        try:
            raw_obj, diag1 = interpret(notes, sc, deadline=deadline)
            gr1 = validate_interpretation(raw_obj, sc, note_count)
        except LLMConfigError as exc:
            _record_degradation(f"LLMConfigError: {exc}")
            logger.error("LLM config error; degrading all notes to no_op: %s", exc)
            return _all_no_op(note_count, reason="Configuration error"), {
                "attempts": 1,
                "elapsed_ms": round((time.monotonic() - t0) * 1000, 2),
                "degraded": True,
                "degradation_reason": str(exc),
                "repairs": [],
                "rejections": [],
            }
        except LLMTimeoutError as exc:
            _record_degradation(f"LLMTimeoutError: {exc}")
            logger.warning("LLM timeout; degrading all notes to no_op: %s", exc)
            return _all_no_op(note_count, reason="Request budget exhausted"), {
                "attempts": 1,
                "elapsed_ms": round((time.monotonic() - t0) * 1000, 2),
                "degraded": True,
                "degradation_reason": str(exc),
                "repairs": [],
                "rejections": [],
            }
        except LLMProviderError as exc:
            # Check backup provider if configured
            if settings.backup_llm_provider and settings.backup_llm_api_key and (deadline - time.monotonic() > 4.0):
                logger.info("Primary LLM provider failed, trying backup provider: %s", settings.backup_llm_provider)
                try:
                    raw_obj, diag1 = interpret(
                        notes,
                        sc,
                        deadline=deadline,
                        provider=settings.backup_llm_provider,
                        model=settings.backup_llm_model,
                        api_key=settings.backup_llm_api_key,
                    )
                    gr1 = validate_interpretation(raw_obj, sc, note_count)
                except Exception as backup_exc:
                    _record_degradation(f"BackupProviderFailed: {backup_exc}")
                    logger.warning("Backup provider also failed: %s; degrading all notes to no_op", backup_exc)
                    return _all_no_op(note_count, reason="Providers unavailable"), {
                        "attempts": 2,
                        "elapsed_ms": round((time.monotonic() - t0) * 1000, 2),
                        "degraded": True,
                        "degradation_reason": f"Primary and backup failed: {backup_exc}",
                        "repairs": [],
                        "rejections": [],
                    }
            else:
                _record_degradation(f"LLMProviderError: {exc}")
                logger.warning("LLM provider error; degrading all notes to no_op: %s", exc)
                return _all_no_op(note_count, reason="Provider error"), {
                    "attempts": 1,
                    "elapsed_ms": round((time.monotonic() - t0) * 1000, 2),
                    "degraded": True,
                    "degradation_reason": str(exc),
                    "repairs": [],
                    "rejections": [],
                }

        # ─── Check Single Repair Retry ────────────────────────────────────────
        attempts = 1
        repaired = False
        final_directives = gr1.directives
        repairs_accum = list(gr1.repairs)
        rejections_accum = list(gr1.rejections)

        any_downgraded = any(d.source == "downgraded_no_op" for d in gr1.directives)
        remaining_budget = deadline - time.monotonic()

        if (gr1.needs_retry or any_downgraded) and remaining_budget > 5.0:
            rejection_text = "; ".join(gr1.rejections) if gr1.rejections else "Some notes were downgraded to no_op due to validation constraints."
            feedback = (
                f"Your previous response was rejected by validation: {rejection_text[:400]}. "
                f"Return corrected JSON for ALL notes, following the schema and rules exactly."
            )
            logger.info("Triggering single repair attempt (remaining_budget=%.2fs): %s", remaining_budget, feedback[:150])

            try:
                repair_obj, _ = interpret(notes, sc, deadline=deadline, feedback=feedback)
                gr_repair = validate_interpretation(repair_obj, sc, note_count)
                attempts = 2
                repairs_accum.extend(gr_repair.repairs)
                rejections_accum.extend(gr_repair.rejections)

                # Merge: prefer validated directive from repair over downgraded from attempt 1
                d1_map = {d.note_index: d for d in gr1.directives}
                d2_map = {d.note_index: d for d in gr_repair.directives}
                merged: list[Directive] = []

                for i in range(note_count):
                    d1 = d1_map.get(i)
                    d2 = d2_map.get(i)

                    if d1 is not None and d1.source != "downgraded_no_op":
                        merged.append(d1)
                    elif d2 is not None and d2.source != "downgraded_no_op":
                        merged.append(d2)
                        repaired = True
                    elif d1 is not None:
                        merged.append(d1)
                    elif d2 is not None:
                        merged.append(d2)
                    else:
                        merged.append(
                            Directive(
                                note_index=i,
                                directive_type="no_op",
                                hours=(),
                                explanation="Safe fallback: note missing after repair attempt.",
                                source="downgraded_no_op",
                            )
                        )

                final_directives = merged

            except Exception as repair_exc:
                logger.warning("Repair attempt failed: %s; keeping attempt 1 directives", repair_exc)
                attempts = 2

        # ─── Ensure Complete Coverage ─────────────────────────────────────────
        final_map = {d.note_index: d for d in final_directives}
        complete_directives: list[Directive] = []
        for i in range(note_count):
            if i in final_map:
                complete_directives.append(final_map[i])
            else:
                complete_directives.append(
                    Directive(
                        note_index=i,
                        directive_type="no_op",
                        hours=(),
                        explanation="Missing note filled with no_op.",
                        source="downgraded_no_op",
                    )
                )
        complete_directives.sort(key=lambda d: d.note_index)

        # Total degradation check: true if all notes ended up downgraded
        is_degraded = (
            all(d.source == "downgraded_no_op" for d in complete_directives)
            and (raw_obj is None or gr1.needs_retry)
        )
        if is_degraded:
            _record_degradation("All notes downgraded after validation/repair")

        elapsed_ms = round((time.monotonic() - t0) * 1000, 2)

        # Cache only fully successful, non-degraded interpretations
        if settings.llm_cache_enabled and not is_degraded:
            cache_key = _compute_cache_key(notes, sc)
            with _CACHE_LOCK:
                _INTERPRET_CACHE[cache_key] = list(complete_directives)
                if len(_INTERPRET_CACHE) > settings.llm_cache_size:
                    _INTERPRET_CACHE.popitem(last=False)

        diagnostics = {
            "attempts": attempts,
            "elapsed_ms": elapsed_ms,
            "degraded": is_degraded,
            "repaired": repaired,
            "cached": False,
            "repairs": repairs_accum,
            "rejections": rejections_accum,
            "parse_mode": diag1.get("parse_mode", "direct"),
        }
        return complete_directives, diagnostics

    except Exception as fatal_exc:
        # Total function guarantee: catch anything unexpected and degrade safely
        _record_degradation(f"FatalException: {fatal_exc}")
        logger.error("Fatal exception in interpret_validated: %s", fatal_exc, exc_info=True)
        return _all_no_op(note_count, reason="Internal interpreter error"), {
            "attempts": 1,
            "elapsed_ms": round((time.monotonic() - t0) * 1000, 2),
            "degraded": True,
            "degradation_reason": str(fatal_exc),
            "repairs": [],
            "rejections": [],
        }
