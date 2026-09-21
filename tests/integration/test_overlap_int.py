"""Integration tests for S3 against real branches built by RepoBuilder.

Covers the DESIGN.md §4 "candidate vs 3 overlapping branches" scenario and the
N=0 edge case.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mergesignal.analysis.diff import diff_refs
from mergesignal.analysis.index import SymbolIndex, diff_symbols, index_paths
from mergesignal.config import Config
from mergesignal.git.repo import Repo
from mergesignal.models import AnalysisContext, BranchDiff
from mergesignal.signals import overlap
from tests.helpers.repo_builder import RepoBuilder

pytestmark = pytest.mark.integration

#: A file with two declarations far enough apart that editing both ends is a
#: file-level, not a hunk-level, collision.
LIB = (
    "def alpha(x):\n    return x\n"
    + "".join(f"# pad {i}\n" for i in range(38))
    + "\ndef beta(y):\n    return y\n"
)


def changed_symbols(repo: Repo, base: str, head: str) -> list:
    """The declarations a branch actually changed — the sharp overlap key."""
    diff = diff_refs(repo, base, head, merge_base=True)
    paths = index_paths(diff)
    before = SymbolIndex.from_ref(repo, repo.merge_base(base, head) or base, paths)
    after = SymbolIndex.from_ref(repo, head, paths)
    return [change.symbol for change in diff_symbols(before, after)]


def branch_diff(repo: Repo, name: str, *, base: str = "main") -> BranchDiff:
    """Build one :class:`~mergesignal.models.BranchDiff` overlap input."""
    return BranchDiff(
        name=name,
        head=name,
        base=base,
        diff=diff_refs(repo, base, name, merge_base=True),
        symbols=changed_symbols(repo, base, name),
    )


def context(
    path: Path, *, head: str = "feature", others: list[str], base: str = "main"
) -> AnalysisContext:
    """Candidate context plus the other branches it might collide with."""
    repo = Repo(path)
    candidate = diff_refs(repo, base, head, merge_base=True)
    merge_base = repo.merge_base(base, head)
    paths = index_paths(candidate)
    changes = diff_symbols(
        SymbolIndex.from_ref(repo, merge_base or base, paths),
        SymbolIndex.from_ref(repo, head, paths),
    )
    return AnalysisContext(
        repo_path=str(path),
        base=base,
        head=head,
        merge_base=merge_base,
        head_diff=candidate,
        head_changes=changes,
        others=[branch_diff(repo, name, base=base) for name in others],
        config=Config(),
    )


@pytest.fixture
def crowded_repo(builder: RepoBuilder) -> Path:
    """One candidate and three other branches with different collision depths."""
    builder.file("lib.py", LIB).file("util.py", "def helper():\n    return 1\n").commit("seed")

    # Both edits change alpha's *extent*, which is what the symbol index needs
    # to report a declaration as changed rather than merely re-spelled.
    builder.branch("feature")
    builder.file(
        "lib.py", LIB.replace("    return x\n", "    doubled = x * 2\n    return doubled\n")
    ).commit("candidate edits alpha")

    builder.checkout("main").branch("rival-symbol")
    builder.file(
        "lib.py", LIB.replace("    return x\n", "    bumped = x + 100\n    return bumped\n")
    ).commit("rival edits alpha too")

    builder.checkout("main").branch("rival-file")
    builder.file("lib.py", LIB.replace("    return y\n", "    return y + 1\n")).commit(
        "rival edits beta"
    )

    builder.checkout("main").branch("rival-none")
    builder.file("util.py", "def helper():\n    return 2\n").commit("rival edits util")

    return builder.checkout("main").build()


def test_no_other_branches_is_skipped(two_branch_repo: Path) -> None:
    signal = overlap.analyze(context(two_branch_repo, others=[]))

    assert signal.status == "skipped"
    assert "no other branches" in signal.summary


def test_candidate_against_three_branches(crowded_repo: Path) -> None:
    signal = overlap.analyze(
        context(crowded_repo, others=["rival-symbol", "rival-file", "rival-none"])
    )

    assert signal.status == "findings"
    assert signal.metadata["compared"] == 3
    assert signal.metadata["collisions"] == 2

    by_branch = {f.evidence["branch"]: f for f in signal.findings}
    assert set(by_branch) == {"rival-symbol", "rival-file"}

    sharpest = by_branch["rival-symbol"]
    assert sharpest.evidence["granularity"] == "symbol"
    assert sharpest.severity == "high"
    assert "alpha" in sharpest.evidence["symbols"]
    assert sharpest.evidence["files"] == ["lib.py"]

    weakest = by_branch["rival-file"]
    assert weakest.evidence["granularity"] == "file"
    assert weakest.severity == "medium"
    assert weakest.evidence["hunk_count"] == 0

    assert [f.evidence["branch"] for f in signal.findings] == ["rival-symbol", "rival-file"]


def test_rename_versus_edit_of_the_old_path_collides(builder: RepoBuilder) -> None:
    builder.file("util.py", "def helper():\n    return 1\n").commit("seed")
    builder.branch("feature").move("util.py", "helpers/util.py")
    builder.commit("move util")
    builder.checkout("main").branch("rival")
    builder.file("util.py", "def helper():\n    return 2\n").commit("edit util in place")
    path = builder.checkout("main").build()

    signal = overlap.analyze(context(path, others=["rival"]))

    (finding,) = signal.findings
    assert "util.py" in finding.evidence["files"]


def test_unsupported_language_still_overlaps_textually(builder: RepoBuilder) -> None:
    """No grammar, no symbols — file/hunk granularity must still work (FR-5)."""
    builder.file("script.zzz", "BEGIN\n  step one\n  step two\nEND\n").commit("seed")
    builder.branch("feature").file("script.zzz", "BEGIN\n  step ONE\n  step two\nEND\n").commit(
        "candidate"
    )
    builder.checkout("main").branch("rival")
    builder.file("script.zzz", "BEGIN\n  step 1!\n  step two\nEND\n").commit("rival")
    path = builder.checkout("main").build()

    signal = overlap.analyze(context(path, others=["rival"]))

    (finding,) = signal.findings
    assert finding.evidence["granularity"] == "hunk"
    assert finding.evidence["direct_hunk_count"] == 1
    assert finding.severity == "high"


def test_disjoint_branch_reports_ok(crowded_repo: Path) -> None:
    signal = overlap.analyze(context(crowded_repo, others=["rival-none"]))

    assert signal.status == "ok"
    assert signal.findings == []
    assert "no overlap" in signal.summary
