import pytest

from api.services.cc_chat.oddish_storage_adapter import OddishStorageAdapter


class FakeStorageClient:
    def __init__(self, files: dict[str, bytes]) -> None:
        self._files = files
        self.list_calls: list[str] = []
        self.download_calls: list[str] = []

    async def list_keys(self, prefix: str) -> list[str]:
        self.list_calls.append(prefix)
        return [k for k in self._files if k.startswith(prefix)]

    async def download_bytes(self, s3_key: str) -> bytes:
        self.download_calls.append(s3_key)
        return self._files[s3_key]


@pytest.mark.asyncio
async def test_list_keys_under_delegates_to_list_keys():
    client = FakeStorageClient({
        "tasks/exp-1/trials/t-0/result.json": b"{}",
        "tasks/exp-1/trials/t-1/result.json": b"{}",
        "tasks/exp-2/trials/t-0/result.json": b"{}",
    })
    adapter = OddishStorageAdapter(client=client)
    keys = await adapter.list_keys_under("tasks/exp-1/trials/")
    assert client.list_calls == ["tasks/exp-1/trials/"]
    assert sorted(keys) == [
        "tasks/exp-1/trials/t-0/result.json",
        "tasks/exp-1/trials/t-1/result.json",
    ]


@pytest.mark.asyncio
async def test_get_object_delegates_to_download_bytes():
    client = FakeStorageClient({"tasks/exp-1/trials/t-0/trial.log": b"hello\n"})
    adapter = OddishStorageAdapter(client=client)
    content = await adapter.get_object("tasks/exp-1/trials/t-0/trial.log")
    assert client.download_calls == ["tasks/exp-1/trials/t-0/trial.log"]
    assert content == b"hello\n"


@pytest.mark.asyncio
async def test_adapter_satisfies_s3_file_store_protocol():
    """End-to-end: pipe the adapter through S3FileStore and confirm files come out."""
    from api.services.cc_chat.file_store import S3FileStore

    client = FakeStorageClient({
        "tasks/exp-1/trials/t-0/result.json": b'{"reward": 1}',
        "tasks/exp-1/trials/t-0/trial.log": b"line\n",
    })
    store = S3FileStore(storage=OddishStorageAdapter(client=client))
    rels = {rel async for rel, _ in store.iter_files("exp-1")}
    assert rels == {"t-0/result.json", "t-0/trial.log"}
