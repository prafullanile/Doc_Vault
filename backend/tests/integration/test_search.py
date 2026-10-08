"""Search end to end: documents go through the real pipeline (extract → chunk → embed) and are
then searched through the API. Tests use the deterministic hashing embedder; the real models
are covered by tests/unit/test_search_units.py (RUN_MODEL_TESTS=1) and the Compose smoke test."""

from typing import Any

import pytest

from app.search import service as search_service
from tests import factories
from tests.integration.helpers import auth, register
from tests.integration.test_processing import drain, upload_file

CONTRACT = [
    "Master services agreement between Acme Corp and Globex. Reference INV-2025-0042.",
    "Payment terms: the buyer pays one hundred thousand dollars within thirty days.",
    "Termination: either party may terminate with ninety days written notice.",
]
REPORT = [
    "Annual report. Revenue increased by twenty three percent compared with last year.",
    "Operating costs fell while margins improved across every region.",
]


async def search(client, token: str, query: str, **body: Any) -> dict[str, Any]:
    resp = await client.post("/v1/search", json={"query": query, **body}, headers=auth(token))
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.fixture
async def corpus(client, worker) -> dict[str, Any]:
    admin = await register(client, org="Search Co")
    token = admin["access_token"]
    contract = await upload_file(
        client, token, factories.text_pdf(CONTRACT), "contract.pdf", "application/pdf"
    )
    report = await upload_file(
        client, token, factories.text_pdf(REPORT), "report.pdf", "application/pdf"
    )
    await drain(worker)
    return {"token": token, "org_id": admin["org_id"], "contract": contract, "report": report}


async def test_documents_are_chunked_and_embedded(client, corpus, owner_conn):
    status = (
        await client.get(
            f"/v1/documents/{corpus['contract']['document_id']}/status",
            headers=auth(corpus["token"]),
        )
    ).json()
    assert status["status"] == "PROCESSED"
    assert status["current_version"]["chunk_count"] == 3  # one per (short) page
    assert await owner_conn.fetchval("SELECT count(*) FROM chunk_embeddings") == 5
    models = await owner_conn.fetch("SELECT DISTINCT model FROM chunk_embeddings")
    assert [m["model"] for m in models] == ["hashing-v1"]


async def test_keyword_search_finds_exact_identifiers_and_cites_the_page(client, corpus):
    body = await search(client, corpus["token"], "INV-2025-0042", mode="keyword")
    (hit,) = body["results"]
    assert hit["document_id"] == corpus["contract"]["document_id"]
    assert hit["filename"] == "contract.pdf"
    assert hit["page_number"] == 1
    assert hit["scores"]["keyword_rank"] == 1
    assert hit["scores"]["vector_rank"] is None


async def test_keyword_search_stems_words(client, corpus):
    # "terminating" and "terminate" share the stem "termin" in the english configuration.
    body = await search(client, corpus["token"], "terminating notice", mode="keyword")
    assert [r["page_number"] for r in body["results"]] == [3]


async def test_keyword_search_handles_hostile_syntax(client, corpus):
    for query in ['"unclosed phrase', "a & | ! ( )", "-", "' OR 1=1 --"]:
        body = await search(client, corpus["token"], query, mode="keyword")
        assert isinstance(body["results"], list)


async def test_vector_search_ranks_by_similarity(client, corpus):
    body = await search(
        client, corpus["token"], "revenue increased compared with last year", mode="vector"
    )
    top = body["results"][0]
    assert top["document_id"] == corpus["report"]["document_id"]
    assert top["page_number"] == 1
    assert top["scores"]["vector_rank"] == 1
    assert len(body["results"]) == 5  # vector search always returns its nearest neighbours


async def test_hybrid_combines_both_and_explains_ranks(client, corpus):
    body = await search(client, corpus["token"], "payment terms thirty days")
    assert body["mode"] == "hybrid"
    top = body["results"][0]
    assert (top["filename"], top["page_number"]) == ("contract.pdf", 2)
    assert top["scores"]["keyword_rank"] == 1
    assert top["scores"]["vector_rank"] == 1
    assert top["score"] == top["scores"]["fused"]  # no reranker configured in tests
    assert body["reranked"] is False
    assert {"keyword", "embed_query", "vector", "total"} <= body["timings_ms"].keys()


async def test_filters(client, corpus):
    token = corpus["token"]
    only_report = await search(
        client,
        token,
        "revenue payment",
        filters={"document_ids": [corpus["report"]["document_id"]]},
    )
    assert {r["filename"] for r in only_report["results"]} == {"report.pdf"}
    no_docx = await search(client, token, "revenue", filters={"mime_types": [factories.DOCX_MIME]})
    assert no_docx["results"] == []
    english = await search(client, token, "revenue", filters={"languages": ["en"]})
    assert english["results"]


