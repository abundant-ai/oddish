import pytest

from api.services.cohort_comparison import MIN_COHORT, is_stale


def test_is_stale_when_cohort_hash_changed():
    block_meta = {"cohort_hash": "aaa", "schema_version": 1}
    assert is_stale(block_meta, current_hash="bbb", schema_version=1) is True


def test_is_stale_when_schema_version_changed():
    # Keying freshness on schema_version alone is what left trajectory
    # summaries serving a retired vocabulary indefinitely; both must match.
    block_meta = {"cohort_hash": "aaa", "schema_version": 1}
    assert is_stale(block_meta, current_hash="aaa", schema_version=2) is True


def test_not_stale_when_both_match():
    block_meta = {"cohort_hash": "aaa", "schema_version": 1}
    assert is_stale(block_meta, current_hash="aaa", schema_version=1) is False


def test_missing_metadata_is_stale():
    assert is_stale(None, current_hash="aaa", schema_version=1) is True
