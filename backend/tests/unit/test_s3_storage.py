"""S3Storage against moto's S3-compatible server (no Docker or AWS account needed).
The Docker Compose smoke test in CI covers the same code against real MinIO."""

import io
from collections.abc import Iterator

import pytest
from moto.server import ThreadedMotoServer

from app.core.config import Settings
from app.documents.storage import ObjectNotFound, S3Storage, StorageError


@pytest.fixture(scope="module")
def s3_endpoint() -> Iterator[str]:
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=0)
    server.start()
    host, port = server.get_host_and_port()
    yield f"http://{host}:{port}"
    server.stop()


def make_storage(endpoint: str, bucket: str = "docunexus-test") -> S3Storage:
    return S3Storage(
        Settings(
            jwt_secret="x" * 40,
            storage_backend="s3",
            s3_endpoint_url=endpoint,
            s3_access_key="test",
            s3_secret_key="test",
            s3_bucket=bucket,
        )
    )


async def test_round_trip(s3_endpoint, tmp_path):
    storage = make_storage(s3_endpoint)
    await storage.ensure_bucket()
    await storage.ensure_bucket()  # idempotent

    key = "tenant/documents/doc/versions/v/original.pdf"
    await storage.put(key, io.BytesIO(b"%PDF-1.7 hello"))
    assert await storage.exists(key)

    destination = tmp_path / "out.pdf"
    await storage.download(key, destination)
    assert destination.read_bytes() == b"%PDF-1.7 hello"

    await storage.delete(key)
    assert not await storage.exists(key)


async def test_missing_object_is_object_not_found(s3_endpoint, tmp_path):
    storage = make_storage(s3_endpoint)
    await storage.ensure_bucket()
    with pytest.raises(ObjectNotFound):
        await storage.download("does/not/exist", tmp_path / "x")


async def test_unreachable_endpoint_is_a_storage_error(tmp_path):
    storage = make_storage("http://127.0.0.1:9")  # nothing listens on port 9
    with pytest.raises(StorageError):
        await storage.put("k", io.BytesIO(b"x"))
