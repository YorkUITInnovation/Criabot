import pytest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from criabot.criabot import Criabot, BotExistsError
from criabot.schemas import (
    CriadexCredentials, MySQLCredentials, RedisCredentials, BotCreateConfig,
    BotNotFoundError, ParentNotFoundError, CircularDependencyError
)
from criabot.database.table import BaseTable
from criabot.database.bots.tables.bots import BotsModel
from criabot.database.bots.tables.bots import BotsModel

@pytest.fixture
def criadex_credentials():
    return CriadexCredentials(api_base="http://localhost", api_key="test_key", master_api_key="master_key")

@pytest.fixture
def mysql_credentials():
    return MySQLCredentials(host="localhost", port=3306, username="root", password="cria", database="criabot")

@pytest.fixture
def redis_credentials():
    return RedisCredentials(host="localhost", port=6379, username="testuser", password="testpass")

@pytest.fixture
def criabot_instance(criadex_credentials, mysql_credentials, redis_credentials):
    with (patch('criabot.criabot.RAGFlowSDK') as MockRAGFlowSDK,
        patch('criabot.criabot.BotDatabaseAPI') as MockBotDatabaseAPI,
        patch('criabot.cache.api.BotCacheAPI') as MockBotCacheAPI,
        patch('criabot.criabot.create_async_engine') as MockEngine):
        criabot = Criabot(criadex_credentials, mysql_credentials, redis_credentials)
        criabot._criadex = MockRAGFlowSDK()
        criabot._mysql_api = MockBotDatabaseAPI()
        criabot._mysql_api.bot_models.insert = AsyncMock(return_value=None)
        criabot._redis_api = MockBotCacheAPI()
        yield criabot

@pytest.mark.asyncio
async def test_create_bot(criabot_instance):
    criabot_instance._mysql_api.bots.exists = AsyncMock(return_value=False)
    criabot_instance._criadex.auth.create = AsyncMock(return_value={"api_key": "new_key"})
    criabot_instance._criadex.manage.create = AsyncMock(return_value=MagicMock())
    criabot_instance._criadex.group_auth.create = AsyncMock()
    criabot_instance._mysql_api.bots.insert = AsyncMock(return_value=1)
    criabot_instance._mysql_api.bot_params.insert = AsyncMock(return_value=None)

    config = BotCreateConfig(llm_model_id=1, embedding_model_id=1, rerank_model_id=1)
    new_auth = await criabot_instance.create(name="new_bot", config=config)

    assert new_auth["api_key"] == "new_key"
    criabot_instance._criadex.auth.create.assert_called_once()
    assert criabot_instance._criadex.manage.create.call_count == 2
    assert criabot_instance._criadex.group_auth.create.call_count == 2
    criabot_instance._mysql_api.bots.insert.assert_called_once()
    criabot_instance._mysql_api.bot_params.insert.assert_called_once()
    for call in criabot_instance._criadex.manage.create.await_args_list:
        payload = call.kwargs["group_config"]
        assert payload["use_knowledge_graph"] is True

