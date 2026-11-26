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


@pytest.mark.parametrize("script", SCRIPTS)
def test_shebang_resolves_bash_from_path(script):
    assert (ROOT / script).read_text().splitlines()[0] == "#!/usr/bin/env bash"


@pytest.mark.skipif(shutil.which("dash") is None, reason="needs a non-bash /bin/sh (dash)")
@pytest.mark.parametrize("script", SCRIPTS)
def test_old_or_non_bash_shell_is_refused_before_any_work(script, tmp_path):
    """Run under dash (no BASH_VERSINFO), the same branch bash 3.2 takes: exit 1, clear message,
    and nothing else ran (cwd stays clean, no .venv created, no docker)."""
    proc = subprocess.run(["dash", str(ROOT / script)], cwd=tmp_path, capture_output=True,
                          text=True, timeout=30, check=False)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "bash >= 5" in proc.stderr and "brew install bash" in proc.stderr
    assert proc.stdout == ""
    assert list(tmp_path.iterdir()) == []
