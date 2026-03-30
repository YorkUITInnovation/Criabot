import pytest
from unittest.mock import AsyncMock
from criabot.bot.inheritance import check_circular_dependency, merge_configurations
from criabot.database.bots.tables.bot_params import BotParametersModel


class TestCheckCircularDependency:
    @pytest.mark.asyncio
    async def test_no_circular_dependency_single_parent(self):
        bot_parents_api = AsyncMock()
        bot_parents_api.get_parent_ids = AsyncMock(return_value=[])
        
        result = await check_circular_dependency(
            bot_parents_api=bot_parents_api,
            child_bot_id=1,
            parent_bot_ids=[2]
        )
        
        assert result is False
        bot_parents_api.get_parent_ids.assert_called_once_with(2)

    @pytest.mark.asyncio
    async def test_no_circular_dependency_multiple_parents(self):
        bot_parents_api = AsyncMock()
        bot_parents_api.get_parent_ids = AsyncMock(return_value=[])
        
        result = await check_circular_dependency(
            bot_parents_api=bot_parents_api,
            child_bot_id=1,
            parent_bot_ids=[2, 3, 4]
        )
        
        assert result is False
        assert bot_parents_api.get_parent_ids.call_count == 3

    @pytest.mark.asyncio
    async def test_direct_circular_dependency(self):
        bot_parents_api = AsyncMock()
        bot_parents_api.get_parent_ids = AsyncMock(side_effect=lambda bot_id: {
            2: [1]
        }.get(bot_id, []))
        
        result = await check_circular_dependency(
            bot_parents_api=bot_parents_api,
            child_bot_id=1,
            parent_bot_ids=[2]
        )
        
        assert result is True

    @pytest.mark.asyncio
    async def test_indirect_circular_dependency(self):
        bot_parents_api = AsyncMock()
        bot_parents_api.get_parent_ids = AsyncMock(side_effect=lambda bot_id: {
            2: [3],
            3: [1]
        }.get(bot_id, []))
        
        result = await check_circular_dependency(
            bot_parents_api=bot_parents_api,
            child_bot_id=1,
            parent_bot_ids=[2]
        )
        
        assert result is True

    @pytest.mark.asyncio
    async def test_deep_circular_dependency(self):
        bot_parents_api = AsyncMock()
        bot_parents_api.get_parent_ids = AsyncMock(side_effect=lambda bot_id: {
            2: [3],
            3: [4],
            4: [1]
        }.get(bot_id, []))
        
        result = await check_circular_dependency(
            bot_parents_api=bot_parents_api,
            child_bot_id=1,
            parent_bot_ids=[2]
        )
        
        assert result is True

    @pytest.mark.asyncio
    async def test_no_circular_with_existing_parents(self):
        bot_parents_api = AsyncMock()
        bot_parents_api.get_parent_ids = AsyncMock(side_effect=lambda bot_id: {
            2: [5],
            3: [6]
        }.get(bot_id, []))
        
        result = await check_circular_dependency(
            bot_parents_api=bot_parents_api,
            child_bot_id=1,
            parent_bot_ids=[2, 3]
        )
        
        assert result is False

    @pytest.mark.asyncio
    async def test_empty_parent_list(self):
        bot_parents_api = AsyncMock()
        
        result = await check_circular_dependency(
            bot_parents_api=bot_parents_api,
            child_bot_id=1,
            parent_bot_ids=[]
        )
        
        assert result is False
        bot_parents_api.get_parent_ids.assert_not_called()

    @pytest.mark.asyncio
    async def test_self_reference_prevention(self):
        bot_parents_api = AsyncMock()
        bot_parents_api.get_parent_ids = AsyncMock(return_value=[])
        
        result = await check_circular_dependency(
            bot_parents_api=bot_parents_api,
            child_bot_id=1,
            parent_bot_ids=[1]
        )
        
        assert result is True


