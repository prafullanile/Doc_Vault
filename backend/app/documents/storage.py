"""Object storage behind one interface: local disk for development and tests, S3-compatible
storage (RustFS/MinIO locally, S3 in production) otherwise."""

import asyncio
import os
import shutil
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Protocol

import boto3
from anyio import to_thread
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from app.core.config import Settings

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client


class StorageError(Exception):
    """The storage backend failed (network, credentials, outage). Callers treat it as transient."""


class ObjectNotFound(StorageError):
    pass


class StorageBackend(Protocol):
    async def put(self, key: str, data: BinaryIO) -> None: ...

    async def download(self, key: str, destination: Path) -> None: ...

    async def delete(self, key: str) -> None: ...

    async def exists(self, key: str) -> bool: ...


class LocalStorage:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):  # keys are server-generated, but be strict
            raise ValueError(f"Storage key escapes the storage root: {key!r}")
        if sys.platform == "win32":
            # Keys nest three UUIDs deep; the extended-length prefix lifts Windows' 260-char
            # path limit.
            return Path("\\\\?\\" + str(path))
        return path

    async def _run[T](self, fn: Callable[[], T]) -> T:
        try:
            return await to_thread.run_sync(fn)
        except ObjectNotFound:
            raise
        except OSError as exc:  # disk full, permissions, ...
            raise StorageError(str(exc)) from exc

    async def put(self, key: str, data: BinaryIO) -> None:
        path = self._path(key)

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Write to a temp file and rename, so readers never see a half-written object.
            tmp = path.with_name(f".{uuid.uuid4().hex[:12]}.tmp")
            try:
                with tmp.open("wb") as out:
                    shutil.copyfileobj(data, out, length=1024 * 1024)
                os.replace(tmp, path)
            finally:
                tmp.unlink(missing_ok=True)

        await self._run(_write)

    async def download(self, key: str, destination: Path) -> None:
        path = self._path(key)

        def _copy() -> None:
            if not path.is_file():
                raise ObjectNotFound(key)
            shutil.copyfile(path, destination)

        await self._run(_copy)

    async def delete(self, key: str) -> None:
        path = self._path(key)
        await self._run(lambda: path.unlink(missing_ok=True))

    async def exists(self, key: str) -> bool:
        path = self._path(key)
        return await self._run(path.is_file)


class S3Storage:
    """boto3 is synchronous, so every call runs in a worker thread. Multipart upload and
    download are handled by boto3's transfer manager."""

    def __init__(self, settings: Settings) -> None:
        self.bucket = settings.s3_bucket
        self.client: S3Client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            region_name=settings.s3_region,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            config=BotoConfig(
                retries={"max_attempts": 3, "mode": "standard"},
                connect_timeout=5,
                read_timeout=60,
                s3={"addressing_style": "path"},  # what self-hosted S3 servers expect
            ),
        )

    async def ensure_bucket(self) -> None:
        def _ensure() -> None:
            try:
                self.client.head_bucket(Bucket=self.bucket)
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") not in ("404", "NoSuchBucket"):
                    raise
                try:
                    self.client.create_bucket(Bucket=self.bucket)
                except ClientError as create_exc:
                    # The API and the worker start together; one of them wins the race.
                    code = create_exc.response.get("Error", {}).get("Code")
                    if code not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                        raise

        await self._run(_ensure)

    async def _run[T](self, fn: Callable[[], T]) -> T:
        try:
            return await to_thread.run_sync(fn)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in ("404", "NoSuchKey"):
                raise ObjectNotFound(str(exc)) from exc
            raise StorageError(str(exc)) from exc
        except Exception as exc:  # connection errors, timeouts
            raise StorageError(str(exc)) from exc

    async def put(self, key: str, data: BinaryIO) -> None:
        await self._run(lambda: self.client.upload_fileobj(data, self.bucket, key))

    async def download(self, key: str, destination: Path) -> None:
        await self._run(lambda: self.client.download_file(self.bucket, key, str(destination)))

    async def delete(self, key: str) -> None:
        await self._run(lambda: self.client.delete_object(Bucket=self.bucket, Key=key))

    async def exists(self, key: str) -> bool:
        try:
            await self._run(lambda: self.client.head_object(Bucket=self.bucket, Key=key))
        except ObjectNotFound:
            return False
        return True


async def create_storage(settings: Settings, startup_timeout: float = 60.0) -> StorageBackend:
    if settings.storage_backend == "local":
        return LocalStorage(settings.storage_local_dir)
    storage = S3Storage(settings)
    # The object store may still be starting (e.g. in Docker Compose): retry for a while.
    deadline = asyncio.get_running_loop().time() + startup_timeout
    while True:
        try:
            await storage.ensure_bucket()
            return storage
        except StorageError:
            if asyncio.get_running_loop().time() >= deadline:
                raise
            await asyncio.sleep(2)
