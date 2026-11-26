# CI — GitHub Actions (tests + shellcheck)

**Status:** spec, not implemented · **Date:** 2026-11-24 · **Branch:** `chore/ci-and-housekeeping`

## Purpose
The repo has no CI. `git-pr-merge` reports `ci: none`, so the only gate before a squash-merge into
`main` is a local test run inside the VM. Add one GitHub Actions workflow that runs the pytest
suite and shellcheck on every PR and every push to `main`. No Redis server and no Docker are needed.
Once checks exist, `git-pr-merge` waits for them and refuses to merge on red (`ci: failed`).
The workflow also runs the suite on macOS: the entry scripts are executed as `./script.sh` through
their `#!/bin/bash` shebang, and on macOS that is Apple's **bash 3.2**, even when a Homebrew bash 5
is first on `PATH`. A Linux runner (bash 5) cannot catch bash-3.2-only breakage.

## User stories / acceptance criteria
- As the maintainer, I can open a PR and see a red/green check before `git-pr-merge` merges it,
  so that a regression (like the `set -e` abort fixed in #5) cannot reach `main` unnoticed.
- As a Mac user of the tool, I can rely on the scripts working with the stock `/bin/bash` 3.2.

- [ ] **AC1**: Given a PR targeting `main`, when it is opened or updated, then the workflow `CI`
  runs the jobs `test (ubuntu-latest, 3.10)`, `test (ubuntu-latest, 3.14)`, `test (macos-latest, 3.14)`
  and `shellcheck`, and each reports a status on the PR.
- [ ] **AC2**: Given a push to `main` (including `git-pr-merge`'s squash-merge), then the same
  workflow runs.
- [ ] **AC3**: Given a commit where any pytest test fails, then the corresponding `test` job fails
  (non-zero exit) and the PR shows a failing check.
- [ ] **AC4**: Given a tracked `*.sh` with a shellcheck warning (default severity), then the
  `shellcheck` job fails, and the log names the file and line.
- [ ] **AC5**: Given a tracked `*.sh` committed without the executable bit (git mode `100644`),
  then the `shellcheck` job fails with a message naming the file. (A regression already seen:
  `sed -i` on the shared mount drops `+x`.)
- [ ] **AC6**: In the `macos-latest` job, the log shows `/bin/bash --version` reporting `3.2.x`,
  proving the shebang-driven tests ran under bash 3.2.
- [ ] **AC7**: A new push to the same PR cancels the in-progress run for that ref.
- [ ] **AC8**: The workflow token is read-only (`permissions: contents: read`). No secrets are
  used or referenced.
- [ ] **AC9**: Given `git-pr-merge` on a branch with a failing check, then its
  `debug/git/git-pr-merge.json` shows `"ci": "failed"` and `"merged": false`. On a green branch it
  shows the checks and `"merged": true`.

## Inputs & outputs
**New file:** `.github/workflows/ci.yml` (the only new file, apart from the README/doc updates).

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
          - { os: macos-latest,  python: "3.14" }   # /bin/bash 3.2
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1   # v7.0.1
        with: { persist-credentials: false }
      - uses: actions/setup-python@6d4fd22b20465ccc0f7354476588e8df7c9ca0e0   # v7.1.0
        with: { python-version: "${{ matrix.python }}", cache: pip, cache-dependency-path: requirements*.txt }
      - run: /bin/bash --version | head -1
      - run: python -m pip install -r requirements-dev.txt
      - run: python -m pytest
  shellcheck:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1   # v7.0.1
        with: { persist-credentials: false }
      - name: Executable bit on tracked scripts
        run: |
          bad="$(git ls-files -s -- '*.sh' | awk '$1 != "100755" {print $4}')"
          if [ -n "$bad" ]; then echo "::error::not executable (git mode != 100755): $bad"; exit 1; fi
      - name: shellcheck
        run: shellcheck --version && git ls-files -z -- '*.sh' | xargs -0 shellcheck
```

- Actions are **pinned by commit SHA**, with the tag in a comment. The SHAs are the latest
  releases, checked 2026-11-24 via the GitHub API. Re-verify them at implementation time.
- `shellcheck` comes preinstalled on `ubuntu-latest`. The job prints its version. Don't install it.
- No Redis keys are touched. No services or containers are started.

## Behavior & edge cases
- **Happy path:** 4 green checks. The job names are stable, so they can later become required
  status checks.
- **Test that needs `/bin/bash`:** `tests/test_create_audit_user.py` and `tests/test_entry_scripts.py`
  are `skipif(not /bin/bash)`. `/bin/bash` exists on both runner OSes, so nothing must be silently
  skipped. The implementer checks that the pytest summary reports **0 skipped** on all 3 jobs, and
  fixes any skip rather than accepting it.
- **Env leakage:** the runner may define unrelated env vars. The tests already strip `REDIS_*` and
  `REDISCLI_AUTH`, and the workflow must not set any of them.
- **`local-test/run.sh` tests:** they stop at the stub `$PYTHON` before Docker (the fake `docker`
  on PATH is the second safety net), so Docker on the runner is never used.
- **Fork PRs:** the trigger is `pull_request` (never `pull_request_target`), and the token is
  read-only with no secrets, so untrusted code gets no privileges.
- **Flaky macOS:** `fail-fast: false`, so a macOS failure doesn't mask the Linux results. A
  macOS-only failure is a real bash-3.2 bug until proven otherwise.
- **Docs-only PRs:** CI still runs (cheap, about 1 min). No `paths-ignore`, so a required check
  can never be left pending.
- **`report.html` / `.venv`:** they are not tracked, so CI never sees them. No artifact upload.

## Out of scope
- **Docker e2e** (`local-test/run.sh`): it stays local only, by decision of 2026-11-24.
- **ruff in CI:** 33 existing findings in `audit.py` must be triaged first (separate task). Once
  that is done, add `ruff check .` as a job, with a project `[tool.ruff]` config.
- **Branch protection / required checks:** a GitHub setting on the host side, made by the user. The
  steps are documented in the README, not automated.
- Release, packaging, coverage upload, Dependabot, CodeQL (possible follow-ups).
- mypy (the project has no type-checking setup).

## Test plan
The workflow is validated by making it fail on purpose and then pass:
1. **Local pre-flight (VM):** `python -m pytest` (76 pass) and `git ls-files -z '*.sh' | xargs -0 shellcheck`.
   Run the exec-bit check snippet locally: empty output on the clean tree, and the file is named
   after `git update-index --chmod=-x run_audit.sh` (reverted afterwards).
   Validate the YAML with `actionlint` if available. If it isn't, ask for it to be added to the kit,
   don't install it. Fallback: `python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/ci.yml'))"`.
2. **First PR run (AC1, AC6, AC8):** ship the workflow via `git-pr-merge`. In the PR checks, confirm
   the 4 jobs, `GNU bash, version 3.2` in the macOS log, and 0 skipped in each pytest summary.
3. **Negative checks (AC3/AC4/AC5/AC9)**, on a throwaway branch, never merged. The user pushes it
   from the host with `git-pr-merge`, which will refuse to merge on red:
   a failing assert → `test` red; an unquoted `$var` in a script → `shellcheck` red; `chmod -x`
   on a script → exec-bit step red. Read `debug/git/git-pr-merge.json`: `ci: failed`, `merged: false`.
   Then close the PR and delete the branch.
4. **AC2 / AC7:** after the merge, the push-to-`main` run appears. Pushing twice quickly to a PR
   shows the first run cancelled.

## Dependencies & risks
- `actions/checkout` v7.0.1, `actions/setup-python` v7.1.0 (pinned SHAs above). No uv in CI:
  `setup-uv` v10.4.0 is only 4 days old, and pip + `requirements-dev.txt` is enough.
- **Riskiest part:** the macOS job. bash 3.2 lacks features that are easy to slip in (`declare -A`,
  `${var,,}`, `mapfile`, `&>>`, negative array indices, and the empty-array `"${a[@]}"` under `set -u`).
  This is the job most likely to go red the first time, and that is the point. Fix the script,
  don't drop the job.
- `ubuntu-latest` / `macos-latest` move to new images over time. If an image update breaks
  something, pin `ubuntu-24.04` / `macos-15`.
- Python 3.10 reaches end of life in 2026-10. Keep it while `requires-python = ">=3.10"`. Raising
  the floor is a separate decision (it would change `pyproject.toml`).
- Public repo: Actions minutes are free, macOS included.
