import pathlib


def test_probe_scope_migration_exists_and_backfills():
    # Resolve relative to this test file so the test is CWD-independent
    # (this repo runs pytest from oddish/, not the repo root).
    versions = pathlib.Path(__file__).resolve().parent.parent / "alembic" / "versions"
    text = "\n".join(p.read_text() for p in versions.glob("*add_probe_scope*.py"))
    assert "add_column" in text and "probe_scope" in text
    assert "is_probe = true" in text  # backfill present
    # down_revision is the actual oddish head (mine_filter_idx_001); the
    # planned merge_tagfix_probe_heads is mid-chain, so using it would fork
    # a second head.
    assert 'down_revision: Union[str, Sequence[str], None] = "mine_filter_idx_001"' in text
