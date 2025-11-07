import pytest
from unittest.mock import AsyncMock, MagicMock
from app.core.security.handlers.master import GetApiKeyMaster
from app.core.security.handlers.any import GetApiKeyAny
from app.core.security.handlers.bots import GetApiKeyBots
from app.core.security.get_api_key import BadAPIKeyException
from CriadexSDK.ragflow_schemas import AuthCheckResponse, GroupAuthCheckResponse

@pytest.mark.asyncio
async def test_get_api_key_master_success():
    """Test that a master key is correctly identified."""
    # Arrange
    get_api_key_master = GetApiKeyMaster()
    get_api_key_master.api_key = "master_key"
    get_api_key_master.get_auth = AsyncMock(return_value=AuthCheckResponse(api_key="master_key", master=True, authorized=True))

    # Act
    result = await get_api_key_master.execute()

    # Assert
    assert result == "master_key"

@pytest.mark.asyncio
async def test_get_api_key_master_failure():
    """Test that a non-master key is correctly rejected."""
    # Arrange
    get_api_key_master = GetApiKeyMaster()
    get_api_key_master.api_key = "non_master_key"
    get_api_key_master.get_auth = AsyncMock(return_value=AuthCheckResponse(api_key="non_master_key", master=False, authorized=True))

    # Act & Assert
    with pytest.raises(BadAPIKeyException) as excinfo:
        await get_api_key_master.execute()
    assert excinfo.value.status_code == 401
    assert "API key was not found or is not a master key." in excinfo.value.detail

@pytest.mark.asyncio
async def test_get_api_key_any_master_key():
    """Test that a master key is accepted."""
    # Arrange
    get_api_key_any = GetApiKeyAny()
    get_api_key_any.api_key = "master_key"
    get_api_key_any.get_auth = AsyncMock(return_value={"api_key": "master_key", "master": True, "authorized": True})

    # Act
    result = await get_api_key_any.execute()

    # Assert
    assert result == "master_key"

@pytest.mark.asyncio
async def test_get_api_key_any_non_master_key_stack_trace_disabled():
    """Test that a non-master key is accepted when stack trace is disabled."""
    # Arrange
    get_api_key_any = GetApiKeyAny()
    get_api_key_any.api_key = "non_master_key"
    get_api_key_any.get_auth = AsyncMock(return_value={"api_key": "non_master_key", "master": False, "authorized": True})
    get_api_key_any.criadex = MagicMock()
    get_api_key_any.criadex._error_stacktrace = False

    # Act
    result = await get_api_key_any.execute()

    # Assert
    assert result == "non_master_key"

@pytest.mark.asyncio
async def test_get_api_key_any_non_master_key_stack_trace_enabled():
    """Test that a non-master key is rejected when stack trace is enabled."""
    # Arrange
    get_api_key_any = GetApiKeyAny()
    get_api_key_any.api_key = "non_master_key"
    get_api_key_any.get_auth = AsyncMock(return_value={"api_key": "non_master_key", "master": False, "authorized": True})
    get_api_key_any.criadex = MagicMock()
    get_api_key_any.criadex._error_stacktrace = True

    # Act & Assert
    with pytest.raises(BadAPIKeyException) as excinfo:
        await get_api_key_any.execute()
    assert excinfo.value.status_code == 401
    assert "Only master keys can access stacktraces!" in excinfo.value.detail

@pytest.mark.asyncio
async def test_get_api_key_bots_master_key():
    """Test that a master key is accepted."""
    # Arrange
    get_api_key_bots = GetApiKeyBots()
    get_api_key_bots.api_key = "master_key"
    get_api_key_bots.get_auth = AsyncMock(return_value={"api_key": "master_key", "master": True, "authorized": True})

    # Act
    result = await get_api_key_bots.execute()

    # Assert
    assert result == "master_key"

@pytest.mark.asyncio
async def test_get_api_key_bots_non_master_key_with_access():
    """Test that a non-master key with access is accepted."""
    # Arrange
    get_api_key_bots = GetApiKeyBots()
    get_api_key_bots.api_key = "non_master_key"
    get_api_key_bots.get_auth = AsyncMock(return_value={"api_key": "non_master_key", "master": False, "authorized": True})
    get_api_key_bots.read_bot_name = AsyncMock(return_value="test_bot")
    get_api_key_bots.get_group_auth = AsyncMock(return_value={"master": False, "authorized": True})
    get_api_key_bots.criadex = MagicMock()
    get_api_key_bots.criadex._error_stacktrace = False

    # Act
    result = await get_api_key_bots.execute()

    # Assert
    assert result == "non_master_key"

@pytest.mark.asyncio
async def test_get_api_key_bots_non_master_key_no_access():
    """Test that a non-master key without access is rejected."""
    # Arrange
    get_api_key_bots = GetApiKeyBots()
    get_api_key_bots.api_key = "non_master_key"
    get_api_key_bots.get_auth = AsyncMock(return_value={"api_key": "non_master_key", "master": False, "authorized": True})
    get_api_key_bots.read_bot_name = AsyncMock(return_value="test_bot")
    get_api_key_bots.get_group_auth = AsyncMock(return_value={"master": False, "authorized": False})
    get_api_key_bots.criadex = MagicMock()
    get_api_key_bots.criadex._error_stacktrace = False

    # Act & Assert
    with pytest.raises(BadAPIKeyException) as excinfo:
        await get_api_key_bots.execute()
    assert excinfo.value.status_code == 401
    assert "Your key is not authorized for accessing this bot." in excinfo.value.detail

@pytest.mark.asyncio
async def test_get_api_key_bots_non_master_key_no_bot_name():
    """Test that a non-master key with no bot name is rejected."""
    # Arrange
    get_api_key_bots = GetApiKeyBots()
    get_api_key_bots.api_key = "non_master_key"
    get_api_key_bots.get_auth = AsyncMock(return_value={"api_key": "non_master_key", "master": False, "authorized": True})
    get_api_key_bots.read_bot_name = AsyncMock(return_value=None)
    get_api_key_bots.criadex = MagicMock()
    get_api_key_bots.criadex._error_stacktrace = False

    # Act & Assert
    with pytest.raises(BadAPIKeyException) as excinfo:
        await get_api_key_bots.execute()
    assert excinfo.value.status_code == 400
    assert "Bot name not included in params!" in excinfo.value.detail
