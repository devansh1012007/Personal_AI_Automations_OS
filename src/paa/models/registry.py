"""A declarative table of the inference platforms this runtime can talk to.

ADR-0007 turns capability into a configuration choice: "swapping the model is a
config change, not a rewrite." That promise only holds if adding a platform is
*data*, not code. This module is that data — one :class:`ProviderSpec` row per
platform, plus a single :func:`build_provider` that turns a name into a live
:class:`~paa.models.base.ModelProvider`.

Three kinds of row exist, because "OpenAI-compatible" is a family and not a
universal:

* **openai** — served by :class:`~paa.models.openai_compatible_provider.OpenAICompatibleProvider`.
  Most hosted platforms (OpenAI, Groq, OpenRouter, Together, DeepSeek, xAI,
  Mistral, Fireworks, Perplexity, Azure) and most local servers (LM Studio,
  llama.cpp's ``llama-server``, vLLM, text-generation-webui) all speak
  ``/v1/chat/completions`` with only path/auth quirks between them, which the
  spec encodes rather than the code branching on.
* **ollama** / **gemini** / **anthropic** — bespoke wire formats, each with its
  own provider class.
* **echo** — the offline deterministic provider, so a fully air-gapped
  ``local_provider = "echo"`` still resolves through the one table.

Localhost is first-class here, not an afterthought: every ``is_local`` row ships
a working loopback default, so ``build_local_inference("lmstudio")`` needs no
configuration at all. Tiers are honest — a local server is
:attr:`~paa.models.base.ModelTier.LOCAL`, a hosted API is ``FRONTIER`` — because
the router's privacy accounting (ADR-0015) is derived from the tier, and a
mislabelled row would make "did this leave the machine?" answer wrongly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import httpx

from paa.models.base import ModelProvider, ModelTier

if TYPE_CHECKING:
    from paa.config import ModelSettings

__all__ = [
    "KNOWN_PROVIDERS",
    "LOCAL_PROVIDERS",
    "ProviderSpec",
    "UnknownProviderError",
    "build_local_inference",
    "build_provider",
    "provider_names",
    "resolve_spec",
]

ProviderKind = Literal["openai", "ollama", "anthropic", "gemini", "echo"]


class UnknownProviderError(ValueError):
    """Raised when a name matches no registry row.

    A plain, listing error rather than a ``KeyError`` three layers up: a typo in
    ``models.local_provider`` should fail startup with the set of valid names,
    not surface as an opaque crash the first time inference is attempted.
    """

    def __init__(self, name: str) -> None:
        known = ", ".join(provider_names())
        super().__init__(f"unknown model provider {name!r}; known providers are: {known}")
        self.name = name


@dataclass(frozen=True)
class ProviderSpec:
    """Everything needed to construct one platform's provider.

    Defaults describe the canonical OpenAI shape; a row overrides only the
    fields where its platform actually diverges. The ``api_key_env`` tuple is
    tried in order and the first variable that is set wins — this is how Gemini
    accepts either ``GEMINI_API_KEY`` or ``GOOGLE_API_KEY`` without the caller
    caring which one is present.
    """

    name: str
    kind: ProviderKind
    default_model: str
    default_base_url: str | None = None
    api_key_env: tuple[str, ...] = ()
    default_tier: ModelTier = ModelTier.FRONTIER
    is_local: bool = False
    aliases: tuple[str, ...] = ()

    # -- openai-family quirks (ignored by other kinds) ----------------------
    chat_path: str = "/v1/chat/completions"
    #: ``{model}`` is substituted at build time — Azure pins the deployment in
    #: the path rather than the body.
    chat_path_template: str | None = None
    models_path: str = "/v1/models"
    auth_header: str = "authorization"
    auth_scheme: str = "Bearer"
    #: Fixed query string (Azure's mandatory ``api-version``); no leading ``?``.
    api_version: str | None = None

    def resolved_api_key(self) -> str | None:
        """The first configured key value, or ``None``. Read at build time."""
        for name in self.api_key_env:
            if value := os.environ.get(name):
                return value
        return None


def _openai(
    name: str,
    default_model: str,
    default_base_url: str,
    api_key_env: tuple[str, ...] = (),
    *,
    is_local: bool = False,
    aliases: tuple[str, ...] = (),
    **quirks: object,
) -> ProviderSpec:
    """Row builder for the OpenAI family — keeps the table below scannable."""
    return ProviderSpec(
        name=name,
        kind="openai",
        default_model=default_model,
        default_base_url=default_base_url,
        api_key_env=api_key_env,
        default_tier=ModelTier.LOCAL if is_local else ModelTier.FRONTIER,
        is_local=is_local,
        aliases=aliases,
        **quirks,  # type: ignore[arg-type]
    )


#: The registry proper. Keyed by canonical name; aliases are resolved in
#: :func:`resolve_spec`. Base URLs deliberately omit the ``/v1`` suffix because
#: ``chat_path`` supplies it — the two are joined by the HTTP layer.
_SPECS: tuple[ProviderSpec, ...] = (
    # -- hosted, OpenAI-compatible ------------------------------------------
    _openai("openai", "gpt-4o-mini", "https://api.openai.com", ("OPENAI_API_KEY",)),
    _openai(
        "groq",
        "llama-3.3-70b-versatile",
        "https://api.groq.com/openai",
        ("GROQ_API_KEY",),
    ),
    _openai(
        "openrouter",
        "openai/gpt-4o-mini",
        "https://openrouter.ai/api",
        ("OPENROUTER_API_KEY",),
    ),
    _openai(
        "together",
        "meta-llama/Llama-3.3-70B-Instruct-Turbo",
        "https://api.together.xyz",
        ("TOGETHER_API_KEY",),
    ),
    _openai("deepseek", "deepseek-chat", "https://api.deepseek.com", ("DEEPSEEK_API_KEY",)),
    _openai(
        "xai",
        "grok-2-latest",
        "https://api.x.ai",
        ("XAI_API_KEY",),
        aliases=("grok",),
    ),
    _openai("mistral", "mistral-large-latest", "https://api.mistral.ai", ("MISTRAL_API_KEY",)),
    _openai(
        "fireworks",
        "accounts/fireworks/models/llama-v3p3-70b-instruct",
        "https://api.fireworks.ai/inference",
        ("FIREWORKS_API_KEY",),
    ),
    _openai(
        "perplexity",
        "sonar",
        "https://api.perplexity.ai",
        ("PERPLEXITY_API_KEY",),
        # Perplexity serves chat completions with no ``/v1`` prefix and exposes
        # no ``/models`` listing.
        chat_path="/chat/completions",
        models_path="/chat/completions",
    ),
    _openai(
        "azure",
        # For Azure the "model" is the *deployment* name the user created.
        "gpt-4o-mini",
        # The resource endpoint must be supplied per deployment; this is only a
        # readable placeholder that build_provider expects to be overridden.
        "https://YOUR-RESOURCE.openai.azure.com",
        ("AZURE_OPENAI_API_KEY", "AZURE_OPENAI_KEY"),
        chat_path_template="/openai/deployments/{model}/chat/completions",
        models_path="/openai/models",
        auth_header="api-key",
        auth_scheme="",
        api_version="2024-10-21",
    ),
    # -- local, OpenAI-compatible -------------------------------------------
    _openai(
        "lmstudio",
        "local-model",
        "http://localhost:1234",
        is_local=True,
        aliases=("lm-studio",),
    ),
    _openai(
        "llamacpp",
        "local-model",
        "http://localhost:8080",
        is_local=True,
        aliases=("llama-cpp", "llama_cpp", "llama-server"),
    ),
    _openai("vllm", "local-model", "http://localhost:8000", is_local=True),
    _openai(
        "tgwebui",
        "local-model",
        "http://localhost:5000",
        is_local=True,
        aliases=("text-generation-webui", "oobabooga"),
    ),
    _openai(
        "openai_compatible",
        "local-model",
        "http://localhost:8000",
        is_local=True,
    ),
    _openai(
        "ollama_openai",
        "qwen2.5:3b-instruct",
        "http://127.0.0.1:11434",
        is_local=True,
    ),
    # -- bespoke ------------------------------------------------------------
    ProviderSpec(
        name="ollama",
        kind="ollama",
        default_model="qwen2.5:3b-instruct",
        default_base_url="http://127.0.0.1:11434",
        default_tier=ModelTier.LOCAL,
        is_local=True,
    ),
    ProviderSpec(
        name="anthropic",
        kind="anthropic",
        default_model="claude-sonnet-5",
        default_base_url="https://api.anthropic.com",
        api_key_env=("ANTHROPIC_API_KEY",),
        default_tier=ModelTier.FRONTIER,
    ),
    ProviderSpec(
        name="gemini",
        kind="gemini",
        default_model="gemini-2.0-flash",
        default_base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key_env=("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        default_tier=ModelTier.FRONTIER,
        aliases=("google", "google-gemini"),
    ),
    ProviderSpec(
        name="echo",
        kind="echo",
        default_model="echo-1",
        default_tier=ModelTier.LOCAL,
        is_local=True,
    ),
)


def _build_index() -> dict[str, ProviderSpec]:
    index: dict[str, ProviderSpec] = {}
    for spec in _SPECS:
        for key in (spec.name, *spec.aliases):
            index[key.lower()] = spec
    return index


#: Canonical-name → spec, including aliases. Consumers should prefer
#: :func:`resolve_spec` so alias resolution and error reporting stay in one place.
KNOWN_PROVIDERS: dict[str, ProviderSpec] = _build_index()

#: Names of every localhost-first provider, for CLI help and doctor output.
LOCAL_PROVIDERS: frozenset[str] = frozenset(s.name for s in _SPECS if s.is_local)


def provider_names() -> list[str]:
    """Canonical provider names, sorted. Used in the unknown-provider message."""
    return sorted(s.name for s in _SPECS)


def resolve_spec(name: str) -> ProviderSpec:
    """Look up a spec by canonical name or alias, case-insensitively."""
    try:
        return KNOWN_PROVIDERS[name.lower()]
    except KeyError:
        raise UnknownProviderError(name) from None


def build_provider(
    name: str,
    model: str | None = None,
    settings: ModelSettings | None = None,
    *,
    tier: ModelTier | None = None,
    base_url: str | None = None,
    api_key_env: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> ModelProvider:
    """Construct the provider named ``name`` from the registry.

    ``model``/``base_url``/``tier`` override the row's defaults when supplied;
    everything else (timeouts, retries) is drawn from ``settings`` when given so
    the whole layer honours one set of knobs. Constructing a provider performs
    no I/O and opens no socket — safe to call during synchronous startup wiring.

    :raises UnknownProviderError: when ``name`` matches no row or alias.
    """
    spec = resolve_spec(name)
    model = model or spec.default_model
    base = base_url or spec.default_base_url
    effective_tier = tier if tier is not None else spec.default_tier
    timeout = float(getattr(settings, "request_timeout_seconds", 120.0))
    max_retries = int(getattr(settings, "max_retries", 2))

    if spec.kind == "echo":
        from paa.models.echo_provider import EchoProvider

        return EchoProvider(model=model, tier=effective_tier, max_retries=max_retries)

    if spec.kind == "ollama":
        from paa.models.ollama_provider import OllamaProvider

        return OllamaProvider(
            model,
            base_url=base or "http://127.0.0.1:11434",
            timeout=timeout,
            max_retries=max_retries,
            client=client,
        )

    if spec.kind == "anthropic":
        from paa.models.anthropic_provider import AnthropicProvider

        default_env = spec.api_key_env[0] if spec.api_key_env else "ANTHROPIC_API_KEY"
        return AnthropicProvider(
            model,
            api_key_env=api_key_env or default_env,
            base_url=base,
            timeout=timeout,
            max_retries=max_retries,
            client=client,
        )

    if spec.kind == "gemini":
        from paa.models.gemini_provider import GeminiProvider

        return GeminiProvider(
            model,
            api_key_env=(api_key_env,) if api_key_env else spec.api_key_env,
            base_url=base,
            tier=effective_tier,
            timeout=timeout,
            max_retries=max_retries,
            client=client,
        )

    # spec.kind == "openai"
    from paa.models.openai_compatible_provider import OpenAICompatibleProvider

    api_key = os.environ.get(api_key_env) if api_key_env else spec.resolved_api_key()
    chat_path = (
        spec.chat_path_template.format(model=model)
        if spec.chat_path_template
        else spec.chat_path
    )
    extra_query = f"api-version={spec.api_version}" if spec.api_version else None
    return OpenAICompatibleProvider(
        model,
        base_url=base or "http://localhost:8000",
        api_key=api_key or None,
        tier=effective_tier,
        name=spec.name,
        timeout=timeout,
        max_retries=max_retries,
        client=client,
        chat_path=chat_path,
        models_path=spec.models_path,
        auth_header=spec.auth_header,
        auth_scheme=spec.auth_scheme,
        extra_query=extra_query,
    )


def build_local_inference(
    backend: str = "ollama",
    *,
    model: str | None = None,
    base_url: str | None = None,
    settings: ModelSettings | None = None,
    client: httpx.AsyncClient | None = None,
) -> ModelProvider:
    """Convenience for standing up a localhost model server in one call.

    The user-facing entry point for "just run it locally": pick a ``backend``
    (``ollama``, ``lmstudio``, ``llamacpp``, ``vllm``, ``tgwebui``, ...) and get
    a provider pointed at that server's conventional loopback port, at
    :attr:`~paa.models.base.ModelTier.LOCAL`, with a
    :meth:`~paa.models.base.ModelProvider.healthcheck` that never raises. Any
    detail can still be overridden.

    :raises UnknownProviderError: when ``backend`` is not a localhost provider.
    """
    spec = resolve_spec(backend)
    if not spec.is_local:
        raise UnknownProviderError(backend)
    return build_provider(
        spec.name,
        model=model,
        settings=settings,
        tier=ModelTier.LOCAL,
        base_url=base_url,
        client=client,
    )
