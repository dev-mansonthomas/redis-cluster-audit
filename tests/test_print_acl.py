"""`audit.py --print-acl USER`: the password is never a CLI argument and never printed in clear.

It is prompted (hidden, with confirmation) on a TTY, or read from stdin when piped; the printed
`ACL SETUSER` line carries `#<sha256>` instead of `>password`.
"""
import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

import audit

ROOT = Path(__file__).resolve().parent.parent
PW = 'my "pass\' word'
DIGEST = hashlib.sha256(PW.encode()).hexdigest()


def _run(tmp_path, *args, stdin=""):
    # cwd=tmp_path so load_dotenv() never picks up a real .env
    return subprocess.run([sys.executable, str(ROOT / "audit.py"), *args], input=stdin,
                          capture_output=True, text=True, cwd=tmp_path, timeout=30, check=False)


@pytest.mark.parametrize("stdin", [PW, PW + "\n", PW + "\r\n"])
def test_piped_password_prints_hashed_line(tmp_path, stdin):
    proc = _run(tmp_path, "--print-acl", "audit_ro", stdin=stdin)
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout.strip()
    assert out == f"ACL SETUSER audit_ro on #{DIGEST} {audit.build_acl_grants()}"
    assert PW not in proc.stdout + proc.stderr
    assert ">" not in out


def test_password_as_argument_is_refused(tmp_path):
    proc = _run(tmp_path, "--print-acl", "audit_ro", PW)
    assert proc.returncode == 2
    assert "no longer" in proc.stderr and "stdin" in proc.stderr
    assert "ACL SETUSER" not in proc.stdout
    assert PW not in proc.stdout


@pytest.mark.parametrize("stdin", ["", "\n"])
def test_empty_password_is_refused(tmp_path, stdin):
    proc = _run(tmp_path, "--print-acl", "audit_ro", stdin=stdin)
    assert proc.returncode == 1
    assert "empty" in proc.stderr.lower()
    assert "ACL SETUSER" not in proc.stdout


@pytest.mark.parametrize("bad", ["x nopass +@all", "a;b", 'q"', "-x", ""])
def test_unsafe_username_is_refused(tmp_path, bad):
    proc = _run(tmp_path, "--print-acl", bad, stdin=PW)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    # A leading '-' is already rejected by argparse itself (it looks like an option).
    assert "username" in proc.stderr.lower() or "expected at least one argument" in proc.stderr
    assert "ACL SETUSER" not in proc.stdout


class _TTY:
    def isatty(self):
        return True


def test_tty_prompts_hidden_with_confirmation(monkeypatch, capsys):
    answers = iter([PW, PW])
    prompts = []
    monkeypatch.setattr(sys, "stdin", _TTY())
    monkeypatch.setattr(audit.getpass, "getpass", lambda p="": prompts.append(p) or next(answers))
    monkeypatch.setattr(sys, "argv", ["audit.py", "--print-acl", "audit_ro"])
    audit.main()
    assert len(prompts) == 2
    assert capsys.readouterr().out.strip().startswith(f"ACL SETUSER audit_ro on #{DIGEST} ")


def test_tty_confirmation_mismatch_exits(monkeypatch, capsys):
    answers = iter([PW, "other"])
    monkeypatch.setattr(sys, "stdin", _TTY())
    monkeypatch.setattr(audit.getpass, "getpass", lambda p="": next(answers))
    monkeypatch.setattr(sys, "argv", ["audit.py", "--print-acl", "audit_ro"])
    with pytest.raises(SystemExit) as exc:
        audit.main()
    assert exc.value.code == 1
    cap = capsys.readouterr()
    assert "do not match" in cap.err and "ACL SETUSER" not in cap.out
