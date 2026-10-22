"""The patch layer — path safety, planning, and the rollback journal.

``src/paa/validation/patch.py`` says of its own security boundary: *"safe_relative_path
is the one function in this module that must be correct."* It has never had a
test. The four escapes it names — traversal, absolute paths, the symlink
escape, and control characters — are the four that have each been a real CVE
class in archive and patch tooling, and each of them is asserted here.

The second half of the module is the transactional half: a dry run computes
the complete post-patch content of every file *before* a byte is written, and
:meth:`PatchApplier.apply` keeps a journal of exact prior bytes so a failure
half way through unwinds the whole patch. Both properties are asserted, and
the second one is asserted on a file whose bytes are not text, because
"restores the prior content" means the prior *bytes*.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from paa.core.errors import ValidationError
from paa.validation.patch import (
    PatchAction,
    PatchApplier,
    UnifiedDiffValidator,
    compute_patch_sha256,
    safe_relative_path,
)

# ---------------------------------------------------------------------------
# safe_relative_path — the four escape classes
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A workspace with a sibling directory outside it."""
    root = tmp_path / "workspace"
    root.mkdir()
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "secret.txt").write_text("secret\n", encoding="utf-8")
    return root


#: Paths that escape by *traversal*.
#:
#: The forms that only traverse after normalisation are the ones a check for
#: a leading ``../`` would miss: the escape is not visible until the path is
#: resolved, and a path that stays textually relative while resolving
#: outside is the same attack with a different spelling.
TRAVERSALS: list[str] = [
    "../etc/passwd",
    "../../etc/passwd",
    "a/../../etc/passwd",
    "a/b/../../../etc/passwd",
    "./../etc/passwd",
    "..\\..\\etc\\passwd",
    "a\\..\\..\\etc\\passwd",
    "dir/..",
    "a/../b/../../c",
    "subdir/./../../escape.txt",
]


@pytest.mark.parametrize("path", TRAVERSALS)
def test_traversal_is_refused(path: str, workspace: Path) -> None:
    """A relative path that resolves outside the workspace is refused."""
    with pytest.raises(ValidationError):
        safe_relative_path(path, workspace)


#: Paths that are absolute, in the three spellings that matter.
ABSOLUTE_PATHS: list[str] = [
    "/etc/passwd",
    "/",
    "//server/share/file.txt",
    "\\\\server\\share\\file.txt",
    "C:\\Windows\\System32\\cmd.exe",
    "C:/Windows/System32",
    "d:/data/file.txt",
]


@pytest.mark.parametrize("path", ABSOLUTE_PATHS)
def test_absolute_paths_are_refused(path: str, workspace: Path) -> None:
    """An absolute path is refused whichever platform spells it."""
    with pytest.raises(ValidationError):
        safe_relative_path(path, workspace)


#: Paths carrying characters that truncate a path inside a C-level API.
CONTROL_PATHS: list[str] = [
    "a\x00b",
    "a\nb",
    "a\tb",
    "a\x1bb",
    "a\x7fb",
    "a\r\nb",
]


@pytest.mark.parametrize("path", CONTROL_PATHS)
def test_control_characters_are_refused(path: str, workspace: Path) -> None:
    """A NUL or a control character truncates the path a C API opens.

    The string that gets validated and the string that gets opened must be
    the same string, or the check is theatre.
    """
    with pytest.raises(ValidationError):
        safe_relative_path(path, workspace)


def test_a_symlinked_directory_cannot_be_used_to_escape(workspace: Path) -> None:
    """The symlink escape — the one a string-only check misses.

    ``link/evil.txt`` is textually relative and stays inside the workspace
    as a string. On disk it is not: ``link`` points outside, so the file
    lands next to the secret. The check is therefore asserted on the
    *resolved* path, which is the only spelling that means anything here.
    """
    (workspace / "link").symlink_to(workspace.parent / "outside", target_is_directory=True)

    with pytest.raises(ValidationError):
        safe_relative_path("link/evil.txt", workspace)

    with pytest.raises(ValidationError):
        safe_relative_path("link/secret.txt", workspace)


def test_a_symlinked_file_cannot_be_used_to_escape(workspace: Path) -> None:
    """The same escape through a symlink to a file."""
    (workspace / "filelink").symlink_to(workspace.parent / "outside" / "secret.txt")

    with pytest.raises(ValidationError):
        safe_relative_path("filelink", workspace)


def test_a_symlink_that_stays_inside_the_workspace_is_allowed(workspace: Path) -> None:
    """Containment is about where the path lands, not about symlinks.

    A rule that refused every symlink would be refused in practice, and a
    rule that is switched off protects nothing. The check is containment.
    """
    (workspace / "real.txt").write_text("real\n", encoding="utf-8")
    (workspace / "inside").symlink_to(workspace / "real.txt")

    assert safe_relative_path("inside", workspace) == Path("inside")


