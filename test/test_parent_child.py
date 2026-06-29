import pytest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from criabot.criabot import Criabot
from criabot.schemas import (
    CriadexCredentials, MySQLCredentials, RedisCredentials,
    BotCreateConfig, BotNotFoundError, ParentNotFoundError, CircularDependencyError
)
from criabot.database.bots.tables.bot_parents import BotParentsAPI, BotParentsConfig, BotParentsModel
from criabot.database.bots.tables.bots import BotsModel, BotsConfig


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


class TestBotParentsAPI:
    @pytest.mark.asyncio
    async def test_insert_parent_child_relationship(self):
        bot_parents_api = BotParentsAPI(MagicMock())
        mock_session = AsyncMock()
        mock_context_manager = MagicMock()
        mock_context_manager.__aenter__ = AsyncMock(return_value=mock_session)
        mock_context_manager.__aexit__ = AsyncMock(return_value=None)
        bot_parents_api.get_async_session = MagicMock(return_value=mock_context_manager)
        
        mock_result = MagicMock()
        mock_result.lastrowid = 1
        mock_session.execute = AsyncMock(return_value=mock_result)
        
        config = BotParentsConfig(child_bot_id=1, parent_bot_id=2, priority=0)
        result = await bot_parents_api.insert(config)
        
        assert result == 1
        mock_session.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_parent_ids(self):
        bot_parents_api = BotParentsAPI(MagicMock())
        
        rel1 = BotParentsModel(id=1, child_bot_id=1, parent_bot_id=2, priority=1)
        rel2 = BotParentsModel(id=2, child_bot_id=1, parent_bot_id=3, priority=0)
        
        bot_parents_api.get_by_child = AsyncMock(return_value=[rel2, rel1])
        
        result = await bot_parents_api.get_parent_ids(child_bot_id=1)
        
        assert result == [3, 2]

    @pytest.mark.asyncio
    async def test_get_child_ids(self):
        bot_parents_api = BotParentsAPI(MagicMock())
        
        rel1 = BotParentsModel(id=1, child_bot_id=2, parent_bot_id=1, priority=0)
        rel2 = BotParentsModel(id=2, child_bot_id=3, parent_bot_id=1, priority=0)
        
        bot_parents_api.get_by_parent = AsyncMock(return_value=[rel1, rel2])
        
        result = await bot_parents_api.get_child_ids(parent_bot_id=1)
        
        assert result == [2, 3]

    @pytest.mark.asyncio
    async def test_exists_relationship(self):
        bot_parents_api = BotParentsAPI(MagicMock())
        mock_session = AsyncMock()
        mock_context_manager = MagicMock()
        mock_context_manager.__aenter__ = AsyncMock(return_value=mock_session)
        mock_context_manager.__aexit__ = AsyncMock(return_value=None)
        bot_parents_api.get_async_session = MagicMock(return_value=mock_context_manager)
        
        mock_entry = MagicMock()
        bot_parents_api.fetchone_or_none = MagicMock(return_value=mock_entry)
        
        result = await bot_parents_api.exists(child_bot_id=1, parent_bot_id=2)
        
        assert result is True

    @pytest.mark.asyncio
    async def test_delete_relationship(self):
        bot_parents_api = BotParentsAPI(MagicMock())
        mock_session = AsyncMock()
        mock_context_manager = MagicMock()
        mock_context_manager.__aenter__ = AsyncMock(return_value=mock_session)
        mock_context_manager.__aexit__ = AsyncMock(return_value=None)
        bot_parents_api.get_async_session = MagicMock(return_value=mock_context_manager)
        
        mock_session.execute = AsyncMock()
        
        await bot_parents_api.delete(child_bot_id=1, parent_bot_id=2)
        
        mock_session.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_all_by_child(self):
        bot_parents_api = BotParentsAPI(MagicMock())
        mock_session = AsyncMock()
        mock_context_manager = MagicMock()
        mock_context_manager.__aenter__ = AsyncMock(return_value=mock_session)
        mock_context_manager.__aexit__ = AsyncMock(return_value=None)
        bot_parents_api.get_async_session = MagicMock(return_value=mock_context_manager)
        
        mock_session.execute = AsyncMock()
        
        await bot_parents_api.delete_all_by_child(child_bot_id=1)
        
        mock_session.execute.assert_called_once()


