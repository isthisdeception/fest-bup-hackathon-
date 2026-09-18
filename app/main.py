"""
FastAPI application for GridWise energy optimization.

Routes:
  GET  /health           -> {"status": "ok"}  (readiness probe)
  POST /optimize-energy  -> optimization result or error

Error envelope (all non-200):
  {"error": "<machine_code>", "detail": "<safe text>"}
  500s also include "error_id" for server-side correlation.
"""

import json
import logging
import re
import time
import traceback
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from pydantic import ValidationError

from app.config import settings
from app.schemas import DomainValidationError, parse_request

# ─── Secret-scrubbing log filter ──────────────────────────────────────────

# Compile once: matches common secret-ish patterns
_SECRET_PATTERNS = re.compile(
    r"api[_\-]?key|authorization|bearer|token|secret",
    re.IGNORECASE,
)


class _SecretScrubFilter(logging.Filter):
    """Strip literal API-key values from every log record.

    Belt-and-braces defense against httpx or provider clients embedding
    the key (e.g. as a query parameter in the request URL) into exception
    messages or log output.
    """

    def __init__(self, key_value: str) -> None:
        super().__init__()
        self._key = key_value

    def filter(self, record: logging.LogRecord) -> bool:
        if self._key:
            record.msg = str(record.msg).replace(self._key, "***REDACTED***")
            if record.args:
                # Format args into the message and replace, then clear args
                try:
                    formatted = record.getMessage()
                    formatted = formatted.replace(self._key, "***REDACTED***")
                    record.msg = formatted
                    record.args = None
                except Exception:
                    pass
        return True


# ─── Logging ───────────────────────────────────────────────────────────────

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

# Install the scrubbing filter on the root logger so it catches everything
if settings.llm_api_key:
    logging.getLogger().addFilter(_SecretScrubFilter(settings.llm_api_key))


# ─── Sanitizer for error details ──────────────────────────────────────────

def _sanitize_detail(detail: str) -> str:
    """Truncate to 300 chars and strip anything that looks like a secret."""
    if len(detail) > 300:
        detail = detail[:297] + "..."
    # Remove any substrings matching secret-ish patterns
    detail = _SECRET_PATTERNS.sub("***", detail)
    # Also scrub literal API key if present
    if settings.llm_api_key:
        detail = detail.replace(settings.llm_api_key, "***REDACTED***")
    return detail


def _format_validation_errors(errors: list[dict]) -> str:
    """Build a compact, safe summary from Pydantic validation errors."""
    parts = []
    for err in errors[:10]:  # cap number of error entries
        loc = ".".join(str(x) for x in err.get("loc", []))
        msg = err.get("msg", "unknown error")
        parts.append(f"{loc}: {msg}" if loc else msg)
    return _sanitize_detail("; ".join(parts))


def _format_domain_errors(errors: list[dict]) -> str:
    """Build a compact, safe summary from DomainValidationError entries."""
    parts = []
    for err in errors[:10]:
        field = err.get("field", "")
        msg = err.get("msg", "unknown error")
        parts.append(f"{field}: {msg}" if field else msg)
    return _sanitize_detail("; ".join(parts))


# ─── FastAPI app ───────────────────────────────────────────────────────────

app = FastAPI(
    title="GridWise Energy Optimizer",
    description="BUP CSE Fest 2026 Preliminary – 24-hour energy scheduling with LLM directive interpretation",
    version="0.1.0",
)


# ─── Exception handlers ───────────────────────────────────────────────────

@app.exception_handler(RequestValidationError)
async def _request_validation_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """FastAPI's own path/query validation errors -> 400."""
    detail = _format_validation_errors(exc.errors())
    logger.warning("RequestValidationError: %s", detail)
    return JSONResponse(
        status_code=400,
        content={"error": "invalid_request", "detail": detail},
    )


@app.exception_handler(StarletteHTTPException)
async def _http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """Standard HTTP exceptions (404, 405, etc.) -> uniform error envelope."""
    error_code_map = {
        404: "not_found",
        405: "method_not_allowed",
    }
    code = error_code_map.get(exc.status_code, "http_error")
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": code, "detail": str(exc.detail)},
    )


