"""The model layer accepts two shapes of configuration, and both must resolve.

:class:`~paa.config.Settings` is the whole runtime configuration;
:class:`~paa.config.ModelSettings` is its ``models`` half. Half the call sites
hold one and half hold the other — ``runtime.py`` holds the first, every
provider factory holds the second — so both have to arrive at the *same* model
configuration.

The resolution is an ``isinstance`` test rather than a ``getattr(settings,
"models", settings)`` fallback, because only the former tells a type checker
which of the two it is holding. ``getattr`` looks equivalent and is not: the
``models`` attribute exists on one member of the union and not the other, so
the fallback is the union itself and every attribute read off the result stops
being checked.

``_env_file=None`` throughout: the repository's own ``.env`` names a local
provider and a model, and a test that asserted on the defaults must not be
decided by a file that is not part of it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from paa.config import ModelSettings, Settings
from paa.core.types import ComplexityModality
from paa.models import get_model_router
from paa.models.base import ModelTier
from paa.models.echo_provider import EchoProvider
from paa.models.router import EscalatingModelRouter


def _settings(tmp_path: Path, **models: Any) -> Settings:
    """A full ``Settings`` whose ``models`` half is set explicitly."""
    settings = Settings(_env_file=None, home=tmp_path)
    for key, value in models.items():
        setattr(settings.models, key, value)
    return settings


def test_a_full_settings_object_resolves_to_its_model_half(tmp_path: Path) -> None:
    """``Settings`` carries the model configuration one level down."""
    settings = _settings(tmp_path, local_provider="echo", escalation_provider="none")

    router = get_model_router(settings)

    assert isinstance(router.local, EchoProvider)
    # ``escalation_provider="none"`` only takes effect if the *model half* was
    # read — a router built from the whole ``Settings`` object would have
    # escalated to the default Anthropic provider.
    assert router.frontier is None


def test_the_model_half_resolves_to_itself() -> None:
    """``ModelSettings`` *is* the model configuration."""
    router = get_model_router(
        ModelSettings(local_provider="echo", escalation_provider="none")
    )

    assert isinstance(router.local, EchoProvider)
    assert router.frontier is None


def test_both_shapes_of_configuration_resolve_identically(tmp_path: Path) -> None:
    """The whole point: the two shapes must not diverge.

    A router built from ``Settings`` and one built from ``Settings.models``
    have to be the same router, or which one a call site happens to hold
    decides which provider the deployment talks to.

    The half is taken from the very same ``Settings`` object rather than from
    a separately constructed ``ModelSettings``: ``Settings`` reads ``PAA_*``
    from the environment and a plain ``ModelSettings`` does not, so two
    independently built halves would differ whenever the environment set one
    of those variables — and the test would then be asserting on the
    environment rather than on the code.
    """
    settings = _settings(
        tmp_path,
        local_provider="echo",
        escalation_provider="none",
        max_concurrent_streams=7,
        escalate_at_or_above=ComplexityModality.STANDARD,
    )

    whole = get_model_router(settings)
    half = get_model_router(settings.models)

    assert whole.describe() == half.describe()


def test_the_router_takes_a_full_settings_object(tmp_path: Path) -> None:
    """The router resolves the same way the factory does."""
    router = EscalatingModelRouter(
        EchoProvider(), None, settings=_settings(tmp_path, max_concurrent_streams=7)
    )

    assert router.max_concurrent_streams == 7


def test_the_router_without_settings_uses_the_defaults() -> None:
    """No configuration at all is the defaults — not an error."""
    router = EscalatingModelRouter(EchoProvider(), None)

    assert router.max_concurrent_streams == ModelSettings().max_concurrent_streams
    assert router.describe()["escalate_at_or_above"] == ComplexityModality.COMPLEX.value


def test_a_misconfigured_settings_object_is_not_silently_defaulted() -> None:
    """A wrong-typed ``settings`` must fail at the boundary, not several frames later.

    The value is cast because it deliberately violates the signature: the
    point of the test is what happens when a caller ignores the type. The old
    code carried the wrong object into ``self._settings`` and failed later
    with an ``AttributeError`` about ``max_concurrent_streams``; the refusal
    now happens where the mistake was made and says what was wanted.

    The *silent* half of the old behaviour is a different input, and the next
    test pins it.
    """
    with pytest.raises(TypeError, match="Settings or a ModelSettings"):
        EscalatingModelRouter(
            EchoProvider(), None, settings=cast(Any, "not-a-settings-object")
        )


def test_a_falsy_misconfigured_settings_object_is_not_silently_defaulted() -> None:
    """The silent default was the *falsy* case, and it is the one that mattered.

    ``resolved or _ModelSettings()`` could not tell "no configuration" from
    "a value that happens to be falsy", so an empty dict was answered with
    the defaults. For a router the defaults are not a neutral answer, because
    what this configuration carries is the *escalation policy* — the modality
    floor and the permission modes that may never escalate. A deployment that
    had configured "never escalate" and handed over an empty dict would have
    run on the default policy instead, and escalated: reasoning material
    leaving the machine under a policy that forbade it, which is the failure
    ADR-0015 exists to prevent.
    """
    with pytest.raises(TypeError, match="Settings or a ModelSettings"):
        EscalatingModelRouter(EchoProvider(), None, settings=cast(Any, {}))


def test_the_factory_refuses_the_same_objects_the_router_does() -> None:
    """One contract, one error: the factory and the router agree.

    ``get_model_router`` is the public entry point and the one a caller is
    most likely to hand a wrong object. If the two disagreed — the factory
    raising an ``AttributeError`` from inside a provider factory while the
    router raised ``TypeError`` — the error a caller saw would depend on
    which of the two they happened to call.
    """
    with pytest.raises(TypeError, match="Settings or a ModelSettings"):
        get_model_router(cast(Any, "not-a-settings-object"))


def test_the_router_reports_the_tier_it_was_given() -> None:
    """The frontier tier comes from the resolved configuration, not a default."""
    router = EscalatingModelRouter(
        EchoProvider(),
        EchoProvider(name="frontier-echo", tier=ModelTier.FRONTIER),
        settings=ModelSettings(),
    )

    assert router.frontier is not None
    assert router.frontier.tier is ModelTier.FRONTIER
    assert router.describe()["frontier"] is not None
