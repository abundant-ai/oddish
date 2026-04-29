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
