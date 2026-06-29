import time
from contextlib import asynccontextmanager
from unittest.mock import MagicMock

import pytest

from criabot.cache.ttl import parse_time_to_seconds
from criabot.cache.objects.chats import Chats, ChatModel
from criabot.cache.objects.gradebooks import Gradebooks, GradebookSessionModel
from criabot.cache.objects.web_searches import WebSearches
from criabot.cache.objects.reranks import Reranks
from criabot.cache.objects.faq_searches import FaqSearches
from CriadexSDK.ragflow_schemas import GroupSearchResponse, TextNodeWithScore, TextNode


class FakeRedis:
    """Minimal in-memory async stand-in for the Redis client."""

    def __init__(self):
        self.store = {}
        self.expirations = {}

    async def set(self, key, value, ex=None):
        self.store[key] = value
        if ex is not None:
            self.expirations[key] = ex

    async def get(self, key):
        value = self.store.get(key)
        if value is None:
            return None
        return value.encode("utf-8") if isinstance(value, str) else value

    async def delete(self, key):
        self.store.pop(key, None)
        self.expirations.pop(key, None)

    async def exists(self, key):
        return 1 if key in self.store else 0

    async def expire(self, key, ttl):
        if key in self.store:
            self.expirations[key] = ttl
            return True
        return False

    async def getex(self, key, ex=None):
        """Atomic GET + EXPIRE (mirrors Redis GETEX)."""
        value = await self.get(key)
        if value is not None and ex is not None:
            self.expirations[key] = ex
        return value


def bind_fake_redis(cache_object, fake):
    @asynccontextmanager
    async def _cm():
        yield fake

    cache_object.redis = _cm
    return cache_object


# --------------------------------------------------------------------------- #
# parse_time_to_seconds
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "value, expected",
    [
        ("1h", 3600),
        ("4h", 4 * 3600),
        ("7d", 7 * 24 * 3600),
        ("2w", 2 * 7 * 24 * 3600),
        ("1m", 30 * 24 * 3600),   # months, not minutes (preserved semantics)
        ("1y", 365 * 24 * 3600),
        ("", 3600),               # default
        ("garbage", 3600),        # malformed -> default
        ("10x", 3600),            # unknown unit -> default
    ],
)
def test_parse_time_to_seconds(value, expected):
    assert parse_time_to_seconds(value) == expected


def test_parse_time_to_seconds_custom_default():
    assert parse_time_to_seconds("", default=42) == 42


# --------------------------------------------------------------------------- #
# Chats cache object
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_chats_set_uses_namespaced_key():
    fake = FakeRedis()
    chats = bind_fake_redis(Chats(pool=MagicMock()), fake)

    await chats.set(chat_id="abc", chat_model=ChatModel(started_at=int(time.time())))

    assert "chat:abc" in fake.store
    assert "abc" not in fake.store


@pytest.mark.asyncio
async def test_chats_get_round_trip():
    fake = FakeRedis()
    chats = bind_fake_redis(Chats(pool=MagicMock()), fake)
    model = ChatModel(started_at=123)

    await chats.set(chat_id="abc", chat_model=model)
    fetched = await chats.get(chat_id="abc")

    assert isinstance(fetched, ChatModel)
    assert fetched.started_at == 123


@pytest.mark.asyncio
async def test_chats_get_missing_returns_none():
    chats = bind_fake_redis(Chats(pool=MagicMock()), FakeRedis())
    assert await chats.get(chat_id="nope") is None


@pytest.mark.asyncio
async def test_chats_exists_uses_native_exists_without_deserializing():
    fake = FakeRedis()
    chats = bind_fake_redis(Chats(pool=MagicMock()), fake)

    # Inject a value that would be invalid JSON; native EXISTS must not parse it.
    fake.store["chat:abc"] = "not-json"

    assert await chats.exists(chat_id="abc") is True
    assert await chats.exists(chat_id="missing") is False


@pytest.mark.asyncio
async def test_chats_delete_uses_namespaced_key():
    fake = FakeRedis()
    chats = bind_fake_redis(Chats(pool=MagicMock()), fake)
    await chats.set(chat_id="abc", chat_model=ChatModel(started_at=1))

    await chats.delete(chat_id="abc")

    assert "chat:abc" not in fake.store


# --------------------------------------------------------------------------- #
# Gradebooks cache object
# --------------------------------------------------------------------------- #

def _session_model() -> GradebookSessionModel:
    return GradebookSessionModel(
        session_id="s1",
        course_id="c1",
        professor_id="p1",
        bot_name="bot",
        phase="intake",
    )