class TestMergeConfigurations:
    def test_merge_empty_list_raises_error(self):
        with pytest.raises(ValueError, match="Cannot merge empty configuration list"):
            merge_configurations([])

    def test_single_config_returns_as_is(self):
        config = BotParametersModel(
            id=1,
            bot_id=1,
            max_input_tokens=2000,
            max_reply_tokens=1024,
            temperature=0.9,
            top_p=0.0,
            top_k=10,
            min_k=0.5,
            top_n=3,
            min_n=0.7,
            llm_generate_related_prompts=True,
            no_context_message="Test message",
            no_context_use_message=False,
            no_context_llm_guess=False,
            system_message="Test system message"
        )
        
        result = merge_configurations([config])
        
        assert result == config
        assert result.bot_id == 1
        assert result.temperature == 0.9

    def test_merge_two_configs_last_wins(self):
        config1 = BotParametersModel(
            id=1,
            bot_id=1,
            max_input_tokens=2000,
            max_reply_tokens=1024,
            temperature=0.7,
            top_p=0.0,
            top_k=5,
            min_k=0.5,
            top_n=3,
            min_n=0.7,
            llm_generate_related_prompts=True,
            no_context_message="Message 1",
            no_context_use_message=False,
            no_context_llm_guess=False,
            system_message="System 1"
        )
        
        config2 = BotParametersModel(
            id=2,
            bot_id=2,
            max_input_tokens=3000,
            max_reply_tokens=2048,
            temperature=0.9,
            top_p=0.1,
            top_k=10,
            min_k=0.6,
            top_n=5,
            min_n=0.8,
            llm_generate_related_prompts=False,
            no_context_message="Message 2",
            no_context_use_message=True,
            no_context_llm_guess=True,
            system_message="System 2"
        )
        
        result = merge_configurations([config1, config2])
        
        assert result.max_input_tokens == 3000
        assert result.max_reply_tokens == 2048
        assert result.temperature == 0.9
        assert result.top_p == 0.1
        assert result.top_k == 10
        assert result.min_k == 0.6
        assert result.top_n == 5
        assert result.min_n == 0.8
        assert result.llm_generate_related_prompts is False
        assert result.no_context_message == "Message 2"
        assert result.no_context_use_message is True
        assert result.no_context_llm_guess is True
        assert result.system_message == "System 2"

    def test_merge_three_configs_last_wins(self):
        config1 = BotParametersModel(
            id=1, bot_id=1, max_input_tokens=1000, max_reply_tokens=512,
            temperature=0.5, top_p=0.0, top_k=3, min_k=0.3, top_n=2, min_n=0.5,
            llm_generate_related_prompts=True, no_context_message="Msg1",
            no_context_use_message=False, no_context_llm_guess=False, system_message="Sys1"
        )
        
        config2 = BotParametersModel(
            id=2, bot_id=2, max_input_tokens=2000, max_reply_tokens=1024,
            temperature=0.7, top_p=0.0, top_k=5, min_k=0.5, top_n=3, min_n=0.7,
            llm_generate_related_prompts=True, no_context_message="Msg2",
            no_context_use_message=False, no_context_llm_guess=False, system_message="Sys2"
        )
        
        config3 = BotParametersModel(
            id=3, bot_id=3, max_input_tokens=3000, max_reply_tokens=2048,
            temperature=0.9, top_p=0.1, top_k=10, min_k=0.6, top_n=5, min_n=0.8,
            llm_generate_related_prompts=False, no_context_message="Msg3",
            no_context_use_message=True, no_context_llm_guess=True, system_message="Sys3"
        )
        
        result = merge_configurations([config1, config2, config3])
        
        assert result.max_input_tokens == 3000
        assert result.temperature == 0.9
        assert result.system_message == "Sys3"

    def test_merge_with_priorities(self):
        config1 = BotParametersModel(
            id=1, bot_id=1, max_input_tokens=1000, max_reply_tokens=512,
            temperature=0.5, top_p=0.0, top_k=3, min_k=0.3, top_n=2, min_n=0.5,
            llm_generate_related_prompts=True, no_context_message="Msg1",
            no_context_use_message=False, no_context_llm_guess=False, system_message="Sys1"
        )
        
        config2 = BotParametersModel(
            id=2, bot_id=2, max_input_tokens=2000, max_reply_tokens=1024,
            temperature=0.7, top_p=0.0, top_k=5, min_k=0.5, top_n=3, min_n=0.7,
            llm_generate_related_prompts=True, no_context_message="Msg2",
            no_context_use_message=False, no_context_llm_guess=False, system_message="Sys2"
        )
        
        config3 = BotParametersModel(
            id=3, bot_id=3, max_input_tokens=3000, max_reply_tokens=2048,
            temperature=0.9, top_p=0.1, top_k=10, min_k=0.6, top_n=5, min_n=0.8,
            llm_generate_related_prompts=False, no_context_message="Msg3",
            no_context_use_message=True, no_context_llm_guess=True, system_message="Sys3"
        )
        
        priorities = {1: 1, 2: 2, 3: 3}
        
        result = merge_configurations([config3, config1, config2], priorities=priorities)
        
        assert result.max_input_tokens == 3000
        assert result.temperature == 0.9
        assert result.system_message == "Sys3"

    def test_merge_with_priorities_reverse_order(self):
        config1 = BotParametersModel(
            id=1, bot_id=1, max_input_tokens=1000, max_reply_tokens=512,
            temperature=0.5, top_p=0.0, top_k=3, min_k=0.3, top_n=2, min_n=0.5,
            llm_generate_related_prompts=True, no_context_message="Msg1",
            no_context_use_message=False, no_context_llm_guess=False, system_message="Sys1"
        )
        
        config2 = BotParametersModel(
            id=2, bot_id=2, max_input_tokens=2000, max_reply_tokens=1024,
            temperature=0.7, top_p=0.0, top_k=5, min_k=0.5, top_n=3, min_n=0.7,
            llm_generate_related_prompts=True, no_context_message="Msg2",
            no_context_use_message=False, no_context_llm_guess=False, system_message="Sys2"
        )
        
        priorities = {1: 2, 2: 1}
        
        result = merge_configurations([config1, config2], priorities=priorities)
        
        assert result.max_input_tokens == 1000
        assert result.temperature == 0.5
        assert result.system_message == "Sys1"

    def test_merge_none_values_not_override(self):
        config1 = BotParametersModel(
            id=1, bot_id=1, max_input_tokens=2000, max_reply_tokens=1024,
            temperature=0.9, top_p=0.0, top_k=10, min_k=0.5, top_n=3, min_n=0.7,
            llm_generate_related_prompts=True, no_context_message="Message",
            no_context_use_message=False, no_context_llm_guess=False, system_message="System"
        )
        
        config2 = BotParametersModel(
            id=2, bot_id=2, max_input_tokens=3000, max_reply_tokens=2048,
            temperature=0.8, top_p=0.1, top_k=15, min_k=0.6, top_n=5, min_n=0.8,
            llm_generate_related_prompts=False, no_context_message="Message2",
            no_context_use_message=True, no_context_llm_guess=True, system_message=None
        )
        
        result = merge_configurations([config1, config2])
        
        assert result.max_input_tokens == 3000
        assert result.system_message == "System"

    def test_merge_all_fields(self):
        config1 = BotParametersModel(
            id=1, bot_id=1, max_input_tokens=1000, max_reply_tokens=500,
            temperature=0.5, top_p=0.0, top_k=5, min_k=0.3, top_n=2, min_n=0.5,
            llm_generate_related_prompts=False, no_context_message="Msg1",
            no_context_use_message=False, no_context_llm_guess=False, system_message="Sys1"
        )
        
        config2 = BotParametersModel(
            id=2, bot_id=2, max_input_tokens=2000, max_reply_tokens=1000,
            temperature=0.7, top_p=0.1, top_k=10, min_k=0.5, top_n=3, min_n=0.7,
            llm_generate_related_prompts=True, no_context_message="Msg2",
            no_context_use_message=True, no_context_llm_guess=True, system_message="Sys2"
        )
        
        result = merge_configurations([config1, config2])
        
        assert result.max_input_tokens == 2000
        assert result.max_reply_tokens == 1000
        assert result.temperature == 0.7
        assert result.top_p == 0.1
        assert result.top_k == 10
        assert result.min_k == 0.5
        assert result.top_n == 3
        assert result.min_n == 0.7
        assert result.llm_generate_related_prompts is True
        assert result.no_context_message == "Msg2"
        assert result.no_context_use_message is True
        assert result.no_context_llm_guess is True
        assert result.system_message == "Sys2"
