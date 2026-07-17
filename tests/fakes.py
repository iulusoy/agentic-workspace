"""Shared fakes for service/API tests: MCP connector and Anthropic runner."""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx

import anthropic


class FakeMcp:
    """Stands in for an initialized mcp.ClientSession."""

    def __init__(self):
        self.calls = []

    async def list_tools(self):
        return SimpleNamespace(
            tools=[
                SimpleNamespace(
                    name="get_phase_guidance",
                    description="MCP guidance tool",
                    inputSchema={"type": "object", "properties": {}},
                )
            ]
        )

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return SimpleNamespace(
            structuredContent=None,
            content=[SimpleNamespace(type="text", text="mcp says hi")],
            isError=False,
        )


@asynccontextmanager
async def fake_mcp_connect(url, headers):
    yield FakeMcp()


class FakeStream:
    """One assistant message: async context manager + event iterator."""

    def __init__(self, events, final):
        self.events = events
        self.final = final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        async def gen():
            for event in self.events:
                yield event

        return gen()

    async def get_final_message(self):
        return self.final


def text_event(text):
    return SimpleNamespace(type="text", text=text)


def final_message(content, input_tokens=10, output_tokens=5):
    return SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            output_tokens=output_tokens,
        ),
        content=content,
    )


class FakeRunner:
    """Mimics tool_runner: yields streams, hands out tool responses between."""

    def __init__(self, turns, error_at=None):
        # turns: list of (FakeStream, tool_response_dict_or_None)
        self.turns = turns
        self.error_at = error_at
        self._responses = [resp for _, resp in turns]

    def __aiter__(self):
        async def gen():
            for i, (stream, _) in enumerate(self.turns):
                if self.error_at == i:
                    raise anthropic.APIError(
                        "boom",
                        httpx.Request("POST", "https://api.anthropic.test"),
                        body=None,
                    )
                yield stream

        return gen()

    async def generate_tool_call_response(self):
        return self._responses.pop(0)


def fake_client_factory(runner):
    def factory(api_key=None, auth_token=None):
        return SimpleNamespace(
            beta=SimpleNamespace(
                messages=SimpleNamespace(tool_runner=lambda **kwargs: runner)
            )
        )

    return factory