@pytest.mark.asyncio
async def test_create_bot_with_parents(criabot_instance):
    criabot_instance._mysql_api.bots.exists = AsyncMock(return_value=False)
    criabot_instance._mysql_api.bots.retrieve_id = AsyncMock(return_value=2)
    criabot_instance._mysql_api.bots.retrieve_by_id = AsyncMock(return_value=BotsModel(id=2, name="parent_bot", created=datetime.now()))
    criabot_instance._criadex.auth.create = AsyncMock(return_value={"api_key": "child_key"})
    criabot_instance._criadex.manage.create = AsyncMock(return_value=MagicMock())
    criabot_instance._criadex.group_auth.create = AsyncMock()
    criabot_instance._mysql_api.bots.insert = AsyncMock(return_value=1)
    criabot_instance._mysql_api.bot_params.insert = AsyncMock(return_value=None)
    criabot_instance._mysql_api.bot_parents.get_parent_ids = AsyncMock(return_value=[])
    criabot_instance._mysql_api.bot_parents.insert = AsyncMock(return_value=1)
    criabot_instance._mysql_api.bot_api_keys.get_active_by_bot = AsyncMock(return_value=MagicMock(api_key="parent_key"))
    criabot_instance._authorize_child_on_parent_groups = AsyncMock()
    criabot_instance._authorize_parents_on_child_groups = AsyncMock()
    criabot_instance._assign_parents = AsyncMock()

    config = BotCreateConfig(
        llm_model_id=1,
        embedding_model_id=1,
        rerank_model_id=1,
        parent_bot_names=["parent_bot"]
    )
    new_auth = await criabot_instance.create(name="child_bot", config=config)

    assert new_auth["api_key"] == "child_key"
    criabot_instance._assign_parents.assert_called_once()
    criabot_instance._authorize_child_on_parent_groups.assert_called_once()
    criabot_instance._authorize_parents_on_child_groups.assert_called_once()

@pytest.mark.asyncio
async def test_about_with_parents_and_children(criabot_instance):
    from criabot.database.bots.tables.bot_params import BotParametersModel
    
    now = datetime.now()
    criabot_instance._mysql_api.bots.retrieve = AsyncMock(return_value=BotsModel(id=1, name="test_bot", created=now))
    criabot_instance._mysql_api.bot_params.retrieve = AsyncMock(return_value=BotParametersModel(
        id=1, bot_id=1, max_input_tokens=2000, max_reply_tokens=1024, temperature=0.9,
        top_p=0.0, top_k=10, min_k=0.5, top_n=3, min_n=0.7,
        llm_generate_related_prompts=True, no_context_message="Test",
        no_context_use_message=False, no_context_llm_guess=False, system_message="Test system"
    ))
    criabot_instance._mysql_api.bot_parents.get_parent_ids = AsyncMock(return_value=[2])
    criabot_instance._mysql_api.bot_parents.get_by_child = AsyncMock(return_value=[
        MagicMock(parent_bot_id=2, priority=0)
    ])
    criabot_instance._mysql_api.bots.retrieve_by_ids = AsyncMock(side_effect=lambda bot_ids: {
        tuple([2]): [BotsModel(id=2, name="parent_bot", created=now)],
        tuple([3, 4]): [
            BotsModel(id=3, name="child1", created=now),
            BotsModel(id=4, name="child2", created=now)
        ]
    }[tuple(bot_ids)])
    criabot_instance._mysql_api.bot_params.retrieve_by_bot_ids = AsyncMock(return_value=[
        BotParametersModel(
            id=2, bot_id=2, max_input_tokens=3000, max_reply_tokens=2048, temperature=0.8,
            top_p=0.1, top_k=15, min_k=0.6, top_n=5, min_n=0.8,
            llm_generate_related_prompts=False, no_context_message="Parent",
            no_context_use_message=True, no_context_llm_guess=True, system_message="Parent system"
        )
    ])
    criabot_instance._mysql_api.bot_parents.get_child_ids = AsyncMock(return_value=[3, 4])

    result = await criabot_instance.about("test_bot")

    assert result.parent_bot_names == ["parent_bot"]
    assert result.children == ["child1", "child2"]
    assert result.effective_config is not None

@pytest.mark.asyncio
async def test_get_parent_bot_names(criabot_instance):
    now = datetime.now()
    criabot_instance.get_id = AsyncMock(return_value=1)
    criabot_instance._mysql_api.bot_parents.get_parent_ids = AsyncMock(return_value=[2, 3])
    criabot_instance._mysql_api.bots.retrieve_by_id = AsyncMock(side_effect=lambda bot_id: {
        2: BotsModel(id=2, name="parent1", created=now),
        3: BotsModel(id=3, name="parent2", created=now)
    }.get(bot_id))

    result = await criabot_instance.get_parent_bot_names("child_bot")

    assert result == ["parent1", "parent2"]

