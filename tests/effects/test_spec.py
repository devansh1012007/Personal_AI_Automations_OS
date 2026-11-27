"""EffectSpec construction, determinism, and the payload round-trip (C9.2).

The spec is the unit the whole registry rests on, so its invariants are
pinned directly: the effect id derives from lineage+seq and nothing else,
a compensation is data with arithmetic that must add up (a remove records
no prior content; a restore records both digest and address), a path is a
bounded normalized workspace-relative posix path, and the serialized form
survives a round-trip while malformed forms read as *no effect* rather
than crashing a reader.
"""

from __future__ import annotations

import uuid

import pytest
from hypothesis import given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st

from paa.effects.spec import (
    CompensationAction,
    CompensationSpec,
    EffectKind,
    EffectSpec,
    OwnerRef,
    effect_id_for,
)

_LINEAGE = uuid.UUID("12345678-1234-5678-1234-567812345678")
_OTHER = uuid.UUID("87654321-8765-4321-8765-432187654321")
_SHA_A = "a" * 64
_SHA_B = "b" * 64


class TestEffectIdDeterminism:
    def test_same_lineage_and_seq_mint_the_same_id(self) -> None:
        assert effect_id_for(_LINEAGE, 3) == effect_id_for(_LINEAGE, 3)

    def test_different_seq_mints_a_different_id(self) -> None:
        assert effect_id_for(_LINEAGE, 3) != effect_id_for(_LINEAGE, 4)

    def test_different_lineage_mints_a_different_id(self) -> None:
        assert effect_id_for(_LINEAGE, 3) != effect_id_for(_OTHER, 3)

    def test_uuid_object_and_hyphenated_string_agree(self) -> None:
        assert effect_id_for(_LINEAGE, 2) == effect_id_for(str(_LINEAGE), 2)

    def test_a_non_uuid_lineage_is_refused(self) -> None:
        with pytest.raises(ValueError, match="badly formed hexadecimal UUID string"):
            effect_id_for("not-a-uuid", 1)

    def test_a_non_positive_seq_is_refused(self) -> None:
        with pytest.raises(ValueError, match="seq must be >= 1"):
            effect_id_for(_LINEAGE, 0)

    @given(seq=st.integers(min_value=1, max_value=10_000))
    @hyp_settings(max_examples=200, deadline=None)
    def test_the_id_is_sixty_four_hex_chars_for_any_seq(self, seq: int) -> None:
        digest = effect_id_for(_LINEAGE, seq)
        assert len(digest) == 64
        assert all(c in "0123456789abcdef" for c in digest)


class TestCompensationSpec:
    def test_remove_records_no_prior_content(self) -> None:
        spec = CompensationSpec(action=CompensationAction.REMOVE, path="out/new.txt")
        assert spec.prior_sha256 is None
        assert spec.prior_bytes_ref is None

    def test_restore_requires_digest_and_address(self) -> None:
        spec = CompensationSpec(
            action=CompensationAction.RESTORE,
            path="out/old.txt",
            prior_sha256=_SHA_A,
            prior_bytes_ref=_SHA_B,
        )
        assert spec.to_payload()["action"] == "restore"

    def test_a_remove_with_prior_content_is_refused(self) -> None:
        with pytest.raises(ValueError, match="remove compensation records no prior"):
            CompensationSpec(
                action=CompensationAction.REMOVE,
                path="out/new.txt",
                prior_sha256=_SHA_A,
            )

    def test_a_restore_without_a_digest_is_refused(self) -> None:
        with pytest.raises(ValueError, match="requires a prior_sha256"):
            CompensationSpec(
                action=CompensationAction.RESTORE,
                path="out/old.txt",
                prior_sha256=None,
                prior_bytes_ref=_SHA_B,
            )

    def test_a_restore_with_a_non_hex_digest_is_refused(self) -> None:
        with pytest.raises(ValueError, match="requires a prior_sha256"):
            CompensationSpec(
                action=CompensationAction.RESTORE,
                path="out/old.txt",
                prior_sha256="not-a-digest",
                prior_bytes_ref=_SHA_B,
            )

    @pytest.mark.parametrize(
        "path",
        [
            "",
            "/abs/path.txt",
            "C:\\win\\path.txt",
            "back\\slash.txt",
            "../escape.txt",
            "a/../b.txt",
            "./here.txt",
            "double//slash.txt",
            "trail/",
            "nul\x00byte.txt",
            "x" * 1025,
        ],
    )
    def test_paths_that_cannot_be_jailed_honestly_are_refused(self, path: str) -> None:
        with pytest.raises(ValueError):
            CompensationSpec(action=CompensationAction.REMOVE, path=path)

    def test_a_long_but_in_bound_path_is_accepted(self) -> None:
        # Exactly at the bound: 2 + 1018 + 4 = 1024 chars. The refused
        # case above is one char over, so the pair pins the boundary from
        # both sides — the length is asserted so the arithmetic cannot
        # silently drift off the boundary again.
        path = "d/" + "x" * 1018 + ".txt"
        assert len(path) == 1024
        spec = CompensationSpec(action=CompensationAction.REMOVE, path=path)
        assert spec.path == path


