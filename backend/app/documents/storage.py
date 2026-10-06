"""Object storage behind an interface. Phase 1 writes to local disk; Phase 2 adds an
S3/MinIO implementation without touching the API or services."""

import os
import shutil
import uuid
from pathlib import Path
from typing import BinaryIO, Protocol

from anyio import to_thread


class StorageBackend(Protocol):
    async def put(self, key: str, data: BinaryIO) -> None: ...

    async def delete(self, key: str) -> None: ...

    async def exists(self, key: str) -> bool: ...


class LocalStorage:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):  # keys are server-generated, but be strict
            raise ValueError(f"Storage key escapes the storage root: {key!r}")
        return path

    async def put(self, key: str, data: BinaryIO) -> None:
        path = self._path(key)

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Write to a temp file and rename, so readers never see a half-written object.
            tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            try:
                with tmp.open("wb") as out:
                    shutil.copyfileobj(data, out, length=1024 * 1024)
                os.replace(tmp, path)
            finally:
                tmp.unlink(missing_ok=True)

        await to_thread.run_sync(_write)

    async def delete(self, key: str) -> None:
        path = self._path(key)
        await to_thread.run_sync(lambda: path.unlink(missing_ok=True))

    async def exists(self, key: str) -> bool:
        path = self._path(key)
        return await to_thread.run_sync(path.is_file)
