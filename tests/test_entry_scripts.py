"""
Tests for the shell entry points' argument handling and report reporting.

local-test/run.sh is exercised ONLY up to its argument parsing: PYTHON points at a stub that exits
with a sentinel code, so a valid run stops at the first "$PY" call (pip install) under `set -e` and
never reaches Docker. A fake `docker` on PATH is a second safety net.
"""
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import audit

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(not Path("/bin/bash").exists(), reason="needs /bin/bash")

SENTINEL = 42


def _exe(path, body):
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def stub_env(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    marker = tmp_path / "docker-called"
    _exe(bindir / "docker", f"#!/bin/sh\ntouch {marker}\nexit 1\n")
    py = _exe(tmp_path / "py-stub", f"#!/bin/sh\nexit {SENTINEL}\n")
    env = dict(os.environ, PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}", PYTHON=str(py))
    return env, marker


def _run_fixture(env, *args):
    return subprocess.run(
        [str(ROOT / "local-test" / "run.sh"), *args],
        capture_output=True, text=True, env=env, timeout=30, check=False,
    )


@pytest.mark.parametrize("bad", ["0", "007", "00", "abc", "1x", "-5"])
def test_fixture_rejects_bad_hotkeys_seconds(stub_env, bad):
    env, marker = stub_env
    proc = _run_fixture(env, "--hotkeys", bad)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "positive integer" in proc.stderr
    assert not marker.exists(), "must refuse before touching Docker"


@pytest.mark.parametrize("args", [("--hotkeys", "10"), ("--hotkeys",)])
def test_fixture_accepts_valid_hotkeys_seconds(stub_env, args):
    env, marker = stub_env
    proc = _run_fixture(env, *args)
    # Parsing passed: the run proceeded to the first "$PY" call (the stub) and stopped there.
    assert proc.returncode == SENTINEL, proc.stdout + proc.stderr
    assert "Mode: hotkeys" in proc.stdout
    assert not marker.exists()


# ── run_audit.sh: the shell must not invent a "Report:" line ──────────────────

def _run_entry(tmp_path, audit_py_body, *args):
    proj = tmp_path / "proj"
    proj.mkdir()
    shutil.copy(ROOT / "run_audit.sh", proj / "run_audit.sh")
    (proj / ".env").write_text("REDIS_HOST_1=127.0.0.1\nREDIS_USERNAME=u\nREDIS_PASSWORD=p\n")
    (proj / "audit.py").write_text(audit_py_body)
    env = dict(os.environ, PYTHON=sys.executable)
    return subprocess.run([str(proj / "run_audit.sh"), *args],
                          capture_output=True, text=True, env=env, cwd=proj, timeout=30, check=False)


@pytest.mark.parametrize("args", [(), ("--hotkeys", "5"), ("--some-new-info-mode",)])
def test_run_audit_does_not_claim_a_report_audit_py_did_not_write(tmp_path, args):
    """An audit.py that exits 0 without writing a report (new info-only mode, early return) must not
    get a "Report: …" line advertised by the shell."""
    proc = _run_entry(tmp_path, 'print("nothing written")\n', *args)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Report" not in proc.stdout


# ── audit.py: it alone announces the report, with an absolute path ────────────

def test_print_acl_grants_mentions_no_report(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["audit.py", "--print-acl-grants"])
    audit.main()
    assert "Report" not in capsys.readouterr().out


def test_successful_run_prints_absolute_report_path(tmp_path, monkeypatch, capsys):
    class _Node:
        host, port = "127.0.0.1", 7001

    class _RC:
        def get_primaries(self):
            return [_Node()]

    stubs = {
        "connect_cluster": lambda: _RC(),
        "check_permissions": lambda rc: None,
        "node_direct_connection": lambda n: object(),
        "collect_info": lambda r: {}, "collect_config": lambda r: {},
        "collect_slowlog": lambda r: [], "collect_client_list": lambda r: [],
        "collect_memory_doctor": lambda r: "", "collect_latency": lambda r: [],
        "collect_acl_data": lambda r: {},
        "analyse_connections": lambda cl: {"total": 0},
        "scan_keyspace": lambda conns: {"total_scanned": 0, "big_keys": []},
        "compute_stats_summary": lambda nd: {"hit_ratio": None},
        "analyse_security": lambda nd: {"findings": []},
        "generate_recommendations": lambda *a: [],
        "analyse_hotkeys": lambda nd: [],
        "build_html_report": lambda *a: "<html></html>",
    }
    for name, fn in stubs.items():
        monkeypatch.setattr(audit, name, fn)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["audit.py"])

    audit.main()

    out = capsys.readouterr().out
    expected = os.path.join(str(tmp_path), audit.OUTPUT_FILE)
    assert (tmp_path / audit.OUTPUT_FILE).exists()
    assert f"Report written to: {expected}" in out
