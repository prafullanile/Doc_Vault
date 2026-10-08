import itertools
import math
import os
import uuid

import pytest

from app.core.config import Settings
from app.search.chunking import Chunk, PageText, chunk_pages, estimate_tokens
from app.search.embeddings import EMBEDDING_DIM, HashingEmbedder, get_embedder, get_reranker
from app.search.fusion import reciprocal_rank_fusion


def words(n: int, prefix: str = "w") -> str:
    return " ".join(f"{prefix}{i}" for i in range(n))


# --- chunking ----------------------------------------------------------------------------------


def test_short_pages_become_one_chunk_each_and_blank_pages_none():
    chunks = chunk_pages(
        [PageText(1, "First page."), PageText(2, "   "), PageText(3, "Third page.", "Annex")],
        target_words=50,
        overlap_words=10,
    )
    assert [(c.position, c.page_number, c.section, c.text) for c in chunks] == [
        (0, 1, None, "First page."),
        (1, 3, "Annex", "Third page."),
    ]


def test_chunks_never_cross_pages():
    pages = [PageText(1, words(70, "a")), PageText(2, words(70, "b"))]
    chunks = chunk_pages(pages, target_words=50, overlap_words=10)
    for chunk in chunks:
        prefixes = {w[0] for w in chunk.text.split()}
        assert prefixes == ({"a"} if chunk.page_number == 1 else {"b"})


def test_long_text_splits_on_sentences_with_overlap():
    sentences = [f"Sentence {i} has exactly six words." for i in range(30)]  # 180 words
    chunks = chunk_pages([PageText(1, " ".join(sentences))], target_words=60, overlap_words=12)
    assert len(chunks) >= 3
    for chunk in chunks:
        assert chunk.text.endswith(".")  # broke on a sentence boundary
        assert len(chunk.text.split()) <= 60 + 12
    for previous, current in itertools.pairwise(chunks):
        # Overlap: the next chunk opens with the previous chunk's trailing sentences.
        last_sentence = "Sentence" + previous.text.rsplit("Sentence", 1)[1]
        first_sentence = current.text.split(".")[0] + "."
        assert last_sentence in current.text
        assert first_sentence in previous.text


def test_text_without_punctuation_is_split_on_words():
    chunks = chunk_pages([PageText(1, words(250))], target_words=100, overlap_words=0)
    assert [len(c.text.split()) for c in chunks] == [100, 100, 50]
    assert " ".join(c.text for c in chunks) == words(250)  # nothing lost


def test_every_word_is_covered():
    text = " ".join(f"Fact number {i} is here." for i in range(80))
    chunks = chunk_pages([PageText(1, text)], target_words=40, overlap_words=8)
    covered = set(" ".join(c.text for c in chunks).split())
    assert covered == set(text.split())


def test_token_estimate_and_validation():
    assert estimate_tokens(100) == 130
    chunk = chunk_pages([PageText(1, words(10))], 50, 5)[0]
    assert isinstance(chunk, Chunk)
    assert chunk.token_count == 13
    with pytest.raises(ValueError):
        chunk_pages([], target_words=10, overlap_words=10)


# --- reciprocal rank fusion --------------------------------------------------------------------


def test_rrf_rewards_agreement_between_retrievers():
    a, b, c, d = (uuid.uuid4() for _ in range(4))
    fused = reciprocal_rank_fusion({"keyword": [a, b, c], "vector": [c, d, a]})
    order = [h.chunk_id for h in fused]
    # a (ranks 1 and 3) and c (ranks 3 and 1) were found by both and beat b and d.
    assert set(order[:2]) == {a, c}
    assert fused[0].ranks.keys() == {"keyword", "vector"}
    assert math.isclose(fused[0].score, 1 / 61 + 1 / 63)
    assert {h.chunk_id: h.ranks for h in fused}[d] == {"vector": 2}


def test_rrf_single_list_keeps_order():
    ids = [uuid.uuid4() for _ in range(5)]
    assert [h.chunk_id for h in reciprocal_rank_fusion({"keyword": ids})] == ids
    assert reciprocal_rank_fusion({}) == []


# --- embeddings --------------------------------------------------------------------------------


def cosine(u: list[float], v: list[float]) -> float:
    return sum(x * y for x, y in zip(u, v, strict=True))


def test_hashing_embedder_is_deterministic_normalised_and_word_sensitive():
    embedder = HashingEmbedder()
    doc = embedder.embed_documents(["Revenue increased by twenty percent"])[0]
    assert len(doc) == EMBEDDING_DIM
    assert math.isclose(math.sqrt(sum(x * x for x in doc)), 1.0)
    assert embedder.embed_query("Revenue increased by twenty percent") == doc
    related = embedder.embed_query("how much revenue increased")
    unrelated = embedder.embed_query("football match tonight")
    assert cosine(doc, related) > cosine(doc, unrelated)


def test_model_factories_respect_settings():
    settings = Settings(jwt_secret="x" * 40, embedding_backend="hashing", reranker_backend="none")
    assert get_embedder(settings).model_name == "hashing-v1"
    assert get_reranker(settings) is None


@pytest.mark.skipif(
    os.environ.get("RUN_MODEL_TESTS") != "1",
    reason="Downloads ~150 MB of models; set RUN_MODEL_TESTS=1 (CI does)",
)
def test_real_models_capture_meaning_not_just_words():
    settings = Settings(jwt_secret="x" * 40)  # defaults: fastembed bge-small + MiniLM reranker
    embedder = get_embedder(settings)
    doc = embedder.embed_documents(["The company's revenue grew by 23 percent this year."])[0]
    paraphrase = embedder.embed_query("How much did sales income increase?")
    unrelated = embedder.embed_query("Who won the football match yesterday?")
    assert cosine(doc, paraphrase) > cosine(doc, unrelated) + 0.1

    reranker = get_reranker(settings)
    assert reranker is not None
    scores = reranker.score(
        "What is the termination notice period?",
        ["Lunch is served at noon.", "Either party may terminate with 90 days written notice."],
    )
    assert scores[1] > scores[0]