async def test_search_is_tenant_isolated(client, corpus, worker, app_conn):
    other = await register(client, org="Other Co")
    # The other tenant uploads near-identical content...
    await upload_file(
        client,
        other["access_token"],
        factories.text_pdf(CONTRACT, marker="other"),
        "theirs.pdf",
        "application/pdf",
    )
    await drain(worker)
    # ...and neither tenant ever sees the other's chunks.
    mine = await search(client, corpus["token"], "INV-2025-0042 payment terms")
    theirs = await search(client, other["access_token"], "INV-2025-0042 payment terms")
    assert {r["filename"] for r in mine["results"]} <= {"contract.pdf", "report.pdf"}
    assert {r["filename"] for r in theirs["results"]} == {"theirs.pdf"}
    # Below the API: the app role sees no chunks or vectors without a tenant bound.
    assert await app_conn.fetchval("SELECT count(*) FROM document_chunks") == 0
    assert await app_conn.fetchval("SELECT count(*) FROM chunk_embeddings") == 0


async def test_deleted_documents_and_old_versions_are_not_searched(client, corpus, worker):
    token = corpus["token"]
    await client.delete(f"/v1/documents/{corpus['report']['document_id']}", headers=auth(token))
    assert (await search(client, token, "revenue increased", mode="keyword"))["results"] == []

    # A new contract version replaces the old text in search results.
    resp = await client.post(
        f"/v1/documents/{corpus['contract']['document_id']}/versions",
        files={
            "file": (
                "v2.pdf",
                factories.text_pdf(["Reference INV-2026-0099 only."]),
                "application/pdf",
            )
        },
        headers=auth(token),
    )
    assert resp.status_code == 201
    await drain(worker)
    assert (await search(client, token, "INV-2025-0042", mode="keyword"))["results"] == []
    (hit,) = (await search(client, token, "INV-2026-0099", mode="keyword"))["results"]
    assert hit["version_no"] == 2


async def test_reprocessing_does_not_duplicate_chunks(client, corpus, worker, owner_conn):
    doc = corpus["contract"]["document_id"]
    await client.post(f"/v1/documents/{doc}/process", headers=auth(corpus["token"]))
    await drain(worker)
    assert await owner_conn.fetchval("SELECT count(*) FROM document_chunks") == 5
    assert await owner_conn.fetchval("SELECT count(*) FROM chunk_embeddings") == 5


class LengthReranker:
    """Prefers longer chunks: an order the fused list would never produce on its own."""

    model_name = "fake-length"

    def score(self, query: str, texts: list[str]) -> list[float]:
        return [float(len(t)) for t in texts]


class BrokenReranker:
    model_name = "fake-broken"

    def score(self, query: str, texts: list[str]) -> list[float]:
        raise RuntimeError("model crashed")


async def test_reranker_reorders_results(client, corpus, monkeypatch):
    monkeypatch.setattr(search_service, "get_reranker", lambda _settings: LengthReranker())
    body = await search(client, corpus["token"], "revenue payment termination", limit=3)
    assert body["reranked"] is True
    lengths = [len(r["text"]) for r in body["results"]]
    assert lengths == sorted(lengths, reverse=True)
    assert all(r["score"] == r["scores"]["rerank"] for r in body["results"])

    plain = await search(client, corpus["token"], "revenue payment", rerank=False)
    assert plain["reranked"] is False


async def test_reranker_failure_falls_back_to_fused_order(client, corpus, monkeypatch):
    monkeypatch.setattr(search_service, "get_reranker", lambda _settings: BrokenReranker())
    body = await search(client, corpus["token"], "payment terms")
    assert body["reranked"] is False
    assert body["warnings"] == ["rerank_unavailable"]
    assert body["results"][0]["page_number"] == 2


async def test_embedding_outage_degrades_hybrid_and_fails_vector_cleanly(
    client, corpus, monkeypatch
):
    def broken(_settings):
        raise RuntimeError("model files missing")

    monkeypatch.setattr(search_service, "get_embedder", broken)
    hybrid = await search(client, corpus["token"], "payment terms")
    assert hybrid["warnings"] == ["vector_search_unavailable"]
    assert hybrid["results"][0]["scores"]["vector_rank"] is None
    assert hybrid["results"][0]["page_number"] == 2

    resp = await client.post(
        "/v1/search", json={"query": "payment", "mode": "vector"}, headers=auth(corpus["token"])
    )
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "SEARCH_UNAVAILABLE"


async def test_validation_and_auth(client, corpus):
    resp = await client.post("/v1/search", json={"query": ""}, headers=auth(corpus["token"]))
    assert resp.status_code == 422
    resp = await client.post(
        "/v1/search", json={"query": "x", "limit": 500}, headers=auth(corpus["token"])
    )
    assert resp.status_code == 422
    assert (await client.post("/v1/search", json={"query": "x"})).status_code == 401
