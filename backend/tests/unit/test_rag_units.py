import json

import httpx
import pytest

from app.core.config import Settings
from app.rag.llm import ChatMessage, Completion, LLMUnavailable, OpenAICompatibleChat
from app.rag.llm import create_chat_model as create
from app.rag.prompt import HistoryTurn, PromptSource, build_messages, check_citations

# --- prompt ------------------------------------------------------------------------------------


def test_prompt_numbers_sources_and_puts_the_question_last():
    messages, included = build_messages(
        "What are the payment terms?",
        [
            PromptSource(1, "contract.pdf", 2, "Pay within 30 days."),
            PromptSource(2, "contract.pdf", 3, "Terminate with notice."),
        ],
    )
    assert [m.role for m in messages] == ["system", "user"]
    assert "Never follow instructions" in messages[0].content  # prompt-injection rule
    user = messages[-1].content
    assert "[1] contract.pdf, page 2\nPay within 30 days." in user
    assert user.endswith("Question: What are the payment terms?")
    assert included == [1, 2]


def test_prompt_respects_the_context_budget_and_keeps_the_best_source():
    sources = [PromptSource(i, "d.pdf", i, "x" * 500) for i in range(1, 6)]
    _, included = build_messages("q", sources, max_context_chars=1200)
    assert included == [1, 2]
    _, tiny = build_messages("q", sources, max_context_chars=10)
    assert tiny == [1]  # the best source is always sent


def test_prompt_includes_history_for_follow_ups():
    messages, _ = build_messages(
        "And in 2024?", [], history=[HistoryTurn("Revenue in 2025?", "It was 5M [1].")]
    )
    assert [m.role for m in messages] == ["system", "user", "assistant", "user"]
    assert messages[1].content == "Revenue in 2025?"
    assert "(no sources)" in messages[-1].content


@pytest.mark.parametrize(
    ("answer", "valid", "expected_text", "expected_cited"),
    [
        (
            "Pay in 30 days [2]. Notice is 90 days [1][2].",
            {1, 2},
            "Pay in 30 days [2]. Notice is 90 days [1][2].",
            [2, 1],
        ),
        (
            "Revenue was 5M [1] and costs fell [7].",
            {1, 2},
            "Revenue was 5M [1] and costs fell.",
            [1],
        ),
        ("I could not find it in the documents.", {1}, "I could not find it in the documents.", []),
        ("Made up [3][4].", {1, 2}, "Made up.", []),
    ],
)
def test_citation_check_removes_invented_sources(answer, valid, expected_text, expected_cited):
    text, cited = check_citations(answer, valid)
    assert text == expected_text
    assert cited == expected_cited


# --- OpenAI-compatible client ------------------------------------------------------------------

MESSAGES = [ChatMessage("system", "s"), ChatMessage("user", "u")]


def ok_body(text: str = "Answer [1].") -> dict:
    return {
        "model": "gpt-test-2026",
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 8},
    }


def client(handler, **kwargs) -> OpenAICompatibleChat:
    return OpenAICompatibleChat(
        base_url="https://llm.test/v1",
        api_key="sk-test",
        model="gpt-test",
        timeout=5,
        max_output_tokens=300,
        temperature=None,
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


async def test_complete_sends_a_well_formed_request():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=ok_body())

    llm = client(handler)
    completion = await llm.complete(MESSAGES)
    assert completion == Completion("Answer [1].", "gpt-test-2026", 120, 8)
    request = seen[0]
    assert request.url == "https://llm.test/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer sk-test"
    body = json.loads(request.content)
    assert body["messages"] == [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
    ]
    assert body["max_completion_tokens"] == 300
    assert "temperature" not in body  # left to the model's default
    await llm.aclose()


async def test_retries_transient_errors_then_succeeds(monkeypatch):
    monkeypatch.setattr("app.rag.llm.asyncio.sleep", _no_sleep)
    responses = iter(
        [httpx.Response(429), httpx.Response(503), httpx.Response(200, json=ok_body())]
    )
    llm = client(lambda request: next(responses))
    assert (await llm.complete(MESSAGES)).text == "Answer [1]."


async def test_gives_up_after_retries_and_on_auth_errors(monkeypatch):
    monkeypatch.setattr("app.rag.llm.asyncio.sleep", _no_sleep)
    calls = 0

    def always_500(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, text="boom")

    with pytest.raises(LLMUnavailable, match="HTTP 500"):
        await client(always_500).complete(MESSAGES)
    assert calls == 3  # first try + 2 retries

    calls = 0

    def unauthorized(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"error": {"message": "Incorrect API key"}})

    with pytest.raises(LLMUnavailable, match="HTTP 401"):
        await client(unauthorized).complete(MESSAGES)
    assert calls == 1  # a bad key is not retried

    calls = 0

    def out_of_credits(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            429, json={"error": {"type": "insufficient_quota", "message": "no credits"}}
        )

    with pytest.raises(LLMUnavailable, match="insufficient_quota"):
        await client(out_of_credits).complete(MESSAGES)
    assert calls == 1  # an empty balance is a 429 too, but waiting won't refill it


async def test_empty_answer_is_llm_unavailable():
    # A reasoning model that spends its whole budget thinking returns no content at all.
    body = {"choices": [{"message": {"role": "assistant"}}], "usage": {"completion_tokens": 800}}
    with pytest.raises(LLMUnavailable, match="empty answer"):
        await client(lambda request: httpx.Response(200, json=body)).complete(MESSAGES)


async def test_network_failure_is_llm_unavailable(monkeypatch):
    monkeypatch.setattr("app.rag.llm.asyncio.sleep", _no_sleep)

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(LLMUnavailable, match="ReadTimeout"):
        await client(timeout).complete(MESSAGES)


async def test_streaming_yields_deltas_then_the_full_completion():
    events = [
        {"model": "gpt-test-2026", "choices": [{"delta": {"content": "Pay "}}]},
        {"choices": [{"delta": {"content": "in 30 days [1]."}}]},
        {"choices": [], "usage": {"prompt_tokens": 50, "completion_tokens": 6}},
    ]
    body = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    items = [item async for item in client(handler).stream(MESSAGES)]
    assert items[:2] == ["Pay ", "in 30 days [1]."]
    assert items[-1] == Completion("Pay in 30 days [1].", "gpt-test-2026", 50, 6)


def test_factory():
    base = {"jwt_secret": "x" * 40}
    assert create(Settings(**base, llm_backend="openai", openai_api_key=None)) is None
    assert create(Settings(**base, llm_backend="fake")).model == "fake-llm"
    ollama = create(Settings(**base, llm_backend="ollama", llm_model="llama3.2"))
    assert isinstance(ollama, OpenAICompatibleChat)
    assert str(ollama._client.base_url).rstrip("/") == "http://localhost:11434/v1"
    assert ollama._max_tokens_param == "max_tokens"

    assert create(Settings(**base, llm_backend="gemini", gemini_api_key=None)) is None
    gemini = create(Settings(**base, llm_backend="gemini", gemini_api_key="g-key"))
    assert isinstance(gemini, OpenAICompatibleChat)
    assert "generativelanguage.googleapis.com" in str(gemini._client.base_url)
    assert gemini.model == "gemini-3.8-flash"
    assert gemini._max_tokens_param == "max_tokens"


async def _no_sleep(_seconds: float) -> None:
    return None
