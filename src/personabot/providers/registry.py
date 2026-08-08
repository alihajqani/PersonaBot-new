"""Provider registry.

Replaces ``importlib.import_module(f"providers.{name}.{module}")``. String
interpolation meant a typo produced a runtime failure, and a provider missing
``run()`` failed only when called — with ``AttributeError``, not a useful
message. An unknown provider name is now rejected at CLI parse time, with the
valid names listed.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Callable, TypeVar

from personabot.errors import ConfigError
from personabot.providers.base import FormSubmitter, SchemaExtractor

_EXTRACTORS: dict[str, type[SchemaExtractor]] = {}
_SUBMITTERS: dict[str, type[FormSubmitter]] = {}

E = TypeVar("E", bound=SchemaExtractor)
S = TypeVar("S", bound=FormSubmitter)


def register_extractor(name: str) -> Callable[[type[E]], type[E]]:
    def decorate(cls: type[E]) -> type[E]:
        cls.name = name
        _EXTRACTORS[name] = cls
        return cls

    return decorate


def register_submitter(name: str) -> Callable[[type[S]], type[S]]:
    def decorate(cls: type[S]) -> type[S]:
        cls.name = name
        _SUBMITTERS[name] = cls
        return cls

    return decorate


def _discover() -> None:
    """Import every provider package so its decorators run."""
    import personabot.providers as package

    for module in pkgutil.iter_modules(package.__path__):
        if module.ispkg:
            importlib.import_module(f"{package.__name__}.{module.name}")


def available() -> list[str]:
    _discover()
    return sorted(set(_EXTRACTORS) | set(_SUBMITTERS))


def get_extractor(name: str) -> type[SchemaExtractor]:
    _discover()
    try:
        return _EXTRACTORS[name]
    except KeyError:
        raise ConfigError(
            f"No schema extractor for provider {name!r}. Available: {available()}"
        ) from None


def get_submitter(name: str) -> type[FormSubmitter]:
    _discover()
    try:
        return _SUBMITTERS[name]
    except KeyError:
        raise ConfigError(
            f"No form submitter for provider {name!r}. Available: {available()}"
        ) from None
