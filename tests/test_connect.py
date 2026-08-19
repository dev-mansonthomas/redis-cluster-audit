"""Task 7 — connect_cluster passes require_full_coverage=False, not the
legacy no-op skip_full_coverage_check."""

import audit


def test_connect_cluster_uses_require_full_coverage(monkeypatch):
    captured = {}

    class FakeCluster:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(audit, "RedisCluster", FakeCluster)
    audit.connect_cluster()

    assert captured.get("require_full_coverage") is False
    assert "skip_full_coverage_check" not in captured
