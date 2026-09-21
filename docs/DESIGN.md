# MergeSignal — Design & Agent Build Plan

MergeSignal answers one question before code lands: **"What happens if this merges?"**
Given a base ref and a head ref (or a PR), it produces a report of four signals.

## 1. Signals

| Signal | Question | Method |
|---|---|---|
| S1 Conflict prediction | Will `git merge` conflict? Where? | `git merge-tree --write-tree` simulation (git ≥2.38), never mutates working tree |
| S2 Semantic breakage | Will it merge cleanly but break? | Tree-sitter symbol analysis: deletions/renames/signature changes on one side vs references added on the other |
| S3 Cross-PR overlap | Which open PRs collide? | File → hunk → symbol-level intersection across branches/PRs |
| S4 Risk / blast radius | How dangerous is this merge? | Weighted score: churn, co-change coupling, hot paths, test-coverage proxy, diff size |

Semantic findings are **heuristics with confidence levels**, not proofs. Unsupported
languages degrade to textual overlap, never crash.

## 2. Architecture

```
src/mergesignal/
├── cli.py                # mergesignal analyze | scan | serve   (typer)
├── config.py             # .mergesignal.yaml loading + validation
├── models.py             # pydantic v2 schemas — THE CONTRACT
├── git/
│   ├── repo.py           # subprocess wrapper over git binary
│   ├── merge_sim.py      # merge simulation + conflict region extraction
│   └── history.py        # churn, co-change, blame stats
├── analysis/
│   ├── diff.py           # structured diff: files, hunks, added/removed lines
│   ├── languages.py      # extension → tree-sitter grammar registry
│   ├── symbols.py        # extract defs & refs per file
│   └── index.py          # symbol index over a tree
├── signals/
│   ├── conflicts.py      # S1
│   ├── semantic.py       # S2
│   ├── overlap.py        # S3
│   └── risk.py           # S4
├── report/
│   ├── render.py         # text / markdown / json renderers
│   └── github_comment.py # collapsible idempotent PR comment
├── github/
│   ├── client.py         # httpx REST client, PR listing
│   └── app.py            # App JWT → installation token, webhook verify
└── service/
    ├── server.py         # FastAPI webhook receiver
    └── worker.py         # checkout → analyze → comment pipeline
tests/
├── unit/
├── integration/          # real repos built in tmp dirs
└── regression/           # golden scenario corpus + JSON snapshots
```

**Key decisions**

- git binary via subprocess, not libgit2 — `merge-tree` is the accurate conflict oracle.
- `tree-sitter` + `tree-sitter-language-pack` — one dep, all grammars; extension→grammar registry with textual fallback.
- pydantic v2 models drive CLI JSON output, service responses, and golden snapshots.
- Every signal implements `analyze(ctx: AnalysisContext) -> Signal` — identical plugin shape.
- `.mergesignal.yaml`: enabled signals, severity threshold, risk weights, hot paths.

## 3. Data contracts (models.py spec)

```python
Severity   = Literal["low", "medium", "high", "critical"]
Confidence = Literal["low", "medium", "high"]
ChangeKind = Literal["added", "removed", "modified", "renamed", "signature_changed"]
SymbolKind = Literal["function", "class", "method", "variable", "import"]

class Hunk(BaseModel):          # file_path, base_range, head_range, added_lines, removed_lines
class DiffFile(BaseModel):      # path, old_path?, is_binary, hunks, language
class Symbol(BaseModel):        # name, kind: SymbolKind, file, line, signature?, parent?
class SymbolChange(BaseModel):  # symbol, kind: ChangeKind, old_signature?, new_signature?
class Reference(BaseModel):     # name, file, line, context
class ConflictRegion(BaseModel):# file, ours_range, theirs_range, ours_text, theirs_text
class Finding(BaseModel):       # signal, severity, confidence, title, detail, file?, line?, evidence: dict
class Signal(BaseModel):        # name, status: ok|findings|skipped|error, findings, summary, metadata
class Report(BaseModel):        # base, head, merge_base, signals: list[Signal], risk_score?, generated_at, version
class AnalysisContext(BaseModel):  # repo_path, base, head, merge_base, base_diff, head_diff, base_symbols, head_symbols
```

## 4. Requirements

