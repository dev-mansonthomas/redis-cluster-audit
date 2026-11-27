# CI — GitHub Actions (tests, ruff, shellcheck)

**Status:** implemented (tasks 1–6); live checks pending (AC1–AC4, AC6 macOS half, AC10) · **Date:** 2026-11-26 (rev. after #9) ·
**Branch:** `chore/ci-and-housekeeping`

## Purpose
The repo has no CI. `git-pr-merge` reports `ci: none`, so the only gate before a squash-merge into
`main` is a local run inside the VM. Add one GitHub Actions workflow that runs pytest, ruff and
shellcheck on every PR and every push to `main`. No Redis server and no Docker are needed. Once
checks exist, `git-pr-merge` waits for them and refuses to merge on red (`ci: failed`).

Since #9 the project **requires bash ≥ 5** (Homebrew on macOS): the scripts use `#!/usr/bin/env bash`
plus a POSIX guard that refuses older shells. The macOS job reproduces the user's setup:
`/bin/bash` 3.2 is present, Homebrew bash 5 is installed and first on `PATH`. It proves both halves.
`env bash` resolves to bash 5, so the suite passes. The real `/bin/bash` 3.2 hits the guard and is
refused.

## Facts verified 2026-11-26 (re-check at implementation time)
| Item | Value | Source |
|---|---|---|
| `ubuntu-latest` | Ubuntu 26.04: bash 5.3.9, dash, **shellcheck 0.11.0** preinstalled | actions/runner-images README + Ubuntu2604-Readme |
| `macos-latest` | macOS 26 arm64: **`/bin/bash` 3.2.57**, Homebrew 7.0.14, **no bash 5** | macos-26-arm64-Readme |
| `actions/checkout` | v7.0.1 → `3d3c42e5aac5ba805825da76410c181273ba90b1` | GitHub API |
| `actions/setup-python` | v7.1.0 → `6d4fd22b20465ccc0f7354476588e8df7c9ca0e0`; `cache: pip` + multi-line `cache-dependency-path` | GitHub API, Context7 `/actions/setup-python/v7.1.0` |
| ruff | **0.17.4** (2026-11-05). `ruff@0.17.4 check .` → all checks passed on `main`. 0.17.6 is newer but < 2 weeks old. | GitHub API + local run |
| PyYAML | **6.0.3** (2025-09). 6.0.4 is 6 days old, so not used yet. | PyPI |

## User stories / acceptance criteria
- As the maintainer, I see a red/green check on every PR before `git-pr-merge` merges it, so that a
  regression (like the `set -e` abort fixed in #5) cannot reach `main` unnoticed.
- As a Mac user, I get either a working script (bash 5 from Homebrew) or a clear refusal
  (stock bash 3.2), never a half-run.

- [ ] **AC1**: Given a PR to `main`, when it is opened or updated, then workflow `CI` runs the jobs
  `test (ubuntu-latest, 3.10)`, `test (ubuntu-latest, 3.14)`, `test (macos-latest, 3.14)` and `lint`.
  Each reports a status on the PR.
- [ ] **AC2**: Given a push to `main` (including `git-pr-merge`'s squash-merge), then the same workflow runs.
- [ ] **AC3**: Given a failing pytest test, then the corresponding `test` job fails.
- [ ] **AC4**: Given a shellcheck warning or a ruff finding, then `lint` fails, and the log names the file and line.
- [x] **AC5**: Given a tracked `*.sh` committed with git mode `100644`, then pytest fails (locally
  and in every `test` job), and the failure names the file.
- [ ] **AC6**: In the macOS job, the log shows `bash --version` = 5.x (Homebrew) and
  `/bin/bash --version` = 3.2.x. The bash-guard test runs **under `/bin/bash` 3.2** there, and under
  `dash` on Linux. The old-shell refusal is proven on real shells.
- [x] **AC7**: With `CI_FORBID_SKIPS=1` (set in the workflow), any skipped test makes pytest exit
  non-zero. Without it, local runs keep the normal skip behaviour.
- [x] **AC8**: A structural test asserts the workflow's contract: triggers, `permissions: contents: read`,
  every `uses:` pinned to a 40-hex SHA, `persist-credentials: false` on checkout, `timeout-minutes`
  on every job, no `pull_request_target`, no `secrets.` reference, the expected matrix, and
  `concurrency.cancel-in-progress: true`.
- [x] **AC9**: The dev tools are pinned in `requirements-dev.txt` (`ruff==0.17.4`, `pyyaml==6.0.3`),
  so local and CI run the same versions.
- [ ] **AC10**: Given `git-pr-merge` on a branch with a failing check, then
  `debug/git/git-pr-merge.json` shows `"ci": "failed"` and `"merged": false`. On a green branch it
  lists the checks and shows `"merged": true`.

## Inputs & outputs
**Create**
- `.github/workflows/ci.yml`. Target shape:

```yaml
name: CI
on:
  pull_request:
    branches: [main]
  push:
    branches: [main]
  workflow_dispatch:
permissions:
  contents: read
concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true
jobs:
  test:
    name: test (${{ matrix.os }}, ${{ matrix.python }})
    runs-on: ${{ matrix.os }}
    timeout-minutes: 10
    strategy:
      fail-fast: false
      matrix:
        include:
          - { os: ubuntu-latest, python: "3.10" }   # requires-python floor
          - { os: ubuntu-latest, python: "3.14" }   # latest stable
          - { os: macos-latest,  python: "3.14" }   # Homebrew bash 5 + stock /bin/bash 3.2
    env:
      CI_FORBID_SKIPS: "1"
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1   # v7.0.1
        with: { persist-credentials: false }
      - uses: actions/setup-python@6d4fd22b20465ccc0f7354476588e8df7c9ca0e0   # v7.1.0
        with:
          python-version: ${{ matrix.python }}
          cache: pip
          cache-dependency-path: |
            requirements.txt
            requirements-dev.txt
      - name: bash 5 via Homebrew (project prerequisite on macOS)
        if: runner.os == 'macOS'
        run: brew install bash
      - name: Shell versions
        run: |
          bash --version | head -1
          /bin/bash --version | head -1
      - run: python -m pip install -r requirements-dev.txt
      - run: python -m pytest -rs
  lint:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1   # v7.0.1
        with: { persist-credentials: false }
      - uses: actions/setup-python@6d4fd22b20465ccc0f7354476588e8df7c9ca0e0   # v7.1.0
        with: { python-version: "3.14", cache: pip, cache-dependency-path: requirements-dev.txt }
      - run: python -m pip install "$(grep -E '^ruff==' requirements-dev.txt)"
      - run: ruff check --output-format=github .
      - run: shellcheck --version && git ls-files -z -- '*.sh' | xargs -0 shellcheck
```

- `tests/test_ci_workflow.py`: the structural contract (AC8, AC9).

**Modify**
- `requirements-dev.txt`: add `ruff==0.17.4` and `pyyaml==6.0.3`.
- `pyproject.toml`: add `[tool.ruff] target-version = "py310"` (matches `requires-python`). The
  rule set stays ruff's default. Determinism comes from the version pin.
- `tests/test_bash_requirement.py`:
  - add an exec-bit test (AC5) that reuses `SCRIPTS`;
  - generalise the old-shell test to the real old shells found on the host (`dash`, and
    `/bin/bash` when its major version is < 5) (AC6).
- `tests/conftest.py`: the `CI_FORBID_SKIPS` hook (AC7).
- `README.md`: CI badge + a short "Continuous integration" section, with the optional host-side
  "required status checks" steps.

No Redis keys, services or containers are touched.

## Behavior & edge cases
- **Happy path:** 4 green checks, with stable job names (they can later become required checks).
- **Skips:**
  - Linux: dash present, `/bin/bash` is 5, so the old-shell set = {dash}.
  - macOS: no dash, `/bin/bash` is 3.2, so the set = {/bin/bash}.
  - Either way there are 0 skips, and `CI_FORBID_SKIPS` turns any future skip into a red build.
- **Homebrew bash on `PATH`:** `/opt/homebrew/bin` comes before `/bin` on the runner. The
  `Shell versions` step makes a wrong resolution visible, and the env-shebang tests would then
  fail (the guard refuses 3.2), so a misconfiguration cannot pass silently.
- **Env leakage:** the tests already strip `REDIS_*` / `REDISCLI_AUTH`. The workflow sets none of them.
- **`local-test/run.sh` tests** stop at the stub `$PYTHON` before Docker (a fake `docker` is on PATH).
- **Fork PRs:** the trigger is `pull_request` (never `pull_request_target`), with a read-only token and no secrets.
- **Docs-only PRs:** CI still runs. No `paths-ignore`, so a required check can never be left pending.
- **`ubuntu-latest` / `macos-latest` image moves:** if a move breaks something, pin `ubuntu-26.04` / `macos-26`.

## Out of scope
- Docker e2e (`local-test/run.sh`): stays local only (decision of 2026-11-24).
- Branch protection: a host-side GitHub setting, documented in the README, not automated.
- Dependabot / Renovate for actions and pins, CodeQL, coverage, releases: possible follow-ups.
- mypy (no type-checking setup in the project).

## Test plan
- **Unit/structure (local + CI):**
  - `tests/test_ci_workflow.py` (AC8, AC9);
  - `tests/test_bash_requirement.py` (AC5, AC6);
  - a pytester-style test of the `CI_FORBID_SKIPS` hook (AC7).
- **Local gate:** `.venv-vm/bin/python -m pytest -rs`, `ruff check .`, `shellcheck` on the tracked scripts.
- **Live (after the first push):** AC1, AC2, AC6 from the PR checks and the logs.
- **Negative:** AC3, AC4, AC10 on a throwaway branch that is never merged.

## Dependencies & risks
- **Riskiest:** the macOS job, for two reasons. First, Homebrew bash must win on `PATH`. Second, the
  env-shebang tests then exercise real macOS userland (BSD `sed`/`awk`, `mktemp`, and so on) for the
  first time in an automated run. A first red run there is likely and useful. Fix the script, don't
  drop the job.
- No `actionlint` in the VM. The structural pytest covers the contract, and GitHub itself validates
  the syntax on the first push. If you want actionlint locally, add it to the kit's
  `scripts/vm-provision.sh` (it must not be hand-installed).
- Python 3.10 reached end of life in 2026-10. It stays while `requires-python = ">=3.10"`; raising
  the floor is a separate decision.
- Public repo: Actions minutes are free, macOS included.

---

## Implementation plan (test-first)

Run all tests with: `.venv-vm/bin/python -m pip install -r requirements-dev.txt` (once the pins are
added: `uv pip install --python .venv-vm/bin/python -r requirements-dev.txt`), then
`.venv-vm/bin/python -m pytest -rs`.

| # | Red: failing test first | Green: minimal change | Refactor |
|---|---|---|---|
| 1 | `tests/test_bash_requirement.py::test_scripts_are_tracked_executable`: every entry of `git ls-files -s -- '*.sh'` has mode `100755`; the message names the offenders. **Prove red** with a temporary `git update-index --chmod=-x run_audit.sh`, then revert it. | None needed: the tree is already 755. The test is the gate. | — |
| 2 | Generalise `test_old_or_non_bash_shell_is_refused_before_any_work` → parametrised over `OLD_SHELLS` = `dash` if found, plus `/bin/bash` if its `BASH_VERSINFO[0]` < 5. Add `test_an_old_shell_is_available` (fails if `OLD_SHELLS` is empty). Prove red by temporarily forcing an empty list. | A small helper `_old_shells()` in the same test file. | Drop the old single-shell skip. |
| 3 | `tests/test_ci_skip_guard.py`: runs a tiny temp test module (one skipped test) in a subprocess, using the `-p pytester` / `runpytest_subprocess` pattern. With `CI_FORBID_SKIPS=1` the exit is non-zero and the summary names the skip; without it the exit is 0. | `tests/conftest.py`: `pytest_sessionfinish(session, exitstatus)` reads the skipped count from the terminal reporter's stats; if `CI_FORBID_SKIPS == "1"` and skipped > 0, set `session.exitstatus = 1` and print the skip list. | — |
| 4 | `tests/test_ci_workflow.py::test_dev_tools_are_pinned`: `requirements-dev.txt` contains `ruff==0.17.4` and `pyyaml==6.0.3` (exact pins). | Add both lines. Install into `.venv-vm`. Add `[tool.ruff] target-version = "py310"` to `pyproject.toml`. Check `ruff check .` (0.17.4) stays green. | — |
| 5 | `tests/test_ci_workflow.py` contract tests on `.github/workflows/ci.yml` (`yaml.safe_load`; note that YAML parses `on:` as the key `True`): triggers `pull_request`/`push` on `main` + `workflow_dispatch`; `permissions == {"contents": "read"}`; every `uses:` matches `^[\w.-]+/[\w.-]+@[0-9a-f]{40}$`; every checkout has `persist-credentials: false`; every job has `timeout-minutes`; matrix == the 3 expected `(os, python)` pairs; `CI_FORBID_SKIPS == "1"` on `test`; the `brew install bash` step is gated on macOS; `concurrency.cancel-in-progress is True`; no `pull_request_target`; the raw text has no `secrets.`; the lint job's ruff version comes from `requirements-dev.txt` (no second pin). **Red:** the file is missing. | Create `.github/workflows/ci.yml` as in *Inputs & outputs*. | Keep the expected values as module constants at the top of the test file. |
| 6 | — (docs) | `README.md`: CI badge, a "Continuous integration" section (what runs, how to reproduce locally, optional host-side required checks: Settings → Branches → `main` → require `test (…)` ×3 + `lint`). Update the agent docs: this spec's status, plus the AC checkboxes once they are proven. | — |
| 7 | Local gate: `pytest -rs` (expect the new count, 0 skipped), `CI_FORBID_SKIPS=1 pytest`, `ruff check .`, shellcheck, gitleaks, pip-audit. | Commit; PR body in `debug/git/pr-body.md`. **User (host):** `git-pr-merge --branch chore/ci-and-housekeeping "ci: add GitHub Actions workflow"`. It waits for the first real CI run. | If macOS goes red: read the log, fix the script or test, push again (the PR is reused). |
| 8 | Negative check (AC3/AC4/AC10): a throwaway branch `ci/negative-check` off `main` with 3 commits: a failing assert, an unquoted var in a script, a `chmod -x`. | **User (host):** `git-pr-merge --branch ci/negative-check "test: CI negative check (do not merge)"` → expect `ci: failed`, `merged: false`. Then close the PR and delete the branch. | Tick AC3/AC4/AC10 in this spec. |

**Done =** the 4 jobs are green on the CI PR and `git-pr-merge.json` lists them with `merged: true`.
The macOS log shows bash 5.x and `/bin/bash` 3.2.x. The negative branch gives `ci: failed`.
Every AC box is ticked.