@pytest.mark.asyncio
async def test_gradebooks_set_uses_namespaced_key():
    fake = FakeRedis()
    gradebooks = bind_fake_redis(Gradebooks(pool=MagicMock()), fake)

    await gradebooks.set(session_id="s1", session_model=_session_model())

    assert "gb:s1" in fake.store


@pytest.mark.asyncio
async def test_gradebooks_round_trip_and_exists():
    fake = FakeRedis()
    gradebooks = bind_fake_redis(Gradebooks(pool=MagicMock()), fake)

    await gradebooks.set(session_id="s1", session_model=_session_model())
    fetched = await gradebooks.get(session_id="s1")

    assert isinstance(fetched, GradebookSessionModel)
    assert fetched.course_id == "c1"
    assert await gradebooks.exists(session_id="s1") is True
    assert await gradebooks.exists(session_id="other") is False


# --------------------------------------------------------------------------- #
# WebSearches cache object
# --------------------------------------------------------------------------- #

def test_web_search_build_key_is_normalized_and_stable():
    a = WebSearches.build_key("  Hello   World ", "en-US", 5)
    b = WebSearches.build_key("hello world", "en-us", 5)
    assert a == b

    # Different inputs must produce different keys.
    assert WebSearches.build_key("hello world", "fr", 5) != a
    assert WebSearches.build_key("hello world", "en-us", 10) != a


@pytest.mark.asyncio
async def test_web_searches_round_trip():
    fake = FakeRedis()
    web = bind_fake_redis(WebSearches(pool=MagicMock()), fake)
    payload = [{"node": {"text": "x"}, "score": 0.9}]

    await web.set(cache_key="k1", nodes_payload=payload)

    assert any(key.startswith("websearch:") for key in fake.store)
    assert await web.get(cache_key="k1") == payload
    assert await web.exists(cache_key="k1") is True


@pytest.mark.asyncio
async def test_web_searches_get_missing_returns_none():
    web = bind_fake_redis(WebSearches(pool=MagicMock()), FakeRedis())
    assert await web.get(cache_key="missing") is None


@pytest.mark.asyncio
async def test_chats_get_touches_expiry_by_default():
    fake = FakeRedis()
    chats = bind_fake_redis(Chats(pool=MagicMock()), fake)
    await chats.set(chat_id="abc", chat_model=ChatModel(started_at=1), ex=60)
    fake.expirations["chat:abc"] = 60

    await chats.get(chat_id="abc")

    # Touch should refresh TTL to the configured chat expiry (not the stale 60s).
    from criabot.cache.objects.chats import CHAT_EXPIRE_TIME
    assert fake.expirations.get("chat:abc") == CHAT_EXPIRE_TIME


@pytest.mark.asyncio
async def test_chats_get_can_skip_touch():
    fake = FakeRedis()
    chats = bind_fake_redis(Chats(pool=MagicMock()), fake)
    await chats.set(chat_id="abc", chat_model=ChatModel(started_at=1), ex=120)
    fake.expirations.clear()

    await chats.get(chat_id="abc", touch=False)

    assert "chat:abc" not in fake.expirations


# --------------------------------------------------------------------------- #
# Reranks cache object
# --------------------------------------------------------------------------- #

def _sample_node(text="doc"):
    return TextNodeWithScore(
        node=TextNode(text=text, metadata={}, text_template="", metadata_template="", class_name="TextNode"),
        score=0.8,
    )


def test_rerank_build_key_stable_for_same_inputs():
    nodes = [_sample_node("alpha")]
    a = Reranks.build_key(prompt="Hello", rerank_model_id=1, top_n=3, min_n=1, nodes=nodes)
    b = Reranks.build_key(prompt="  hello ", rerank_model_id=1, top_n=3, min_n=1, nodes=nodes)
    assert a == b


@pytest.mark.asyncio
async def test_reranks_round_trip():
    fake = FakeRedis()
    reranks = bind_fake_redis(Reranks(pool=MagicMock()), fake)
    payload = [_sample_node("x").model_dump(mode="json")]

    await reranks.set(cache_key="rk1", ranked_nodes_payload=payload)

    assert any(k.startswith("rerank:") for k in fake.store)
    assert await reranks.get(cache_key="rk1") == payload


# --------------------------------------------------------------------------- #
# FaqSearches cache object
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_faq_searches_round_trip_group_response():
    fake = FakeRedis()
    faq = bind_fake_redis(FaqSearches(pool=MagicMock()), fake)
    payload = {
        "group_name": "faq-group",
        "response": GroupSearchResponse(nodes=[], assets=[], search_units=0, metadata={}),
        "sources": [],
        "graph_metadata": None,
    }

    await faq.set(cache_key="fk1", payload=payload)
    restored = await faq.get(cache_key="fk1")

    assert isinstance(restored["response"], GroupSearchResponse)
    assert restored["group_name"] == "faq-group"
