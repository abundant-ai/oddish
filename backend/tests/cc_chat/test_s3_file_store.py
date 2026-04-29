import pytest

from api.services.cc_chat.file_store import S3FileStore


class FakeStorage:
    def __init__(self, files: dict[str, bytes]) -> None:
        # files keyed by full S3 key, e.g. "tasks/exp-1/trials/t-0/result.json"
        self._files = files

    async def list_keys_under(self, prefix: str) -> list[str]:
        return [k for k in self._files if k.startswith(prefix)]

    async def get_object(self, key: str) -> bytes:
        return self._files[key]


@pytest.mark.asyncio
async def test_s3_file_store_yields_relative_paths():
    storage = FakeStorage({
        "tasks/exp-1/trials/t-0/result.json": b'{"reward": 1}',
        "tasks/exp-1/trials/t-0/trial.log": b"hello\n",
        "tasks/exp-1/trials/t-1/result.json": b'{"reward": 0}',
    })
    store = S3FileStore(storage=storage, prefix_template="tasks/{experiment_id}/trials/")
    files = [(rel, content) async for rel, content in store.iter_files("exp-1")]
    rels = {rel for rel, _ in files}
    assert "t-0/result.json" in rels
    assert "t-0/trial.log" in rels
    assert "t-1/result.json" in rels
