"""The default stream() contract: concatenated chunks equal the completion."""

from __future__ import annotations

from paa.models.base import CompletionRequest, Message
from paa.models.echo_provider import EchoProvider
from paa.models.registry import build_provider

from .conftest import RequestRecorder, mock_client, openai_chat_response


async def test_echo_provider_streams_its_completion() -> None:
    provider = EchoProvider(responses={"ping": "pong"})
    request = CompletionRequest(messages=(Message.user("ping"),))

    chunks = [chunk async for chunk in provider.stream(request)]

    assert "".join(chunks) == "pong"


async def test_openai_provider_default_stream_yields_the_body() -> None:
    recorder = RequestRecorder(lambda _req: openai_chat_response("streamed text"))
    provider = build_provider("vllm", client=mock_client(recorder))
    request = CompletionRequest(messages=(Message.user("hi"),))

    chunks = [chunk async for chunk in provider.stream(request)]

    assert "".join(chunks) == "streamed text"


async def test_empty_completion_yields_no_chunks() -> None:
    provider = EchoProvider(responses={"quiet": ""})
    request = CompletionRequest(messages=(Message.user("quiet"),))

    chunks = [chunk async for chunk in provider.stream(request)]

    assert chunks == []