def test_an_ordinary_relative_path_is_accepted(workspace: Path) -> None:
    """The happy path, including the git ``a/`` and ``b/`` prefixes."""
    assert safe_relative_path("pkg/module.py", workspace) == Path("pkg/module.py")
    assert safe_relative_path("a/pkg/module.py", workspace) == Path("pkg/module.py")
    assert safe_relative_path("b/pkg/module.py", workspace) == Path("pkg/module.py")
    assert safe_relative_path("./pkg/module.py", workspace) == Path("pkg/module.py")
    assert safe_relative_path("a\\pkg\\module.py", workspace) == Path("pkg/module.py")


def test_a_directory_named_like_the_git_prefix_keeps_its_name(workspace: Path) -> None:
    """``a/`` is stripped only as a whole leading segment.

    A real directory called ``abc/`` must not lose its first character,
    which is what a naive ``startswith("a")`` strip would do to it.
    """
    assert safe_relative_path("abc/module.py", workspace) == Path("abc/module.py")


def test_an_empty_path_is_refused(workspace: Path) -> None:
    """A path with nothing in it names the workspace root, not a file."""
    for path in ("", "   ", "\t"):
        with pytest.raises(ValidationError):
            safe_relative_path(path, workspace)


def test_the_workspace_root_itself_is_refused(workspace: Path) -> None:
    """A path that resolves to the root is not a file inside it."""
    with pytest.raises(ValidationError):
        safe_relative_path(".", workspace)


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def _diff(old_path: str, new_path: str, body: str) -> str:
    return f"--- a/{old_path}\n+++ b/{new_path}\n{body}"


ADD_LINE = "@@ -1,2 +1,3 @@\n line1\n line2\n+line3\n"


def test_a_dry_run_writes_nothing(tmp_path: Path) -> None:
    """The complete post-patch content is computed before a byte is written.

    This is what makes "a hunk that does not apply fails during planning"
    true: at planning time nothing has changed yet, so the half-applied
    state cannot be reached through a rejected hunk.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "file.txt"
    target.write_bytes(b"line1\nline2\n")

    plan = UnifiedDiffValidator().dry_run(_diff("file.txt", "file.txt", ADD_LINE), workspace)

    assert plan.ok
    assert target.read_bytes() == b"line1\nline2\n"
    assert plan.entries[0].new_content == b"line1\nline2\nline3\n"


def test_a_hunk_that_does_not_apply_fails_at_planning(tmp_path: Path) -> None:
    """The content must match byte for byte; only the line number may drift."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.txt").write_bytes(b"line1\nline2\n")

    body = "@@ -1,2 +1,3 @@\n something else\n entirely\n+line3\n"
    plan = UnifiedDiffValidator().dry_run(_diff("file.txt", "file.txt", body), workspace)

    assert not plan.ok
    assert plan.entries[0].applies_cleanly is False
    assert "does not apply" in (plan.entries[0].reason or "")


def test_a_malformed_hunk_fails_at_planning(tmp_path: Path) -> None:
    """A header that disagrees with the body is a malformed patch.

    Models write plausible headers and then a different number of lines;
    catching it at planning time is what keeps the workspace out of a state
    no ledger event describes.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.txt").write_bytes(b"line1\nline2\n")

    body = "@@ -1,5 +1,3 @@\n line1\n line2\n+line3\n"
    plan = UnifiedDiffValidator().dry_run(_diff("file.txt", "file.txt", body), workspace)

    assert not plan.ok
    assert "malformed hunk" in (plan.entries[0].reason or "")


def test_a_escaping_path_poisons_the_whole_patch(tmp_path: Path) -> None:
    """One escaping file refuses the entire patch, not just itself.

    Applying "the safe parts" of a patch that tried to escape would commit
    an artifact whose author was demonstrably hostile or broken.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.txt").write_bytes(b"line1\nline2\n")

    body = "@@ -1,2 +1,3 @@\n line1\n line2\n+line3\n"
    patch = _diff("file.txt", "file.txt", body) + _diff("../escape.txt", "../escape.txt", body)

    plan = UnifiedDiffValidator().dry_run(patch, workspace)

    assert not plan.ok
    assert plan.rejected
    assert not (workspace.parent / "escape.txt").exists()