class TestEffectSpecConstruction:
    def test_for_call_derives_id_owner_and_scope(self) -> None:
        effect = EffectSpec.for_call(
            _LINEAGE,
            7,
            EffectKind.FILE_WRITE,
            CompensationSpec(action=CompensationAction.REMOVE, path="a.txt"),
        )
        assert effect.effect_id == effect_id_for(_LINEAGE, 7)
        assert effect.scope == str(_LINEAGE)
        assert effect.owner == OwnerRef(str(_LINEAGE), 7, "PIPELINE_CALL_COMPLETED")

    def test_an_irreversible_effect_declares_no_compensation(self) -> None:
        effect = EffectSpec.for_call(_LINEAGE, 1, EffectKind.IRREVERSIBLE)
        assert effect.compensation is None

    def test_an_irreversible_effect_with_a_compensation_is_refused(self) -> None:
        with pytest.raises(ValueError, match="irreversible"):
            EffectSpec.for_call(
                _LINEAGE,
                1,
                EffectKind.IRREVERSIBLE,
                CompensationSpec(action=CompensationAction.REMOVE, path="a.txt"),
            )

    def test_a_file_write_without_a_compensation_is_refused(self) -> None:
        with pytest.raises(ValueError, match="requires a compensation spec"):
            EffectSpec.for_call(_LINEAGE, 1, EffectKind.FILE_WRITE)

    def test_an_id_that_is_not_the_deterministic_one_is_refused(self) -> None:
        with pytest.raises(ValueError, match="not the deterministic id"):
            EffectSpec(
                effect_id=_SHA_A,
                kind=EffectKind.IRREVERSIBLE,
                scope=str(_LINEAGE),
                owner=OwnerRef(str(_LINEAGE), 1, "PIPELINE_CALL_COMPLETED"),
            )

    def test_a_scope_that_differs_from_the_owner_lineage_is_refused(self) -> None:
        with pytest.raises(ValueError, match="scope to coincide"):
            EffectSpec(
                effect_id=effect_id_for(_LINEAGE, 1),
                kind=EffectKind.IRREVERSIBLE,
                scope=str(_OTHER),
                owner=OwnerRef(str(_LINEAGE), 1, "PIPELINE_CALL_COMPLETED"),
            )

    def test_an_unknown_owner_event_type_is_refused(self) -> None:
        with pytest.raises(ValueError, match="unknown owner event type"):
            OwnerRef(str(_LINEAGE), 1, "TOOL_CALL_COMPLETED")


class TestPayloadRoundTrip:
    def test_a_compensable_spec_survives_the_round_trip(self) -> None:
        effect = EffectSpec.for_call(
            _LINEAGE,
            4,
            EffectKind.FILE_EDIT,
            CompensationSpec(
                action=CompensationAction.RESTORE,
                path="src/main.py",
                prior_sha256=_SHA_A,
                prior_bytes_ref=_SHA_B,
            ),
        )
        assert EffectSpec.from_payload(effect.to_payload()) == effect

    def test_an_irreversible_spec_serializes_a_null_compensation(self) -> None:
        effect = EffectSpec.for_call(_LINEAGE, 5, EffectKind.IRREVERSIBLE)
        payload = effect.to_payload()
        assert payload["compensation"] is None
        assert EffectSpec.from_payload(payload) == effect

    @pytest.mark.parametrize(
        "payload",
        [
            None,
            "a string",
            [],
            {},
            {"v": 2, "id": _SHA_A},
            {"v": 1},
            {"v": 1, "id": _SHA_A, "kind": "time_travel", "scope": str(_LINEAGE)},
            {
                "v": 1,
                "id": _SHA_A,
                "kind": "irreversible",
                "scope": str(_LINEAGE),
                "owner": "not-a-dict",
            },
            {
                "v": 1,
                "id": _SHA_A,
                "kind": "irreversible",
                "scope": str(_LINEAGE),
                "owner": {"lineage": str(_LINEAGE), "seq": 1},
            },
            # A compensation field that fails to parse refuses the whole
            # record: a restore that cannot be honoured must not read as
            # an effect with nothing to compensate.
            {
                "v": 1,
                "id": _SHA_A,
                "kind": "irreversible",
                "scope": str(_LINEAGE),
                "owner": {
                    "lineage": str(_LINEAGE),
                    "seq": 1,
                    "event_type": "PIPELINE_CALL_COMPLETED",
                },
                "compensation": {"action": "restore", "path": "x"},
            },
            # A structurally-valid payload whose fields violate the
            # construction invariants (here: the id is not the
            # deterministic one for the owner) reads as no effect.
            {
                "v": 1,
                "id": _SHA_B,
                "kind": "irreversible",
                "scope": str(_LINEAGE),
                "owner": {
                    "lineage": str(_LINEAGE),
                    "seq": 1,
                    "event_type": "PIPELINE_CALL_COMPLETED",
                },
            },
        ],
    )
    def test_malformed_payloads_read_as_no_effect(self, payload: object) -> None:
        assert EffectSpec.from_payload(payload) is None
