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


# ── Phase A coverage — over-privileged users / ACL log (C2–C6) ────────────────

def _titles(node):
    return [f["title"] for f in audit.analyse_security([node])["findings"]]


def test_named_user_all_commands_flagged(make_user, secure_config):          # C2
    nd = _node("n:6379", dict(secure_config), [make_user("app", all_commands=True)])
    assert any("unrestricted command access" in t for t in _titles(nd))


def test_named_user_dangerous_flagged(make_user, secure_config):             # C3
    nd = _node("n:6379", dict(secure_config),
               [make_user("app", dangerous=True, all_commands=False)])
    assert any("dangerous command category" in t for t in _titles(nd))


def test_named_user_write_all_keys_flagged(make_user, secure_config):        # C4
    nd = _node("n:6379", dict(secure_config),
               [make_user("app", write=True, all_commands=False,
                          dangerous=False, all_keys=True)])
    assert any("can write to all keys" in t for t in _titles(nd))


def test_no_acl_users_flagged(secure_config):                                # C5
    nd = _node("n:6379", dict(secure_config), [])
    assert any("ACL data unavailable" in t for t in _titles(nd))


def test_acl_log_auth_failures_flagged(make_user, secure_config):            # C6
    log = [{"reason": "auth"} for _ in range(6)]
    nd = _node("n:6379", dict(secure_config),
               [make_user("default", enabled=False)], log=log)
    assert any("authentication failures" in t for t in _titles(nd))


def test_acl_log_auth_failures_aggregated_count(make_user, secure_config):   # C6
    # ACL LOG aggregates identical failures into ONE entry carrying a count.
    log = [{"reason": "auth", "count": 6}]
    nd = _node("n:6379", dict(secure_config),
               [make_user("default", enabled=False)], log=log)
    assert any("authentication failures" in t for t in _titles(nd))
