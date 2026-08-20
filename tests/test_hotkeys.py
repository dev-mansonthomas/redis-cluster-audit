"""HOTKEYS (Redis >= 8.6) invasive opt-in mode: version gate, reply parsing,
tracking workflow, recommendations, and report section."""

import pytest
import redis

import audit

# Exact HOTKEYS GET reply captured from redis:8.6.5 via redis-py (RESP2,
# decode_responses=True): a one-element list wrapping a dict; the per-key
# metrics are flat [key, value, ...] lists.
REAL_REPLY = [{
    "tracking-active": 0, "sample-ratio": 1, "selected-slots": [[0, 16383]],
    "all-commands-all-slots-us": 6963, "net-bytes-all-commands-all-slots": 16834000,
    "collection-start-time-unix-ms": 1787154094702, "collection-duration-ms": 3013,
    "total-cpu-time-user-ms": 23, "total-cpu-time-sys-ms": 46, "total-net-bytes": 16834000,
    "by-cpu-time-us": ["hot:key:A", 6836, "warm:key:B", 127],
    "by-net-bytes": ["hot:key:A", 16296000, "warm:key:B", 538000],
}]


class _FakeHK:
    """Records HOTKEYS subcommands; returns a canned GET reply."""

    def __init__(self, get_reply=REAL_REPLY, fail_start=False):
        self.calls = []
        self._get_reply = get_reply
        self._fail_start = fail_start

    def execute_command(self, *args):
        self.calls.append(args)
        if args[:2] == ("HOTKEYS", "START") and self._fail_start:
            raise redis.exceptions.ResponseError("unknown command 'HOTKEYS'")
        if args[:2] == ("HOTKEYS", "GET"):
            return self._get_reply
        return "OK"


# ── version gate ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("ver,expected", [
    ("8.6.5", True), ("8.6.0", True), ("8.8.0", True), ("9.0.0", True),
    ("8.5.9", False), ("6.2.20", False), ("7.4.0", False), ("bogus", False),
])
def test_version_at_least(ver, expected):
    assert audit.version_at_least(ver, audit.HOTKEYS_MIN_VERSION) is expected


# ── reply parsing (against the real shape) ────────────────────────────────────

def test_parse_hotkeys_get_real_shape():
    parsed = audit._parse_hotkeys_get(REAL_REPLY)
    assert parsed["by_cpu"] == {"hot:key:A": 6836, "warm:key:B": 127}
    assert parsed["by_net"]["hot:key:A"] == 16296000
    assert parsed["total_cpu"] == 6963
    assert parsed["total_net"] == 16834000


def test_parse_hotkeys_get_empty():
    assert audit._parse_hotkeys_get(None) is None
    assert audit._parse_hotkeys_get([]) is None


# ── tracking workflow ─────────────────────────────────────────────────────────

def test_collect_hotkeys_runs_full_workflow(monkeypatch):
    monkeypatch.setattr(audit.time, "sleep", lambda _s: None)   # no real wait
    node = _FakeHK()
    res = audit.collect_hotkeys(node, 3, top_k=10)
    subs = [c[1] for c in node.calls if c[0] == "HOTKEYS"]
    assert subs == ["START", "GET", "STOP", "RESET"]            # correct sequence + cleanup
    start = next(c for c in node.calls if c[:2] == ("HOTKEYS", "START"))
    assert "DURATION" in start and "3" in start and "CPU" in start and "NET" in start
    assert res["by_cpu"]["hot:key:A"] == 6836


def test_collect_hotkeys_unavailable_returns_none(monkeypatch):
    monkeypatch.setattr(audit.time, "sleep", lambda _s: None)
    node = _FakeHK(fail_start=True)
    assert audit.collect_hotkeys(node, 3) is None


# ── recommendations ───────────────────────────────────────────────────────────

def test_analyse_hotkeys_flags_dominant_key():
    parsed = audit._parse_hotkeys_get(REAL_REPLY)
    recs = audit.analyse_hotkeys([{"label": "n:6379", "hotkeys": parsed}])
    titles = [r["title"] for r in recs]
    assert any("hot:key:A" in t and "CPU time" in t for t in titles)
    assert any("network bytes" in t for t in titles)


def test_analyse_hotkeys_no_dominant_key():
    # top key is 40% of CPU and 40% of net — under the 50% "hot" bar
    balanced = {"by_cpu": {"a": 40, "b": 35, "c": 25}, "total_cpu": 100,
                "by_net": {"a": 40, "b": 35, "c": 25}, "total_net": 100}
    assert audit.analyse_hotkeys([{"label": "n", "hotkeys": balanced}]) == []


def test_analyse_hotkeys_ignores_nodes_without_data():
    assert audit.analyse_hotkeys([{"label": "n", "info": {}}]) == []


# ── report section ────────────────────────────────────────────────────────────

def test_section_hotkeys_empty_when_not_run():
    assert audit.section_hotkeys([{"label": "n", "info": {}}]) == ""


def test_section_hotkeys_renders_shares():
    parsed = audit._parse_hotkeys_get(REAL_REPLY)
    html = audit.section_hotkeys([{"label": "n:6379", "hotkeys": parsed}])
    assert "Hot Keys" in html
    assert "hot:key:A" in html
    assert "98.2%" in html   # 6836 / 6963 CPU share
