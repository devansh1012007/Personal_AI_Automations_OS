"""The public embedding surface: one contract, forwarded verbatim.

``paa.models.get_embedder`` is a re-export of
``paa.storage.vector.embeddings.get_embedder``. Its contract is the
implementation's contract, and the two must not drift: the wrapper exists so
that "give me a model" is one import, not so that it can be a second, looser
one.

The signature was narrowed from ``Any`` to ``Settings | ModelSettings`` when
the implementation switched from a ``getattr(settings, "models", settings)``
fallback to an ``isinstance`` resolution, and the tolerance that fallback
carried — a ``Settings``-shaped *proxy* exposing only ``.models`` — went with
it. These tests pin what replaced it: both real shapes are accepted, they
reach the same encoder, and a proxy is no longer quietly resolved.

The encoder itself is not the subject here. sentence-transformers is an
optional extra, so on a machine without it the implementation selects the
deterministic hash embedder; the tests below assert on *which configuration
was honoured*, not on the encoder's quality, so they hold either way.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from paa.config import ModelSettings, Settings
from paa.models import get_embedder


def _settings(tmp_path: Path) -> Settings:
    """A full ``Settings`` whose ``.env`` is not read.

    ``_env_file=None`` because the repository's own ``.env`` names a provider
    and a model: a test that asserted on the configuration must not be
    decided by a file that is not part of it.
    """
    return Settings(_env_file=None, home=tmp_path)


def test_the_model_half_is_accepted() -> None:
    """``ModelSettings`` *is* the model configuration."""
    embedder = get_embedder(ModelSettings())

    assert embedder.dimensions == ModelSettings().embedding_dimensions


def test_the_whole_settings_object_is_accepted(tmp_path: Path) -> None:
    """``Settings`` carries the model configuration one level down."""
    embedder = get_embedder(_settings(tmp_path))

    assert embedder.dimensions == ModelSettings().embedding_dimensions


def test_both_shapes_reach_the_same_encoder(tmp_path: Path) -> None:
    """The two shapes must not diverge.

    A caller holding a ``Settings`` and a caller holding its ``models`` half
    are asking for the same encoder; which one they happen to hold must not
    decide what they get.
    """
    whole = get_embedder(_settings(tmp_path))
    half = get_embedder(_settings(tmp_path).models)

    assert whole.model_name == half.model_name
    assert whole.dimensions == half.dimensions


def test_a_settings_shaped_proxy_is_no_longer_quietly_resolved() -> None:
    """The removed tolerance, pinned as removed.

    The old ``getattr(settings, "models", settings)`` fallback resolved a
    proxy — any object exposing a ``.models`` attribute — and handed back a
    working encoder. The ``isinstance`` resolution does not recognise one,
    so the proxy is treated as the model settings itself and fails on the
    field it does not have.

    The failure is an ``AttributeError`` rather than a neat ``TypeError``,
    and that is the implementation's to improve: this test exists to say that
    the proxy is *not* silently accepted, which is the contract the public
    wrapper now shares with the implementation it forwards to.
    """
    class _Proxy:
        """A ``Settings``-shaped object that is not a ``Settings``."""

        def __init__(self) -> None:
            self.models = ModelSettings()

    with pytest.raises(AttributeError):
        get_embedder(cast(Any, _Proxy()))