@app.exception_handler(Exception)
async def _unhandled_exception_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    """Catch-all for any unhandled exception -> 500 with error_id."""
    error_id = uuid.uuid4().hex[:8]
    logger.error(
        "Unhandled exception [error_id=%s]:\n%s",
        error_id,
        traceback.format_exc(),
    )
    return JSONResponse(
        status_code=500,
        content={
            "error": "internal_error",
            "detail": "An internal error occurred.",
            "error_id": error_id,
        },
    )


# ─── Request timing middleware ─────────────────────────────────────────────

@app.middleware("http")
async def _timing_middleware(request: Request, call_next):
    """Log method, path, status, and elapsed ms for every request."""
    start = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - start) * 1000
    logger.info(
        "%s %s -> %d (%.1f ms)",
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
    )
    return response


# ─── Startup event ─────────────────────────────────────────────────────────

@app.on_event("startup")
async def _startup() -> None:
    """Log redacted config and warm up scipy/HiGHS on startup."""
    logger.info("GridWise starting up")
    logger.info("Settings (redacted): %s", settings.redacted())

    # Warmup: trivial LP solve to pre-load scipy/HiGHS import paths
    try:
        from app.optimizer import warmup_solve
        ok = warmup_solve()
        logger.info("Scipy/HiGHS warmup: %s", "ok" if ok else "failed (non-critical)")
    except Exception as exc:
        logger.warning("Warmup solve exception (non-critical): %s", exc)


# ─── GET /health ───────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict:
    """Readiness probe. No LLM call, no network I/O, no lazy init.

    Returns exactly {"status": "ok"} with HTTP 200.
    Source: Problem Statement Section 6.2; Participant Guide Section 08.
    """
    return {"status": "ok"}


# ─── POST /optimize-energy ─────────────────────────────────────────────────

@app.post("/optimize-energy")
async def optimize_energy(request: Request) -> JSONResponse:
    """Accept a 24-hour energy scenario and return an optimized schedule.

    Error mapping:
      - JSON parse error          -> 400  (malformed_json)
      - Structural violation      -> 400  (invalid_request)
      - Domain violation          -> 422  (unprocessable_scenario)
      - Internal / unexpected     -> 500  (internal_error)
    """
    # 1. Read raw body and parse JSON
    try:
        body_bytes = await request.body()
        if len(body_bytes) > 2 * 1024 * 1024:
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_request", "detail": "Payload exceeds 2 MB limit."},
            )
        payload = json.loads(body_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        detail = _sanitize_detail(str(exc))
        logger.warning("JSON parse error: %s", detail)
        return JSONResponse(
            status_code=400,
            content={"error": "malformed_json", "detail": detail},
        )

    # 2. Validate with parse_request (structural -> 400, domain -> 422)
    try:
        validated_request = parse_request(payload)
    except DomainValidationError as exc:
        detail = _format_domain_errors(exc.errors)
        logger.warning("Domain validation error: %s", detail)
        return JSONResponse(
            status_code=422,
            content={"error": "unprocessable_scenario", "detail": detail},
        )
    except ValidationError as exc:
        detail = _format_validation_errors(exc.errors())
        logger.warning("Structural validation error: %s", detail)
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_request", "detail": detail},
        )
    except Exception:
        error_id = uuid.uuid4().hex[:8]
        logger.error(
            "Unexpected validation error [error_id=%s]:\n%s",
            error_id,
            traceback.format_exc(),
        )
        return JSONResponse(
            status_code=500,
            content={
                "error": "internal_error",
                "detail": "An internal error occurred.",
                "error_id": error_id,
            },
        )

    # 3. Run pipeline
    try:
        from app.pipeline import run as pipeline_run

        result = await pipeline_run(validated_request)
        return JSONResponse(status_code=200, content=result)

    except Exception:
        error_id = uuid.uuid4().hex[:8]
        logger.error(
            "Pipeline error [error_id=%s]:\n%s",
            error_id,
            traceback.format_exc(),
        )
        return JSONResponse(
            status_code=500,
            content={
                "error": "internal_error",
                "detail": "An internal error occurred.",
                "error_id": error_id,
            },
        )


# ─── Uvicorn entry point ──────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=settings.port,
        log_level=settings.log_level.lower(),
    )
