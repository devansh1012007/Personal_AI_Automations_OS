"""Runtime configuration.

Every tunable constant that appears as a magic number in the RFC lives here
with its RFC section cited, so the maths in ``docs/architecture/overview.md``
and the behaviour of the code cannot drift apart silently.

Configuration precedence: explicit kwargs > environment (``PAA_*``) >
``.env`` file > defaults.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from dotenv import dotenv_values
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from paa.core.types import ComplexityModality, PermissionMode

__all__ = [
    "ContextSettings",
    "MemorySettings",
    "ModelSettings",
    "PolicySettings",
    "SandboxSettings",
    "Settings",
    "StorageSettings",
    "dotenv_value",
    "get_settings",
    "reset_dotenv_cache",
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
    #: store (ADR-0004, the laptop default); ``s3`` is an S3-compatible server
    #: (ADR-0019, ADR-0026). Both are content-addressed and verify sha256 on
    #: read, so they are interchangeable behind one factory.
    #:
    #: ``minio`` is accepted as an alias for ``s3``. The RFC named MinIO
    #: specifically, and a deployment written against it should keep working —
    #: but the substrate is the S3 *protocol*, and the server behind it is a
    #: choice, not a contract. MinIO's community distribution is archived
    #: upstream and its registry path now answers 401 for anonymous pulls, so
    #: naming one vendor in the selector made a setting that could not be
    #: satisfied on a stock host.
    backend_coldlake: Literal["filesystem", "s3", "minio"] = "filesystem"

    sqlite_path: Path = Field(default_factory=lambda: _default_home() / "state" / "paa.db")
    postgres_dsn: str | None = None

    qdrant_path: Path = Field(default_factory=lambda: _default_home() / "state" / "qdrant")
    qdrant_url: str | None = None

    kuzu_path: Path = Field(default_factory=lambda: _default_home() / "state" / "graph")

    #: Content-addressed blob store. Replaces MinIO on the laptop (ADR-0004).
    cold_lake_path: Path = Field(default_factory=lambda: _default_home() / "cold_lake")

    #: S3 endpoint for ``backend_coldlake == "s3"`` (ADR-0019, ADR-0026), e.g.
    #: ``http://seaweedfs:8333`` inside Docker. ``None`` on the laptop topology.
    #:
    #: The field is named for the *protocol*, not for a server: the client is
    #: the S3 API, and which server answers is the deployment's business. The
    #: legacy ``minio_*`` names still work — see the alias validator below.
    s3_endpoint: str | None = None
    s3_bucket: str = "paa-cold-lake"
    #: TLS to the object store. Compose runs the object store on the internal
    #: network, so the in-cluster hop defaults to plaintext there; a public S3
    #: must set True.
    s3_secure: bool = False
    #: SPEC DEVIATION (docs/adr/0019): credentials are read from these named
    #: environment variables at construction, never stored in config or the
    #: ledger — the same indirection ModelSettings uses for API keys.
    s3_access_key_env: str = "PAA_S3_ACCESS_KEY"
    # S105: this is the *name* of an environment variable, not a secret value.
    s3_secret_key_env: str = "PAA_S3_SECRET_KEY"

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

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_minio_names(cls, data: Any) -> Any:
        """Accept the pre-ADR-0026 ``minio_*`` names, and normalise them.

        The object substrate is S3, and the server behind it is the
        deployment's choice — so the *fields* are named for the protocol.
        A deployment written against the earlier names should keep working
        without editing its ``.env``, so the legacy spellings are read and
        mapped here rather than being broken.

        The legacy name wins only when the new one is absent: an operator
        who has both set is asking for two different things, and the
        explicit new name is the one they meant.
        """
        if not isinstance(data, dict):
            return data
        legacy = {
            "minio_endpoint": "s3_endpoint",
            "minio_bucket": "s3_bucket",
            "minio_secure": "s3_secure",
            "minio_access_key_env": "s3_access_key_env",
            "minio_secret_key_env": "s3_secret_key_env",
        }
        for old, new in legacy.items():
            if old in data:
                if new not in data or data[new] is None:
                    data[new] = data.pop(old)
                else:
                    data.pop(old)
        if data.get("backend_coldlake") == "minio":
            data["backend_coldlake"] = "s3"
        return data


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

    #: Time charged to the *container's start*, over and above the step's own
    #: timeout.
    #:
    #: A modality profile's timeout is a budget on the step's **work**, and
    #: the two backends have wildly different overheads. The subprocess
    #: backend spawns a process in milliseconds, so the budget is effectively
    #: all work. The docker backend has to ask the daemon to create a
    #: container, mount the workspace, and start the process — around half a
    #: second on a warm host and several seconds on a loaded one.
    #:
    #: Without the allowance the SIMPLE profile's quarter-of-a-second budget
    #: is spent *entirely* on the start, and every simple step times out
    #: before it has run a single instruction — a deployment that looks
    #: healthy and fails every trivial task. The overhead is the backend's,
    #: so it is charged to the backend, and it is a setting because the
    #: right value depends on how loaded the host is.
    container_start_grace_seconds: float = Field(default=5.0, ge=0)

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

    #: Host↔container path translation for the Docker backend.
    #:
    #: The Docker daemon resolves a bind mount's source path against the
    #: **host** filesystem. When the runtime itself runs in a container, the
    #: path it knows is not the path the daemon must be given: a workspace at
    #: ``/data/workspaces/task-1`` inside the runtime is at
    #: ``<host>/workspaces/task-1`` on the host, and passing the former to
    #: ``docker run -v`` silently mounts a *different* directory — one the
    #: host creates, empty and root-owned.
    #:
    #: The format is comma-separated ``inside=host`` pairs, and the longest
    #: matching prefix wins. Empty means "the same path", which is the
    #: correct answer when the runtime is not containerised.
    #:
    #: It is a **string**, not a tuple of pairs, on purpose. pydantic-settings
    #: decodes a complex-typed field from an environment variable as JSON, so
    #: a tuple field would demand ``[["/data","/srv/paa/data"]]`` in the
    #: environment — and an operator who writes the obvious
    #: ``/data=/srv/paa/data`` would get a settings error at boot, with the
    #: container dying before it could print a diagnostic. ``NoDecode`` stops
    #: the JSON decode; the parsing happens here, where the error message can
    #: name the format.
    #:
    #: Example (this is what docker-compose.yml sets)::
    #:
    #:     PAA_SANDBOX__HOST_PATH_MAP="/data=/srv/paa/data"
    host_path_map: Annotated[str, NoDecode] = ""

    @property
    def host_path_pairs(self) -> tuple[tuple[str, str], ...]:
        """The parsed form of :attr:`host_path_map`.

        Strict: a malformed pair raises :class:`ValueError` naming the format
        and the offending text. It is strict because the alternative is
        *silent* degradation — a dropped pair means the Docker daemon is
        handed a path it does not recognise, and it answers by creating an
        empty, root-owned directory in its place. A wrong mount that fails
        loudly is strictly better than one that quietly mounts the wrong
        thing.

        The error is raised at the point of use, not at construction. The
        runtime must still boot on a misconfigured host: a container that
        dies during settings import cannot run ``paa doctor``, and the doctor
        is exactly the run that tells the operator what is wrong.
        :attr:`host_path_map_error` is the lenient view, for the diagnostic.
        """
        pairs: list[tuple[str, str]] = []
        for chunk in self.host_path_map.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            if "=" not in chunk:
                raise ValueError(
                    f"host_path_map entry {chunk!r} is not 'inside=host'. "
                    "Entries are comma-separated, e.g. "
                    "'/data=/srv/paa/data'"
                )
            inside, _, host = chunk.partition("=")
            inside = inside.strip()
            host = host.strip()
            # A trailing slash is noise, except on "/" itself — which is a
            # meaningful mapping ("everything under this root") and must not
            # be stripped into the empty string.
            if inside != "/":
                inside = inside.rstrip("/")
            if host != "/":
                host = host.rstrip("/")
            if not inside.startswith("/"):
                raise ValueError(
                    f"host_path_map entry {chunk!r} has a non-absolute "
                    f"'inside' path {inside!r}; it must be an absolute path "
                    "as the runtime sees it"
                )
            if not host.startswith("/"):
                raise ValueError(
                    f"host_path_map entry {chunk!r} has a non-absolute 'host' "
                    f"path {host!r}; it must be an absolute path on the "
                    "machine running the Docker daemon"
                )
            pairs.append((inside, host))
        return tuple(pairs)

    @property
    def host_path_map_error(self) -> str | None:
        """Why :attr:`host_path_map` cannot be parsed, or ``None``.

        The lenient counterpart to :attr:`host_path_pairs`: a diagnostic must
        be able to *report* a broken setting without raising on it.
        """
        if not self.host_path_map.strip():
            return None
        try:
            _ = self.host_path_pairs
        except ValueError as exc:
            return str(exc)
        return None

    #: The network a container with ``allow_network=True`` joins when the
    #: spec does not name one. Empty means the Docker daemon's default
    #: bridge, which has full host and internet reachability.
    #:
    #: The server topology does **not** set this: the tool layer chooses a
    #: network per plugin (the internal ``paa_tools`` for one that needs no
    #: egress, ``paa_tools_egress`` for one that does), and a value set
    #: globally would silently give every networked container the same
    #: reachability. Leaving it empty is the loud option — the backend logs
    #: that it is about to use the default bridge.
    default_container_network: str = ""


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
    #: Transport-level retries for *transient* failures — timeouts, connection
    #: errors and HTTP 408/429/5xx — before a provider is declared unavailable
    #: (SPEC DEVIATION (docs/adr/0021)). Deliberately a separate knob from
    #: :attr:`max_retries`, which is structured-output *repair* only: it re-asks
    #: after a malformed answer and never protects the request itself. Without
    #: this, one 429 or 5xx from a hosted gateway ends the whole run, and
    #: escalation policy can forbid the fallback that would have absorbed it.
    max_transport_retries: int = Field(default=2, ge=0)

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

    #: Escape hatch for the loopback constraint.
    #:
    #: RFC §6 "Out of Scope" forbids public ingestion routes, and the default
    #: enforces that. But a containerised runtime *has* to bind something
    #: other than loopback: Docker publishes a port by rewriting the
    #: destination to the container's address, and a socket bound to the
    #: container's own loopback does not accept a packet addressed to that
    #: interface. The host-side binding stays loopback-only, so the escape
    #: hatch loosens the *container's* bind, not the host's exposure.
    #:
    #: It is a *field* rather than a bare environment variable read inside a
    #: validator, so that it appears in the settings schema, in ``paa doctor``,
    #: and in the deployment guide's configuration table — and so that the
    #: compose file's variable list can be checked against the schema, which
    #: is exactly the check that caught it being invisible.
    allow_non_loopback: bool = False

    @model_validator(mode="after")
    def _enforce_loopback(self) -> Settings:
        """Refuse non-loopback binds unless explicitly overridden.

        A model validator rather than a field validator, because the check
        needs both ``api_host`` and ``allow_non_loopback`` — a field
        validator sees one field at a time and cannot ask its sibling.
        """
        allowed = {"127.0.0.1", "localhost", "::1"}
        if self.api_host not in allowed and not self.allow_non_loopback:
            raise ValueError(
                f"api_host {self.api_host!r} is not loopback. The runtime is "
                "single-user and local-first; set PAA_ALLOW_NON_LOOPBACK=1 to "
                "override deliberately."
            )
        return self

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


_DOTENV_CACHE: dict[str, str] | None = None


def dotenv_value(name: str) -> str | None:
    """Value of ``name`` in the ``.env`` of the current working directory.

    Deliberately does **not** write to ``os.environ``. pydantic-settings already
    reads that file for the ``PAA_*`` fields of :class:`Settings`; this covers
    the variables that are *not* ``Settings`` fields but are still configured
    there — chiefly each provider's API key, which
    :func:`paa.models.registry.build_provider` resolves by env-var name. Without
    it a credential placed in ``.env`` is read by config and silently ignored by
    the model layer, surfacing as a 401 from the provider instead of a
    configuration error.

    Mutating the environment instead would inject every ``PAA_*`` line into
    ``os.environ`` for the whole process, including test runs, so the file is
    parsed into a cache and consulted on demand.

    Values already exported in the environment are expected to win; callers
    check ``os.environ`` first and fall back here.
    """
    global _DOTENV_CACHE
    if _DOTENV_CACHE is None:
        path = Path.cwd() / ".env"
        parsed = dotenv_values(path) if path.is_file() else {}
        # ``dotenv_values`` types unset keys as ``None``; drop them so the cache
        # stays ``dict[str, str]`` and ``KEY=`` means the same as an absent key.
        _DOTENV_CACHE = {k: v for k, v in parsed.items() if v}
    return _DOTENV_CACHE.get(name)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()


def reset_settings_cache() -> None:
    """Drop the cached singleton. Tests use this after mutating the environment."""
    get_settings.cache_clear()


def reset_dotenv_cache() -> None:
    """Forget the parsed ``.env``. Tests use this after changing directory."""
    global _DOTENV_CACHE
    _DOTENV_CACHE = None
