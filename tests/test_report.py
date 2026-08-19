"""Task 3 & 4 — HTML report correctness in section_keyspace."""

import audit


def _keyspace(ttl_distribution, total_scanned):
    return {
        "total_scanned": total_scanned,
        "ttl_distribution": ttl_distribution,
        "type_distribution": {"string": total_scanned},
        "big_keys": [],
        "expiring_soon": [],
    }


def test_dbsize_shows_key_count_not_dict_repr():
    nd = [{"label": "n1:6379",
           "info": {"db0": {"keys": 1234, "expires": 10, "avg_ttl": 0},
                    "expired_keys": 5}}]
    html = audit.section_keyspace(_keyspace({"no_ttl": 1, "has_ttl": 0}, 1), nd)
    assert "1234" in html
    # a dict repr would leak the other field names into the cell
    assert "avg_ttl" not in html
    assert "expires" not in html


def test_dbsize_missing_db0_falls_back():
    nd = [{"label": "n1:6379", "info": {"expired_keys": 0}}]
    html = audit.section_keyspace(_keyspace({"no_ttl": 0, "has_ttl": 0}, 0), nd)
    assert "—" in html  # graceful fallback, no crash


def test_has_ttl_percentage_uses_real_count_not_subtraction():
    # no_ttl=40, has_ttl=30, expired=30 over 100 → Has-TTL must read 30%, not 60%
    nd = [{"label": "n1:6379", "info": {"db0": {"keys": 100}, "expired_keys": 0}}]
    html = audit.section_keyspace(
        _keyspace({"no_ttl": 40, "has_ttl": 30, "expired": 30}, 100), nd)
    assert "40%" in html          # no_ttl
    assert "30%" in html          # has_ttl (real count), green only
    assert "60%" not in html      # the old 100 - no_ttl_pct bug
    assert "Expired" in html      # the third bucket is surfaced
