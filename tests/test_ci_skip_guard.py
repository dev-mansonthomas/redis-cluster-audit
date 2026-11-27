"""CI_FORBID_SKIPS=1 turns any skipped test into a failing run (CI must not go green on skips)."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

CONFTEST = Path(__file__).resolve().parent / "conftest.py"


def _run(tmp_path, env_value):
    shutil.copy(CONFTEST, tmp_path / "conftest.py")
    (tmp_path / "test_sample.py").write_text(
        "import pytest\n\n"
        "def test_ok():\n    pass\n\n"
        "@pytest.mark.skip(reason='needs a missing tool')\n"
        "def test_skipped():\n    pass\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "CI_FORBID_SKIPS"}
    if env_value is not None:
        env["CI_FORBID_SKIPS"] = env_value
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir", str(tmp_path),
         "-c", os.devnull, str(tmp_path)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60, check=False,
    )


def test_skip_fails_the_run_when_forbidden(tmp_path):
    proc = _run(tmp_path, "1")
    assert proc.returncode != 0, proc.stdout
    assert "CI_FORBID_SKIPS" in proc.stdout and "test_skipped" in proc.stdout


def test_skip_is_allowed_by_default(tmp_path):
    proc = _run(tmp_path, None)
    assert proc.returncode == 0, proc.stdout
    assert "1 skipped" in proc.stdout
