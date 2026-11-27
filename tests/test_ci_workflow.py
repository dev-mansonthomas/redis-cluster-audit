"""Contract of the CI workflow (.github/workflows/ci.yml) and of the pinned dev tools."""
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
REQ_DEV = ROOT / "requirements-dev.txt"
PINS = {"ruff": "0.17.4", "pyyaml": "6.0.3"}
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
EXPECTED_JOBS = {"test", "lint"}
EXPECTED_MATRIX = {("ubuntu-latest", "3.10"), ("ubuntu-latest", "3.14"), ("macos-latest", "3.14")}
PINNED_ACTION = re.compile(r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$")


def _req_lines():
    return [ln.split("#", 1)[0].strip() for ln in REQ_DEV.read_text().splitlines()]


def test_dev_tools_are_pinned():
    lines = [ln.lower() for ln in _req_lines()]
    for name, version in PINS.items():
        assert f"{name}=={version}" in lines, f"{name} must be pinned to =={version} in requirements-dev.txt"


@pytest.fixture(scope="module")
def wf():
    assert WORKFLOW.is_file(), f"missing {WORKFLOW.relative_to(ROOT)}"
    return yaml.safe_load(WORKFLOW.read_text())


def _steps(wf):
    return [(name, step) for name, job in wf["jobs"].items() for step in job["steps"]]


def test_triggers(wf):
    on = wf.get("on", wf.get(True))  # YAML 1.1 parses the bare key `on` as True
    assert set(on) == {"pull_request", "push", "workflow_dispatch"}
    assert on["pull_request"]["branches"] == ["main"]
    assert on["push"]["branches"] == ["main"]


def test_no_privileged_trigger_and_no_secrets():
    text = WORKFLOW.read_text()
    assert "pull_request_target" not in text
    assert "secrets." not in text


def test_token_is_read_only(wf):
    assert wf["permissions"] == {"contents": "read"}
    for name, job in wf["jobs"].items():
        assert "permissions" not in job, f"job {name} widens the token"


def test_concurrency_cancels_superseded_runs(wf):
    assert wf["concurrency"]["cancel-in-progress"] is True


def test_jobs_and_timeouts(wf):
    assert set(wf["jobs"]) == EXPECTED_JOBS
    for name, job in wf["jobs"].items():
        assert isinstance(job.get("timeout-minutes"), int), f"job {name} has no timeout-minutes"


def test_actions_are_pinned_by_sha(wf):
    uses = [step["uses"] for _, step in _steps(wf) if "uses" in step]
    assert uses
    for ref in uses:
        assert PINNED_ACTION.match(ref), f"not pinned to a full commit SHA: {ref}"


def test_checkout_does_not_persist_credentials(wf):
    checkouts = [step for _, step in _steps(wf) if step.get("uses", "").startswith("actions/checkout@")]
    assert len(checkouts) == len(wf["jobs"])
    for step in checkouts:
        assert step.get("with", {}).get("persist-credentials") is False


def test_test_matrix(wf):
    job = wf["jobs"]["test"]
    assert job["strategy"]["fail-fast"] is False
    pairs = {(e["os"], e["python"]) for e in job["strategy"]["matrix"]["include"]}
    assert pairs == EXPECTED_MATRIX
    assert job["env"]["CI_FORBID_SKIPS"] == "1"
    runs = [step.get("run", "") for step in job["steps"]]
    assert any("pytest" in r for r in runs)


def test_macos_installs_homebrew_bash(wf):
    brew = [step for step in wf["jobs"]["test"]["steps"] if "brew install bash" in step.get("run", "")]
    assert len(brew) == 1 and brew[0].get("if") == "runner.os == 'macOS'"


def test_lint_runs_ruff_from_the_pin_and_shellcheck(wf):
    runs = "\n".join(step.get("run", "") for step in wf["jobs"]["lint"]["steps"])
    assert "ruff check" in runs and "shellcheck" in runs
    assert "requirements-dev.txt" in runs, "ruff version must come from requirements-dev.txt"
    assert not re.search(r"ruff==\d", runs), "second ruff pin in the workflow"
