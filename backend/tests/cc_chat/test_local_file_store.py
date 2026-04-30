from pathlib import Path

import pytest

from api.services.cc_chat.file_store import LocalFileStore

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_BASE = REPO_ROOT / "jobs"
FIXTURE_EXPERIMENT_ID = "2026-04-26__16-45-36"


@pytest.mark.asyncio
async def test_local_file_store_yields_relative_paths_and_bytes():
    store = LocalFileStore(base_path=FIXTURE_BASE)
    files = []
    async for rel, content in store.iter_files(FIXTURE_EXPERIMENT_ID):
        files.append((rel, content))

    assert files, "expected at least one file"
    rel_paths = {rel for rel, _ in files}
    assert "hello-world__eU7yQqg/result.json" in rel_paths
    assert "hello-world__eU7yQqg/trial.log" in rel_paths

    # Content is bytes and matches the file on disk
    for rel, content in files:
        assert isinstance(content, bytes)
        on_disk = (FIXTURE_BASE / FIXTURE_EXPERIMENT_ID / rel).read_bytes()
        assert content == on_disk


@pytest.mark.asyncio
async def test_local_file_store_skips_dotfiles():
    store = LocalFileStore(base_path=FIXTURE_BASE)
    rel_paths = [rel async for rel, _ in store.iter_files(FIXTURE_EXPERIMENT_ID)]
    for rel in rel_paths:
        assert not any(part.startswith(".") for part in Path(rel).parts), (
            f"unexpected dotfile path: {rel}"
        )


@pytest.mark.asyncio
async def test_local_file_store_missing_experiment_yields_nothing():
    store = LocalFileStore(base_path=FIXTURE_BASE)
    files = [f async for f in store.iter_files("does-not-exist")]
    assert files == []
