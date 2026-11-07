import asyncio
import os

import pytest

from core.gemini_client import GeminiClient, GeminiError, _assert_allowed
from shared.config import GEMINI_API_KEY


class _FakeInteractions:
    """Records the request body; stands in for the SDK transport only."""

    def __init__(self, reply_text="{}", delay=0.0, exc=None):
        self.calls = []
        self.reply_text, self.delay, self.exc = reply_text, delay, exc

    async def create(self, **body):
        self.calls.append(body)
        await asyncio.sleep(self.delay)
        if self.exc:
            raise self.exc
        return type("I", (), {"id": "v1_test", "status": "completed", "output_text": self.reply_text})()


def _client(**kw):
    fake = _FakeInteractions(**kw)
    sdk = type("SDK", (), {"aio": type("Aio", (), {"interactions": fake})()})()
    return GeminiClient(api_key="unused", client=sdk), fake


async def test_request_uses_thinking_level_and_no_deprecated_params():
    client, fake = _client(reply_text='{"ok": true}')
    reply = await client.generate("hi", role="fast_router", thinking_level="low",
                                  json_schema={"type": "object"}, previous_interaction_id="v1_prev")
    body = fake.calls[0]
    assert body["model"] == "gemini-3.5-flash-lite"
    assert body["generation_config"] == {"thinking_level": "low"}
    assert body["previous_interaction_id"] == "v1_prev"
    assert body["response_format"]["mime_type"] == "application/json"
    for banned in ("temperature", "top_p", "top_k", "thinking_budget"):
        assert banned not in body and banned not in body["generation_config"]
    assert reply.data == {"ok": True} and reply.interaction_id == "v1_test"


def test_forbidden_keys_rejected():
    with pytest.raises(ValueError):
        _assert_allowed({"thinking_level": "low", "temperature": 0.2})


async def test_errors_and_timeouts_become_gemini_error():
    client, _ = _client(exc=ConnectionError("dns"))
    with pytest.raises(GeminiError, match="dns"):
        await client.generate("hi")
    client, _ = _client(delay=1.0)
    with pytest.raises(GeminiError, match="timed out"):
        await client.generate("hi", timeout_s=0.05)
    client, _ = _client(reply_text="not json")
    with pytest.raises(GeminiError, match="invalid JSON"):
        await client.generate("hi", json_schema={"type": "object"})


async def test_missing_key_is_reported():
    with pytest.raises(GeminiError, match="GEMINI_API_KEY"):
        await GeminiClient(api_key="").generate("hi")


@pytest.mark.live
@pytest.mark.skipif(not GEMINI_API_KEY or os.getenv("PREMORTEM_OFFLINE"), reason="needs GEMINI_API_KEY")
async def test_live_round_trip_with_state():
    client = GeminiClient()
    first = await client.generate("Remember the word 'tyrosine'. Reply OK.", role="fast_router",
                                  thinking_level="low", timeout_s=30)
    second = await client.generate("Which word did I ask you to remember? One word.", role="fast_router",
                                   thinking_level="low", previous_interaction_id=first.interaction_id,
                                   timeout_s=30)
    assert "tyrosine" in second.text.lower()
