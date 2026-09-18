"""
Centralized configuration module.

All tunables are sourced from environment variables with safe defaults.
`load_dotenv()` loads `.env` at import time (no-op in production containers
where real env vars are set).
"""

import os
import logging
from pathlib import Path

from dotenv import load_dotenv

# Resolve .env relative to the project root (parent of app/), not CWD.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")

logger = logging.getLogger(__name__)


def _env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid float for %s=%r, using default %s", name, raw, default)
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid int for %s=%r, using default %s", name, raw, default)
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("true", "1", "yes")


class Settings:
    """Application settings loaded from environment variables."""

    def __init__(self) -> None:
        # LLM provider settings
        self.llm_provider: str = _env_str("LLM_PROVIDER")
        self.llm_model: str = _env_str("LLM_MODEL")
        self.llm_api_key: str = _env_str("LLM_API_KEY")
        self.llm_base_url: str = _env_str("LLM_BASE_URL")

        # LLM tuning
        self.llm_timeout_s: float = _env_float("LLM_TIMEOUT_S", 8.0)
        self.llm_max_attempts: int = _env_int("LLM_MAX_ATTEMPTS", 2)
        self.llm_total_budget_s: float = _env_float("LLM_TOTAL_BUDGET_S", 15.0)
        self.llm_temperature: float = _env_float("LLM_TEMPERATURE", 0.0)
        self.llm_max_output_tokens: int = _env_int("LLM_MAX_OUTPUT_TOKENS", 700)

        # LLM cache
        self.llm_cache_size: int = _env_int("LLM_CACHE_SIZE", 256)
        self.llm_cache_enabled: bool = _env_bool("LLM_CACHE_ENABLED", True)

        # Backup LLM (Gate G3, Step 18)
        self.backup_llm_provider: str = _env_str("BACKUP_LLM_PROVIDER")
        self.backup_llm_model: str = _env_str("BACKUP_LLM_MODEL")
        self.backup_llm_api_key: str = _env_str("BACKUP_LLM_API_KEY")

        # Behavior
        self.strict_note_count: bool = _env_bool("STRICT_NOTE_COUNT", False)

        # Server
        self.port: int = _env_int("PORT", 8000)
        self.log_level: str = _env_str("LOG_LEVEL", "INFO")

        # Constants (not configurable via env)
        self.numeric_tolerance: float = 0.01       # Problem Statement Section 11.5
        self.internal_eps: float = 1e-6            # Internal comparisons
        self.prompt_version: str = "v1"            # Part of the cache key

    def redacted(self) -> dict:
        """Return all settings as a dict with API keys masked.

        Only this method may ever be logged - never log raw settings.
        """
        d = {}
        for key, value in self.__dict__.items():
            if "api_key" in key:
                d[key] = "***set***" if value else "***empty***"
            else:
                d[key] = value
        return d


# Module-level singleton
settings = Settings()

# Log configuration once at import (startup)
logger.info("Configuration loaded: provider=%s model=%s", settings.llm_provider, settings.llm_model)
logger.info("Full config (redacted): %s", settings.redacted())
