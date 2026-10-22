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

from paa.config import ModelSettings, PolicySettings, Settings
from paa.core.types import ComplexityModality, PermissionMode
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


# -- the mode the deployment runs in ------------------------------------------


def _deployment(tmp_path: Path, mode: PermissionMode) -> Settings:
    """A whole ``Settings`` whose policy carries ``mode``.

    The model half is the offline pair — a local ``echo`` and an
    ``anthropic`` frontier — so the healthcheck can be exercised without a
    server, a key or a socket: ``AnthropicProvider.healthcheck`` is
    credential-presence only and makes no network call by design.
    """
    settings = Settings(_env_file=None, home=tmp_path, policy=PolicySettings(mode=mode))
    settings.models.local_provider = "echo"
    settings.models.escalation_provider = "anthropic"
    return settings


def test_the_factory_hands_the_deployment_mode_to_the_router(tmp_path: Path) -> None:
    """The mode the deployment runs in is the mode the router acts on.

    The mode lives on ``Settings.policy``, and the factory used to narrow its
    argument to the model half — which carries no mode at all — before handing
    it over. The router therefore never saw the deployment's mode and fell
    back to its fail-closed default, ``LOCKDOWN``: safe, and wrong for every
    deployment that is not in LOCKDOWN.

    The property is asked through the factory rather than through the
    router's constructor, because the factory *is* the call site the fix is
    about: the router already read the mode, and the mode never reached it.
    """
    router = get_model_router(_deployment(tmp_path, PermissionMode.ASK))

    assert router.permission_mode is PermissionMode.ASK


def test_the_factory_fails_closed_when_the_caller_names_no_mode() -> None:
    """A caller that hands over only the model half still gets the safe one.

    ``ModelSettings`` carries no permission mode, so a router built from it
    cannot know whether escalation is permitted. "No" is the answer that
    cannot leak, and it is the answer a **default** has to be.
    """
    router = get_model_router(ModelSettings(local_provider="echo"))

    assert router.permission_mode is PermissionMode.LOCKDOWN


async def test_the_factory_s_router_probes_the_frontier_when_it_may(tmp_path: Path) -> None:
    """The end of the chain: the mode reaches the healthcheck.

    The healthcheck probes the frontier — a network call — and it may only do
    that when the deployment's mode permits escalation. The frontier's key is
    **absent** from the report when it was not probed, and absent means "not
    asked", which is not the same as "asked, and it cannot serve".
    """
    router = get_model_router(_deployment(tmp_path, PermissionMode.ASK))

    report = await router.healthcheck()

    assert "anthropic" in report
    assert report["echo"] is True


async def test_the_factory_s_router_does_not_probe_it_under_lockdown(
    tmp_path: Path,
) -> None:
    """The other side of the same door: honouring the mode is not blocking.

    A LOCKDOWN deployment must not have its frontier probed, and the reason
    it did not is the mode it was built with — not a coincidence.
    """
    router = get_model_router(_deployment(tmp_path, PermissionMode.LOCKDOWN))

    report = await router.healthcheck()

    assert "anthropic" not in report
    assert report["echo"] is True
