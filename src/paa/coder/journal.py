"""Ledger-before-side-effect journal for coder tool calls (N2.2).

The N2.1 freeze (ADR-0046) named the vocabulary and the
:class:`~paa.coder.loop.ToolJournal` protocol; this module is the producer.
Every call reaches the ledger *before* the tool executes, in exactly the
order the loop guarantees:

    PRE hook (may modify / redact args)
      -> ``before(ctx)``   TOOL_CALL_PROPOSED, attesting the **post-hook**
         args (``args_sha256`` of the canonical JSON of the redacted args,
         plus the persisted intent a crash-recovery sweep will classify
         an interrupted call from)
      -> permission / ASK (the checks themselves stay in the loop)
      -> execute
      -> ``after(ctx, ok, result_sha)``  TOOL_CALL_COMPLETED | FAILED
    or, on a refusal at any gate, ``denied(ctx, reason)``  TOOL_CALL_DENIED

Two rules make that ordering safe rather than merely documented.

**Never attest pre-hook arguments.** ``before`` is called only with a
``ToolCtx`` the loop built *after* the PRE hook returned, so
``args_sha256`` and ``args_preview_redacted`` describe the args the
permission check and the tool actually saw. A redaction is a new frozen
``ToolCtx``, never an in-place edit (the dataclass is frozen for exactly
this reason).

**A call that cannot be identified cannot be journalled, and therefore
cannot execute.** The ADR-0008 idempotency key is
``(correlation_id, event_type, attempt, discriminator)`` with the
discriminator ``f"{session_id}:{call_id}:{kind}"``. A ``:` in either id
makes two different calls share one discriminator (``"s:1"/"c"`` and
``"s"/"1:c"`` collide), and an empty ``call_id`` collapses every call
in the session to one discriminator — in both cases the second
PROPOSED would dedupe away and its tool would execute unjournalled. The
journal rejects both shapes with :class:`ValueError` *before* appending
anything, and the protocol's "raise to abort, never a flag" rule means
the loop does not then execute the tool. The loop mints a real
``call_id`` for the legacy empty-id case (a fresh uuid hex per call), so
the rejection only ever fires for ids a caller deliberately supplied
that cannot be made unambiguous.

Persisted intent is computed here, not in the loop: the loop is
tool-agnostic and must not learn what ``write_file`` means, while this
module may know the file tools' argument shapes because classifying
their crashes is its job. :func:`compute_persisted_intent` hashes the
*actual* target file (jailed, never the raw string) and predicts the
intended result from the post-hook args; for tools that touch no file
it returns ``(None, None, None)`` and the payload records explicit
nulls, never absent keys (ADR-0046).

SPEC DEVIATION (docs/adr/0046) — the tier fold: the journal builds every
payload from an explicit field list and refuses, before appending, any
payload carrying ``fact_id``. A fact-shaped key in a coder payload would
otherwise be folded into tier state by ``apply_tier_event``'s
default-to-T2 branch (W2-REV-01 MINOR-2). The journal cannot receive a
``fact_id`` through its API at all — no method accepts one — so the
check is a tripwire against a future edit that widens a payload; the
structural exclusion of coder types from the tier fold itself belongs
to the fold's owner and is deferred, not worked around here.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from paa.coder.loop import ToolCtx
from paa.coder.workspace import Workspace, WorkspaceViolation, assert_inside_workspace
from paa.core.types import EventType
from paa.ledger.events import (
    BlobStoreLike,
    LedgerEvent,
    canonical_json,
    maybe_blob_payload,
    payload_requires_blob,
    payload_size_bytes,
)
from paa.ledger.store import LedgerStore
from paa.validation.patch import compute_patch_sha256

__all__ = [
    "LedgerToolJournal",
    "args_preview_redacted",
    "args_sha256_of",
    "compute_persisted_intent",
    "discriminator_for",
]

#: The discriminator kinds, one per journalled outcome (ADR-0046).
_KIND_PROPOSED = "PROPOSED"
_KIND_DENIED = "DENIED"
_KIND_COMPLETED = "COMPLETED"
_KIND_FAILED = "FAILED"

#: Tool names whose calls touch a jailed file, per the N1.1-N1.4
#: catalogues. Anything else (reads, shell, git, memory tools) touches
#: no file this journal can hash, so its intent is three explicit nulls.
_FILE_TOOLS = frozenset({"write_file", "edit_file"})

#: Preview budget (ADR-0046): the ledger stays readable, so a preview is
#: short on purpose. Values render as shapes (see
#: :func:`_render_preview_value`), so this cap only bounds the number of
#: arguments, never a single value's content.
_PREVIEW_MAX_CHARS = 240

#: Argument keys that name a secret whatever their value looks like. The
#: value is masked outright rather than pattern-redacted, because a
#: short credential can sit below every secret pattern and still not
#: belong in a ledger row.
_SECRET_KEY_HINTS = ("secret", "token", "password", "passwd", "api_key", "apikey", "key")


def discriminator_for(session_id: str, call_id: str, kind: str) -> str:
    """The ADR-0008 discriminator for one journalled outcome.

    ``kind`` is one of ``PROPOSED`` / ``DENIED`` / ``COMPLETED`` /
    ``FAILED`` — the four outcomes of one call are four distinct events.
    Callers must go through :func:`_validate_ids` first: this function
    does not check, because the check needs to name which id offended.
    """
    return f"{session_id}:{call_id}:{kind}"


def _validate_ids(session_id: str, call_id: str) -> None:
    """Reject ids that cannot be made unambiguous in a discriminator.

    An empty id is not "no id": the discriminator would collapse every
    call sharing it to one key. A ``:` in either id makes the
    ``session:call_id:kind`` split ambiguous, so two genuinely distinct
    calls can produce the same discriminator and the second would dedupe
    away unjournalled. Both are caller errors, raised as
    :class:`ValueError` before anything is appended; the protocol has no
    "do not execute" flag precisely so this raise is the abort.
    """
    for label, value in (("session_id", session_id), ("call_id", call_id)):
        if not value:
            raise ValueError(f"ToolJournal requires a non-empty {label}")
        if ":" in value:
            raise ValueError(
                f"ToolJournal requires {label} without ':', got {value!r} "
                "— it would collide with another call's discriminator"
            )


def args_sha256_of(arguments: Mapping[str, Any]) -> str:
    """SHA-256 of the canonical JSON of the post-hook arguments.

    ``canonical_json`` (sorted keys, no whitespace) is the one
    serialisation the ledger hashes with, so "structurally equal args"
    has exactly one digest here and in every proof that recomputes it.
    """
    return hashlib.sha256(canonical_json(dict(arguments)).encode("utf-8")).hexdigest()


def args_preview_redacted(tool: str, arguments: Mapping[str, Any]) -> str:
    """A short, human-readable summary of the args — never their content.

    The preview orients a human reading ``paa ledger`` output (which
    args, how large); the attested form is ``args_sha256``, so the
    preview can afford to say less, and it must: a string value is
    rendered as a shape summary (``<str len=… sha256=…>``), never
    verbatim. That is the only rule that holds against an *opaque*
    secret — a credential with no ``sk-`` shape, handed to no one as a
    literal — because neither the generic patterns nor a
    secret-named-key mask can see it (W2-IMPL-04's canary, N2.5). A
    preview that showed short values verbatim would leak exactly the
    secrets the ledger must never carry; a shape summary cannot, by
    construction, whatever the value is. Values under secret-named
    keys are still masked outright (``key=***``) so the common case
    reads familiarly. Non-string scalars (numbers, booleans, null)
    render as-is; containers render their shape recursively. The
    whole preview is capped, ending with an ellipsis rather than a
    partial value that looks complete.
    """
    parts: list[str] = []
    for key in sorted(arguments):
        value = arguments[key]
        if _is_secret_key(key):
            parts.append(f"{key}=***")
            continue
        rendered = _render_preview_value(value)
        parts.append(f"{key}={rendered}")
    preview = f"{tool}({', '.join(parts)})"
    if len(preview) > _PREVIEW_MAX_CHARS:
        preview = preview[: _PREVIEW_MAX_CHARS - 1] + "…"
    return preview


def _is_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(hint in lowered for hint in _SECRET_KEY_HINTS)


def _render_preview_value(value: Any) -> str:
    """Render one arg value as a shape, never its content.

    Strings summarise to ``<str len=N sha256=xxxxxxxx>`` — length and a
    short digest fingerprint, so two different values still read as
    different while neither is disclosed. Containers recurse so a
    nested ``env`` allowlist is summarised key-by-key rather than
    dumped. Numbers, booleans and null render as-is: they are passed
    through ``redact_detail`` unchanged elsewhere for the same reason
    (they cannot carry a credential's shape), and a numeric token is
    not a string a model copies verbatim from an environment.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
        return f"<str len={len(value)} sha256={digest}>"
    if isinstance(value, Mapping):
        inner = ", ".join(
            f"{key}:{_render_preview_value(value[key])}" for key in sorted(value)
        )
        return "{" + inner + "}"
    if isinstance(value, (list, tuple)):
        inner = ", ".join(_render_preview_value(item) for item in value)
        return f"[{inner}]"
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()[:8]
    return f"<value sha256={digest}>"


def compute_persisted_intent(
    workspace: Workspace | None,
    tool: str,
    arguments: Mapping[str, Any],
) -> tuple[str | None, str | None, str | None]:
    """``(pre_sha256, post_sha256, patch_sha256)`` for one proposed call.

    Computed from the *actual* files, before the tool runs, so the
    recovery sweep (N2.3) can classify a PROPOSED-without-outcome as
    applied (live file == post), not applied (live file == pre), or
    unknown. The rules per tool:

    * ``write_file`` — ``pre`` is the hash of the existing target's
      bytes, or ``None`` when the target does not exist (a creation;
      the sweep reads "file absent" against a ``None`` pre as
      not-applied, which is why ``None`` here means "no prior file",
      disambiguated by the tool name in the payload). ``post`` is the
      hash of the intended UTF-8 bytes.
    * ``edit_file`` — ``pre`` as above for the existing file; ``post``
      is the hash of the file with ``old_str`` replaced by ``new_str``
      exactly once (``None`` when that prediction cannot be made —
      wrong arity, missing file, or a non-unique match — because the
      tool will refuse it anyway, and an unmakeable prediction must not
      be guessed at).
    * ``apply_patch`` — ``patch`` is ``compute_patch_sha256`` of the
      supplied patch; ``pre``/``post`` are ``None`` because a patch can
      touch many files and a single hash would be a lie.
    * everything else — three ``None``: explicit nulls, so the sweep
      never has to distinguish "absent" from "null".

    A path that fails the jail or cannot be read yields ``None`` intent
    rather than raising: the tool itself will refuse that call with its
    own frozen reason, and a journal that raised here would abort the
    loop before the refusal could be recorded. Intent is a
    classification aid; it must not become a second gate.

    INVARIANT (fidelity limit, disclosed): ``post`` is a prediction
    from the post-hook args under the tools' own text model (UTF-8,
    LF-normalised, BOM re-attached). A file whose on-disk line endings
    the applier re-joins differently (a CRLF prior) can therefore
    differ from the recorded ``post`` byte-for-byte, and the sweep
    classifies that call ``unknown`` (never auto-repeat) rather than
    ``applied``. The prediction is exact for LF files, which is the
    shape the write tools produce for new content; the alternative —
    letting each tool report its own intent — would need a second
    protocol method this ADR froze without one.
    """
    if tool == "apply_patch":
        patch = arguments.get("patch")
        if isinstance(patch, str) and patch:
            return (None, None, compute_patch_sha256(patch))
        return (None, None, None)
    if tool not in _FILE_TOOLS or workspace is None:
        return (None, None, None)

    raw_path = arguments.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return (None, None, None)
    try:
        # Join like the write tools do (``workspace.root / Path(raw)``):
        # a workspace-relative path must resolve inside the workspace,
        # not against the process cwd. An absolute raw path survives
        # the join as itself, so the jail still catches an escape.
        resolved = assert_inside_workspace(workspace, workspace.root / Path(raw_path))
    except (WorkspaceViolation, ValueError, OSError):
        return (None, None, None)
    pre = _hash_file(resolved)

    if tool == "write_file":
        content = arguments.get("content")
        if not isinstance(content, str):
            return (pre, None, None)
        return (pre, _sha256_text(content), None)

    # edit_file
    old_str = arguments.get("old_str")
    new_str = arguments.get("new_str")
    if not isinstance(old_str, str) or not old_str or not isinstance(new_str, str):
        return (pre, None, None)
    predicted = _predict_edit(resolved, old_str, new_str)
    return (pre, _sha256_text(predicted) if predicted is not None else None, None)


def _hash_file(path: Path) -> str | None:
    """SHA-256 of a file's bytes, or ``None`` when there is no file."""
    try:
        if not path.is_file():
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _predict_edit(path: Path, old_str: str, new_str: str) -> str | None:
    """The file's text with ``old_str`` replaced once, or ``None``.

    Mirrors the edit tool's text model (UTF-8, LF-normalised, a single
    BOM re-attached) so the prediction is the tool's own output for an
    LF file. Any shape the tool would refuse (binary, non-UTF-8, zero
    or multiple matches, unreadable) predicts ``None``: the sweep then
    reports the call ``unknown`` instead of a guessed hash.
    """
    try:
        if not path.is_file():
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    has_bom = text.startswith("\ufeff")
    body = text[len("\ufeff") :] if has_bom else text
    normalised = body.replace("\r\n", "\n").replace("\r", "\n")
    old_normalised = old_str.replace("\r\n", "\n").replace("\r", "\n")
    if normalised.count(old_normalised) != 1:
        return None
    new_normalised = new_str.replace("\r\n", "\n").replace("\r", "\n")
    replaced = normalised.replace(old_normalised, new_normalised, 1)
    return ("\ufeff" + replaced) if has_bom else replaced


def _assert_no_fact_id(payload: dict[str, Any], event_type: EventType) -> None:
    """Tripwire against a payload edit that would leak into tier state.

    ``apply_tier_event`` folds *any* event carrying ``fact_id`` into
    tier state (defaulting to T2); a coder payload must therefore never
    name one. The journal's methods build payloads from explicit field
    lists, so this cannot fire today — it exists so a future field
    addition that smuggles a fact reference in fails loudly at append
    time instead of fabricating tier state at every rebuild.
    """
    if "fact_id" in payload:
        raise ValueError(
            f"coder payload for {event_type.value} must not carry 'fact_id' "
            "— it would be folded into tier state by apply_tier_event"
        )


class LedgerToolJournal:
    """Append the ADR-0046 tool-call events to one ledger lineage.

    Constructed with the runtime's :class:`~paa.ledger.store.LedgerStore`
    and the lineage (correlation) the coder session journals into — the
    two seams :class:`~paa.runtime.Runtime` already hands the composition
    root. ``attempt`` is always 0 and the discriminator is
    ``session:call_id:kind`` (see :func:`discriminator_for`), so a
    redelivered PROPOSED append returns the existing event through the
    store's own dedupe-then-seal transaction; the journal adds no second
    dedupe of its own.

    The three methods satisfy :class:`~paa.coder.loop.ToolJournal`
    structurally (async, same arities); the class deliberately does not
    inherit the protocol, matching the loop module's own idiom for
    ``Tool`` / ``ToolCallingModel``.

    The optional blob store (N2.6) is the size-check target: when a
    journalled payload's canonical form exceeds the ledger's inline
    limit, the payload is offloaded to the store and the row keeps only
    the descriptor. Without a store an oversized payload raises rather
    than bloating the row — a journal that cannot journal a call within
    the size contract must abort it, exactly as it aborts a call whose
    ids cannot be made unambiguous.
    """

    def __init__(
        self,
        ledger: LedgerStore,
        correlation_id: uuid.UUID,
        *,
        blob_store: BlobStoreLike | None = None,
    ) -> None:
        self._ledger = ledger
        self._correlation_id = correlation_id
        self._blob_store = blob_store

    def _blobbed(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Apply the N2.6 size check to one payload.

        Small payloads pass through unchanged. Oversized payloads are
        blobbed through the events contract, which is the single place
        the threshold, the descriptor shape, and the hash verification
        live; this module adds only the policy that an oversized payload
        without a blob store is a :class:`ValueError` (raised before any
        append, so — as with :func:`_validate_ids` — the raise is the
        abort and the tool does not execute unjournalled).
        """
        if not payload_requires_blob(payload):
            return payload
        if self._blob_store is None:
            raise ValueError(
                "journal payload exceeds the 8 KB inline limit "
                f"({payload_size_bytes(payload)} bytes) and no blob store is "
                "configured — refusing to bloat the ledger"
            )
        return maybe_blob_payload(payload, self._blob_store)

    async def before(self, ctx: ToolCtx) -> None:
        """Append TOOL_CALL_PROPOSED attesting the post-hook args.

        Called by the loop after the PRE hook and before any permission
        decision or execution — the ledger-before-side-effect point. A
        duplicate delivery appends nothing new: the store returns the
        existing event for the same discriminator.
        """
        _validate_ids(ctx.session_id, ctx.call_id)
        payload: dict[str, Any] = {
            "v": 1,
            "session_id": ctx.session_id,
            "call_id": ctx.call_id,
            "tool": ctx.tool,
            "args_sha256": ctx.args_sha256,
            "args_preview_redacted": ctx.args_preview_redacted,
            "isolation_level": ctx.isolation_level.name,
            "pre_sha256": ctx.pre_sha256,
            "post_sha256": ctx.post_sha256,
            "patch_sha256": ctx.patch_sha256,
        }
        _assert_no_fact_id(payload, EventType.TOOL_CALL_PROPOSED)
        blobbed = self._blobbed(payload)
        _assert_no_fact_id(blobbed, EventType.TOOL_CALL_PROPOSED)
        await self._ledger.append(
            LedgerEvent.create(
                self._correlation_id,
                EventType.TOOL_CALL_PROPOSED,
                attempt=0,
                discriminator=discriminator_for(
                    ctx.session_id, ctx.call_id, _KIND_PROPOSED
                ),
                payload=blobbed,
            )
        )

    async def denied(self, ctx: ToolCtx, reason: str) -> None:
        """Append TOOL_CALL_DENIED — proof the tool never executed."""
        _validate_ids(ctx.session_id, ctx.call_id)
        payload: dict[str, Any] = {
            "v": 1,
            "session_id": ctx.session_id,
            "call_id": ctx.call_id,
            "tool": ctx.tool,
            "reason": reason,
        }
        _assert_no_fact_id(payload, EventType.TOOL_CALL_DENIED)
        blobbed = self._blobbed(payload)
        _assert_no_fact_id(blobbed, EventType.TOOL_CALL_DENIED)
        await self._ledger.append(
            LedgerEvent.create(
                self._correlation_id,
                EventType.TOOL_CALL_DENIED,
                attempt=0,
                discriminator=discriminator_for(ctx.session_id, ctx.call_id, _KIND_DENIED),
                payload=blobbed,
            )
        )

    async def after(self, ctx: ToolCtx, ok: bool, result_sha: str | None) -> None:
        """Append COMPLETED (``ok``) or FAILED (not ``ok``).

        A FAILED row never attests a partial result hash: ``result_sha``
        is forced to ``None`` on failure even if a caller passes one,
        because a failed call has no trustworthy result. A COMPLETED row
        needs its result hash — ``after(ctx, True, None)`` is a caller
        bug and raises rather than writing a row that claims success
        with nothing hashed.
        """
        _validate_ids(ctx.session_id, ctx.call_id)
        if ok:
            if result_sha is None:
                raise ValueError(
                    "COMPLETED requires result_sha — a success row with no "
                    "result hash cannot be classified by N2.3"
                )
            payload: dict[str, Any] = {
                "v": 1,
                "session_id": ctx.session_id,
                "call_id": ctx.call_id,
                "tool": ctx.tool,
                "result_sha256": result_sha,
                "args_sha256": ctx.args_sha256,
                "isolation_level": ctx.isolation_level.name,
            }
            _assert_no_fact_id(payload, EventType.TOOL_CALL_COMPLETED)
            blobbed = self._blobbed(payload)
            _assert_no_fact_id(blobbed, EventType.TOOL_CALL_COMPLETED)
            await self._ledger.append(
                LedgerEvent.create(
                    self._correlation_id,
                    EventType.TOOL_CALL_COMPLETED,
                    attempt=0,
                    discriminator=discriminator_for(
                        ctx.session_id, ctx.call_id, _KIND_COMPLETED
                    ),
                    payload=blobbed,
                )
            )
        else:
            failed_payload: dict[str, Any] = {
                "v": 1,
                "session_id": ctx.session_id,
                "call_id": ctx.call_id,
                "tool": ctx.tool,
                # The frozen protocol carries no failure reason (the
                # loop knows the exception, the journal method does
                # not), so the row records the outcome, not the cause.
                # N2.3's recovery FAILED — reason naming the
                # interruption — is written by the sweep, a different
                # emitter with the information this one lacks.
                "reason": "tool failed",
                "result_sha256": None,
                "args_sha256": ctx.args_sha256,
                "isolation_level": ctx.isolation_level.name,
            }
            _assert_no_fact_id(failed_payload, EventType.TOOL_CALL_FAILED)
            blobbed_failed = self._blobbed(failed_payload)
            _assert_no_fact_id(blobbed_failed, EventType.TOOL_CALL_FAILED)
            await self._ledger.append(
                LedgerEvent.create(
                    self._correlation_id,
                    EventType.TOOL_CALL_FAILED,
                    attempt=0,
                    discriminator=discriminator_for(ctx.session_id, ctx.call_id, _KIND_FAILED),
                    payload=blobbed_failed,
                )
            )
