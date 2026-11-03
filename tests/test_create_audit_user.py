"""
Integration tests for create_audit_user.sh.

These run the REAL script (through its own #!/bin/bash shebang) in a throwaway directory, against a
fake `redis-cli` that can refuse connections or return error replies. No Redis server is needed.

The point of the suite: provisioning must be resilient to ONE bad node. The script runs under
`set -e`, and an unreachable node must be recorded as FAILED while the remaining nodes are still
provisioned — not abort the whole run mid-line.
"""
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "create_audit_user.sh"

pytestmark = pytest.mark.skipif(not Path("/bin/bash").exists(), reason="needs /bin/bash")

CLUSTER_NODES = (
    "id1 127.0.0.1:7001@17001 myself,master - 0 0 1 connected 0-5460\n"
    "id2 127.0.0.1:7002@17002 master - 0 0 2 connected 5461-10922\n"
    "id3 127.0.0.1:7003@17003 master - 0 0 3 connected 10923-16383\n"
)

# A stand-in for redis-cli: logs its argv, answers CLUSTER NODES, and can refuse a connection
# (exit 1, like a real "Connection refused") or reply with a Redis error at exit 0 (older clients).
FAKE_REDIS_CLI = """#!__PY__
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps(argv) + "\\n")
port = argv[argv.index("-p") + 1] if "-p" in argv else ""
if "CLUSTER" in argv and "NODES" in argv:
    sys.stdout.write(os.environ.get("FAKE_CLUSTER_NODES", ""))
    sys.exit(0)
if "SETUSER" in argv:
    if port in os.environ.get("FAKE_REFUSE", "").split(","):
        sys.stderr.write("Could not connect to Redis at 127.0.0.1:%s: Connection refused\\n" % port)
        sys.exit(1)
    if port in os.environ.get("FAKE_ERRREPLY", "").split(","):
        print("ERR User audit_ro: Syntax error")
        sys.exit(0)
print("OK")
"""

FAKE_AUDIT_PY = 'print("~* &* nocommands +INFO +PING")\n'

# Prompts in order: audit user, password, confirm, admin user, admin password, grant HOTKEYS?
PROMPTS = "audit_ro\n{pw}\n{pw}\n\n\nN\n"


def run_script(tmp_path, password="pw", refuse="", errreply=""):
    proj = tmp_path / "proj"
    bindir = tmp_path / "bin"
    proj.mkdir()
    bindir.mkdir()
    shutil.copy(SCRIPT, proj / "create_audit_user.sh")
    (proj / ".env").write_text("REDIS_HOST_1=127.0.0.1\nREDIS_PORT_1=7001\n")
    (proj / "audit.py").write_text(FAKE_AUDIT_PY)
    cli = bindir / "redis-cli"
    cli.write_text(FAKE_REDIS_CLI.replace("__PY__", sys.executable))
    cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "calls.log"

    env = dict(
        os.environ,
        PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}",
        PYTHON=sys.executable,
        FAKE_LOG=str(log),
        FAKE_CLUSTER_NODES=CLUSTER_NODES,
        FAKE_REFUSE=refuse,
        FAKE_ERRREPLY=errreply,
    )
    # Execute through the script's own shebang (macOS ships bash 3.2 there), like a user would.
    proc = subprocess.run(
        [str(proj / "create_audit_user.sh")],
        input=PROMPTS.format(pw=password),
        capture_output=True, text=True, env=env, cwd=proj, timeout=60,
    )
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    setuser_ports = [c[c.index("-p") + 1] for c in calls if "SETUSER" in c]
    return proc, calls, setuser_ports


def test_all_nodes_ok(tmp_path):
    proc, _, ports = run_script(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert ports == ["7001", "7002", "7003"]
    assert "Done. Now set in .env" in proc.stdout


def test_unreachable_node_does_not_abort_the_run(tmp_path):
    """Regression: under `set -e`, a refused connection used to kill the script at the first bad node,
    so the remaining nodes were never provisioned and no FAILED/summary was printed."""
    proc, _, ports = run_script(tmp_path, refuse="7002")
    out = proc.stdout
    assert ports == ["7001", "7002", "7003"], f"stopped early: {ports}\n{out}\n{proc.stderr}"
    assert "127.0.0.1:7002 ... FAILED" in out
    assert "Connection refused" in out, "the real reason should be shown, not swallowed"
    assert "127.0.0.1:7003 ... OK" in out, "nodes after the failure must still be provisioned"
    assert "Some nodes failed" in out
    assert proc.returncode == 1


def test_redis_error_reply_is_reported_as_failure(tmp_path):
    """A Redis error reply at exit 0 (older redis-cli) must not be reported as OK."""
    proc, _, ports = run_script(tmp_path, errreply="7002")
    assert ports == ["7001", "7002", "7003"]
    assert "127.0.0.1:7002 ... FAILED" in proc.stdout
    assert "Syntax error" in proc.stdout
    assert "Some nodes failed" in proc.stdout
    assert proc.returncode == 1


def test_password_with_spaces_stays_one_argument(tmp_path):
    """The password is passed as its own argv token, so spaces never split it."""
    proc, calls, _ = run_script(tmp_path, password="my pass")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    setuser = next(c for c in calls if "SETUSER" in c)
    assert ">my pass" in setuser
    assert "my" not in setuser and "pass" not in setuser
