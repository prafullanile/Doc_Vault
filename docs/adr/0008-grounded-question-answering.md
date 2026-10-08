# ADR-0008: Grounded question answering (RAG) with verifiable citations

**Status:** Accepted · Phase 5

## Context
§25-27 call for questions answered from the user's documents, with citations, and require
that the system never invents a source. LLMs readily answer from general knowledge and
fabricate references. The design has to make ungrounded answers hard to produce and easy to
detect.

## Decision

**Pipeline:** question → hybrid search (ADR-0007) → drop weak sources → numbered context →
LLM → citation check → stored answer.

**No relevant sources, no LLM call**
- Sources the cross-encoder scores below `rag_min_rerank_score` (-4.0) are dropped.
- If none remain, the API answers with a fixed "not found" text and status
  `NO_RELEVANT_DOCUMENTS`, without calling the LLM.
- Measured with the real reranker: relevant passages scored +5 to +9 and unrelated ones about
  -11, so -4.0 separates them cleanly. The rule also saves the cost of pointless calls.

**Citations are checked, not trusted**
- The model must cite `[n]`.
- Afterwards, any marker that doesn't match a source actually sent is deleted from the answer.
  Citations can therefore only point at retrieved, tenant-scoped evidence.
- `grounded` is true only if at least one valid citation remains, and is recorded per answer.

**Prompt injection**
- Document text is untrusted. The system prompt says to ignore instructions inside sources,
  and sources sit in the user turn, never in the system turn.
- This mitigates the risk but doesn't eliminate it. Citation checking still bounds what the
  answer can claim as evidence.

**Provider-neutral client**
- One client speaks the OpenAI Chat Completions protocol. That covers OpenAI, Google Gemini
  (through its OpenAI-compatible endpoint) and local Ollama (`LLM_BACKEND`, `LLM_BASE_URL`,
  `LLM_MODEL`).
- Rate limits, 5xx errors and network failures are retried twice with backoff. Authentication
  errors, request errors and an exhausted quota (a 429 with `insufficient_quota`) are not
  retried.
- An empty answer counts as a failure. Reasoning models can spend their whole output budget
  "thinking" and return no text.
- Provider default models age out: Gemini 2.5 Flash closed to new users during development.
  The defaults live in one place and `LLM_MODEL` overrides them.

**Degradation**
- If the LLM is down or not configured (no key), the response still contains the retrieved
  sources, with status `LLM_UNAVAILABLE`, rather than an error (§45).

**Conversations**
- `conversation_id` threads follow-ups. The last 3 answered turns go to the model.
- Retrieval for a follow-up also uses the previous question: a question such as "and in 2024?"
  retrieves nothing on its own.

**Streaming** uses server-sent events:
- `sources` first.
- `delta` events with the answer text as it is generated.
- `done` with the checked, stored answer.

Retrieval runs inside the request. Generation and storage run in the stream with a fresh,
tenant-bound session.

**Audit and cost**
- Every question is stored with its answer, status, model, token usage, latencies and the
  numbered sources it was given (copied, so later reprocessing can't alter past evidence).
- These records are append-only for the API role (INSERT and SELECT only).
- They are the raw data for RAG evaluation and AI cost tracking (§58).

## Consequences
- Answers are only as good as retrieval. If the right page isn't in the top 6 sources, the
  model says it couldn't find the answer, which is the intended failure mode.
- Streamed deltas are raw model output. A removed invalid marker appears only in the final
  `done` answer, and clients should display that one.
- There is no rate limit on `/v1/query` yet, so any member can run up LLM cost. Redis rate
  limiting (Phase 7) is needed before real use.
- English-centric, like search (ADR-0007).