**Functional**
- FR-1 Simulate merges without mutating working tree/index/refs; report conflicted files + regions.
- FR-2 Structured diffs for base→head and merge-base→each side.
- FR-3 Symbol defs/refs extraction (functions, classes, methods, imports) via tree-sitter.
- FR-4 Detect deleted-still-referenced, renamed-old-name-referenced, signature-change-new-callers; confidence attached.
- FR-5 Cross-PR collision ranking: symbol > hunk > file granularity; `--prs` (GitHub) and `--branches` (local) modes.
- FR-6 Risk score 0–100, weighted and configurable.
- FR-7 CLI: `analyze --base B --head H [--format text|json|md]`, `scan`, `serve`; exit 0 clean / 1 findings over threshold / 2 error.
- FR-8 GitHub: App auth, webhook signature verify, idempotent PR comment, fully optional (offline-capable tool).

**Non-functional**
- NFR-1 Typical PR (<50 files) < 10s excluding clone.  NFR-2 Never mutate user state.
- NFR-3 No unhandled failure on detached HEAD, unborn branch, binaries, unsupported langs, old git.
- NFR-4 Python ≥3.11, pinned deps, `pip install .`.  NFR-5 Tokens via env only.

**Testing**
- Unit: pure functions (diff parse, symbols, overlap math, scoring).
- Integration: repo-builder helper scripts real git repos in tmp dirs → run signals end-to-end.
- Regression: golden corpus — scripted repo + expected report JSON snapshot per scenario
  (clean merge, textual conflict, rename-vs-new-caller, signature change, 3-way PR overlap,
  unsupported language, binary file). Snapshots fail CI on silent behavior change.
- Service: FastAPI test client + recorded webhook fixtures + mocked GitHub API.

---

## 5. Subagent build workflow

Designed for Claude Code: spawn agents with the Task tool; launch all agents within a
phase **in a single message** so they run in parallel. File ownership is disjoint —
agents never edit each other's paths, so no merge conflicts. Every prompt is
self-contained; every agent reads `docs/DESIGN.md` first.

### Execution graph

```
Phase 0 (sequential)   A: Foundation & Contracts
                              │
Phase 1 (parallel)   ┌────────┼────────┐
                     B: Git   C: Analysis  D: Reporting
                     engine   engine       layer
                              │
Phase 2 (parallel)   ┌────────┴────────┐
                     E: Signal engines  F: GitHub + service
                              │
Phase 3 (sequential)   G: Integration, regression corpus, CI, packaging
```

### Ownership map

| Agent | Owns | May NOT touch |
|---|---|---|
| A | pyproject.toml, all `src/mergesignal/**/*.py` stubs, models.py, config.py, cli.py, tests/conftest.py, tests/helpers/repo_builder.py | — |
| B | `git/merge_sim.py`, `git/history.py`, `tests/{unit,integration}/test_{merge_sim,history}*` | anything outside `git/` + its tests |
| C | `analysis/*.py`, `tests/{unit,integration}/test_{diff,symbols,index,languages}*` | anything outside `analysis/` + its tests |
| D | `report/*.py`, `tests/unit/test_{render,github_comment}*` | anything outside `report/` + its tests |
| E | `signals/*.py`, `tests/{unit,integration}/test_{conflicts,semantic,overlap,risk}*` | anything outside `signals/` + its tests |
| F | `github/*.py`, `service/*.py`, `cli.py` serve cmd, `tests/{unit,integration}/test_{github,service}*` | anything outside those paths |
| G | `tests/regression/**`, `.github/workflows/ci.yml`, README touch-up, cross-module fixes | (integration agent may edit anything to wire seams) |

### Agent A — Foundation & Contracts (Phase 0, run alone)

