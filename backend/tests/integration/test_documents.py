import asyncio
import hashlib
import io
import zipfile

from tests.integration.conftest import MAX_UPLOAD_BYTES
from tests.integration.helpers import add_member, auth, pdf_bytes, register, upload


def docx_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<w:document/>")
    return buf.getvalue()


async def test_upload_returns_queued_and_location(client, settings):
    admin = await register(client)
    content = pdf_bytes()
    resp = await upload(client, admin["access_token"], content, filename="annual-report.pdf")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "QUEUED"
    assert resp.headers["Location"] == f"/v1/documents/{body['document_id']}"

    doc = (await client.get(resp.headers["Location"], headers=auth(admin["access_token"]))).json()
    assert doc["filename"] == "annual-report.pdf"
    assert doc["mime_type"] == "application/pdf"
    assert doc["size_bytes"] == len(content)
    assert doc["checksum"] == hashlib.sha256(content).hexdigest()
    assert doc["version"] == 1
    assert "storage_key" not in doc  # internal detail

    stored = (
        settings.storage_local_dir
        / admin["org_id"]
        / "documents"
        / body["document_id"]
        / "original.pdf"
    )
    assert stored.read_bytes() == content


async def test_type_is_detected_from_content_not_client_claims(client):
    admin = await register(client)
    token = admin["access_token"]

    docx = await upload(client, token, docx_bytes(), filename="contract.pdf")
    assert docx.status_code == 201
    got = await client.get(f"/v1/documents/{docx.json()['document_id']}", headers=auth(token))
    assert got.json()["mime_type"].endswith("wordprocessingml.document")

    exe = await upload(client, token, b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 64, "invoice.pdf")
    assert exe.status_code == 415
    assert exe.json()["error"]["code"] == "UNSUPPORTED_FILE_TYPE"

    plain_zip = io.BytesIO()
    with zipfile.ZipFile(plain_zip, "w") as archive:
        archive.writestr("payload.bin", "x")
    rejected = await upload(client, token, plain_zip.getvalue(), "file.docx")
    assert rejected.status_code == 415


async def test_empty_and_oversized_files_are_rejected(client):
    admin = await register(client)
    empty = await upload(client, admin["access_token"], b"", "empty.pdf")
    assert empty.status_code == 422
    assert empty.json()["error"]["code"] == "EMPTY_FILE"

    big = await upload(client, admin["access_token"], b"%PDF-" + b"0" * MAX_UPLOAD_BYTES, "big.pdf")
    assert big.status_code == 413
    assert big.json()["error"]["code"] == "FILE_TOO_LARGE"


async def test_filename_is_sanitized(client):
    admin = await register(client)
    resp = await upload(client, admin["access_token"], filename="../../etc/pa<ss>wd|.pdf")
    doc = await client.get(
        f"/v1/documents/{resp.json()['document_id']}", headers=auth(admin["access_token"])
    )
    assert doc.json()["filename"] == "passwd.pdf"


async def test_duplicate_upload_returns_existing_id(client):
    admin = await register(client)
    content = pdf_bytes()
    first = await upload(client, admin["access_token"], content)
    second = await upload(client, admin["access_token"], content, filename="copy.pdf")
    assert second.status_code == 409
    error = second.json()["error"]
    assert error["code"] == "DUPLICATE_DOCUMENT"
    assert error["details"]["existing_document_id"] == first.json()["document_id"]


async def test_concurrent_duplicate_uploads_create_exactly_one_document(client):
    admin = await register(client)
    content = pdf_bytes()
    responses = await asyncio.gather(
        *(upload(client, admin["access_token"], content) for _ in range(5))
    )
    assert sorted(r.status_code for r in responses) == [201, 409, 409, 409, 409]
    listing = await client.get("/v1/documents", headers=auth(admin["access_token"]))
    assert len(listing.json()["items"]) == 1


async def test_same_file_in_two_tenants_is_not_a_duplicate(client):
    content = pdf_bytes()
    a = await register(client)
    b = await register(client)
    assert (await upload(client, a["access_token"], content)).status_code == 201
    assert (await upload(client, b["access_token"], content)).status_code == 201


