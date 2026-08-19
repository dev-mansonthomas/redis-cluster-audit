def test_import_audit():
    import audit
    assert hasattr(audit, "main")
