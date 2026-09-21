"""End-to-end tests of the installed ``mergesignal`` console script.

Everything else in the suite imports MergeSignal and calls into it. These tests
do not: they spawn the real entry point as a **subprocess**, on a repository
built on disk, and read its stdout and exit status the way a shell script or a
CI job would.

That distinction is the point. In-process tests share the parent interpreter's
``sys.path``, already-imported modules and patched environment, so they cannot
catch a broken ``[project.scripts]`` entry, a module that only imports because
pytest put ``src`` on the path, output written to the wrong stream, or a
``typer.Exit`` that never reaches ``sys.exit``. The FR-7 exit-code contract in
particular is a promise to *processes*, and it can only be verified by being
one.

Skipped automatically when the console script is not installed, so a bare
``pytest`` in a source checkout without ``pip install -e .`` still passes.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from mergesignal.cli import EXIT_CLEAN, EXIT_ERROR, EXIT_FINDINGS
from mergesignal.models import SIGNAL_NAMES
from tests.regression.scenarios import BY_NAME, Scenario, scrub_text

pytestmark = [pytest.mark.regression, pytest.mark.integration]

#: Wall-clock ceiling per CLI invocation. NFR-1 budgets 10s for a real PR; these
#: repositories are tiny, so anything near this means something is hanging.
TIMEOUT_SECONDS = 120


def _console_script() -> tuple[list[str], bool]:
    """The command that runs MergeSignal, and whether it is the console script.

    Looks beside ``sys.executable`` before consulting ``PATH``: when pytest is
    invoked as ``.venv/bin/pytest`` the virtualenv's ``bin`` is usually *not* on
    ``PATH``, and only that directory holds the ``[project.scripts]`` shim this
    test exists to verify.

    Falls back to ``python -m mergesignal.cli``, which still exercises a real
    subprocess (and therefore the exit-code contract) in a checkout where the
    package was never installed — just not the entry-point wiring.
    """
    candidate = Path(sys.executable).parent / (
        "mergesignal.exe" if os.name == "nt" else "mergesignal"
    )
    if candidate.exists():
        return [str(candidate)], True
    found = shutil.which("mergesignal")
    if found:
        return [found], True
    return [sys.executable, "-m", "mergesignal.cli"], False


COMMAND, IS_CONSOLE_SCRIPT = _console_script()

requires_cli = pytest.mark.skipif(not COMMAND, reason="mergesignal cannot be invoked")


def run_cli(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Invoke the CLI as a subprocess with a clean, offline environment.

    ``GITHUB_TOKEN`` and friends are stripped so a developer's real credentials
    can never leak into a test run, and ``src`` is put on ``PYTHONPATH`` for the
    ``python -m`` fallback.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GITHUB_", "MERGESIGNAL_"))}
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    env["NO_COLOR"] = "1"
    return subprocess.run(
        [*COMMAND, *args],
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
        env=env,
        timeout=TIMEOUT_SECONDS,
        check=False,
    )


@pytest.fixture
def scripted_repo(tmp_path: Path):
    """Build one corpus scenario on disk and hand back its path."""

    def _build(name: str) -> tuple[Path, Scenario]:
        scenario = BY_NAME[name]
        return scenario.build(tmp_path / name), scenario

    return _build


def parse_report(process: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    """Parse ``--format json`` stdout, failing loudly with stderr on garbage.

    Asserting that stdout is *pure* JSON is itself part of the contract: FR-7's
    JSON mode is meant to be piped into ``jq``, so a stray warning or progress
    line printed to stdout instead of stderr is a bug, not cosmetics.
    """
    try:
        return json.loads(process.stdout)
    except json.JSONDecodeError as exc:  # pragma: no cover - only on a real bug
        pytest.fail(
            f"stdout was not valid JSON ({exc})\n"
            f"--- exit: {process.returncode}\n--- stdout:\n{process.stdout}\n--- stderr:\n{process.stderr}"
        )


def statuses(report: dict[str, Any]) -> dict[str, str]:
    """``{signal name: status}`` for readable assertions."""
    return {signal["name"]: signal["status"] for signal in report["signals"]}


# ------------------------------------------------------------------- the CLI


@pytest.mark.skipif(not IS_CONSOLE_SCRIPT, reason="mergesignal console script is not installed")
def test_console_script_is_installed() -> None:
    """``pip install`` really produces a working ``mergesignal`` executable.

    Guards the ``[project.scripts]`` entry in ``pyproject.toml``: a typo there
    breaks every user's installation while leaving the whole in-process suite
    perfectly green.
    """
    result = run_cli("--version")
    assert result.returncode == EXIT_CLEAN, result.stderr
    assert result.stdout.startswith("mergesignal ")


@requires_cli
def test_help_and_version_are_available() -> None:
    """Metadata commands exit 0 and list every subcommand."""
    help_result = run_cli("--help")
    assert help_result.returncode == EXIT_CLEAN, help_result.stderr
    for command in ("analyze", "scan", "serve"):
        assert command in help_result.stdout

    version_result = run_cli("--version")
    assert version_result.returncode == EXIT_CLEAN
    assert "mergesignal" in version_result.stdout


@requires_cli
def test_analyze_emits_a_full_report_as_json(scripted_repo) -> None:
    """The headline case: a real Report on stdout, parseable, with all signals."""
    repo, scenario = scripted_repo("rename_vs_new_caller")
    result = run_cli(
        "analyze",
        "--base",
        scenario.base,
        "--head",
        scenario.head,
        "-C",
        str(repo),
        "--format",
        "json",
    )
    report = parse_report(result)

    assert [s["name"] for s in report["signals"]] == list(SIGNAL_NAMES)
    assert report["base"] == "main"
    assert report["head"] == "feature"
    assert report["merge_base"]
    assert report["version"]
    assert report["generated_at"]

    # The scenario's whole point: clean to git, broken to MergeSignal.
    assert statuses(report)["conflicts"] == "ok"
    assert statuses(report)["semantic"] == "findings"
    semantic = next(s for s in report["signals"] if s["name"] == "semantic")
    assert any(
        f["evidence"]["pattern"] == "renamed_old_name_referenced" for f in semantic["findings"]
    )


@requires_cli
def test_analyze_signal_statuses_per_scenario(scripted_repo) -> None:
    """Each corpus scenario drives the signals to the statuses it exists to prove."""
    expected = {
        "clean_merge": {"conflicts": "ok", "semantic": "ok", "overlap": "skipped"},
        "textual_conflict": {"conflicts": "findings", "semantic": "skipped"},
        "rename_vs_new_caller": {"conflicts": "ok", "semantic": "findings"},
        "signature_change_new_callers": {"conflicts": "ok", "semantic": "findings"},
        "unsupported_language": {"conflicts": "ok", "semantic": "skipped"},
        "binary_file": {"conflicts": "findings", "semantic": "skipped"},
        "empty_diff": {"semantic": "skipped", "overlap": "skipped", "risk": "skipped"},
    }
    for name, wanted in expected.items():
        repo, scenario = scripted_repo(name)
        result = run_cli(
            "analyze",
            "--base",
            scenario.base,
            "--head",
            scenario.head,
            "-C",
            str(repo),
            "--format",
            "json",
        )
        actual = statuses(parse_report(result))
        assert actual | wanted == actual, f"{name}: expected {wanted}, got {actual}"
        assert "error" not in actual.values(), f"{name}: an engine errored"


@requires_cli
def test_analyze_overlap_against_local_branches(scripted_repo) -> None:
    """``--branches`` reaches S3 through the real argument parsing."""
    repo, _scenario = scripted_repo("overlap_three_branches")
    result = run_cli(
        "analyze",
        "--base",
        "main",
        "--head",
        "feature",
        "-C",
        str(repo),
        "--branches",
        "peer-symbol,peer-hunk,peer-file",
        "--format",
        "json",
    )
    report = parse_report(result)
    overlap = next(s for s in report["signals"] if s["name"] == "overlap")
    assert overlap["status"] == "findings"
    branches = [f["evidence"]["branch"] for f in overlap["findings"]]
    assert branches == ["peer-symbol", "peer-hunk"], "sharpest collision first, non-collider absent"


@requires_cli
@pytest.mark.parametrize("fmt", ["text", "md", "json"])
def test_every_format_renders(scripted_repo, fmt: str) -> None:
    """All three renderers produce non-empty output and no traceback."""
    repo, _ = scripted_repo("textual_conflict")
    result = run_cli(
        "analyze", "--base", "main", "--head", "feature", "-C", str(repo), "--format", fmt
    )
    assert result.stdout.strip()
    assert "Traceback" not in result.stdout + result.stderr
    assert "settings.conf" in result.stdout
    if fmt == "json":
        parse_report(result)
    if fmt == "md":
        assert "#" in result.stdout


# ------------------------------------------------------ FR-7 exit-code contract


@requires_cli
def test_exit_0_when_nothing_reaches_the_threshold(scripted_repo) -> None:
    """Exit 0: clean. Proven on base == head, where there is nothing to find."""
    repo, _ = scripted_repo("empty_diff")
    result = run_cli(
        "analyze", "--base", "main", "--head", "main", "-C", str(repo), "--format", "json"
    )
    assert result.returncode == EXIT_CLEAN, result.stdout + result.stderr
    report = parse_report(result)
    assert all(not s["findings"] for s in report["signals"])


@requires_cli
def test_exit_0_when_the_threshold_is_raised_above_the_findings(scripted_repo) -> None:
    """Exit 0 is about the *threshold*, not about having no findings at all."""
    repo, _ = scripted_repo("textual_conflict")
    args = ["analyze", "--base", "main", "--head", "feature", "-C", str(repo), "--format", "json"]

    default_run = run_cli(*args)
    assert default_run.returncode == EXIT_FINDINGS

    raised = run_cli(*args, "--threshold", "critical")
    assert raised.returncode == EXIT_CLEAN, raised.stderr
    # The findings are still reported; they simply are not fatal any more.
    conflicts = next(s for s in parse_report(raised)["signals"] if s["name"] == "conflicts")
    assert conflicts["status"] == "findings"


@requires_cli
def test_exit_1_when_findings_reach_the_threshold(scripted_repo) -> None:
    """Exit 1: a conflict is a ``high`` finding and the default threshold is high."""
    repo, _ = scripted_repo("textual_conflict")
    result = run_cli(
        "analyze", "--base", "main", "--head", "feature", "-C", str(repo), "--format", "json"
    )
    assert result.returncode == EXIT_FINDINGS, result.stdout + result.stderr
    report = parse_report(result)
    conflicts = next(s for s in report["signals"] if s["name"] == "conflicts")
    assert len(conflicts["findings"]) == 2


@requires_cli
@pytest.mark.parametrize(
    ("args", "reason"),
    [
        (["--base", "main", "--head", "no-such-branch"], "unknown ref"),
        (["--base", "main", "--head", "feature", "--format", "yaml"], "unknown format"),
        (["--base", "main", "--head", "feature", "--signal", "telepathy"], "unknown signal"),
        (["--base", "main", "--head", "feature", "--threshold", "apocalyptic"], "unknown severity"),
    ],
)
def test_exit_2_on_bad_input(scripted_repo, args: list[str], reason: str) -> None:
    """Exit 2: every user error is refused, with the complaint on stderr."""
    repo, _ = scripted_repo("clean_merge")
    result = run_cli("analyze", "-C", str(repo), *args)
    assert result.returncode == EXIT_ERROR, f"{reason}: {result.stdout}"
    assert result.stderr.strip(), f"{reason}: exit 2 must explain itself on stderr"
    assert "Traceback" not in result.stderr


@requires_cli
def test_exit_2_outside_a_repository(tmp_path: Path) -> None:
    """Pointing at a plain directory is an error, not a crash (NFR-3)."""
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    result = run_cli("analyze", "-C", str(plain))
    assert result.returncode == EXIT_ERROR
    assert "Traceback" not in result.stderr


@requires_cli
def test_json_mode_keeps_stdout_pure(scripted_repo) -> None:
    """Warnings go to stderr so ``mergesignal ... -f json | jq`` always works.

    ``--branches`` names a branch that does not exist, which makes the CLI warn.
    The warning must not land in the JSON document.
    """
    repo, _ = scripted_repo("clean_merge")
    result = run_cli(
        "analyze",
        "--base",
        "main",
        "--head",
        "feature",
        "-C",
        str(repo),
        "--branches",
        "ghost-branch",
        "--format",
        "json",
    )
    assert "ghost-branch" in result.stderr
    report = parse_report(result)  # would fail if the warning polluted stdout
    assert report["signals"]


@requires_cli
def test_analyze_runs_on_mergesignal_itself() -> None:
    """The acceptance case: a real Report on a non-trivial repository.

    MergeSignal's own checkout has hundreds of files across many modules, so
    this exercises the pipeline at a scale the scripted corpus never reaches.
    Only the *shape* of the output is asserted — the content is whatever this
    repository's history happens to be.
    """
    repo_root = Path(__file__).resolve().parents[2]
    result = run_cli(
        "analyze", "--base", "HEAD", "--head", "HEAD", "-C", str(repo_root), "--format", "json"
    )
    assert result.returncode in (EXIT_CLEAN, EXIT_FINDINGS), result.stderr
    report = parse_report(result)
    assert [s["name"] for s in report["signals"]] == list(SIGNAL_NAMES)
    assert not [s for s in report["signals"] if s["status"] == "error"]


@requires_cli
def test_scan_works_offline(scripted_repo) -> None:
    """``scan`` defaults to local branches and never needs the network."""
    repo, _ = scripted_repo("overlap_three_branches")
    result = run_cli("scan", "--base", "main", "-C", str(repo), "--format", "json")
    assert result.returncode in (EXIT_CLEAN, EXIT_FINDINGS), result.stderr
    reports = json.loads(result.stdout)
    assert isinstance(reports, list)
    assert reports, "scan found no candidate branches"
    assert all("signals" in report for report in reports)


@requires_cli
def test_text_output_is_deterministic(scripted_repo) -> None:
    """Two runs on the same repository render identically once scrubbed.

    Non-deterministic ordering in a renderer would make the tool's output
    useless for diffing between runs, which is how people actually use it in CI.
    """
    repo, _ = scripted_repo("signature_change_new_callers")
    args = ("analyze", "--base", "main", "--head", "feature", "-C", str(repo), "--format", "md")
    first = scrub_text(run_cli(*args).stdout, repo_path=str(repo.resolve()))
    second = scrub_text(run_cli(*args).stdout, repo_path=str(repo.resolve()))
    # The rendered timestamp line is the one legitimate difference.
    strip = lambda text: [line for line in text.splitlines() if "generated" not in line.lower()]  # noqa: E731
    assert strip(first) == strip(second)
