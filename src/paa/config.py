"""Runtime configuration.

Every tunable constant that appears as a magic number in the RFC lives here
with its RFC section cited, so the maths in ``docs/architecture.md`` and the
behaviour of the code cannot drift apart silently.

Configuration precedence: explicit kwargs > environment (``PAA_*``) >
``.env`` file > defaults.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from paa.core.types import ComplexityModality, PermissionMode

__all__ = [
    "ContextSettings",
    "MemorySettings",
    "ModelSettings",
    "PolicySettings",
    "SandboxSettings",
    "Settings",
    "StorageSettings",
    "get_settings",
    "reset_settings_cache",
]

Probability = Annotated[float, Field(ge=0.0, le=1.0)]


def _default_home() -> Path:
    """Root for all runtime state.

    Honours ``PAA_HOME``; otherwise ``~/.paa``. Deliberately outside the repo so
    that a ``git clean`` never destroys the ledger.
    """
    if env := os.environ.get("PAA_HOME"):
        return Path(env).expanduser().resolve()
    return (Path.home() / ".paa").resolve()


# ---------------------------------------------------------------------------


class StorageSettings(BaseModel):
    """Polyglot storage substrate locations and backend selection.

    SPEC DEVIATION (docs/adr/0001-0005): the RFC specifies PostgreSQL, a Qdrant
    server, Redis and MinIO — roughly 4-6 GB of resident memory before any work
    happens, all via Docker. The target machine has ~3.5 GB free and no Docker.
    Each substrate therefore defaults to an embedded engine with the same
    logical contract; the server-backed variants remain selectable.
    """

    backend_relational: Literal["sqlite", "postgres"] = "sqlite"
    backend_vector: Literal["auto", "qdrant_local", "qdrant_server", "numpy"] = "auto"
    backend_graph: Literal["auto", "kuzu", "sqlite"] = "auto"
    backend_queue: Literal["sqlite", "redis"] = "sqlite"

    #: Cold-lake object substrate. ``filesystem`` is the content-addressed local
    #: store (ADR-0004, the laptop default); ``minio`` restores the RFC's S3
    #: server for the Docker deployment (ADR-0019). Both are content-addressed
    #: and verify sha256 on read, so they are interchangeable behind one factory.
    backend_coldlake: Literal["filesystem", "minio"] = "filesystem"

    sqlite_path: Path = Field(default_factory=lambda: _default_home() / "state" / "paa.db")
    postgres_dsn: str | None = None

    qdrant_path: Path = Field(default_factory=lambda: _default_home() / "state" / "qdrant")
    qdrant_url: str | None = None

    kuzu_path: Path = Field(default_factory=lambda: _default_home() / "state" / "graph")

    #: Content-addressed blob store. Replaces MinIO on the laptop (ADR-0004).
    cold_lake_path: Path = Field(default_factory=lambda: _default_home() / "cold_lake")

    #: MinIO / S3 endpoint for ``backend_coldlake == "minio"`` (ADR-0019), e.g.
    #: ``http://minio:9000`` inside Docker. ``None`` on the laptop topology.
    minio_endpoint: str | None = None
    minio_bucket: str = "paa-cold-lake"
    #: TLS to the object store. Compose runs MinIO on the internal network, so
    #: the in-cluster hop defaults to plaintext there; a public S3 must set True.
    minio_secure: bool = False
    #: SPEC DEVIATION (docs/adr/0019): credentials are read from these named
    #: environment variables at construction, never stored in config or the
    #: ledger — the same indirection ModelSettings uses for API keys.
    minio_access_key_env: str = "PAA_MINIO_ACCESS_KEY"
    # S105: this is the *name* of an environment variable, not a secret value.
    minio_secret_key_env: str = "PAA_MINIO_SECRET_KEY"

    redis_url: str = "redis://127.0.0.1:6379/0"

    #: Markdown control layer — the human-editable strategic memory (RFC §9).
    vault_path: Path = Field(default_factory=lambda: _default_home() / "vault")

    #: Host workspaces that sandboxes mutate via patches.
    workspace_root: Path = Field(default_factory=lambda: _default_home() / "workspaces")

    #: zstd level for cold-lake payloads. 3 is the speed/ratio knee; the lake is
    #: write-heavy and read-rare, so we do not pay for higher levels.
    zstd_level: int = Field(default=3, ge=1, le=22)

    #: SQLite busy timeout. Generous because the curator holds long write
    #: transactions during nightly consolidation.
    sqlite_busy_timeout_ms: int = Field(default=10_000, ge=100)

    def all_paths(self) -> list[Path]:
        """Directories that must exist before the runtime boots."""
        return [
            self.sqlite_path.parent,
            self.qdrant_path,
            self.kuzu_path.parent,
            self.cold_lake_path,
            self.vault_path,
            self.workspace_root,
        ]


class ContextSettings(BaseModel):
    """Bounded-context construction. RFC §5."""

    #: Hard ceiling on the planner's context packet (RFC §5, DoD item 3).
    token_ceiling: int = Field(default=1500, ge=128)

    #: Worker packets are tighter — the worker needs paths and primitives, not
    #: narrative (RFC §2.1 agent 5).
    worker_token_ceiling: int = Field(default=1000, ge=64)

    #: Semantic matches below this cosine similarity are discarded outright.
    relevance_floor: Probability = 0.75

    #: Relational facts below this confidence never enter the candidate pool.
    confidence_floor: Probability = 0.70

    #: Facts at or above this importance survive pruning even when they do not
    #: resolve a required slot — these are the system invariants.
    invariant_importance: Probability = 0.85

    #: Slot-fill ratio at or above which the planner may run unassisted.
    density_proceed: Probability = 0.85

    #: Below ``density_proceed`` but at or above this, trigger background
    #: hydration from the cold lake and retry.
    density_hydrate: Probability = 0.40

    #: Characters per token for the cheap estimator. 4.0 is the standard
    #: English approximation; the real tokenizer is used when available.
    chars_per_token: float = Field(default=4.0, gt=0)

    @model_validator(mode="after")
    def _check_density_ordering(self) -> ContextSettings:
        if self.density_hydrate >= self.density_proceed:
            raise ValueError("density_hydrate must be strictly below density_proceed")
        return self


class MemorySettings(BaseModel):
    """Decay, pruning and contradiction handling. RFC §4."""

    #: Confidence below which a fact is evicted from hot serving and
    #: compressed into a one-line cold-lake summary (RFC §4.2).
    prune_confidence_floor: Probability = 0.15

    #: Contradiction score at or above which both records are quarantined and a
    #: human tie-break is demanded (RFC §4.2).
    contradiction_threshold: Probability = 0.75

    #: Confidence both records are degraded to on quarantine.
    contradiction_degraded_confidence: Probability = 0.20

    #: Per-domain decay coefficients λ in ``C(t) = C0 · e^(-λt)``, t in days
    #: (RFC §4.1 matrix). Domains absent here do not decay.
    decay_lambda: dict[str, float] = Field(
        default_factory=lambda: {
            "long_term_distilled": 0.001,
            "semantic": 0.002,
            "reflection": 0.002,
            "relationship": 0.004,
            "temporal": 0.01,
            "tool": 0.05,
            "operational": 1.0,
        }
    )

    #: Graph edges below this weight are severed during curation.
    relationship_prune_floor: Probability = 0.10

    #: Reinforcement multiplier δ in the importance index (RFC §15.12).
    use_count_reinforcement: float = Field(default=0.01, ge=0.0)

    #: How often the decay sweep runs.
    decay_sweep_interval_hours: int = Field(default=6, ge=1)

    #: Nightly curation window (local time), and its hard wall-clock cap.
    curation_window_start_hour: int = Field(default=2, ge=0, le=23)
    curation_max_runtime_hours: float = Field(default=2.0, gt=0)


class PolicySettings(BaseModel):
    """Risk gating. RFC §9, §2.1 agent 4."""

    mode: PermissionMode = PermissionMode.ASK

    #: Cosine similarity against an anti-goal at or above which the task is
    #: killed outright (RFC §2.1 agent 4).
    anti_goal_threshold: Probability = 0.82

    #: Confidence a plan must reach for AUTO mode to skip the human gate
    #: (RFC §9.1).
    auto_confidence_floor: Probability = 0.80

    #: Skills at or above this risk profile always demand a human gate,
    #: regardless of mode. Belt-and-braces over the permission matrix.
    always_gate_risk_profile: Probability = 0.90

    #: Policy evaluation must stay under this to keep the hot path responsive
    #: (RFC §2.1 agent 4 budget). Breaches are logged, not fatal.
    latency_budget_ms: float = Field(default=50.0, gt=0)


class SandboxSettings(BaseModel):
    """Containment. RFC §13, §14.

    SPEC DEVIATION (docs/adr/0006): gVisor (``runsc``) is a Linux user-space
    kernel and does not exist on Windows. The backend is pluggable; the
    Windows default combines a workspace jail, Job Object memory/CPU caps, a
    severed network path and a mandatory AST pre-scan. That is materially
    weaker than gVisor's syscall interception and is documented as such.
    """

    backend: Literal["auto", "subprocess", "wsl", "docker", "dryrun"] = "auto"

    #: OCI runtime the docker backend requests via ``--runtime`` (RFC §13). The
    #: default ``runc`` is stock namespaces; ``runsc`` selects gVisor's syscall
    #: interception (ADR-0019), the RFC's actual containment requirement, and is
    #: only honoured when the daemon reports a ``runsc`` runtime is registered.
    #: A string, not a Literal, so a site can name a bespoke runtime (e.g.
    #: ``kata-runtime``) without a code change.
    container_runtime: str = "runc"

    #: Wall-clock kill switch when the modality does not pin one.
    default_timeout_seconds: float = Field(default=30.0, gt=0)

    #: SPEC DEVIATION (docs/adr/0009): the RFC's termination rule integrates
    #: memory over time (``∫MemoryUsage(t)dt > Ceiling``), which yields
    #: MB-seconds — dimensionally not a memory bound. We enforce peak RSS,
    #: which is what cgroups and Job Objects actually measure.
    enforce_peak_rss: bool = True

    #: Sampling period for the resource watchdog.
    watchdog_interval_seconds: float = Field(default=0.25, gt=0)

    #: Heartbeat cadence sandboxes emit to prove liveness (RFC §1.4).
    heartbeat_interval_seconds: float = Field(default=5.0, gt=0)

    #: Missed heartbeats before the host declares a worker dead.
    heartbeat_miss_tolerance: int = Field(default=3, ge=1)

    #: Deny network egress unless the skill holds ``PERM_NET_EGRESS``.
    default_deny_network: bool = True

    #: Maximum bytes captured from a sandbox's stdout/stderr, to stop a runaway
    #: process from exhausting host memory through the pipe.
    max_capture_bytes: int = Field(default=8 * 1024 * 1024, ge=4096)

    #: Nested delegation ceiling (RFC §2.2). Modality profiles may lower it.
    absolute_recursion_ceiling: int = Field(default=4, ge=0)


class ModelSettings(BaseModel):
    """Inference providers.

    SPEC DEVIATION (docs/adr/0007): the RFC pins vLLM + Llama-3-8B-Q8 at 85% of
    VRAM plus a concurrent Mistral-7B critic. vLLM publishes Linux-only wheels
    and requires CUDA/ROCm; the target machine is Windows with a 2 GB AMD iGPU.
    The runtime is therefore provider-agnostic and local-first with explicit,
    ledger-logged escalation.
    """

    #: Cheap local model for routing, classification and extraction.
    #:
    #: Any name in :mod:`paa.models.registry` is accepted (``ollama``,
    #: ``lmstudio``, ``llamacpp``, ``vllm``, ``tgwebui``, ``echo`` ...), so
    #: localhost inference is a one-word config change. The legacy values
    #: ``"ollama"``/``"llamacpp"``/``"echo"`` keep their exact previous
    #: behaviour and honour :attr:`local_base_url`; other registry names use
    #: their own loopback default unless :attr:`local_api_base` overrides it.
    local_provider: str = "ollama"
    local_model: str = "qwen2.5:3b-instruct"
    local_base_url: str = "http://127.0.0.1:11434"
    #: Base-URL override for a registry-selected local provider. ``None`` uses
    #: the provider's conventional localhost port.
    local_api_base: str | None = None

    #: Frontier model used only when a task escalates. Accepts any registry name
    #: (``anthropic``, ``gemini``, ``openai``, ``groq``, ``openrouter`` ...) or
    #: ``"none"`` to disable escalation entirely (air-gapped operation).
    escalation_provider: str = "anthropic"
    escalation_model: str = "claude-sonnet-5"
    escalation_api_key_env: str = "ANTHROPIC_API_KEY"
    escalation_base_url: str | None = None
    #: Base-URL override for a registry-selected escalation provider. ``None``
    #: uses the platform's published endpoint.
    escalation_api_base: str | None = None

    #: Modalities at or above which escalation is permitted at all.
    escalate_at_or_above: ComplexityModality = ComplexityModality.COMPLEX

    #: Never escalate in these modes, whatever the modality says. LOCKDOWN is
    #: an air-gap promise; escalation is a network call.
    escalation_forbidden_modes: tuple[PermissionMode, ...] = (PermissionMode.LOCKDOWN,)

    #: Concurrent generative streams. 2 on constrained hardware (RFC §6.2).
    max_concurrent_streams: int = Field(default=2, ge=1)

    request_timeout_seconds: float = Field(default=120.0, gt=0)
    max_retries: int = Field(default=2, ge=0)

    #: Embedding model. 384 dimensions matches the RFC's Qdrant collections.
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_dimensions: int = Field(default=384, ge=8)

    #: Fall back to a deterministic hash embedder when sentence-transformers is
    #: absent. Keeps the runtime testable without a 2 GB torch install; recall
    #: quality is materially worse, so it warns loudly at startup.
    allow_hash_embedder_fallback: bool = True


class QueueSettings(BaseModel):
    """Backpressure and dispatch. RFC §6."""

    #: Depth at which the orchestrator degrades modality and sheds load.
    backpressure_depth: int = Field(default=10, ge=1)

    #: Depth at which non-essential ingestion is refused with HTTP 429.
    shed_load_depth: int = Field(default=50, ge=1)

    #: Delivery attempts before a message is parked in the dead-letter queue.
    max_delivery_attempts: int = Field(default=3, ge=1)

    #: How long a claimed-but-unacked message may sit before redelivery.
    visibility_timeout_seconds: float = Field(default=300.0, gt=0)

    #: Distributed lock TTL (RFC §1.4 ``lock:entity:*``).
    entity_lock_ttl_seconds: float = Field(default=60.0, gt=0)

    @model_validator(mode="after")
    def _check_depth_ordering(self) -> QueueSettings:
        if self.shed_load_depth <= self.backpressure_depth:
            raise ValueError("shed_load_depth must exceed backpressure_depth")
        return self


class ObservabilitySettings(BaseModel):
    """Tracing and metrics. RFC §10."""

    enabled: bool = True
    service_name: str = "paa-runtime"
    otlp_endpoint: str | None = None
    """When None, traces are recorded to the local ledger only — no egress."""

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = False
    #: Retain execution traces this long before the curator compresses them.
    trace_retention_days: int = Field(default=30, ge=1)


# ---------------------------------------------------------------------------


class Settings(BaseSettings):
    """Top-level runtime configuration."""

    model_config = SettingsConfigDict(
        env_prefix="PAA_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        validate_default=True,
    )

    home: Path = Field(default_factory=_default_home)

    storage: StorageSettings = Field(default_factory=StorageSettings)
    context: ContextSettings = Field(default_factory=ContextSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    policy: PolicySettings = Field(default_factory=PolicySettings)
    sandbox: SandboxSettings = Field(default_factory=SandboxSettings)
    models: ModelSettings = Field(default_factory=ModelSettings)
    queue: QueueSettings = Field(default_factory=QueueSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)

    #: Bind address for the ingestion API. Loopback only — the RFC's "no public
    #: edge ingestion" constraint is enforced here rather than documented.
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8787, ge=1, le=65535)

    @field_validator("api_host")
    @classmethod
    def _enforce_loopback(cls, v: str) -> str:
        """Refuse non-loopback binds unless explicitly overridden.

        RFC §6 "Out of Scope" forbids public ingestion routes. Making this a
        validator means a typo in ``.env`` cannot silently expose the runtime.
        """
        allowed = {"127.0.0.1", "localhost", "::1"}
        if v not in allowed and os.environ.get("PAA_ALLOW_NON_LOOPBACK") != "1":
            raise ValueError(
                f"api_host {v!r} is not loopback. The runtime is single-user and "
                "local-first; set PAA_ALLOW_NON_LOOPBACK=1 to override deliberately."
            )
        return v

    @model_validator(mode="after")
    def _propagate_home(self) -> Settings:
        """Re-root storage paths when ``home`` is overridden.

        Sub-model defaults are computed at construction from ``PAA_HOME``, so a
        caller passing ``Settings(home=...)`` directly would otherwise get
        mismatched paths. Only untouched defaults are moved.
        """
        default_root = _default_home()
        if self.home == default_root:
            return self

        s = self.storage
        for field, relative in (
            ("sqlite_path", Path("state") / "paa.db"),
            ("qdrant_path", Path("state") / "qdrant"),
            ("kuzu_path", Path("state") / "graph"),
            ("cold_lake_path", Path("cold_lake")),
            ("vault_path", Path("vault")),
            ("workspace_root", Path("workspaces")),
        ):
            if getattr(s, field) == default_root / relative:
                object.__setattr__(s, field, self.home / relative)
        return self

    def ensure_directories(self) -> None:
        """Create every directory the runtime needs. Idempotent."""
        self.home.mkdir(parents=True, exist_ok=True)
        for path in self.storage.all_paths():
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()


def reset_settings_cache() -> None:
    """Drop the cached singleton. Tests use this after mutating the environment."""
    get_settings.cache_clear()
