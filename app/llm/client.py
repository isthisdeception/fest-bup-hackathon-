"""
LLM Client: Provider adapter over httpx.

Exposes:
  - `call_model(system_prompt, user_payload, json_schema=None, deadline=None) -> str`
  - `probe() -> bool`
  - Exceptions: `LLMError`, `LLMTimeoutError`, `LLMConfigError`, `LLMProviderError`

Features:
  - Sync httpx.Client lazily created and reused with keep-alive limits
  - Adapters for gemini, openai, groq, openrouter, ollama
  - Strict bounded retries (max 1 retry on timeout/transport/429/5xx, 0.4s backoff)
  - Fast fail with LLMConfigError on 400/401/403/404
  - Deadline honoring (monotonic timestamp)
  - Secret scrubbing on all error bodies and logs; never logs URLs or request headers
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# ─── Typed Exceptions ─────────────────────────────────────────────────────────

class LLMError(Exception):
    """Base exception for all LLM errors."""


class LLMTimeoutError(LLMError):
    """Raised when LLM call times out or exceeds request deadline."""


class LLMConfigError(LLMError):
    """Raised on configuration or client error (HTTP 400/401/403/404, missing key). Never retried."""


class LLMProviderError(LLMError):
    """Raised on provider failures (5xx, unexpected response structure, transport errors)."""


# ─── Secret Sanitization ──────────────────────────────────────────────────────

_SECRET_PATTERNS = re.compile(
    r"api[_\-]?key|authorization|bearer|token|secret",
    re.IGNORECASE,
)


def _sanitize_body(text: str, max_len: int = 200) -> str:
    """Truncate to max_len chars and scrub API keys and secret-like tokens."""
    if not text:
        return ""
    clean = " ".join(text.split())
    if len(clean) > max_len:
        clean = clean[:max_len] + "..."
    clean = _SECRET_PATTERNS.sub("***", clean)
    if settings.llm_api_key:
        clean = clean.replace(settings.llm_api_key, "***REDACTED***")
    if settings.backup_llm_api_key:
        clean = clean.replace(settings.backup_llm_api_key, "***REDACTED***")
    return clean


# ─── Client Singleton ─────────────────────────────────────────────────────────

_client: httpx.Client | None = None


def _get_client() -> httpx.Client:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.Client(
            timeout=httpx.Timeout(settings.llm_timeout_s, connect=3.0),
            limits=httpx.Limits(max_keepalive_connections=8, max_connections=16),
        )
    return _client


# ─── Provider Adapters ────────────────────────────────────────────────────────

def _extract_gemini(data: dict) -> str:
    candidates = data.get("candidates", [])
    if not candidates:
        prompt_feedback = data.get("promptFeedback")
        raise LLMProviderError(f"Gemini returned no candidates (promptFeedback: {prompt_feedback})")
    content = candidates[0].get("content", {})
    parts = content.get("parts", [])
    if not parts:
        finish_reason = candidates[0].get("finishReason", "UNKNOWN")
        raise LLMProviderError(f"Gemini response has no parts (finishReason: {finish_reason})")
    return parts[0].get("text", "")


def _extract_openai(data: dict) -> str:
    choices = data.get("choices", [])
    if not choices:
        raise LLMProviderError("OpenAI-compatible provider returned no choices in response")
    message = choices[0].get("message", {})
    content = message.get("content", "")
    return str(content) if content is not None else ""


def _extract_ollama(data: dict) -> str:
    message = data.get("message", {})
    return str(message.get("content", ""))


def _to_gemini_schema(schema: dict | None) -> dict | None:
    """Convert standard JSON Schema to Gemini v1beta proto-compatible schema."""
    if not isinstance(schema, dict):
        return schema
    s: dict[str, Any] = {}
    for k, v in schema.items():
        if k == "additionalProperties":
            continue
        elif k == "type" and isinstance(v, list):
            types = [t for t in v if t != "null"]
            s["type"] = types[0] if types else "string"
            if "null" in v:
                s["nullable"] = True
        elif isinstance(v, dict):
            s[k] = _to_gemini_schema(v)
        elif isinstance(v, list):
            s[k] = [_to_gemini_schema(x) if isinstance(x, dict) else x for x in v]
        else:
            s[k] = v
    return s


def _prepare_request(
    system_prompt: str,
    user_payload: str,
    json_schema: dict | None,
    provider: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
) -> tuple[str, dict[str, str], dict[str, Any], Callable[[dict], str]]:
    """Build URL, headers, JSON body, and extraction function for configured provider."""
    chosen_provider = (provider or settings.llm_provider or "").lower().strip()
    if not chosen_provider:
        raise LLMConfigError("LLM_PROVIDER is not configured")

    chosen_model = model or settings.llm_model or ""
    if not chosen_model:
        raise LLMConfigError("LLM_MODEL is not configured")

    chosen_key = api_key if api_key is not None else settings.llm_api_key
    if chosen_provider != "ollama" and not chosen_key:
        raise LLMConfigError(f"LLM_API_KEY is not configured for provider '{chosen_provider}'")

    chosen_base = base_url if base_url is not None else settings.llm_base_url

    if chosen_provider == "gemini":
        default_base = "https://generativelanguage.googleapis.com/v1beta"
        base_endpoint = chosen_base.rstrip("/") if chosen_base else default_base
        url = f"{base_endpoint}/models/{chosen_model}:generateContent"
        headers = {
            "x-goog-api-key": chosen_key,
            "Content-Type": "application/json",
        }
        gen_config: dict[str, Any] = {
            "temperature": settings.llm_temperature,
            "maxOutputTokens": settings.llm_max_output_tokens,
        }
        if json_schema is not None:
            gen_config["response_mime_type"] = "application/json"
            gen_config["response_schema"] = _to_gemini_schema(json_schema)

        body: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": user_payload}]}],
            "generationConfig": gen_config,
        }
        if system_prompt:
            body["system_instruction"] = {"parts": [{"text": system_prompt}]}

        return url, headers, body, _extract_gemini

    elif chosen_provider in ("openai", "groq", "openrouter"):
        default_base = {
            "openai": "https://api.openai.com/v1",
            "groq": "https://api.groq.com/openai/v1",
            "openrouter": "https://openrouter.ai/api/v1",
        }[chosen_provider]
        base_endpoint = chosen_base.rstrip("/") if chosen_base else default_base
        url = f"{base_endpoint}/chat/completions"
        headers = {
            "Authorization": f"Bearer {chosen_key}",
            "Content-Type": "application/json",
        }
        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_payload})

        body = {
            "model": chosen_model,
            "messages": messages,
            "temperature": settings.llm_temperature,
            "max_tokens": settings.llm_max_output_tokens,
        }
        if json_schema is not None:
            if chosen_provider == "openai":
                body["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "interpretation",
                        "strict": True,
                        "schema": json_schema,
                    },
                }
            else:
                body["response_format"] = {"type": "json_object"}

        return url, headers, body, _extract_openai

    elif chosen_provider == "ollama":
        base_endpoint = chosen_base.rstrip("/") if chosen_base else "http://localhost:11434"
        url = f"{base_endpoint}/api/chat"
        headers = {"Content-Type": "application/json"}
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_payload})

        body = {
            "model": chosen_model,
            "messages": messages,
            "options": {
                "temperature": settings.llm_temperature,
                "num_predict": settings.llm_max_output_tokens,
            },
            "stream": False,
        }
        if json_schema is not None:
            body["format"] = json_schema

        return url, headers, body, _extract_ollama

    else:
        raise LLMConfigError(f"Unsupported LLM_PROVIDER: {chosen_provider}")


# ─── Core Function ────────────────────────────────────────────────────────────

def call_model(
    system_prompt: str,
    user_payload: str,
    json_schema: dict | None = None,
    deadline: float | None = None,
    provider: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
) -> str:
    """Call configured LLM with system prompt, user payload, and optional JSON schema.

    Parameters
    ----------
    system_prompt : str
        System instructions / task description.
    user_payload : str
        The prompt or serialized context for the model.
    json_schema : dict | None
        Optional structured-output JSON schema to enforce.
    deadline : float | None
        Monotonic timestamp (`time.monotonic()`) after which no further calls or
        retries will be initiated.
    provider : str | None
        Optional provider override (e.g. backup provider).
    model : str | None
        Optional model override.
    api_key : str | None
        Optional API key override.
    base_url : str | None
        Optional base URL override.

    Returns
    -------
    str
        Raw text returned by model.

    Raises
    ------
    LLMTimeoutError : on request timeout or deadline expiry.
    LLMConfigError : on configuration/client errors (400/401/403/404, missing key).
    LLMProviderError : on server 5xx or unparseable provider envelope.
    """
    url, headers, body, extractor = _prepare_request(
        system_prompt, user_payload, json_schema, provider, model, api_key, base_url
    )

    for attempt in range(1, 3):
        now = time.monotonic()
        if deadline is not None and now >= deadline:
            raise LLMTimeoutError("Deadline exceeded before LLM call attempt")

        if deadline is not None:
            remaining = deadline - now
            if remaining <= 0:
                raise LLMTimeoutError("Deadline exceeded before LLM call attempt")
            per_call_timeout = min(settings.llm_timeout_s, remaining)
        else:
            per_call_timeout = settings.llm_timeout_s

        timeout_obj = httpx.Timeout(per_call_timeout, connect=min(3.0, per_call_timeout))
        client = _get_client()

        try:
            resp = client.post(url, headers=headers, json=body, timeout=timeout_obj)

            if resp.status_code in (400, 401, 403, 404):
                # Configuration / auth error -> single fast failure, no retry
                raise LLMConfigError(f"HTTP {resp.status_code}: {_sanitize_body(resp.text)}")

            if resp.status_code == 429:
                if attempt < 2:
                    retry_after = resp.headers.get("Retry-After")
                    try:
                        wait_s = float(retry_after) if retry_after else 2.0
                    except (ValueError, TypeError):
                        wait_s = 2.0
                    wait_s = min(wait_s, 3.0)
                    now = time.monotonic()
                    if deadline is not None and now + wait_s >= deadline:
                        raise LLMTimeoutError(f"Deadline exceeded during 429 backoff")
                    time.sleep(wait_s)
                    continue
                raise LLMProviderError(f"HTTP 429: {_sanitize_body(resp.text)}")

            if resp.status_code >= 500:
                if attempt < 2:
                    now = time.monotonic()
                    if deadline is not None and now + 0.4 >= deadline:
                        raise LLMTimeoutError(f"Deadline exceeded during retry wait (HTTP {resp.status_code})")
                    time.sleep(0.4)
                    continue
                raise LLMProviderError(f"HTTP {resp.status_code}: {_sanitize_body(resp.text)}")

            if resp.status_code != 200:
                raise LLMProviderError(f"HTTP {resp.status_code}: {_sanitize_body(resp.text)}")

            data = resp.json()
            result_text = extractor(data)
            logger.debug("Raw model output: %s", result_text)
            return result_text

        except (LLMConfigError, LLMTimeoutError):
            raise
        except httpx.TimeoutException as exc:
            if attempt < 2:
                now = time.monotonic()
                if deadline is not None and now + 0.4 >= deadline:
                    raise LLMTimeoutError(f"Deadline exceeded during retry wait after timeout: {exc.__class__.__name__}")
                time.sleep(0.4)
                continue
            raise LLMTimeoutError(f"LLM request timed out: {exc.__class__.__name__}")
        except httpx.TransportError as exc:
            if attempt < 2:
                now = time.monotonic()
                if deadline is not None and now + 0.4 >= deadline:
                    raise LLMTimeoutError(f"Deadline exceeded during retry wait after transport error: {exc.__class__.__name__}")
                time.sleep(0.4)
                continue
            raise LLMProviderError(f"LLM transport error: {exc.__class__.__name__}")
        except LLMProviderError:
            raise
        except Exception as exc:
            raise LLMProviderError(f"Unexpected error during LLM call: {exc.__class__.__name__}: {_sanitize_body(str(exc))}")

    raise LLMProviderError("LLM call loop exhausted without result")


# ─── Reachability Probe ───────────────────────────────────────────────────────

def probe() -> bool:
    """Perform a 1-token 'reply ok' call to verify provider reachability.

    Returns True if reachable and returns a non-empty response, False otherwise.
    Never raises.
    """
    try:
        reply = call_model(
            system_prompt="",
            user_payload="Reply with the single word ok",
            json_schema=None,
            deadline=time.monotonic() + 10.0,
        )
        return bool(reply and reply.strip())
    except Exception as exc:
        logger.warning("LLM probe failed: %s", exc)
        return False