```
You are implementing the foundation of MergeSignal, a Python pre-merge intelligence
tool. Read docs/DESIGN.md fully first — it is the spec.

MISSION: Create the complete project scaffold and contracts that 6 parallel agents
will implement against. Downstream agents code to your signatures — be precise.

DELIVER:
1. pyproject.toml (src layout, Python >=3.11, name "mergesignal", console script
   `mergesignal = mergesignal.cli:app`). Declare ALL deps now so no later agent
   touches this file: typer, rich, pydantic>=2, pyyaml, httpx, tree-sitter,
   tree-sitter-language-pack, fastapi, uvicorn, pyjwt[cryptography];
   dev extras: pytest, pytest-cov, ruff. Pin minimum versions, no `latest`.
2. src/mergesignal/models.py — complete pydantic v2 models per §3 of DESIGN.md.
   Every field typed and docstringed. Add model validators where sensible.
3. src/mergesignal/config.py — load .mergesignal.yaml into a pydantic Config model
   (enabled_signals, severity_threshold, risk_weights, hot_paths, github section).
   Sane defaults; missing file → defaults.
4. src/mergesignal/cli.py — typer app with `analyze`, `scan`, `serve` commands,
   full option signatures (--base, --head, --format, --prs, --branches, --config),
   exit-code contract per FR-7. Bodies may call the (stubbed) signal pipeline.
5. src/mergesignal/git/repo.py — IMPLEMENT FULLY: subprocess git wrapper.
   Methods: run(args)->str, rev_parse, merge_base, diff_names, file_content_at(ref,path),
   list_branches, git_version. Timeout + GitError exception. Never mutates state.
6. Stub files for every remaining module in the §2 tree with COMPLETE function/class
   signatures, docstrings describing behavior + edge cases, body = raise
   NotImplementedError. Signatures must match DESIGN.md; where it is silent, choose
   sensible ones — they become the contract.
7. tests/conftest.py + tests/helpers/repo_builder.py — IMPLEMENT FULLY: a RepoBuilder
   that scripts real git repos in tmp_path (commit files on branches, control
   authors/dates, merge, return repo path). This is shared test infrastructure —
   make it ergonomic: builder.file("a.py", "...").commit("msg").branch("feature")...
8. Wire cli.py so `mergesignal analyze --base X --head Y` runs the signal pipeline
   (stubs raise NotImplementedError — catch per-signal into status="error" Signals
   so the CLI works end-to-end immediately).

ACCEPTANCE: `pip install -e .[dev]` works; `pytest` collects (even if all impl tests
are absent); `mergesignal analyze` on a real repo prints a Report skeleton with
per-signal error status; `ruff check src` clean.
```

### Agent B — Git engine (Phase 1, parallel)

```
You are implementing the git engine of MergeSignal. Read docs/DESIGN.md first.

CONTRACTS: models.py and git/repo.py are DONE — read them, do not modify them.
Stubs at src/mergesignal/git/merge_sim.py and git/history.py define your signatures.
tests/helpers/repo_builder.py exists — use it for integration tests.

OWN ONLY: src/mergesignal/git/merge_sim.py, src/mergesignal/git/history.py,
tests/unit/test_merge_sim.py, tests/unit/test_history.py,
tests/integration/test_merge_sim_int.py. Touch nothing else.

IMPLEMENT:
- merge_sim.py: simulate merge of head into base WITHOUT mutating working
  tree/index/refs. Primary path: `git merge-tree --write-tree <base> <head>`
  (git ≥2.38) → parse conflicted file info + extract ConflictRegions (parse
  conflict markers from the written tree's blobs). Fallback for older git:
  temporary worktree + `git merge --no-commit`, collect `git diff --name-only
  --diff-filter=U`, always clean up the worktree even on failure.
- history.py: churn (commit count per path since N days), co-change frequency
  (pairs of paths committed together), author share per path. All bounded —
  cap `git log` traversal, document limits.

EDGE CASES: unborn branch, missing refs (GitError propagates), binary files in
conflict (region text may be absent — mark, don't crash), filenames with spaces
/unicode, merge already up-to-date (zero conflicts, not an error).

ACCEPTANCE: unit tests for parsing paths; integration tests build real repos via
RepoBuilder: (a) clean merge → no conflicts, (b) same-line edits → correct
ConflictRegions, (c) git fallback path exercised if git <2.38 logic permits —
otherwise mock. `pytest tests/unit/test_merge_sim.py tests/unit/test_history.py
tests/integration/test_merge_sim_int.py -x` green.
```

### Agent C — Analysis engine (Phase 1, parallel)

