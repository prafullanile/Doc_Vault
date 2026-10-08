# ADR-0007: Hybrid search in PostgreSQL with local models

**Status:** Accepted · Phase 4

## Context
§8 and §23–24 call for keyword search, vector search, hybrid merging and reranking. They should
start in PostgreSQL with pgvector, without adding a separate vector database (§5), and work
without paid APIs (§50).

## Decision

**Chunking**
- Chunks never cross a page boundary, so every search hit cites exactly one page.
- Chunks are about 180 words, with about 30 words of overlap, broken at sentence boundaries.
  Text without punctuation (tables, OCR output) is split on words instead.
- The chunker version is part of `pipeline_version`.

**Models.** Both run locally on CPU through fastembed (ONNX Runtime), with no PyTorch:

| Role | Model | Size |
|---|---|---|
| Embeddings | `BAAI/bge-small-en-v1.5`, 384-d | 67 MB |
| Reranking (cross-encoder) | `Xenova/ms-marco-MiniLM-L-6-v2` | 80 MB |

- Both sit behind small `Embedder` and `Reranker` interfaces.
- Tests use a deterministic hashing embedder, so they need no model download. A separate test,
  enabled in CI, checks the real models.
- The Docker image bakes the model files in and runs offline (`HF_HUB_OFFLINE=1`).

**Storage**
- `document_chunks` holds a generated `tsvector` column (english configuration) with a GIN
  index.
- `chunk_embeddings` holds `vector(384)` with an HNSW cosine index, keyed by
  `(chunk_id, model)`. A same-sized replacement model can be backfilled alongside the current
  one and switched over without downtime.

**Retrieval**
- **Keyword search:** `websearch_to_tsquery`, which accepts "quoted phrases", OR and -exclusions
  and never fails on user input.
- **Vector search:** HNSW with `hnsw.iterative_scan = relaxed_order` (pgvector ≥ 0.8). HNSW
  filters *after* walking the graph, so without the iterative scan a small tenant in a large
  index could get too few results.
- **Fusion:** Reciprocal Rank Fusion (k = 60). It uses ranks, not raw scores, because the two
  score scales are not comparable.
- **Reranking:** the cross-encoder rescores the top 30 fused candidates, and the top *k* are
  returned.
- **Explainability:** every result reports its keyword rank, vector rank, fused score and
  rerank score, plus per-step timings.

**Scope and isolation**
- Search covers only the current version of non-deleted documents.
- Every query filters by `tenant_id` explicitly, and RLS applies on the tenant-bound session.

**Degradation**
- If the embedder fails, hybrid search becomes keyword-only, with a warning. Vector-only search
  returns `503 SEARCH_UNAVAILABLE`.
- If the reranker fails, the fused order is kept, with a warning.

## Consequences
- No extra datastore: search, metadata and tenant isolation share PostgreSQL transactions and
  RLS.
- **English-centric:** the full-text configuration and the embedding model are English.
  Multilingual support needs a multilingual model and per-language `tsvector` configurations.
  Language is already detected per version.
- Re-embedding with a model of a *different* dimension needs a migration (a new column or
  table). The application refuses to start a mismatched model.
- Query embedding and reranking run in the API process on CPU (tens of milliseconds). At
  higher load they could move to a dedicated ML service (§48 `ml-service/`) behind the same
  interfaces.
- OpenSearch or Qdrant stay deferred until measurements show PostgreSQL is the bottleneck.
