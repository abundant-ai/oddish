from __future__ import annotations

from typing import Protocol


class _StorageClientLike(Protocol):
    async def list_keys(self, prefix: str) -> list[str]: ...
    async def download_bytes(self, s3_key: str) -> bytes: ...


class OddishStorageAdapter:
    """Adapts oddish.db.storage.StorageClient to S3FileStore's _StorageLike protocol.

    The cc-chat S3FileStore expects `list_keys_under` / `get_object`; the oddish
    StorageClient calls them `list_keys` / `download_bytes`. This is a pure
    name-translation layer.
    """

    def __init__(self, client: _StorageClientLike | None = None) -> None:
        if client is None:
            from oddish.db.storage import get_storage_client

            client = get_storage_client()
        self._client = client

    async def list_keys_under(self, prefix: str) -> list[str]:
        return await self._client.list_keys(prefix)

    async def get_object(self, key: str) -> bytes:
        return await self._client.download_bytes(key)