```
You are implementing the code-analysis engine of MergeSignal. Read docs/DESIGN.md.

CONTRACTS: models.py is DONE — read it, do not modify. Stubs in
src/mergesignal/analysis/{diff,languages,symbols,index}.py define your signatures.
RepoBuilder is available for tests.

OWN ONLY: src/mergesignal/analysis/*.py and
tests/{unit,integration}/test_{diff,languages,symbols,index}*.py.

IMPLEMENT:
- diff.py: parse `git diff` output into DiffFile/Hunk models — added/removed line
  ranges per side, renames (old_path), binary detection, new/deleted files.
- languages.py: extension → tree-sitter language registry via
  tree-sitter-language-pack. Unknown extension → None (textual fallback path).
  Cover at minimum: .py .js .ts .tsx .jsx .go .rs .java.
- symbols.py: per file content + language, extract Symbol defs (function, class,
  method, variable, import — with signatures where the grammar gives them) and
  References (name usages with file/line). Use tree-sitter queries per language;
  shared traversal helpers. Robust to syntax errors — extract what's parseable.
- index.py: SymbolIndex over a git ref — for each changed file (and optionally
  whole-tree shallow scan), maps name → defs and name → refs. Diff-aware: produce
  SymbolChange lists comparing two refs (removed, added, renamed-suspect,
  signature_changed).

EDGE CASES: binary files (skip symbols), huge files (cap parse size, document),
unsupported languages (empty symbols, language=None), files deleted on one side,
partial syntax errors mid-file.

ACCEPTANCE: unit tests with inline source snippets per language asserting exact
Symbol/Reference extraction; integration tests diff two repo-builder refs and
assert SymbolChanges (rename detection, signature change detection).
`pytest tests -k "diff or symbols or index or languages" -x` green.
```

### Agent D — Reporting layer (Phase 1, parallel)

```
You are implementing MergeSignal's reporting layer. Read docs/DESIGN.md.

CONTRACTS: models.py is DONE — read it, do not modify. Stubs in
src/mergesignal/report/{render,github_comment}.py define your signatures.

OWN ONLY: src/mergesignal/report/*.py and
tests/unit/test_{render,github_comment}*.py.

IMPLEMENT:
- render.py: Report → three renderers. `text`: rich console output, signals grouped
  with severity-colored findings, summary footer. `json`: model_dump_json, stable
  key order. `markdown`: headings per signal, tables of findings, collapsible
  <details> for verbose evidence.
- github_comment.py: Report → GitHub PR comment body: summary table of 4 signal
  statuses, per-signal <details> sections, risk score badge (shields-style or
  emoji-free text), hidden marker comment `<!-- mergesignal:v1 -->` so the service
  can find and UPDATE its previous comment instead of spamming. Include a
  `find_existing_comment(comments)` helper that locates the marker.

ACCEPTANCE: fixture Report objects → snapshot assertions for all three formats;
marker round-trip test (render → detect). Deterministic output (sort findings by
severity then file). `pytest tests/unit/test_render.py
tests/unit/test_github_comment.py -x` green.
```

### Agent E — Signal engines (Phase 2, parallel — after B & C)

```
You are implementing MergeSignal's four signal engines — the product's core.
Read docs/DESIGN.md first.

CONTRACTS: models.py, git/*, analysis/* are DONE — read them, do not modify.
Stubs in src/mergesignal/signals/{conflicts,semantic,overlap,risk}.py define
signatures. Each exposes `analyze(ctx: AnalysisContext) -> Signal` and never
raises — errors become Signal(status="error").

OWN ONLY: src/mergesignal/signals/*.py and
tests/{unit,integration}/test_{conflicts,semantic,overlap,risk}*.py.

IMPLEMENT:
- conflicts.py (S1): wrap merge_sim → Signal with a Finding per ConflictRegion,
  severity high; summary counts files/regions.
- semantic.py (S2): the heart. Compare SymbolChanges on side A vs References
  added on side B (and vice versa): removed-symbol-still-referenced (high conf
  same-file, medium cross-file), renamed-old-name-referenced,
  signature_changed-with-new-callers. Finding evidence carries both sides'
  locations. Never fabricate — only report what the index supports.
- overlap.py (S3): given candidate diff + list of other branch/PR diffs, rank
  collisions: symbol overlap > hunk-range overlap > file overlap. Findings
  per colliding branch with granularity + locations.
- risk.py (S4): 0–100 weighted score from config.risk_weights: churn,
  co-change coupling, hot-path glob match (config.hot_paths), test-file proxy
  (changed file has sibling/mirrored test path), diff size. Finding per
  contributing factor so the score is explainable.

EDGE CASES: signal inputs missing (e.g. unsupported language → status="skipped"
with reason), empty diffs, N=0 other branches for overlap.

ACCEPTANCE: per-signal unit tests + integration tests via RepoBuilder covering
DESIGN.md §4 regression scenarios relevant to each signal.
`pytest tests -k "conflicts or semantic or overlap or risk" -x` green.
```

