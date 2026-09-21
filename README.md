# MergeSignal

**What happens if this merges?**

MergeSignal answers that before the merge button gets pressed. Point it at a base
ref and a head ref and it reports four signals:

| Signal | Question it answers | How |
|---|---|---|
| **S1 Conflicts** | Will `git merge` conflict, and where? | `git merge-tree --write-tree` simulation — never touches your working tree |
| **S2 Semantic breakage** | Will it merge *cleanly* and still break? | tree-sitter symbol analysis: one side deletes/renames/re-signatures, the other side references |
| **S3 Cross-PR overlap** | Which other open branches or PRs collide? | file → hunk → symbol intersection, ranked sharpest-first |
| **S4 Risk** | How dangerous is this merge? | explainable 0–100 score from churn, co-change coupling, hot paths, a test-coverage proxy and diff size |

S2 is the one git cannot tell you. When one branch renames `load_settings` and
another adds a call to the old name, git merges both without complaint and the
result raises `ImportError` on the first run. That is what MergeSignal is for.

Semantic findings are **verified against the actual merged tree**, not just
inferred from the two diffs. MergeSignal runs `git merge-tree --write-tree`,
greps the resulting tree for each removed or renamed name, and re-parses every
hit file: a finding fires only when the merged result really contains a call
to a name nothing defines. References the merge itself rewrote away, names the
other side still defines, and optional-parameter additions (`f(a)` →
`f(a, b=None)`) produce no finding. Matches that cannot be proven — a bare
name collision across distant modules, an unparseable signature — are held in
the report's `suppressed` metadata, auditable in `--format json` but unable to
drive the exit code.

The risk score is **context, not verdict**: churn, co-change, hot-path and
size factors decompose in `factor_evidence` metadata and never appear as
findings unless you set `risk_threshold`. Unsupported languages degrade to
textual overlap and report `skipped` — never a crash, and never a false
"looks fine".

## Install

Requires **Python ≥ 3.11** and **git ≥ 2.38** (for `merge-tree --write-tree`;
older git falls back to a temporary worktree, which is slower but correct).

```bash
pip install mergesignal
```

From a checkout:

```bash
pip install -e ".[dev]"     # dev extras add pytest + ruff
```

## Quickstart

```bash
# Will merging 'feature' into 'main' cause trouble?
mergesignal analyze --base main --head feature

# Machine-readable, for CI
mergesignal analyze --base main --head feature --format json | jq '.signals[].status'

# Which of my other branches collide with this one?
mergesignal analyze --base main --head feature --branches all

# Against GitHub PRs (needs GITHUB_TOKEN and the refs fetched locally)
mergesignal analyze --base main --head feature --prs open

# Which of all my open branches are fighting each other?
mergesignal scan --base main
```

Output formats: `--format text` (default, colourised), `md` (for PR comments),
`json` (stable key order — the format the golden regression corpus snapshots).

Useful flags:

| Flag | Effect |
|---|---|
| `-C, --repo PATH` | repository to analyse (default: `.`) |
| `--signal NAME` | run only these signals; repeatable or comma-separated |
| `--threshold LEVEL` | severity at or above which the command exits 1 |
| `--config PATH` | config file (default: search upward for `.mergesignal.yaml`) |
| `-v, --verbose` | include full evidence in the output |

## Exit codes

The contract, for use in scripts and CI:

| Code | Meaning |
|---|---|
| `0` | Clean — no finding reached the severity threshold |
| `1` | Findings at or above the threshold |
| `2` | Error — bad arguments, git failure, unreadable config, or a signal engine failed |

Errors beat findings: a run where an engine blew up exits `2` even if the engines
that *did* run found nothing, because it cannot honestly claim the merge is clean.

In a CI step, treat `1` as "report it", not "fail the build", unless you mean to
gate on it:

```bash
mergesignal analyze --base "$BASE_SHA" --head HEAD --format md || [ "$?" -eq 1 ]
```

## Configuration

Drop a `.mergesignal.yaml` at your repository root. Everything is optional;
missing keys take the defaults shown.

```yaml
# Which engines to run. Order is ignored — reports are always in canonical order.
enabled_signals: [conflicts, semantic, overlap, risk]

# Findings at or above this severity make the CLI exit 1.
severity_threshold: high        # low | medium | high | critical

# Opt-in CI gate: emit one 'risk score' finding when the score meets this value.
# Default is no gate — risk informs the report but never blocks on its own.
# risk_threshold: 70

# How the 0-100 risk score is composed. Weights are relative, not required to sum to 1.
risk_weights:
  churn: 0.25                   # recent commit activity on the touched paths
  co_change: 0.20               # files that historically change together but aren't in this diff
  hot_paths: 0.25               # matches the hot_paths globs below
  test_coverage: 0.15           # changed source files with no test touched (a proxy, not real coverage)
  diff_size: 0.15               # raw files-touched and lines-churned

# Blast-radius globs. '**' works; a bare name matches at any depth.
hot_paths:
  - "src/mergesignal/models.py"
  - "**/migrations/**"
  - "*.proto"

# Excluded from all analysis — vendored or generated code.
ignore_paths:
  - ".git/**"
  - "vendor/**"

analysis:
  max_files: 500                # stop indexing symbols past this many changed files
  max_file_bytes: 1000000       # skip tree-sitter on files bigger than this
  history_days: 90              # churn / co-change window
  history_max_commits: 2000     # hard cap on `git log` traversal
  git_timeout_seconds: 30.0     # per-invocation subprocess timeout

github:                         # entirely optional; MergeSignal works offline
  repo: owner/name              # inferred from the 'origin' remote when omitted
  comment: true                 # service posts/updates one idempotent PR comment
  check_run: false
  max_prs: 20
```

**Tokens come from the environment only, never from this file.** `GITHUB_TOKEN`
for a PAT, or `MERGESIGNAL_APP_ID` + `MERGESIGNAL_PRIVATE_KEY` +
`MERGESIGNAL_WEBHOOK_SECRET` for GitHub App auth.

## Webhook service

Optional. `mergesignal serve` runs a FastAPI receiver that verifies webhook
signatures, analyses `pull_request` events in an isolated temp clone, and
upserts a single collapsible PR comment (it finds its previous comment by a
hidden marker rather than spamming a new one each push).

```bash
export MERGESIGNAL_WEBHOOK_SECRET=...      # required; unsigned traffic is refused
export MERGESIGNAL_APP_ID=... MERGESIGNAL_PRIVATE_KEY=...
mergesignal serve --host 0.0.0.0 --port 8000
```

## Guarantees

- **Never mutates your repository.** No checkouts, no fetches, no ref writes, no
  index or working-tree changes — merges are *simulated*.
- **Never crashes on the awkward cases.** Detached HEAD, unborn branches, binary
  files, unsupported languages, filenames with spaces or unicode, old git: each
  degrades to a `skipped` signal with a reason.
- **Offline-capable.** GitHub is entirely optional; everything but `--prs` works
  with no network.

## Development

```bash
pip install -e ".[dev]"
pytest -q                                   # full suite
pytest tests/unit -q                        # fast, no git
pytest tests/regression -q                  # golden corpus + end-to-end CLI
ruff check src tests
```

`tests/regression/` is a **golden corpus**: each scenario scripts a real git
repository, runs the whole pipeline and compares the report against a committed
JSON snapshot, so a silent behaviour change fails CI instead of shipping. To
accept an intended change:

```bash
pytest tests/regression --snapshot-update
git diff tests/regression/snapshots          # read every changed line
```

Architecture and the full requirement list live in [docs/DESIGN.md](docs/DESIGN.md).

## License

MIT
