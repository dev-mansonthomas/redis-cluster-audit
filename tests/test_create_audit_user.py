"""
Integration tests for create_audit_user.sh.

These run the REAL script (through its own #!/bin/bash shebang) in a throwaway directory, against a
fake `redis-cli` that can refuse connections or return error replies. No Redis server is needed.

The point of the suite: provisioning must be resilient to ONE bad node. The script runs under
`set -e`, and an unreachable node must be recorded as FAILED while the remaining nodes are still
provisioned — not abort the whole run mid-line.
"""
import hashlib
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

# A stand-in for redis-cli. It logs one JSON record per call to FAKE_LOG — its argv and whether
# REDISCLI_AUTH was set / equal to FAKE_EXPECT_AUTH (a boolean, never the value) — and logs any
# command read from stdin to a SEPARATE file, FAKE_STDIN_LOG, so tests can assert that no secret ever
# appears in argv. It answers CLUSTER NODES, and can refuse a connection (exit 1, like a real
# "Connection refused") or reply with a Redis error at exit 0 (redis-cli does that for stdin input).
FAKE_REDIS_CLI = """#!__PY__
import json, os, sys
argv = sys.argv[1:]
# Like redis-cli: with no command in argv, the command is read from stdin.
opts_with_value = {"-h", "-p", "--user", "--pass", "-a"}
i, cmd = 0, []
while i < len(argv):
    if argv[i] in opts_with_value: i += 2; continue
    if argv[i].startswith("-"): i += 1; continue
    cmd = argv[i:]; break
stdin_cmd = []
if not cmd:
    data = sys.stdin.read()
    with open(os.environ["FAKE_STDIN_LOG"], "a") as f:
        f.write(json.dumps(data) + "\\n")
    stdin_cmd = data.split()
auth = os.environ.get("REDISCLI_AUTH")
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps({
        "argv": argv,
        "stdin_cmd": stdin_cmd[:2],
        "auth_set": auth is not None,
        "auth_ok": auth == os.environ.get("FAKE_EXPECT_AUTH"),
    }) + "\\n")
words = cmd or stdin_cmd
port = argv[argv.index("-p") + 1] if "-p" in argv else ""
if "CLUSTER" in words and "NODES" in words:
    sys.stdout.write(os.environ.get("FAKE_CLUSTER_NODES", ""))
    sys.exit(0)
if "SETUSER" in words:
    if port in os.environ.get("FAKE_REFUSE", "").split(","):
        sys.stderr.write("Could not connect to Redis at 127.0.0.1:%s: Connection refused\\n" % port)
        sys.exit(1)
    if port in os.environ.get("FAKE_ERRREPLY", "").split(","):
        print("ERR User audit_ro: Syntax error")
        sys.exit(0)
print("OK")
"""

FAKE_AUDIT_PY = 'print("~* &* nocommands +INFO +PING +CLUSTER|INFO")\n'

# Prompts in order: audit user, password, confirm, admin user, admin password, grant HOTKEYS?
PROMPTS = "{user}\n{pw}\n{pw}\n{admin_user}\n{admin_pw}\n{hk}\n"


def run_script(tmp_path, password="pw", refuse="", errreply="", cluster_nodes=CLUSTER_NODES,
               user="audit_ro", admin_user="", admin_pw="", hk="N", extra_env=None):
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
    stdin_log = tmp_path / "stdin.log"

    env = dict(
        os.environ,
        PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}",
        PYTHON=sys.executable,
        FAKE_LOG=str(log),
        FAKE_STDIN_LOG=str(stdin_log),
        FAKE_CLUSTER_NODES=cluster_nodes,
        FAKE_REFUSE=refuse,
        FAKE_ERRREPLY=errreply,
        FAKE_EXPECT_AUTH=admin_pw,
    )
    env.pop("REDISCLI_AUTH", None)
    for k in [k for k in env if k.startswith("REDIS_")]:  # only the test's .env may define targets
        del env[k]
    env.update(extra_env or {})
    # Execute through the script's own shebang (macOS ships bash 3.2 there), like a user would.
    proc = subprocess.run(
        [str(proj / "create_audit_user.sh")],
        input=PROMPTS.format(user=user, pw=password, admin_user=admin_user, admin_pw=admin_pw, hk=hk),
        capture_output=True, text=True, env=env, cwd=proj, timeout=60, check=False,
    )
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    proc.raw_log = log.read_text() if log.exists() else ""
    proc.stdin_cmds = ([json.loads(line) for line in stdin_log.read_text().splitlines()]
                       if stdin_log.exists() else [])
    setuser_ports = [c["argv"][c["argv"].index("-p") + 1] for c in calls
                     if "SETUSER" in c["argv"] or c["stdin_cmd"] == ["ACL", "SETUSER"]]
    # Only the provisioning SETUSER (the +HOTKEYS follow-up also says SETUSER, in argv).
    setuser_ports = [p for i, p in enumerate(setuser_ports)
                     if hk.upper() != "Y" or i % 2 == 0]
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


