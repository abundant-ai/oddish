from __future__ import annotations

from pathlib import Path
from typing import AsyncIterator, Protocol


class ExperimentFileStore(Protocol):
    async def iter_files(
        self, experiment_id: str
    ) -> AsyncIterator[tuple[str, bytes]]:
        """Yield (relative_path, file_bytes) for every artifact in the experiment."""
        ...


class LocalFileStore:
    """Reads experiment files from a local directory tree.

    Layout: <base_path>/<experiment_id>/<trial_id>/...
    Yields paths relative to <base_path>/<experiment_id>/.
    """

    def __init__(self, base_path: Path | str) -> None:
        self.base_path = Path(base_path)

    async def iter_files(
        self, experiment_id: str
    ) -> AsyncIterator[tuple[str, bytes]]:
        root = self.base_path / experiment_id
        if not root.is_dir():
            return
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if any(part.startswith(".") for part in path.relative_to(root).parts):
                continue
            rel = path.relative_to(root).as_posix()
            yield rel, path.read_bytes()


class _StorageLike(Protocol):
    """Just the methods we need; lets us inject test doubles."""
    async def list_keys_under(self, prefix: str) -> list[str]: ...
    async def get_object(self, key: str) -> bytes: ...


class S3FileStore:
    """Reads experiment files from S3 via the oddish StorageClient.

    The S3 layout in production is `tasks/<experiment_id>/trials/<trial_id>/...`
    but is configurable via `prefix_template` for forward-compat.
    """

    def __init__(
        self,
        *,
        storage: _StorageLike,
        prefix_template: str = "tasks/{experiment_id}/trials/",
    ) -> None:
        self._storage = storage
        self._prefix_template = prefix_template

    async def iter_files(
        self, experiment_id: str
    ) -> AsyncIterator[tuple[str, bytes]]:
        prefix = self._prefix_template.format(experiment_id=experiment_id)
        keys = await self._storage.list_keys_under(prefix)
        for key in keys:
            rel = key[len(prefix):]
            if any(part.startswith(".") for part in rel.split("/") if part):
                continue
            content = await self._storage.get_object(key)
            yield rel, content
