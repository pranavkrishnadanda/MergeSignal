# CLAUDE.md

Guidance for AI agents working in this repository. The full spec — architecture,
requirements, and the rationale behind each subsystem — lives in
[docs/DESIGN.md](docs/DESIGN.md). Read it before making non-trivial changes.

## What this is

MergeSignal: pre-merge intelligence for git. Given a base ref and a head ref it
reports four signals — textual conflict prediction (S1, via
`git merge-tree --write-tree`), semantic breakage (S2, tree-sitter symbol
analysis), cross-PR overlap (S3), and an explainable risk score (S4). Ships as a
CLI (`mergesignal analyze|scan|serve`) and a FastAPI webhook service.

## Commands

```bash
pip install -e ".[dev]"              # install with dev extras
pytest -q                            # full suite (~2 min; builds real git repos)
pytest tests/unit -q                 # fast subset, no git needed
pytest tests/regression -q           # golden corpus + end-to-end CLI
pytest tests/regression --snapshot-update   # regenerate golden snapshots —
                                            # then read `git diff` line by line
ruff check src tests                 # lint (line-length 100, py311 target)
mergesignal analyze --base main --head feature   # dogfood on this repo
```

## Layout

```
src/mergesignal/
  cli.py        # typer commands + build_context / run_pipeline / collect_others
  models.py     # pydantic v2 schemas — THE CONTRACT everything codes against
  config.py     # .mergesignal.yaml -> Config (extra keys rejected)
  git/          # subprocess git wrapper, merge simulation, history stats
  analysis/     # diff parsing, tree-sitter registry, symbol index
  signals/      # conflicts.py, semantic.py, overlap.py, risk.py
  report/       # text/md/json renderers + idempotent PR comment
  github/       # REST client, App auth, webhook signature verification
  service/      # FastAPI webhook receiver + checkout/analyze/comment worker
tests/
  helpers/repo_builder.py   # RepoBuilder: scripts REAL git repos in tmp dirs
  unit/  integration/  regression/   # see "Testing" below
```

## Invariants — do not break these

- **Signal engines never raise.** `analyze(ctx) -> Signal`; `run_signal` traps
  every exception into `status="error"`. Missing inputs → `skipped` + reason,
  never a fake `ok`.
- **Never mutate the user's repository** (NFR-2). No fetches, checkouts, ref
  writes, or index changes in the CLI path. The *service* may fetch freely —
  its clone is a throwaway temp dir.
- **`models.py` is the contract.** Changing a field or type ripples to every
  signal, renderer, and snapshot. Touch it deliberately.
- **GitHub is optional.** Everything except `--prs`/the service must work with
  no network and no token.
- **Exit codes are a contract:** 0 clean, 1 findings ≥ threshold, 2 error.
  Errors outrank findings.

## Testing conventions

- **Never mock git.** Integration and regression tests build real repositories
  with `RepoBuilder` (fixed identity, deterministic clock). Mock the GitHub API
  with `httpx.MockTransport`; mock nothing else.
- **Golden corpus:** `tests/regression/scenarios.py` scripts one repo per
  scenario; reports are normalised (shas, temp paths, timestamps scrubbed) and
  compared to `snapshots/*.json`. Regenerate with `--snapshot-update` and review
  the diff line by line — every changed line is a change in what users see.
- **`Scenario.start_time` must stay relative to now** (default: now − 30 days).
  The risk engine's churn/co-change factors run `git log --since=90.days.ago`,
  evaluated at analysis time — a fixed date silently ages out of the window and
  both factors report "unavailable" while the suite stays green. This exact bug
  existed; `test_history_factors_are_measured` is the tripwire.
- Every snapshot scenario also carries explicit assertions in
  `test_golden_corpus.py` — a snapshot shows *that* output moved, the assertion
  pins *what* was supposed to hold. Keep both when adding a scenario.

## Conventions

- `from __future__ import annotations`; type hints on public functions; module
  docstrings explain *why*, including documented failure/degradation modes.
- Google-style docstrings; ruff lint rules E,F,I,UP,B,W,C4,SIM,RUF,BLE.
- Comments/docstrings in this codebase document deliberate design trade-offs —
  preserve them when editing.
- Secrets only ever come from environment variables; never from config files
  or logs.