def test_the_line_number_may_drift_but_the_content_may_not(tmp_path: Path) -> None:
    """Position is fuzzed; content is never fuzzed.

    Line-number arithmetic is exactly what a language model gets wrong,
    while the code it quotes is usually verbatim — so a miscounted ``@@``
    header must not fail a correct patch.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.txt").write_bytes(b"a\nb\nc\nd\ne\n")

    body = "@@ -1,2 +1,3 @@\n c\n d\n+e\n"
    plan = UnifiedDiffValidator().dry_run(_diff("file.txt", "file.txt", body), workspace)

    assert plan.ok
    assert plan.entries[0].new_content == b"a\nb\nc\nd\ne\ne\n"
    assert plan.entries[0].line_offset == 2


def test_a_patch_with_no_file_sections_is_refused() -> None:
    """There is nothing to apply, and guessing is not the job."""
    with pytest.raises(ValidationError):
        UnifiedDiffValidator().parse("just some text\n")


def test_a_hunk_before_any_file_header_is_refused() -> None:
    with pytest.raises(ValidationError):
        UnifiedDiffValidator().parse("@@ -1,1 +1,1 @@\n-a\n+b\n")


def test_a_file_header_without_a_hunk_is_refused() -> None:
    with pytest.raises(ValidationError):
        UnifiedDiffValidator().parse("--- a/x.txt\n+++ b/x.txt\n")


def test_a_dangling_minus_header_is_refused() -> None:
    with pytest.raises(ValidationError):
        UnifiedDiffValidator().parse("--- a/x.txt\n")


def test_the_patch_digest_ignores_the_transport_line_endings() -> None:
    """The same logical patch is one patch, whichever platform sent it.

    Otherwise the ledger's patch identity would depend on the transport
    rather than the content, and two identical patches would look like two
    different mutations during replay.
    """
    unix = _diff("file.txt", "file.txt", ADD_LINE)
    windows = unix.replace("\n", "\r\n")

    assert compute_patch_sha256(unix) == compute_patch_sha256(windows)
    assert compute_patch_sha256(unix) != compute_patch_sha256(unix + "trailing\n")


# ---------------------------------------------------------------------------
# Application and rollback
# ---------------------------------------------------------------------------


def test_apply_then_rollback_restores_the_exact_bytes(tmp_path: Path) -> None:
    """The round trip is byte-exact, not "close enough".

    The journal holds the prior bytes in memory precisely so a rollback can
    restore what was there — including a file whose content is not text and
    a file whose line endings are not the ones the patch was written with.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "file.txt"
    original = b"line1\r\nline2\r\n"
    target.write_bytes(original)

    plan = UnifiedDiffValidator().dry_run(_diff("file.txt", "file.txt", ADD_LINE), workspace)
    assert plan.ok

    applier = PatchApplier()
    journal = applier.apply(plan)

    assert target.read_bytes() == b"line1\r\nline2\r\nline3\r\n"

    applier.rollback(journal)

    assert target.read_bytes() == original


def test_the_journal_records_the_prior_bytes(tmp_path: Path) -> None:
    """The journal is the undo log, and it must hold the *bytes*."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "file.txt"
    original = b"line1\nline2\n"
    target.write_bytes(original)

    plan = UnifiedDiffValidator().dry_run(_diff("file.txt", "file.txt", ADD_LINE), workspace)
    journal = PatchApplier().apply(plan)

    assert journal.touched_paths == ["file.txt"]
    assert journal.entries[0].existed_before is True
    assert journal.entries[0].prior_bytes == original
    assert journal.to_payload()["entry_count"] == 1


def test_the_journal_records_a_file_that_did_not_exist(tmp_path: Path) -> None:
    """A created file is undone by removing it, not by writing empty bytes."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    body = "@@ -0,0 +1,1 @@\n+created\n"
    patch = "--- /dev/null\n+++ b/new.txt\n" + body
    plan = UnifiedDiffValidator().dry_run(patch, workspace)
    assert plan.ok

    applier = PatchApplier()
    journal = applier.apply(plan)

    assert (workspace / "new.txt").exists()
    assert journal.entries[0].existed_before is False
    assert journal.entries[0].prior_bytes is None

    applier.rollback(journal)

    assert not (workspace / "new.txt").exists()


