from fastapi.params import Security

from app.core.app import app as _app  # noqa: F401 - import registers route limit decorators.
from app.controllers import manage
from app.controllers.schemas import (
    BOT_MANAGEMENT_CREATE_RATE_LIMIT,
    BOT_MANAGEMENT_READ_RATE_LIMIT,
    BOT_MANAGEMENT_WRITE_RATE_LIMIT,
    CHAT_RATE_LIMIT,
    CHAT_READ_RATE_LIMIT,
    CHAT_START_RATE_LIMIT,
    CONTENT_READ_RATE_LIMIT,
    CONTENT_WRITE_RATE_LIMIT,
    bot_management_limiter,
    chat_limiter,
    general_limiter,
)
from app.core.security.handlers.bots import GetApiKeyBots


def _assert_route_limit(limiter, route_key: str, expected_limit: str) -> None:
    limits = limiter._route_limits.get(route_key)

    assert limits, f"Missing rate limit for {route_key}"

    limit_item = limits[0].limit
    expected_amount, expected_granularity = expected_limit.split("/", 1)

    assert limit_item.amount == int(expected_amount)
    assert limit_item.GRANULARITY.name == expected_granularity


def test_chat_routes_have_rate_limits() -> None:
    route_limits = {
        "app.controllers.chats.send.execute": CHAT_RATE_LIMIT,
        "app.controllers.chats.query.execute": CHAT_RATE_LIMIT,
        "app.controllers.chats.start.execute": CHAT_START_RATE_LIMIT,
        "app.controllers.chats.end.execute": CHAT_READ_RATE_LIMIT,
        "app.controllers.chats.history.execute": CHAT_READ_RATE_LIMIT,
        "app.controllers.chats.exists.execute": CHAT_READ_RATE_LIMIT,
    }

    for route_key, expected_limit in route_limits.items():
        limiter = general_limiter if route_key.endswith("start.execute") else chat_limiter
        _assert_route_limit(limiter, route_key, expected_limit)


def test_content_routes_have_bot_scoped_rate_limits() -> None:
    route_limits = {
        "app.controllers.content.documents.upload.execute": CONTENT_WRITE_RATE_LIMIT,
        "app.controllers.content.documents.update.execute": CONTENT_WRITE_RATE_LIMIT,
        "app.controllers.content.documents.delete.execute": CONTENT_WRITE_RATE_LIMIT,
        "app.controllers.content.documents.list.execute": CONTENT_READ_RATE_LIMIT,
        "app.controllers.content.questions.upload.execute": CONTENT_WRITE_RATE_LIMIT,
        "app.controllers.content.questions.update.execute": CONTENT_WRITE_RATE_LIMIT,
        "app.controllers.content.questions.delete.execute": CONTENT_WRITE_RATE_LIMIT,
        "app.controllers.content.questions.list.execute": CONTENT_READ_RATE_LIMIT,
    }

    for route_key, expected_limit in route_limits.items():
        _assert_route_limit(bot_management_limiter, route_key, expected_limit)


def test_manage_routes_have_bot_scoped_rate_limits() -> None:
    route_limits = {
        "app.controllers.manage.create.execute": BOT_MANAGEMENT_CREATE_RATE_LIMIT,
        "app.controllers.manage.update.execute": BOT_MANAGEMENT_WRITE_RATE_LIMIT,
        "app.controllers.manage.delete.execute": BOT_MANAGEMENT_WRITE_RATE_LIMIT,
        "app.controllers.manage.about.execute": BOT_MANAGEMENT_READ_RATE_LIMIT,
        "app.controllers.manage.children.execute": BOT_MANAGEMENT_READ_RATE_LIMIT,
        "app.controllers.manage.parents.execute": BOT_MANAGEMENT_READ_RATE_LIMIT,
    }

    for route_key, expected_limit in route_limits.items():
        _assert_route_limit(bot_management_limiter, route_key, expected_limit)


def test_parent_child_manage_routes_require_bot_auth_in_production() -> None:
    for view in (manage.children.view, manage.parents.view):
        assert view.dependencies
        assert isinstance(view.dependencies[0], Security)
        assert isinstance(view.dependencies[0].dependency, GetApiKeyBots)