### Agent F — GitHub integration & service (Phase 2, parallel)

```
You are implementing MergeSignal's GitHub layer and webhook service.
Read docs/DESIGN.md.

CONTRACTS: models.py, cli.py, report/* are DONE — read them, do not modify.
Stubs in src/mergesignal/github/{client,app}.py and
src/mergesignal/service/{server,worker}.py define signatures. The signal pipeline
entry point is in cli.py — reuse it, don't reimplement.

OWN ONLY: src/mergesignal/github/*.py, src/mergesignal/service/*.py, the `serve`
command body in cli.py, and tests/{unit,integration}/test_{github,service}*.py.

IMPLEMENT:
- github/app.py: GitHub App auth — JWT from app id + PEM (env vars), exchange for
  installation token, cache until expiry. verify_webhook(payload, signature, secret)
  with hmac.compare_digest.
- github/client.py: httpx REST client — list open PRs, get PR head/base refs,
  list PR comments, create/update comment (uses github_comment.find_existing_comment
  for idempotency), post check-run optional. Token via env GITHUB_TOKEN or App auth.
- service/server.py: FastAPI app — POST /webhook receives pull_request events
  (opened/synchronize/reopened), verifies signature, enqueues analysis.
  GET /healthz. Config from env: MERGESIGNAL_APP_ID, _PRIVATE_KEY, _WEBHOOK_SECRET.
- service/worker.py: pipeline — shallow-clone/fetch the PR refs to a temp dir,
  run the signal pipeline, render github_comment, post/update PR comment.
  Isolated temp dirs, cleanup always, per-run timeout.

ACCEPTANCE: webhook signature verify unit tests (valid/invalid/tampered);
FastAPI TestClient test with recorded pull_request webhook fixture and mocked
GitHub API (httpx MockTransport) asserting the comment upsert path; worker test
against a RepoBuilder repo. `pytest tests -k "github or service" -x` green.
```

### Agent G — Integration & regression corpus (Phase 3, run alone)

```
You are the integration agent for MergeSignal — all subsystem agents have finished.
Read docs/DESIGN.md first.

MISSION: wire the seams, build the golden regression corpus, add CI, package.

DELIVER:
1. Run the FULL suite: `pytest -x`. Fix cross-boundary mismatches (you may edit
   any file to integrate — keep the models.py contract stable unless a change is
   clearly a bug, in which case update all call sites).
2. tests/regression/: golden corpus. For each scenario, a RepoBuilder script +
   expected Report JSON snapshot + a test asserting equality (normalize
   timestamps/paths). Scenarios: clean merge; textual conflict (2 regions);
   rename-vs-new-caller semantic break; signature-change-with-new-callers;
   candidate vs 3 overlapping branches; unsupported language fallback;
   binary file in diff; empty diff (base==head).
3. End-to-end CLI test: `mergesignal analyze` on a scripted repo → parse stdout
   JSON → assert signal statuses; exit-code contract (0/1/2) verified.
4. .github/workflows/ci.yml: pytest + ruff on Python 3.11/3.12, git ≥2.38 noted.
5. README.md: install, quickstart, config example, exit codes — concise.
6. `pip install -e .` fresh-env smoke: `mergesignal --help`, `analyze` on this
   repo itself.

ACCEPTANCE: entire suite green; regression snapshots committed; CI file valid;
`mergesignal analyze` produces a real Report on a non-trivial repo.
```

## 6. Orchestration notes (for Claude Code)

- Spawn each agent with the Task tool using the prompt blocks above verbatim —
  prepend nothing, they are self-contained.
- Within a phase, launch agents in ONE message (parallel). Across phases, wait.
- Same checkout is fine — ownership is disjoint by design. No worktrees needed.
- If a phase-1 agent finishes early, still wait for its siblings before Phase 2 —
  E depends on B+C; F depends on D.
- After G, review `git diff` per owned-path to audit each agent's footprint.