class TestCriabotParentChild:
    @pytest.mark.asyncio
    async def test_create_bot_with_single_parent(self, criabot_instance):
        criabot_instance._mysql_api.bots.exists = AsyncMock(return_value=False)
        criabot_instance._mysql_api.bots.retrieve_id = AsyncMock(side_effect=lambda name: {"parent_bot": 2}.get(name, None))
        criabot_instance._mysql_api.bots.retrieve_by_id = AsyncMock(return_value=BotsModel(id=2, name="parent_bot", created=datetime.now()))
        criabot_instance._criadex.auth.create = AsyncMock(return_value={"api_key": "child_key"})
        criabot_instance._criadex.manage.create = AsyncMock(return_value=MagicMock())
        criabot_instance._criadex.group_auth.create = AsyncMock()
        criabot_instance._mysql_api.bots.insert = AsyncMock(return_value=1)
        criabot_instance._mysql_api.bot_params.insert = AsyncMock(return_value=None)
        criabot_instance._mysql_api.bot_parents.get_parent_ids = AsyncMock(return_value=[])
        criabot_instance._mysql_api.bot_parents.insert = AsyncMock(return_value=1)
        criabot_instance._mysql_api.bot_api_keys.get_active_by_bot = AsyncMock(return_value=MagicMock(api_key="parent_key"))
        
        config = BotCreateConfig(
            llm_model_id=1,
            embedding_model_id=1,
            rerank_model_id=1,
            parent_bot_names=["parent_bot"]
        )
        
        result = await criabot_instance.create(name="child_bot", config=config)
        
        assert result["api_key"] == "child_key"
        criabot_instance._mysql_api.bot_parents.insert.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_bot_with_multiple_parents(self, criabot_instance):
        now = datetime.now()
        criabot_instance._mysql_api.bots.exists = AsyncMock(return_value=False)
        criabot_instance._mysql_api.bots.retrieve_id = AsyncMock(side_effect=lambda name: {
            "parent1": 2, "parent2": 3
        }.get(name, None))
        criabot_instance._mysql_api.bots.retrieve_by_id = AsyncMock(side_effect=lambda bot_id: {
            2: BotsModel(id=2, name="parent1", created=now),
            3: BotsModel(id=3, name="parent2", created=now)
        }.get(bot_id))
        criabot_instance._criadex.auth.create = AsyncMock(return_value={"api_key": "child_key"})
        criabot_instance._criadex.manage.create = AsyncMock(return_value=MagicMock())
        criabot_instance._criadex.group_auth.create = AsyncMock()
        criabot_instance._mysql_api.bots.insert = AsyncMock(return_value=1)
        criabot_instance._mysql_api.bot_params.insert = AsyncMock(return_value=None)
        criabot_instance._mysql_api.bot_parents.get_parent_ids = AsyncMock(return_value=[])
        criabot_instance._mysql_api.bot_parents.insert = AsyncMock(return_value=1)
        criabot_instance._mysql_api.bot_api_keys.get_active_by_bot = AsyncMock(return_value=MagicMock(api_key="parent_key"))
        
        config = BotCreateConfig(
            llm_model_id=1,
            embedding_model_id=1,
            rerank_model_id=1,
            parent_bot_names=["parent1", "parent2"]
        )
        
        result = await criabot_instance.create(name="child_bot", config=config)
        
        assert result["api_key"] == "child_key"
        assert criabot_instance._mysql_api.bot_parents.insert.call_count == 2

    @pytest.mark.asyncio
    async def test_create_bot_with_parent_priorities(self, criabot_instance):
        now = datetime.now()
        criabot_instance._mysql_api.bots.exists = AsyncMock(return_value=False)
        criabot_instance._mysql_api.bots.retrieve_id = AsyncMock(side_effect=lambda name: {
            "parent1": 2, "parent2": 3
        }.get(name, None))
        criabot_instance._mysql_api.bots.retrieve_by_id = AsyncMock(side_effect=lambda bot_id: {
            2: BotsModel(id=2, name="parent1", created=now),
            3: BotsModel(id=3, name="parent2", created=now)
        }.get(bot_id))
        criabot_instance._criadex.auth.create = AsyncMock(return_value={"api_key": "child_key"})
        criabot_instance._criadex.manage.create = AsyncMock(return_value=MagicMock())
        criabot_instance._criadex.group_auth.create = AsyncMock()
        criabot_instance._mysql_api.bots.insert = AsyncMock(return_value=1)
        criabot_instance._mysql_api.bot_params.insert = AsyncMock(return_value=None)
        criabot_instance._mysql_api.bot_parents.get_parent_ids = AsyncMock(return_value=[])
        criabot_instance._mysql_api.bot_parents.insert = AsyncMock(return_value=1)
        criabot_instance._mysql_api.bot_api_keys.get_active_by_bot = AsyncMock(return_value=MagicMock(api_key="parent_key"))
        
        config = BotCreateConfig(
            llm_model_id=1,
            embedding_model_id=1,
            rerank_model_id=1,
            parent_bot_names=["parent1", "parent2"],
            parent_priorities={"parent1": 1, "parent2": 2}
        )
        
        result = await criabot_instance.create(name="child_bot", config=config)
        
        assert result["api_key"] == "child_key"

    @pytest.mark.asyncio
    async def test_create_bot_with_nonexistent_parent(self, criabot_instance):
        criabot_instance._mysql_api.bots.exists = AsyncMock(return_value=False)
        criabot_instance._mysql_api.bots.retrieve_id = AsyncMock(return_value=None)
        
        config = BotCreateConfig(
            llm_model_id=1,
            embedding_model_id=1,
            rerank_model_id=1,
            parent_bot_names=["nonexistent_parent"]
        )
        
        with pytest.raises(ParentNotFoundError):
            await criabot_instance._get_parent_ids_by_name(["nonexistent_parent"])

    @pytest.mark.asyncio
    async def test_create_bot_with_circular_dependency(self, criabot_instance):
        criabot_instance._mysql_api.bots.exists = AsyncMock(return_value=False)
        criabot_instance._mysql_api.bots.retrieve_id = AsyncMock(return_value=2)
        criabot_instance._mysql_api.bots.retrieve_by_id = AsyncMock(return_value=BotsModel(id=2, name="parent_bot", created=datetime.now()))
        criabot_instance._criadex.auth.create = AsyncMock(return_value={"api_key": "child_key"})
        criabot_instance._criadex.manage.create = AsyncMock(return_value=MagicMock())
        criabot_instance._criadex.group_auth.create = AsyncMock()
        criabot_instance._mysql_api.bots.insert = AsyncMock(return_value=1)
        criabot_instance._mysql_api.bot_params.insert = AsyncMock(return_value=None)
        criabot_instance._mysql_api.bot_parents.get_parent_ids = AsyncMock(side_effect=lambda child_bot_id: {
            2: [1]
        }.get(child_bot_id, []))
        
        config = BotCreateConfig(
            llm_model_id=1,
            embedding_model_id=1,
            rerank_model_id=1,
            parent_bot_names=["parent_bot"]
        )
        
        with pytest.raises(CircularDependencyError):
            await criabot_instance.create(name="child_bot", config=config)

    @pytest.mark.asyncio
    async def test_get_parent_ids_by_name(self, criabot_instance):
        criabot_instance._mysql_api.bots.retrieve_id = AsyncMock(side_effect=lambda name: {
            "parent1": 2,
            "parent2": 3
        }.get(name, None))
        
        result = await criabot_instance._get_parent_ids_by_name(["parent1", "parent2"])
        
        assert result == {"parent1": 2, "parent2": 3}

    @pytest.mark.asyncio
    async def test_get_parent_ids_by_name_missing_parent(self, criabot_instance):
        criabot_instance._mysql_api.bots.retrieve_id = AsyncMock(side_effect=lambda name: {
            "parent1": 2
        }.get(name, None))
        
        with pytest.raises(ParentNotFoundError):
            await criabot_instance._get_parent_ids_by_name(["parent1", "nonexistent"])

    @pytest.mark.asyncio
    async def test_get_children_bot_names(self, criabot_instance):
        now = datetime.now()
        criabot_instance.get_id = AsyncMock(return_value=1)
        criabot_instance._mysql_api.bot_parents.get_child_ids = AsyncMock(return_value=[2, 3])
        criabot_instance._mysql_api.bots.retrieve_by_id = AsyncMock(side_effect=lambda bot_id: {
            2: BotsModel(id=2, name="child1", created=now),
            3: BotsModel(id=3, name="child2", created=now)
        }.get(bot_id))
        
        result = await criabot_instance.get_children_bot_names("parent_bot")
        
        assert result == ["child1", "child2"]

    @pytest.mark.asyncio
    async def test_get_children_bot_names_no_children(self, criabot_instance):
        criabot_instance.get_id = AsyncMock(return_value=1)
        criabot_instance._mysql_api.bot_parents.get_child_ids = AsyncMock(return_value=[])
        
        result = await criabot_instance.get_children_bot_names("parent_bot")
        
        assert result == []

    @pytest.mark.asyncio
    async def test_update_parent_relationships(self, criabot_instance):
        now = datetime.now()
        criabot_instance.get_id = AsyncMock(return_value=1)
        criabot_instance._mysql_api.bot_parents.get_parent_ids = AsyncMock(return_value=[2])
        criabot_instance._mysql_api.bots.retrieve_id = AsyncMock(side_effect=lambda name: {
            "new_parent": 3
        }.get(name, None))
        criabot_instance._mysql_api.bots.retrieve_by_id = AsyncMock(side_effect=lambda bot_id: {
            2: BotsModel(id=2, name="old_parent", created=now),
            3: BotsModel(id=3, name="new_parent", created=now)
        }.get(bot_id))
        criabot_instance._mysql_api.bot_api_keys.get_active_by_bot = AsyncMock(return_value=MagicMock(api_key="child_key"))
        criabot_instance._mysql_api.bot_parents.get_parent_ids = AsyncMock(return_value=[])
        criabot_instance._authorize_child_on_parent_groups = AsyncMock()
        criabot_instance._authorize_parents_on_child_groups = AsyncMock()
        criabot_instance._revoke_child_from_parent_groups = AsyncMock()
        criabot_instance._revoke_parents_from_child_groups = AsyncMock()
        criabot_instance._assign_parents = AsyncMock()
        
        await criabot_instance.update_parent_relationships(
            child_name="child_bot",
            new_parent_names=["new_parent"]
        )
        
        criabot_instance._assign_parents.assert_called_once()

    @pytest.mark.asyncio
    async def test_about_with_parents(self, criabot_instance):
        from criabot.database.bots.tables.bot_params import BotParametersModel
        
        now = datetime.now()
        criabot_instance._mysql_api.bots.retrieve = AsyncMock(return_value=BotsModel(id=1, name="child_bot", created=now))
        criabot_instance._mysql_api.bot_params.retrieve = AsyncMock(return_value=BotParametersModel(
            id=1, bot_id=1, max_input_tokens=2000, max_reply_tokens=1024, temperature=0.9,
            top_p=0.0, top_k=10, min_k=0.5, top_n=3, min_n=0.7,
            llm_generate_related_prompts=True, no_context_message="Test",
            no_context_use_message=False, no_context_llm_guess=False, system_message="Child system",
            web_search_global_enabled=True, web_search_enabled=True,
            faq_fallback_enabled=True, faq_fallback_threshold=0.9,
        ))
        criabot_instance._mysql_api.bot_parents.get_parent_ids = AsyncMock(return_value=[2])
        criabot_instance._mysql_api.bot_parents.get_by_child = AsyncMock(return_value=[
            MagicMock(parent_bot_id=2, priority=0)
        ])
        criabot_instance._mysql_api.bots.retrieve_by_ids = AsyncMock(return_value=[
            BotsModel(id=2, name="parent_bot", created=now)
        ])
        from criabot.database.bots.tables.bot_params import BotParametersModel
        
        criabot_instance._mysql_api.bot_params.retrieve_by_bot_ids = AsyncMock(return_value=[
            BotParametersModel(
                id=2, bot_id=2, max_input_tokens=3000, max_reply_tokens=2048, temperature=0.8,
                top_p=0.1, top_k=15, min_k=0.6, top_n=5, min_n=0.8,
                llm_generate_related_prompts=False, no_context_message="Parent",
                no_context_use_message=True, no_context_llm_guess=True, system_message="Parent system",
                web_search_global_enabled=False, web_search_enabled=False,
                faq_fallback_enabled=False, faq_fallback_threshold=0.2,
            )
        ])
        criabot_instance._mysql_api.bot_parents.get_child_ids = AsyncMock(return_value=[])
        
        result = await criabot_instance.about("child_bot")
        
        assert result.parent_bot_names == ["parent_bot"]
        assert result.children == []
        assert result.effective_config is not None
        assert result.effective_config.web_search_enabled is True
        assert result.effective_config.web_search_global_enabled is True
        assert result.effective_config.faq_fallback_enabled is True
        assert result.effective_config.faq_fallback_threshold == 0.9
