"""Deployment configuration, loaded from ``.env`` / the environment.

This module deliberately does **nothing** on import. The predecessor
(``config.py``) loaded ``.env``, cleared every root logging handler, mutated
``os.environ``, built a global key manager, and raised ``ValueError`` when
``BASE_FORM_URL`` was unset — all as import side effects. That made it
impossible to import any part of the system without a populated ``.env``, and
impossible to unit-test anything at all.

Here, ``Settings`` is an ordinary immutable object constructed once in
``cli.py`` and passed down explicitly. Nothing else reads the environment.

Scope note: this layer holds **only** deployment concerns — credentials,
endpoints, concurrency, browser and Tor wiring — which are the same for every
questionnaire run from this checkout. Everything about a particular survey,
including its form URL and output location, is bound in
:class:`personabot.workspace.Workspace`. In 2.x the output layout and the form
URL lived here, which is why each new questionnaire needed a copy of the whole
repository.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from personabot.errors import ConfigError

# Keys are enumerated rather than declared, so an arbitrary number can be
# supplied. Bounded to keep a typo from causing an unbounded scan.
_MAX_API_KEYS = 64
_KEY_PATTERN = re.compile(r"^GEMINI_API_KEY_(\d+)$", re.IGNORECASE)


class Settings(BaseSettings):
    """Immutable deployment configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # `allow`, not `ignore`: the numbered GEMINI_API_KEY_* entries are not
        # declared fields, and `ignore` would drop the ones defined in `.env`
        # before `_collect_api_keys` could see them.
        extra="allow",
        frozen=True,
    )

    # ===== SURVEY SELECTION =====
    # Which survey to run when the CLI does not say. `--survey`/`--config` win.
    survey: str | None = Field(
        default=None,
        description="Survey id: a folder name under SURVEYS_DIR.",
    )
    survey_config: Path | None = Field(
        default=None,
        description="Explicit path to a survey YAML; wins over SURVEY.",
    )
    surveys_dir: Path = Path("surveys")
    prompts_dir: Path = Path("prompts")
    # Where per-client worktrees live; default: `clients/` beside this checkout.
    clients_dir: Path | None = None

    # ===== LLM BACKEND =====
    ai_provider: Literal["gemini", "vllm"] = "gemini"

    gemini_model_name: str = "gemini-2.5-pro"
    gemini_proxy: str | None = None

    vllm_base_url: str = "http://localhost:8000/v1"
    vllm_model_name: str = ""
    vllm_api_key: SecretStr = SecretStr("EMPTY")

    llm_timeout_seconds: int = Field(default=180, gt=0)
    llm_max_retries: int = Field(default=3, ge=0)

    # The quota that actually binds on Gemini's free tier is input tokens per
    # minute, per project, per model — not requests. Rotating keys does not help
    # if the pool has no idea what each one has spent, so tell it the ceiling and
    # it will pace itself below it. Read the number off the model's row in
    # https://ai.dev/rate-limit (the TPM column). 0 disables pacing.
    gemini_input_tokens_per_minute: int = Field(default=0, ge=0)

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
    # A FALLBACK only. A survey declares its own URL in `form.url`, and that
    # always wins: a URL shared by every survey in `.env` must never redirect
    # survey A's answers into survey B's form. See `Workspace.form_url`.
    base_form_url: str | None = None

    headless_mode: bool = True
    slow_mo: int = Field(default=50, ge=0)
    browser_channel: str | None = "chrome"
    # What a context reports as its locale and timezone. A respondent in Tehran
    # does not browse with an en-US / UTC fingerprint.
    browser_locale: str = "fa-IR"
    browser_timezone: str = "Asia/Tehran"
    browser_incognito: bool = Field(
        default=True,
        description=(
            "Launch the browser in incognito/private mode. Playwright contexts "
            "are already isolated profiles, so this changes no storage semantics "
            "— it makes the window visibly incognito when running headful."
        ),
    )
    block_heavy_assets: bool = Field(
        default=True,
        description=(
            "Abort image, font and media requests. Forms need their scripts, not "
            "their banner artwork: on a slow link this was the difference between "
            "5 concurrent submissions taking 9s and one of them timing out after "
            "65s. Failure screenshots lose decorative images as a result."
        ),
    )
    max_pages: int = Field(
        default=40, ge=1,
        description=(
            "Default ceiling on form pagination, used when a survey does not set "
            "`form.max_pages`. Replaces `while True`."
        ),
    )
    submission_delay_seconds: float = Field(default=15.0, ge=0)

    # --- per-attempt budgets ---
    # Deliberately tight rather than generous. With a pool of circuits to rotate
    # through, abandoning a slow route and trying another beats waiting one out:
    # a healthy circuit delivered the target form in 8-9s, while the slowest
    # usable one took 64s, so a budget large enough to accommodate the bad
    # circuit costs a minute on every failure and still fails on a dead one.
    nav_timeout_seconds: int = Field(
        default=60, ge=5,
        description="Budget for the initial navigation to the form, per attempt.",
    )
    ready_timeout_seconds: int = Field(
        default=45, ge=5,
        description=(
            "Budget for the form to become interactive once navigation returns "
            "— the provider's own readiness gate, not page load."
        ),
    )
    browser_max_attempts: int = Field(
        default=3, ge=1, le=10,
        description=(
            "How many routes one unit of work may try before failing. Only "
            "failures raised before anything is POSTed are retried, so this "
            "cannot cause a double submission. Capped at 2 when USE_TOR is off: "
            "without Tor every attempt takes the same route, so only a second "
            "try against a hung connection is worth making."
        ),
    )

    # ===== TOR =====
    use_tor: bool = False
    tor_socks_host: str = "127.0.0.1"
    tor_socks_port: int = Field(default=9050, gt=0, lt=65536)
    # NoDecode: without it pydantic-settings JSON-decodes a tuple field straight
    # from `.env`, before any validator runs, so `9052,9053` raises a
    # SettingsError instead of reaching `_split_extra_ports`.
    tor_socks_extra_ports: Annotated[tuple[int, ...], NoDecode] = Field(
        default=(),
        description=(
            "Additional SocksPorts Tor is listening on. Circuits are isolated "
            "per SocksPort, and that is the only isolation mechanism Chrome "
            "supports — it refuses SOCKS5 authentication outright. Each port "
            "listed here must exist in torrc."
        ),
    )
    tor_control_port: int = Field(default=9051, gt=0, lt=65536)
    tor_control_password: SecretStr = SecretStr("")
    unique_exit_ips: bool = Field(
        default=True,
        description=(
            "Never submit one survey twice from the same Tor exit IP. Each "
            "survey keeps a ledger (output/<id>/exit_ips.jsonl); a circuit whose "
            "IP is already in it is burned and another one is tried."
        ),
    )
    exit_ip_min_gap: int = Field(
        default=0, ge=0,
        description=(
            "With UNIQUE_EXIT_IPS: 0 = an exit IP is used once per survey. N > 0 "
            "= it may be reused after N other submissions (1 = never twice in a "
            "row). Large surveys run out of fresh exits; a gap keeps them moving."
        ),
    )
    tor_circuit_cooldown_seconds: float = Field(
        default=60.0, ge=0,
        description=(
            "How long a SocksPort that just failed is skipped in the rotation. "
            "Should match `MaxCircuitDirtiness` in torrc: that is how long the "
            "port keeps handing out the same bad circuit, so coming back any "
            "sooner reuses the route that just failed."
        ),
    )

    # ===== OUTPUT =====
    # The ROOT. Each survey writes under `<output_dir>/<survey id>/`.
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

    @field_validator("gemini_proxy", "base_form_url", "survey", mode="before")
    @classmethod
    def _blank_is_none(cls, v: object) -> object:
        return v or None

    @field_validator("survey_config", "clients_dir", mode="before")
    @classmethod
    def _blank_path_is_none(cls, v: object) -> object:
        return v or None

    @field_validator("tor_socks_extra_ports", mode="before")
    @classmethod
    def _split_extra_ports(cls, v: object) -> object:
        """Accept ``TOR_SOCKS_EXTRA_PORTS="9052, 9053"``.

        pydantic-settings would otherwise demand JSON for a tuple field, which
        is a hostile thing to ask of a `.env` file.
        """
        if isinstance(v, str):
            return tuple(part.strip() for part in v.split(",") if part.strip())
        return v

    @model_validator(mode="after")
    def _collect_api_keys(self) -> "Settings":
        """Gather GEMINI_API_KEY_* from `.env` and the process environment.

        Both sources are needed. `.env` entries arrive as model extras (pydantic
        -settings parses the file, it never writes to `os.environ`), while keys
        exported in the shell or injected by CI only exist in `os.environ`. A
        real environment variable wins over the same index in `.env`, matching
        pydantic-settings' own precedence.

        Scans the whole namespace and sorts by index instead of counting upward
        from 1 and stopping at the first gap. The old behaviour meant defining
        `_1` and `_3` silently loaded exactly one key — the second was simply
        never seen, and the only symptom was hitting quota sooner than expected.
        """
        if self.gemini_api_keys:
            return self

        found: dict[int, str] = {}
        for source in (self.model_extra or {}, os.environ):
            for name, value in source.items():
                match = _KEY_PATTERN.match(name)
                if not match or not isinstance(value, str) or not value.strip():
                    continue
                index = int(match.group(1))
                if index <= _MAX_API_KEYS:
                    found[index] = value.strip()

        keys = tuple(SecretStr(found[index]) for index in sorted(found))
        object.__setattr__(self, "gemini_api_keys", keys)
        return self

    # ------------------------------------------------------------------ helpers

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
    def effective_browser_attempts(self) -> int:
        """Route attempts per unit of work, given whether Tor is on.

        With Tor, every attempt can take a different circuit, so the whole
        budget is worth spending. Without it the route never changes, and the
        only failure a retry fixes is a single hung connection on a slow link
        — one more try covers that, and further ones only repeat the failure.
        """
        return self.browser_max_attempts if self.use_tor else min(2, self.browser_max_attempts)

    @property
    def tor_proxy_server(self) -> str:
        return f"socks5://{self.tor_socks_host}:{self.tor_socks_port}"

    @property
    def tor_proxy_servers(self) -> tuple[str, ...]:
        """Every SOCKS endpoint available for circuit isolation, in order.

        ``tor_socks_port`` first, then the extras. Duplicates are dropped so a
        port repeated in ``TOR_SOCKS_EXTRA_PORTS`` cannot bias the rotation.
        """
        ports: list[int] = [self.tor_socks_port]
        for port in self.tor_socks_extra_ports:
            if port not in ports:
                ports.append(port)
        return tuple(f"socks5://{self.tor_socks_host}:{p}" for p in ports)


def load_settings(**overrides: object) -> Settings:
    """Build a Settings instance. The only place the environment is read.

    CLI flags arrive as `overrides` and win over `.env`.
    """
    try:
        return Settings(**overrides)  # type: ignore[arg-type]
    except Exception as exc:  # pydantic ValidationError carries the field paths
        raise ConfigError(f"Invalid configuration: {exc}") from exc