AUDIT_PW = 'my "pass\' word'      # spaces + both quote kinds
ADMIN_PW = 'adm "x\' y'


def test_no_secret_in_argv_admin_auth_via_env_audit_hash_via_stdin(tmp_path):
    """Neither the audit password, its hash, nor the admin password may ever be a redis-cli argument
    (they would be visible in `ps`). The admin password goes through REDISCLI_AUTH; the audit user's
    password is sent pre-hashed (`#<sha256>`) on stdin."""
    digest = hashlib.sha256(AUDIT_PW.encode()).hexdigest()
    proc, calls, ports = run_script(tmp_path, password=AUDIT_PW, admin_user="admin",
                                    admin_pw=ADMIN_PW, hk="y")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert ports == ["7001", "7002", "7003"]

    for line in proc.raw_log.splitlines():
        for secret in (AUDIT_PW, digest, ADMIN_PW, "my", "adm"):
            assert secret not in json.loads(line)["argv"], line
        assert AUDIT_PW not in line and digest not in line and ADMIN_PW not in line, line
    for c in calls:
        assert "--pass" not in c["argv"] and "-a" not in c["argv"]
        assert c["argv"][c["argv"].index("--user") + 1] == "admin"
        assert c["auth_set"] and c["auth_ok"], f"REDISCLI_AUTH missing/wrong for {c['argv']}"

    setusers = [cmd for cmd in proc.stdin_cmds if cmd.startswith("ACL SETUSER")]
    assert len(setusers) == 3
    for cmd in setusers:
        assert cmd.split()[:4] == ["ACL", "SETUSER", "audit_ro", "on"]
        assert cmd.split()[4] == "#" + digest
        assert ">" not in cmd and AUDIT_PW not in cmd
        assert cmd.split()[5:] == ["~*", "&*", "nocommands", "+INFO", "+PING", "+CLUSTER|INFO"]
    assert AUDIT_PW not in proc.stdout + proc.stderr
    assert digest not in proc.stdout + proc.stderr


def test_blank_admin_password_does_not_leak_an_inherited_redis_cli_auth(tmp_path):
    """Blank admin password means "no auth": an REDISCLI_AUTH exported in the operator's shell must
    not be silently sent to every node."""
    proc, calls, ports = run_script(tmp_path, extra_env={"REDISCLI_AUTH": "inherited"})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert ports == ["7001", "7002", "7003"]
    assert calls and not any(c["auth_set"] for c in calls)


@pytest.mark.parametrize("bad", ["x nopass +@all", "a;b", "bad\"q", "-x"])
def test_unsafe_audit_username_is_rejected(tmp_path, bad):
    """The username is now part of a stdin command line, so whitespace/quotes would inject ACL rules."""
    proc, calls, _ = run_script(tmp_path, user=bad)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "username" in (proc.stdout + proc.stderr).lower()
    assert calls == []


def test_node_flag_filter_matches_whole_tokens(tmp_path):
    """`nofailover` is a healthy node and must be provisioned; only a confirmed `fail` (not the
    merely-suspected PFAIL `fail?`, which is still reachable) is skipped."""
    nodes = (
        "id1 127.0.0.1:7001@17001 myself,master - 0 0 1 connected 0-5460\n"
        "id2 127.0.0.1:7002@17002 master,nofailover - 0 0 2 connected 5461-10922\n"
        "id3 127.0.0.1:7003@17003 master,fail - 0 0 3 connected 10923-16383\n"
        "id4 127.0.0.1:7004@17004 slave,fail? id1 0 0 1 connected\n"
        "id5 :0@0 noaddr,handshake - 0 0 0 disconnected\n"
    )
    proc, _, ports = run_script(tmp_path, cluster_nodes=nodes)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert ports == ["7001", "7002", "7004"]


def test_standalone_server_falls_back_to_env_seeds(tmp_path):
    """On a non-cluster server redis-cli prints the CLUSTER NODES error reply at exit 0. That line must
    not be parsed as a node (it used to yield a bogus target `This`); fall back to the .env seeds."""
    proc, _, ports = run_script(
        tmp_path, cluster_nodes="ERR This instance has cluster support disabled\n")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Target nodes: 127.0.0.1:7001" in proc.stdout
    assert ports == ["7001"]
