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
