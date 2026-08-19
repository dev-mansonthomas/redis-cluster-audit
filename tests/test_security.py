"""Task 6 & 9 — analyse_security inspects every node; empty input is safe."""

import audit


def _node(label, config, users, log=None):
    return {"label": label, "config": config,
            "acl_data": {"users": users, "log": log or []}}


def test_flags_findings_on_non_first_node(make_user, secure_config):
    clean = _node("n0:6379", dict(secure_config),
                  [make_user("default", enabled=False)])
    bad = _node("n2:6379",
                {**secure_config, "protected-mode": "no"},
                [make_user("default", enabled=True, nopass=True, all_commands=True)])
    res = audit.analyse_security([clean, bad])
    titles = [f["title"].lower() for f in res["findings"]]
    assert any("no password" in t for t in titles)          # default nopass on node 2
    assert any("protected-mode" in t for t in titles)       # protected-mode no on node 2
    # the offending node is named, since node 0 was clean
    blob = " ".join(f["detail"] for f in res["findings"])
    assert "n2:6379" in blob


def test_empty_node_list_is_safe():
    res = audit.analyse_security([])
    assert res["findings"] == []
    assert res["users"] == []
