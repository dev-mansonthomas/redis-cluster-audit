"""Task 10 — check_permissions: read-only proceeds; a successful write is
undone (UNLINK) and the run aborts."""

import pytest
import redis

import audit


class _ReadOnly:
    def set(self, *a, **k):
        raise redis.exceptions.NoPermissionError("NOPERM this user has no permissions")


class _Writable:
    def __init__(self):
        self.calls = {}

    def set(self, *a, **k):
        self.calls["set"] = (a, k)
        return True

    def unlink(self, *a):
        self.calls["unlink"] = a
        return 1

    def delete(self, *a):
        self.calls["delete"] = a
        return 1


def test_readonly_user_proceeds():
    assert audit.check_permissions(_ReadOnly()) is None


def test_writable_user_is_cleaned_up_and_aborts():
    rc = _Writable()
    with pytest.raises(SystemExit):
        audit.check_permissions(rc)
    # the canary write must be undone, not left behind
    assert "unlink" in rc.calls or "delete" in rc.calls


class _WritableNoUnlink(_Writable):
    """Pre-4.0 server: UNLINK is an unknown command, so cleanup must fall back to DEL."""

    def unlink(self, *a):
        raise redis.exceptions.ResponseError("ERR unknown command 'UNLINK'")


class _WritableCleanupFails(_Writable):
    def unlink(self, *a):
        raise redis.exceptions.ConnectionError("connection lost")

    def delete(self, *a):
        raise redis.exceptions.ConnectionError("connection lost")


def test_cleanup_falls_back_to_del_when_unlink_is_unknown():
    rc = _WritableNoUnlink()
    with pytest.raises(SystemExit):
        audit.check_permissions(rc)
    assert "delete" in rc.calls


def test_failed_cleanup_is_reported_not_swallowed(capsys):
    """If the canary can't be removed, say so (it still expires: EX 5) — and still abort."""
    with pytest.raises(SystemExit) as exc:
        audit.check_permissions(_WritableCleanupFails())
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "could not remove" in out.lower() and "expires" in out.lower()


def test_unexpected_bug_in_cleanup_is_not_hidden():
    """Only Redis errors are tolerated during cleanup; a programming error must surface."""
    class _Buggy(_Writable):
        def unlink(self, *a):
            raise TypeError("bug")

    with pytest.raises(TypeError):
        audit.check_permissions(_Buggy())
