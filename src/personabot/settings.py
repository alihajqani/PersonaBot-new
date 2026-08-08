"""Runtime configuration, loaded from ``.env`` / the environment.

This module deliberately does **nothing** on import. The predecessor
(``config.py``) loaded ``.env``, cleared every root logging handler, mutated
``os.environ``, built a global key manager, and raised ``ValueError`` when
``BASE_FORM_URL`` was unset — all as import side effects. That made it
impossible to import any part of the system without a populated ``.env``, and
impossible to unit-test anything at all.

Here, ``Settings`` is an ordinary immutable object constructed once in
``cli.py`` and passed down explicitly. Nothing else reads the environment.

Scope note: this layer holds **only** deployment concerns — credentials,
endpoints, concurrency, browser and Tor wiring. Nothing about any particular
survey lives here; that belongs in the survey YAML (see
``personabot.config.survey_spec``).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from personabot.errors import ConfigError

# Keys are enumerated rather than declared, so an arbitrary number can be
# supplied. Bounded to keep a typo from causing an unbounded scan.
_MAX_API_KEYS = 64
_KEY_PATTERN = re.compile(r"^GEMINI_API_KEY_(\d+)$")


class Settings(BaseSettings):
    """Immutable runtime configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # ===== SURVEY SELECTION =====
    # Which instrument to run. Everything domain-specific lives behind this path.
    survey_config: Path = Field(
        default=Path("configs/surveys/_template.yaml"),
        description="Path to the survey spec YAML.",
    )

    # ===== LLM BACKEND =====
    ai_provider: Literal["gemini", "vllm"] = "gemini"

    gemini_model_name: str = "gemini-2.5-pro"
    gemini_proxy: str | None = None

    vllm_base_url: str = "http://localhost:8000/v1"
    vllm_model_name: str = ""
    vllm_api_key: SecretStr = SecretStr("EMPTY")

    llm_timeout_seconds: int = Field(default=180, gt=0)
    llm_max_retries: int = Field(default=3, ge=0)

    # Legacy pacing knob. With a real rate limiter and bounded concurrency this
    # should normally stay 0; kept because free-tier Gemini still benefits.
    ai_call_delay_seconds: float = Field(default=0.0, ge=0)

    persona_batch_size: int = Field(default=10, gt=0)

    # ===== CONCURRENCY =====
    concurrent_llm_tasks: int = Field(
        default=4, ge=1, le=64,
        description="Max in-flight persona/answer generations.",
    )
    concurrent_submissions: int = Field(
        default=2, ge=1, le=32,
        description="Max in-flight browser submissions in this process.",
    )

    # ===== FORM / BROWSER =====
    # Optional at construction: phases 2 and 3 never touch the network, and
    # `status` and `validate-config` must work with no form configured at all.
    # Phases 1 and 4 call `require_form_url()`.
    base_form_url: str | None = None

    headless_mode: bool = True
    slow_mo: int = Field(default=50, ge=0)
    browser_channel: str | None = "chrome"
    max_pages: int = Field(
        default=40, ge=1,
        description="Hard ceiling on form pagination. Replaces `while True`.",
    )
    submission_delay_seconds: float = Field(default=15.0, ge=0)

    # ===== TOR =====
    use_tor: bool = False
    tor_socks_host: str = "127.0.0.1"
    tor_socks_port: int = Field(default=9050, gt=0, lt=65536)
    tor_control_port: int = Field(default=9051, gt=0, lt=65536)
    tor_control_password: SecretStr = SecretStr("")

    # ===== OUTPUT =====
    output_dir: Path = Path("output")

    # ===== LOGGING =====
    log_level: str = "INFO"

    # Populated from GEMINI_API_KEY_1..N; see `_collect_api_keys`.
    gemini_api_keys: tuple[SecretStr, ...] = ()

    # ---------------------------------------------------------------- validators

    @field_validator("browser_channel", mode="before")
    @classmethod
    def _blank_channel_is_none(cls, v: object) -> object:
        """An empty BROWSER_CHANNEL means "use Playwright's bundled Chromium"."""
        return v or None

    @field_validator("gemini_proxy", mode="before")
    @classmethod
    def _blank_proxy_is_none(cls, v: object) -> object:
        return v or None

    @model_validator(mode="after")
    def _collect_api_keys(self) -> "Settings":
        """Gather GEMINI_API_KEY_* from the environment.

        Scans the whole namespace and sorts by index instead of counting upward
        from 1 and stopping at the first gap. The old behaviour meant defining
        `_1` and `_3` silently loaded exactly one key — the second was simply
        never seen, and the only symptom was hitting quota sooner than expected.
        """
        if self.gemini_api_keys:
            return self

        found: list[tuple[int, str]] = []
        for name, value in os.environ.items():
            match = _KEY_PATTERN.match(name)
            if match and value.strip():
                index = int(match.group(1))
                if index <= _MAX_API_KEYS:
                    found.append((index, value.strip()))

        found.sort(key=lambda pair: pair[0])
        keys = tuple(SecretStr(value) for _, value in found)
        object.__setattr__(self, "gemini_api_keys", keys)
        return self

    # ------------------------------------------------------------------ helpers

    def require_form_url(self) -> str:
        """Return BASE_FORM_URL, or explain which phases need it.

        Called by phases 1 and 4 at the point of use, so that phases 2, 3 and the
        read-only commands remain runnable without one.
        """
        if not self.base_form_url:
            raise ConfigError(
                "BASE_FORM_URL is not set. It is required by phase 1 (schema "
                "extraction) and phase 4 (form submission). Phases 2 and 3, "
                "`status` and `validate-config` do not need it."
            )
        return self.base_form_url

    def require_llm_ready(self) -> None:
        """Validate the selected LLM backend is usable before a phase starts."""
        if self.ai_provider == "gemini" and not self.gemini_api_keys:
            raise ConfigError(
                "AI_PROVIDER=gemini but no GEMINI_API_KEY_* was found. Define at "
                "least GEMINI_API_KEY_1 in .env."
            )
        if self.ai_provider == "vllm" and not self.vllm_model_name:
            raise ConfigError("AI_PROVIDER=vllm but VLLM_MODEL_NAME is not set.")

    @property
    def tor_proxy_server(self) -> str:
        return f"socks5://{self.tor_socks_host}:{self.tor_socks_port}"

    # --- output layout (the filesystem is the state machine) ---

    @property
    def schema_file(self) -> Path:
        return self.output_dir / "form_schema.json"

    @property
    def personas_dir(self) -> Path:
        return self.output_dir / "personas"

    @property
    def answers_dir(self) -> Path:
        return self.output_dir / "answers"

    @property
    def receipts_dir(self) -> Path:
        return self.output_dir / "receipts"

    def ensure_directories(self) -> None:
        """Create the output tree. Idempotent."""
        for path in (
            self.output_dir,
            self.personas_dir,
            self.personas_dir / "done",
            self.answers_dir,
            self.answers_dir / "done",
            self.answers_dir / "inprogress",
            self.receipts_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


def load_settings(**overrides: object) -> Settings:
    """Build a Settings instance. The only place the environment is read.

    CLI flags arrive as `overrides` and win over `.env`.
    """
    try:
        return Settings(**overrides)  # type: ignore[arg-type]
    except Exception as exc:  # pydantic ValidationError carries the field paths
        raise ConfigError(f"Invalid configuration: {exc}") from exc
