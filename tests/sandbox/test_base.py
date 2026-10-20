"""The sandbox's self-report, rendered once for every surface.

``paa doctor`` (CLI) and the TUI dashboard both show what the sandbox backend
knows about the host it is running on. The rendering lives in
:func:`paa.sandbox.base.sandbox_report_rows` so the two surfaces cannot drift
apart — a runtime that tells the operator two different things about the same
host, depending on which surface they happened to open, is worse than either
of them being wrong alone, because the operator has no way to tell which to
believe.
"""

from __future__ import annotations

from paa.sandbox.base import collect_sandbox_facts, sandbox_report_rows


def test_a_backend_with_no_facts_contributes_no_rows() -> None:
    """A dry-run backend has nothing to say, and says it by saying nothing."""
    assert sandbox_report_rows({}) == []


def test_the_sandbox_row_names_the_backend_and_the_level() -> None:
    rows = sandbox_report_rows({"backend": "docker", "isolation_level": "VM"})
    assert rows[0] == ("sandbox", "docker (VM)")


def test_a_registered_runtime_says_registered() -> None:
    rows = sandbox_report_rows(
        {
            "backend": "docker",
            "isolation_level": "VM",
            "container_runtime": "runsc",
            "runtime_available": True,
        }
    )
    assert ("oci runtime", "runsc (registered)") in rows


def test_a_missing_runtime_names_the_fallback_and_the_level_it_implies() -> None:
    """The fallback is the fact an operator needs, and the level it implies.

    The isolation level is in the sentence because that is the number the
    policy layer reads when it decides whether a skill may run — and it is
    derived from the answer to this very question.
    """
    rows = sandbox_report_rows(
        {
            "backend": "docker",
            "isolation_level": "NAMESPACE",
            "container_runtime": "runsc",
            "runtime_available": False,
        }
    )
    value = dict(rows)["oci runtime"]
    assert "NOT registered" in value
    assert "runc" in value
    assert "NAMESPACE" in value


def test_an_unprobed_runtime_is_not_reported_as_missing() -> None:
    """A backend that never asked cannot claim the answer is "no".

    Reporting "could not determine" instead of "not registered" is the
    difference between sending an operator to reinstall a runtime that is
    installed and working, and telling them the truth.
    """
    rows = sandbox_report_rows(
        {
            "backend": "docker",
            "isolation_level": "NAMESPACE",
            "container_runtime": "runsc",
            "runtime_available": None,
        }
    )
    assert dict(rows)["oci runtime"] == "runsc (could not determine)"


def test_the_image_row_says_whether_the_image_is_there() -> None:
    present = sandbox_report_rows(
        {
            "backend": "docker",
            "isolation_level": "VM",
            "image": "paa/base_worker:v4.1",
            "image_present": True,
        }
    )
    assert ("sandbox image", "paa/base_worker:v4.1 (present)") in present

    absent = sandbox_report_rows(
        {
            "backend": "docker",
            "isolation_level": "VM",
            "image": "paa/base_worker:v4.1",
            "image_present": False,
        }
    )
    assert "NOT present" in dict(absent)["sandbox image"]

    unknown = sandbox_report_rows(
        {
            "backend": "docker",
            "isolation_level": "VM",
            "image": "paa/base_worker:v4.1",
            "image_present": None,
        }
    )
    assert dict(unknown)["sandbox image"] == "paa/base_worker:v4.1"


def test_a_broken_host_path_map_is_reported_as_its_own_row() -> None:
    """The fix for a broken map is a configuration change, so it says so."""
    rows = sandbox_report_rows(
        {
            "backend": "docker",
            "isolation_level": "NAMESPACE",
            "host_path_map_error": "entry '/data:/srv' is not 'inside=host'",
        }
    )
    value = dict(rows)["host path map"]
    assert "MISCONFIGURED" in value
    assert "inside=host" in value


def test_a_failed_diagnostic_is_reported_rather_than_swallowed() -> None:
    """The doctor exists to report the broken thing.

    A diagnostic that failed is the most important thing there is to say, so
    it gets its own row and the error text is not dropped on the floor.
    """
    rows = sandbox_report_rows(
        {"backend": "docker", "diagnostic_error": "the daemon timed out"}
    )
    assert ("sandbox", "docker (diagnostics failed)") in rows
    assert "the daemon timed out" in dict(rows)["diagnostics"]


def test_a_failed_diagnostic_suppresses_the_facts_it_could_not_get() -> None:
    """A half-answered report is worse than an honest one.

    The rows below the failure would be the *stale* ones — the snapshot
    taken before the probe — and printing them next to a failure is how a
    diagnostic ends up contradicting itself.
    """
    rows = sandbox_report_rows(
        {
            "backend": "docker",
            "isolation_level": "NAMESPACE",
            "diagnostic_error": "the daemon timed out",
            "container_runtime": "runsc",
            "runtime_available": False,
        }
    )
    assert [label for label, _ in rows] == ["sandbox", "diagnostics"]


class _BrokenBackend:
    """A backend that breaks its own contract in each of the three ways."""

    name = "docker"

    def __init__(self, behaviour: str) -> None:
        self._behaviour = behaviour

    async def diagnose(self) -> object:
        if self._behaviour == "raises":
            raise RuntimeError("the daemon timed out")
        if self._behaviour == "list":
            return ["not", "a", "mapping"]
        if self._behaviour == "string":
            return "not a mapping either"
        return {}


async def test_a_raising_diagnostic_becomes_a_reported_failure() -> None:
    """A diagnostic that throws is a diagnostic that lies.

    The doctor exists to report the broken thing, and this is exactly the
    run whose output the operator needs — so the failure is *reported*,
    with the backend's name, rather than raised.
    """
    facts = await collect_sandbox_facts(_BrokenBackend("raises"))
    assert facts["backend"] == "docker"
    assert "the daemon timed out" in facts["diagnostic_error"]


async def test_a_diagnostic_that_is_not_a_mapping_becomes_a_reported_failure() -> None:
    """A backend that returns the wrong type must not take the doctor down."""
    for behaviour in ("list", "string"):
        facts = await collect_sandbox_facts(_BrokenBackend(behaviour))
        assert facts["backend"] == "docker"
        assert "not a mapping" in facts["diagnostic_error"]


async def test_a_missing_identity_is_filled_in() -> None:
    """The row is never blank, even if the backend forgot its own name."""
    facts = await collect_sandbox_facts(_BrokenBackend("empty"))
    assert facts["backend"] == "docker"


def test_the_rows_are_in_a_stable_order() -> None:
    """The order is part of the output: the level sits above the facts it
    is derived from, so a reader cannot see them out of step."""
    rows = sandbox_report_rows(
        {
            "backend": "docker",
            "isolation_level": "VM",
            "daemon": "reachable",
            "container_runtime": "runsc",
            "runtime_available": True,
            "image": "paa/base_worker:v4.1",
            "image_present": True,
        }
    )
    assert [label for label, _ in rows] == [
        "sandbox",
        "docker daemon",
        "oci runtime",
        "sandbox image",
    ]
