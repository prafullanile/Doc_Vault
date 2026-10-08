"""Chat model behind one small interface. OpenAI and Ollama both speak the OpenAI Chat
Completions protocol, so one HTTP client covers both; switching is configuration only.

Failures surface as LLMUnavailable after bounded retries, and the caller degrades (§45:
"LLM unavailable → controlled error/fallback").
"""

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx
import structlog

from app.core.config import Settings

log = structlog.get_logger(__name__)

Role = Literal["system", "user", "assistant"]
DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "ollama": "http://localhost:11434/v1",
}
DEFAULT_MODELS = {"openai": "gpt-4.1-mini", "gemini": "gemini-3.8-flash", "ollama": "llama3.2:1b"}
RETRY_STATUSES = {408, 409, 429, 500, 502, 503, 504}


@dataclass(frozen=True)
class ChatMessage:
    role: Role
    content: str


@dataclass
class Completion:
    text: str
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class LLMUnavailable(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class ChatModel(Protocol):
    model: str

    async def complete(self, messages: Sequence[ChatMessage]) -> Completion: ...

    def stream(self, messages: Sequence[ChatMessage]) -> AsyncIterator[str | Completion]:
        """Yields text deltas, then one final Completion with the full text and usage."""
        ...

    async def aclose(self) -> None: ...


class OpenAICompatibleChat:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        model: str,
        timeout: float,
        max_output_tokens: int,
        temperature: float | None,
        max_tokens_param: str = "max_completion_tokens",
        retries: int = 2,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.model = model
        self._max_output_tokens = max_output_tokens
        self._temperature = temperature
        self._max_tokens_param = max_tokens_param
        self._retries = retries
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=httpx.Timeout(timeout, connect=10.0),
            transport=transport,
        )

    def _payload(self, messages: Sequence[ChatMessage], stream: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            self._max_tokens_param: self._max_output_tokens,
        }
        if self._temperature is not None:
            payload["temperature"] = self._temperature
        if stream:
            payload["stream"] = True
            payload["stream_options"] = {"include_usage": True}
        return payload

    async def _post(self, payload: dict[str, Any], stream: bool) -> httpx.Response:
        """Sends with retries on rate limits, server errors and network failures."""
        for attempt in range(self._retries + 1):
            try:
                request = self._client.build_request("POST", "/chat/completions", json=payload)
                response = await self._client.send(request, stream=stream)
            except httpx.TransportError as exc:  # connect/read timeouts, DNS, refused
                reason = f"network error: {type(exc).__name__}"
            else:
                if response.status_code < 400:
                    return response
                body = (await response.aread()).decode(errors="replace")[:300]
                await response.aclose()
                reason = f"HTTP {response.status_code}: {body}"
                # Bad key or unknown model won't fix itself. Neither will an exhausted quota,
                # which OpenAI reports as 429 like an ordinary rate limit.
                if response.status_code not in RETRY_STATUSES or "insufficient_quota" in body:
                    raise LLMUnavailable(reason)
            if attempt < self._retries:
                log.warning("llm_retry", attempt=attempt + 1, reason=reason)
                await asyncio.sleep(2**attempt)
        raise LLMUnavailable(reason)

    async def complete(self, messages: Sequence[ChatMessage]) -> Completion:
        response = await self._post(self._payload(messages, stream=False), stream=False)
        data = response.json()
        try:
            text = data["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise LLMUnavailable(f"unexpected response shape: {str(data)[:200]}") from exc
        if not text.strip():
            # Reasoning models can spend the whole token budget "thinking" and return nothing.
            raise LLMUnavailable("empty answer (output token budget exhausted?)")
        usage = data.get("usage") or {}
        return Completion(
            text=text,
            model=data.get("model", self.model),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
        )

    async def stream(self, messages: Sequence[ChatMessage]) -> AsyncIterator[str | Completion]:
        response = await self._post(self._payload(messages, stream=True), stream=True)
        parts: list[str] = []
        final = Completion(text="", model=self.model)
        try:
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                event = json.loads(data)
                final.model = event.get("model", final.model)
                if usage := event.get("usage"):
                    final.prompt_tokens = usage.get("prompt_tokens")
                    final.completion_tokens = usage.get("completion_tokens")
                for choice in event.get("choices") or []:
                    if delta := (choice.get("delta") or {}).get("content"):
                        parts.append(delta)
                        yield delta
        except httpx.TransportError as exc:
            raise LLMUnavailable(f"stream interrupted: {type(exc).__name__}") from exc
        finally:
            await response.aclose()
        final.text = "".join(parts)
        if not final.text.strip():
            raise LLMUnavailable("empty answer (output token budget exhausted?)")
        yield final

    async def aclose(self) -> None:
        await self._client.aclose()


class FakeChat:
    """Deterministic stand-in for tests: answers with the first sentence of source [1] and
    cites it, or says nothing was found when given no sources."""

    model = "fake-llm"

    def __init__(self) -> None:
        self.calls: list[list[ChatMessage]] = []

    def _answer(self, messages: Sequence[ChatMessage]) -> str:
        prompt = messages[-1].content
        if "[1]" not in prompt:
            return "I could not find this in the provided documents."
        source = prompt.split("[1]", 1)[1].split("\n", 2)[1]  # the line after "[1] file, page"
        sentence = source.strip().split(". ")[0].rstrip(".")
        return f"{sentence} [1]."

    async def complete(self, messages: Sequence[ChatMessage]) -> Completion:
        self.calls.append(list(messages))
        text = self._answer(messages)
        return Completion(text=text, model=self.model, prompt_tokens=100, completion_tokens=10)

    async def stream(self, messages: Sequence[ChatMessage]) -> AsyncIterator[str | Completion]:
        completion = await self.complete(messages)
        words = completion.text.split(" ")
        for i, word in enumerate(words):
            yield word if i == len(words) - 1 else word + " "
        yield completion

    async def aclose(self) -> None:
        return None


def create_chat_model(settings: Settings) -> ChatModel | None:
    """None means "not configured" (e.g. OpenAI selected but no key): answers degrade to
    sources-only instead of failing the request."""
    backend = settings.llm_backend
    if backend == "fake":
        return FakeChat()
    secret = {"openai": settings.openai_api_key, "gemini": settings.gemini_api_key}.get(backend)
    api_key = secret.get_secret_value() if secret else None
    if backend in ("openai", "gemini") and not api_key:
        log.warning("llm_not_configured", hint=f"set {backend.upper()}_API_KEY")
        return None
    return OpenAICompatibleChat(
        base_url=settings.llm_base_url or DEFAULT_BASE_URLS[backend],
        api_key=api_key,
        model=settings.llm_model or DEFAULT_MODELS[backend],
        timeout=settings.llm_timeout_seconds,
        max_output_tokens=settings.llm_max_output_tokens,
        temperature=settings.llm_temperature,
        # OpenAI's newer models only accept max_completion_tokens; Gemini's compatibility
        # layer and Ollama expect the classic max_tokens.
        max_tokens_param="max_completion_tokens" if backend == "openai" else "max_tokens",
    )
