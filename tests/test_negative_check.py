"""THROWAWAY (branch ci/negative-check, never merged): must make every `test` job fail."""


def test_ci_must_go_red():
    assert 1 == 2, "deliberate failure: proves a red pytest blocks git-pr-merge"