@pytest.mark.asyncio
async def test_update_parameters(criabot_instance):
    from criabot.database.bots.tables.bot_params import BotParametersBaseConfig
    
    criabot_instance._mysql_api.bots.retrieve_id = AsyncMock(return_value=1)
    criabot_instance._mysql_api.bot_params.update = AsyncMock()

    params = BotParametersBaseConfig(temperature=0.8, top_k=15)
    await criabot_instance.update_parameters("test_bot", params)

    criabot_instance._mysql_api.bot_params.update.assert_called_once_with(bot_id=1, config=params)

@pytest.mark.asyncio
async def test_update_parameters_bot_not_found(criabot_instance):
    from criabot.database.bots.tables.bot_params import BotParametersBaseConfig
    
    criabot_instance._mysql_api.bots.retrieve_id = AsyncMock(return_value=None)

    params = BotParametersBaseConfig(temperature=0.8)
    with pytest.raises(BotNotFoundError):
        await criabot_instance.update_parameters("nonexistent_bot", params)

@pytest.mark.asyncio
async def test_create_bot_that_exists(criabot_instance):
    criabot_instance._mysql_api.bots.exists = AsyncMock(return_value=True)
    with pytest.raises(BotExistsError):
        await criabot_instance.create(name="existing_bot", config=MagicMock())


@pytest.mark.asyncio
async def test_create_new_bot_auth_group_retries_when_group_not_ready(criabot_instance):
    criabot_instance._criadex.group_auth.create = AsyncMock(
        side_effect=[
            Exception('[404] {"code":"GROUP_NOT_FOUND","message":"Group not found"}'),
            {"status": 200},
        ]
    )

    result = await criabot_instance._create_new_bot_auth_group(
        group_name="child_bot-document-index",
        bot_api_key="child_key",
    )

    assert result == {"status": 200}
    assert criabot_instance._criadex.group_auth.create.await_count == 2


@pytest.mark.asyncio
async def test_create_bot_rollback_does_not_delete_preexisting_groups(criabot_instance):
    criabot_instance._mysql_api.bots.exists = AsyncMock(return_value=False)
    criabot_instance._criadex.auth.create = AsyncMock(return_value={"api_key": "new_key"})
    criabot_instance._criadex.auth.delete = AsyncMock()
    criabot_instance._criadex.manage.delete = AsyncMock()
    criabot_instance._create_new_bot_groups = AsyncMock(
        return_value=(
            {"group_name": "existing_bot-question-index", "created": False},
            {"group_name": "existing_bot-document-index", "created": False},
        )
    )
    criabot_instance._mysql_api.bots.insert = AsyncMock(return_value=1)
    criabot_instance._mysql_api.bot_params.insert = AsyncMock(side_effect=Exception("boom"))
    criabot_instance._mysql_api.bot_params.delete = AsyncMock()
    criabot_instance._mysql_api.bots.delete = AsyncMock()

    config = BotCreateConfig(llm_model_id=1, embedding_model_id=1, rerank_model_id=1)

    with pytest.raises(Exception, match="boom"):
        await criabot_instance.create(name="existing_bot", config=config)

    criabot_instance._criadex.manage.delete.assert_not_called()
    criabot_instance._criadex.auth.delete.assert_awaited_once_with(api_key="new_key")


@pytest.mark.asyncio
async def test_table_creation_on_initialize(criabot_instance):
    with patch('criabot.criabot.create_async_engine') as mock_create_async_engine, \
         patch('criabot.criabot.BotDatabaseAPI') as MockBotDatabaseAPI:

        # Mocks for the first engine (init_engine)
        mock_init_engine = MagicMock()
        mock_init_connection = AsyncMock()
        mock_init_engine.begin.return_value.__aenter__.return_value = mock_init_connection

        # Mocks for the second engine (mysql_engine)
        mock_mysql_engine = MagicMock()

        mock_create_async_engine.side_effect = [mock_init_engine, mock_mysql_engine]

        mock_mysql_api = AsyncMock()
        MockBotDatabaseAPI.return_value = mock_mysql_api

        await criabot_instance.initialize()

        assert mock_create_async_engine.call_count == 2
        mock_mysql_api.initialize.assert_called_once()


