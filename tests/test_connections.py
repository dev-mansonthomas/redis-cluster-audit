"""N2 & N3 — analyse_connections captures per-user and per-IP signals."""

import audit


def test_counts_external_default_user_connections():
    clients = {"n:6379": [
        {"addr": "10.0.0.5:5000", "idle": "1", "cmd": "get", "user": "default"},
        {"addr": "127.0.0.1:6000", "idle": "1", "cmd": "ping", "user": "default"},  # loopback admin → ignored
        {"addr": "10.0.0.6:5001", "idle": "1", "cmd": "get", "user": "app_user"},
    ]}
    res = audit.analyse_connections(clients)
    assert res["default_user_external"] == 1
    assert res["per_ip"]["10.0.0.5"] == 1


def test_per_ip_concentration_counted():
    clients = {"n:6379": [
        {"addr": f"10.0.0.9:{5000 + p}", "idle": "0", "cmd": "get", "user": "app"}
        for p in range(300)
    ]}
    res = audit.analyse_connections(clients)
    assert res["per_ip"]["10.0.0.9"] == 300