async def test_list_paginates_without_gaps_or_duplicates(client):
    admin = await register(client)
    token = admin["access_token"]
    created = [(await upload(client, token)).json()["document_id"] for _ in range(5)]

    seen: list[str] = []
    cursor = None
    while True:
        params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        page = (await client.get("/v1/documents", params=params, headers=auth(token))).json()
        seen += [item["id"] for item in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == list(reversed(created))  # newest first

    oldest_first = await client.get(
        "/v1/documents", params={"sort": "created_at", "limit": 10}, headers=auth(token)
    )
    assert [i["id"] for i in oldest_first.json()["items"]] == created


async def test_list_filters_and_bad_cursor(client):
    admin = await register(client)
    token = admin["access_token"]
    await upload(client, token)
    queued = await client.get("/v1/documents", params={"status": "QUEUED"}, headers=auth(token))
    assert len(queued.json()["items"]) == 1
    failed = await client.get("/v1/documents", params={"status": "FAILED"}, headers=auth(token))
    assert failed.json()["items"] == []

    bad = await client.get("/v1/documents", params={"cursor": "garbage!"}, headers=auth(token))
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "INVALID_CURSOR"


async def test_rename_uses_optimistic_locking(client):
    admin = await register(client)
    token = admin["access_token"]
    doc_id = (await upload(client, token)).json()["document_id"]

    ok = await client.patch(
        f"/v1/documents/{doc_id}",
        json={"filename": "renamed.pdf", "version": 1},
        headers=auth(token),
    )
    assert ok.status_code == 200
    assert ok.json()["version"] == 2
    assert ok.json()["filename"] == "renamed.pdf"

    stale = await client.patch(
        f"/v1/documents/{doc_id}",
        json={"filename": "lost-update.pdf", "version": 1},
        headers=auth(token),
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "VERSION_CONFLICT"
    assert stale.json()["error"]["details"]["current_version"] == 2


async def test_concurrent_renames_with_same_version_one_wins(client):
    admin = await register(client)
    token = admin["access_token"]
    doc_id = (await upload(client, token)).json()["document_id"]
    responses = await asyncio.gather(
        *(
            client.patch(
                f"/v1/documents/{doc_id}",
                json={"filename": f"n{i}.pdf", "version": 1},
                headers=auth(token),
            )
            for i in range(4)
        )
    )
    assert sorted(r.status_code for r in responses) == [200, 409, 409, 409]


async def test_delete_is_admin_only_and_soft(client, owner_conn):
    admin = await register(client)
    user = await add_member(client, admin["access_token"], "USER")
    content = pdf_bytes()
    doc_id = (await upload(client, user["access_token"], content)).json()["document_id"]

    forbidden = await client.delete(f"/v1/documents/{doc_id}", headers=auth(user["access_token"]))
    assert forbidden.status_code == 403

    deleted = await client.delete(f"/v1/documents/{doc_id}", headers=auth(admin["access_token"]))
    assert deleted.status_code == 204
    gone = await client.get(f"/v1/documents/{doc_id}", headers=auth(admin["access_token"]))
    assert gone.status_code == 404
    assert gone.json()["error"]["code"] == "DOCUMENT_NOT_FOUND"
    again = await client.delete(f"/v1/documents/{doc_id}", headers=auth(admin["access_token"]))
    assert again.status_code == 404

    # Soft delete: the row is still there, and the same file can be uploaded again.
    assert await owner_conn.fetchval("SELECT deleted_at IS NOT NULL FROM documents") is True
    reupload = await upload(client, admin["access_token"], content)
    assert reupload.status_code == 201


async def test_viewer_can_read_but_not_write(client):
    admin = await register(client)
    viewer = await add_member(client, admin["access_token"], "VIEWER")
    doc_id = (await upload(client, admin["access_token"])).json()["document_id"]

    assert (
        await client.get(f"/v1/documents/{doc_id}", headers=auth(viewer["access_token"]))
    ).status_code == 200
    assert (await upload(client, viewer["access_token"])).status_code == 403
    rename = await client.patch(
        f"/v1/documents/{doc_id}",
        json={"filename": "x.pdf", "version": 1},
        headers=auth(viewer["access_token"]),
    )
    assert rename.status_code == 403


async def test_document_changes_are_audited(client):
    admin = await register(client)
    token = admin["access_token"]
    doc_id = (await upload(client, token)).json()["document_id"]
    await client.delete(f"/v1/documents/{doc_id}", headers=auth(token))
    logs = (await client.get("/v1/organizations/current/audit-logs", headers=auth(token))).json()[
        "items"
    ]
    assert [(i["action"], i["target_id"]) for i in logs[:2]] == [
        ("document.deleted", doc_id),
        ("document.uploaded", doc_id),
    ]
