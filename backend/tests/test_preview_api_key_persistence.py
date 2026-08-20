"""A preview branch keeps its own API keys, and never another environment's.

Two properties, pulling in opposite directions:

* PERSISTENCE -- a rebuild must not invalidate the key a developer just created
  from the preview dashboard. Rebuilds are not rare: the trust marker is
  written last, so any cancelled run leaves the branch untrusted and the next
  push rebuilds it. Before this, three pushes in a row silently destroyed the
  key three times.
* ISOLATION -- an API key is a credential minted against one environment. It
  must never be copied from prod into a preview, nor from one preview into
  another. Persistence is achieved by reading the rows from and writing them
  back to the SAME branch database, never by sampling them from elsewhere.
"""

import importlib.util
from pathlib import Path

import preview_seed
import pytest


def _load_bootstrap():
    path = (
        Path(__file__).resolve().parents[2]
        / ".github/scripts/preview/bootstrap_preview_db.py"
    )
    spec = importlib.util.spec_from_file_location("bootstrap_preview_db", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestIsolation:
    """API keys never cross an environment boundary."""

    def test_api_keys_are_never_sampled_from_prod(self):
        assert "api_keys" in preview_seed._NEVER_SAMPLED_TABLES

    def test_api_keys_are_not_reconciled_from_the_sample(self):
        assert "api_keys" not in preview_seed._RECONCILED_TABLES

    def test_sampling_a_credential_table_raises(self):
        """A future edit that pulls api_keys into the sample must fail loudly.

        A broken preview seed is recoverable; a leaked credential is not.
        """
        with pytest.raises(RuntimeError, match="refusing to copy credential"):
            preview_seed._assert_no_forbidden_tables(
                {"experiments": [{"id": "e1"}], "api_keys": [{"id": "k1"}]}
            )

    def test_an_ordinary_sample_passes(self):
        preview_seed._assert_no_forbidden_tables(
            {"experiments": [{"id": "e1"}], "trials": [{"id": "t1"}]}
        )


class TestPersistence:
    """The branch's own keys survive its rebuild."""

    def test_api_keys_are_preserved_across_a_rebuild(self):
        assert "api_keys" in _load_bootstrap()._PRESERVED_TABLES

    def test_capture_precedes_the_drop_and_restore_follows_the_upgrade(self):
        """Order is the whole correctness argument.

        Capture must read the table before DROP SCHEMA destroys it, and the
        restore must run after ``upgrade head`` so the table has its final
        shape. Trust is still marked last.
        """
        source = (
            Path(__file__).resolve().parents[2]
            / ".github/scripts/preview/bootstrap_preview_db.py"
        ).read_text()
        body = source.split("def _rebuild(", 1)[1].split("\ndef ", 1)[0]

        order = [
            step
            for step in (
                "_capture_preserved_rows",
                "_reset_schema",
                "_run_seed",
                "_upgrade_head",
                "_restore_preserved_rows",
                "_mark_trusted",
            )
            if step in body
        ]
        assert order == [
            "_capture_preserved_rows",
            "_reset_schema",
            "_run_seed",
            "_upgrade_head",
            "_restore_preserved_rows",
            "_mark_trusted",
        ], f"unexpected rebuild step order: {order}"