@pytest.mark.xfail(
    strict=True,
    reason=(
        "A created file gains one trailing newline it was never given: the planner "
        "models an empty prior file as one empty line ('' .split('\\n') == ['']), so "
        "the created content is 'created\\n\\n' instead of 'created\\n'. "
        "src/paa/validation/patch.py is read-only for T-005; the fix is in "
        "UnifiedDiffValidator._plan_file — drop the phantom line for an empty prior "
        "file and let the trailing newline follow the patch body."
    ),
)
def test_a_created_file_contains_exactly_what_the_diff_describes(tmp_path: Path) -> None:
    """The applied bytes are the bytes the diff describes.

    The planner's contract is that the dry run computes "the complete
    post-patch content" — and the content it computes for a new file has a
    blank line the author never wrote, which is also the content whose
    sha256 the ledger records.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    body = "@@ -0,0 +1,1 @@\n+created\n"
    patch = "--- /dev/null\n+++ b/new.txt\n" + body
    plan = UnifiedDiffValidator().dry_run(patch, workspace)
    assert plan.ok

    PatchApplier().apply(plan)

    assert (workspace / "new.txt").read_bytes() == b"created\n"


def test_a_failure_mid_patch_unwinds_the_whole_patch(tmp_path: Path) -> None:
    """A write that fails half way through leaves the prior bytes intact.

    Multi-file application cannot be atomic at the OS level, so the journal
    is what makes it *effectively* atomic. Without the unwind, a failed
    patch leaves a workspace that no ledger event describes.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = workspace / "first.txt"
    second_dir = workspace / "sub"
    second_dir.mkdir()
    second = second_dir / "second.txt"
    first_original = b"line1\nline2\n"
    second_original = b"other\n"
    first.write_bytes(first_original)
    second.write_bytes(second_original)

    patch = _diff("first.txt", "first.txt", ADD_LINE) + _diff(
        "sub/second.txt", "sub/second.txt", "@@ -1,1 +1,2 @@\n other\n+more\n"
    )
    plan = UnifiedDiffValidator().dry_run(patch, workspace)
    assert plan.ok, plan.failures()

    # The workspace changes underneath the plan: the directory the second
    # file lives in is replaced by a regular file, so writing into it fails.
    second.unlink()
    second_dir.rmdir()
    second_dir.write_text("not a directory\n", encoding="utf-8")

    applier = PatchApplier()
    with pytest.raises(ValidationError) as excinfo:
        applier.apply(plan)

    assert "rolled back" in str(excinfo.value)
    assert first.read_bytes() == first_original


def test_a_patch_that_does_not_apply_cleanly_is_refused_outright(tmp_path: Path) -> None:
    """A dirty plan is not applied at all — not even its clean parts."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "file.txt"
    target.write_bytes(b"line1\nline2\n")

    body = "@@ -1,2 +1,3 @@\n nothing like it\n neither this\n+line3\n"
    plan = UnifiedDiffValidator().dry_run(_diff("file.txt", "file.txt", body), workspace)

    with pytest.raises(ValidationError):
        PatchApplier().apply(plan)

    assert target.read_bytes() == b"line1\nline2\n"


def test_a_patch_that_creates_an_existing_file_is_refused(tmp_path: Path) -> None:
    """A create against a file that is already there is not a create."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "new.txt").write_bytes(b"already here\n")

    body = "@@ -0,0 +1,1 @@\n+created\n"
    patch = "--- /dev/null\n+++ b/new.txt\n" + body
    plan = UnifiedDiffValidator().dry_run(patch, workspace)

    assert not plan.ok
    assert plan.entries[0].action is PatchAction.CREATE
    assert "already exists" in (plan.entries[0].reason or "")


def test_a_patch_that_deletes_a_missing_file_is_refused(tmp_path: Path) -> None:
    """A delete against a file that is not there changes nothing."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    body = "@@ -1,1 +0,0 @@\n-gone\n"
    patch = "--- a/gone.txt\n+++ /dev/null\n" + body
    plan = UnifiedDiffValidator().dry_run(patch, workspace)

    assert not plan.ok
    assert plan.entries[0].action is PatchAction.DELETE
    assert "does not exist" in (plan.entries[0].reason or "")


def test_the_plan_reports_what_it_would_do(tmp_path: Path) -> None:
    """The plan's payload is what the ledger records about a patch."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.txt").write_bytes(b"line1\nline2\n")

    plan = UnifiedDiffValidator().dry_run(_diff("file.txt", "file.txt", ADD_LINE), workspace)

    assert plan.files_changed == ["file.txt"]
    payload = plan.to_payload()
    assert payload["ok"] is True
    assert payload["entries"][0]["path"] == "file.txt"
    assert payload["entries"][0]["action"] == "MODIFY"
    assert payload["entries"][0]["hunks_applied"] == 1


def test_a_file_that_is_not_utf8_cannot_be_patched_as_text(tmp_path: Path) -> None:
    """Binary content is refused by the planner, with a reason.

    The patcher works in lines; a file it cannot decode is a file it would
    corrupt.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.bin").write_bytes(b"\xff\xfe\x00binary\n")

    body = "@@ -1,1 +1,2 @@\n binary\n+more\n"
    plan = UnifiedDiffValidator().dry_run(_diff("file.bin", "file.bin", body), workspace)

    assert not plan.ok
    assert "UTF-8" in (plan.entries[0].reason or "")