@pytest.mark.asyncio
async def test_sync_faq_site_uses_crawler_and_indexes(criabot_instance):
    criabot_instance.sync_faq_group = AsyncMock(
        return_value={
            "group_name": "eclass-faq-bot-document-index",
            "uploaded_files": ["faq-page-1", "faq-page-2"],
            "graph_build_job": {"job_id": "job-1"},
        }
    )
    fake_pages = [
        {"url": "https://lthelp.yorku.ca/eclass", "title": "/", "text": "FAQ root"},
        {"url": "https://lthelp.yorku.ca/page-1", "title": "/page-1", "text": "FAQ one"},
    ]

    with patch("criabot.criabot.FAQCrawler") as mock_crawler:
        mock_crawler.return_value.crawl = AsyncMock(return_value=fake_pages)
        result = await criabot_instance.sync_faq_site(max_pages=2)

    assert result["pages_crawled"] == 2
    criabot_instance.sync_faq_group.assert_awaited_once()
    sync_args = criabot_instance.sync_faq_group.call_args[1]
    assert sync_args["documents"][0].file_contents["nodes"][0]["type"] == "UncategorizedText"
    assert sync_args["documents"][0].file_contents["nodes"][0]["metadata"] == {}
    status = criabot_instance.get_faq_sync_status()
    assert status["state"] == "READY"
    assert status["indexed_files"] == 2


def test_update_faq_sync_config(criabot_instance):
    updated = criabot_instance.update_faq_sync_config(
        source_url="https://lthelp.yorku.ca/eclass",
        group_name="custom-faq-group",
        max_pages=10,
        timeout_seconds=15,
    )
    assert updated["source_url"] == "https://lthelp.yorku.ca/eclass"
    assert updated["group_name"] == "custom-faq-group"
    assert updated["max_pages"] == 10
    assert updated["timeout_seconds"] == 15.0


@pytest.mark.asyncio
async def test_gradebook_session_flow(criabot_instance):
    start = await criabot_instance.start_gradebook_session(
        course_id="EECS-1234-F2026",
        professor_id="prof_jsmith",
        bot_name="eecs-1234-bot",
        moodle_resources=[{"name": "Course Syllabus.pdf", "content_preview": "Assignments 25%, Midterm 30%, Final 30%"}],
        course_activities=[{"cmid": 1, "module": "assign", "name": "Homework 1"}],
    )
    assert start["session_id"].startswith("gb-")
    assert start["phase"] == "ANALYSIS"

    session_id = start["session_id"]
    chat = await criabot_instance.gradebook_chat(session_id=session_id, prompt="Please generate proposal")
    assert chat["phase"] in {"PROPOSAL", "ANALYSIS", "REFINEMENT"}
    proposal = await criabot_instance.gradebook_proposal(session_id=session_id)
    assert proposal["proposal"] is not None

    accepted = await criabot_instance.gradebook_accept(session_id=session_id)
    assert accepted["phase"] == "ACCEPTED"
    assert accepted["content_mapping"] is not None


@pytest.mark.asyncio
async def test_gradebook_status_missing_session_raises(criabot_instance):
    with pytest.raises(KeyError):
        await criabot_instance.gradebook_status(session_id="missing-session")


@pytest.mark.asyncio
async def test_sync_faq_site_invalid_url_marks_error_status(criabot_instance):
    with pytest.raises(ValueError):
        await criabot_instance.sync_faq_site(source_url="not-a-valid-url")
    status = criabot_instance.get_faq_sync_status()
    assert status["state"] == "ERROR"
    assert status["error"] is not None