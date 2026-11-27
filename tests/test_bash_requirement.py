"""Every shell script targets bash >= 5 (Homebrew bash on macOS) through `#!/usr/bin/env bash`,
and refuses to run under an older shell before doing anything."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = sorted(
    subprocess.run(["git", "ls-files", "*.sh"], cwd=ROOT, capture_output=True, text=True,
                   check=True).stdout.split()
)


def test_scripts_are_found():
    assert "create_audit_user.sh" in SCRIPTS and "local-test/run.sh" in SCRIPTS


def test_scripts_are_tracked_executable():
    """Git mode, not the working-tree bit: a 100644 script checked out on a runner or a fresh clone
    fails with 'Permission denied' when run as ./script.sh."""
    out = subprocess.run(["git", "ls-files", "-s", "--", "*.sh"], cwd=ROOT, capture_output=True,
                         text=True, check=True).stdout
    not_exec = [line.split("\t", 1)[1] for line in out.splitlines() if not line.startswith("100755 ")]
    assert not_exec == [], f"tracked without the exec bit (git mode != 100755): {not_exec}"


@pytest.mark.parametrize("script", SCRIPTS)
def test_shebang_resolves_bash_from_path(script):
    assert (ROOT / script).read_text().splitlines()[0] == "#!/usr/bin/env bash"


def _old_shells():
    """Real shells the guard must refuse on this host: dash (no BASH_VERSION, Linux) and the system
    /bin/bash when it is older than 5 (3.2 on macOS)."""
    shells = []
    dash = shutil.which("dash")
    if dash:
        shells.append(dash)
    if Path("/bin/bash").exists():
        major = subprocess.run(["/bin/bash", "-c", "echo ${BASH_VERSINFO[0]}"], capture_output=True,
                               text=True, check=False).stdout.strip()
        if major.isdigit() and int(major) < 5:
            shells.append("/bin/bash")
    return shells


OLD_SHELLS = _old_shells()


def test_an_old_shell_is_available():
    """Otherwise the refusal test below silently collects nothing."""
    assert OLD_SHELLS, "no old shell found (need dash, or a /bin/bash older than 5)"


@pytest.mark.parametrize("shell", OLD_SHELLS)
@pytest.mark.parametrize("script", SCRIPTS)
def test_old_or_non_bash_shell_is_refused_before_any_work(script, shell, tmp_path):
    """Exit 1 with a clear message, and nothing else ran (cwd stays clean, no .venv, no docker)."""
    proc = subprocess.run([shell, str(ROOT / script)], cwd=tmp_path, capture_output=True,
                          text=True, timeout=30, check=False)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "bash >= 5" in proc.stderr and "brew install bash" in proc.stderr
    assert proc.stdout == ""
    assert list(tmp_path.iterdir()) == []
