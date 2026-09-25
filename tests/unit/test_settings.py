"""API key collection from `.env` and the process environment.

Regression: `_collect_api_keys` scanned only `os.environ`, but pydantic-settings
parses `.env` into the model and never writes to `os.environ`. Keys defined the
documented way — `GEMINI_API_KEY_1` in `.env` — were therefore invisible, and a
fully configured project failed with "no GEMINI_API_KEY_* was found". They only
worked if also exported in the shell.
"""

from __future__ import annotations

import pytest

from personabot.errors import ConfigError
from personabot.settings import Settings


def write_env(tmp_path, body: str):
    path = tmp_path / ".env"
    path.write_text(body, encoding="utf-8")
    return path


def secrets(settings: Settings) -> list[str]:
    return [key.get_secret_value() for key in settings.gemini_api_keys]


class TestKeyCollection:
    def test_keys_defined_only_in_env_file_are_found(self, tmp_path, monkeypatch):
        """The regression: `.env` keys with nothing exported in the shell."""
        monkeypatch.delenv("GEMINI_API_KEY_1", raising=False)
        env = write_env(tmp_path, 'AI_PROVIDER="gemini"\nGEMINI_API_KEY_1="from_dotenv"\n')

        settings = Settings(_env_file=env)

        assert secrets(settings) == ["from_dotenv"]
        settings.require_llm_ready()  # must not raise

    def test_keys_exported_in_the_shell_are_still_found(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY_1", "from_shell")
        settings = Settings(_env_file=write_env(tmp_path, ""))
        assert secrets(settings) == ["from_shell"]

    def test_both_sources_merge(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY_1", raising=False)
        monkeypatch.setenv("GEMINI_API_KEY_2", "shell_two")
        env = write_env(tmp_path, 'GEMINI_API_KEY_1="dotenv_one"\n')

        assert secrets(Settings(_env_file=env)) == ["dotenv_one", "shell_two"]

    def test_process_environment_wins_over_env_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY_1", "shell_wins")
        env = write_env(tmp_path, 'GEMINI_API_KEY_1="dotenv_loses"\n')

        assert secrets(Settings(_env_file=env)) == ["shell_wins"]

    def test_gaps_in_numbering_do_not_truncate(self, tmp_path, monkeypatch):
        """Pre-existing guarantee: `_1` and `_3` must both load."""
        for index in (1, 2, 3):
            monkeypatch.delenv(f"GEMINI_API_KEY_{index}", raising=False)
        env = write_env(tmp_path, 'GEMINI_API_KEY_1="one"\nGEMINI_API_KEY_3="three"\n')

        assert secrets(Settings(_env_file=env)) == ["one", "three"]

    def test_keys_are_ordered_by_index_not_file_order(self, tmp_path, monkeypatch):
        for index in (1, 2, 10):
            monkeypatch.delenv(f"GEMINI_API_KEY_{index}", raising=False)
        env = write_env(
            tmp_path,
            'GEMINI_API_KEY_10="ten"\nGEMINI_API_KEY_2="two"\nGEMINI_API_KEY_1="one"\n',
        )

        assert secrets(Settings(_env_file=env)) == ["one", "two", "ten"]

    def test_blank_keys_are_skipped(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY_2", raising=False)
        env = write_env(tmp_path, 'GEMINI_API_KEY_1=""\nGEMINI_API_KEY_2="  "\n')

        assert secrets(Settings(_env_file=env)) == []

    def test_indices_beyond_the_cap_are_ignored(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY_1", raising=False)
        env = write_env(tmp_path, 'GEMINI_API_KEY_999="too_far"\n')

        assert secrets(Settings(_env_file=env)) == []

    def test_explicit_keys_are_not_overwritten_by_discovery(self, tmp_path, monkeypatch):
        """Constructing with keys directly (as the tests do) short-circuits the scan."""
        monkeypatch.setenv("GEMINI_API_KEY_1", "from_shell")
        settings = Settings(_env_file=write_env(tmp_path, ""), gemini_api_keys=("explicit",))
        assert secrets(settings) == ["explicit"]


class TestRequireLLMReady:
    def test_gemini_without_keys_explains_the_fix(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY_1", raising=False)
        settings = Settings(_env_file=write_env(tmp_path, 'AI_PROVIDER="gemini"\n'))

        with pytest.raises(ConfigError, match="GEMINI_API_KEY_1"):
            settings.require_llm_ready()

    def test_vllm_ignores_missing_gemini_keys(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY_1", raising=False)
        env = write_env(tmp_path, 'AI_PROVIDER="vllm"\nVLLM_MODEL_NAME="local/model"\n')

        Settings(_env_file=env).require_llm_ready()  # must not raise
