from typing import List, Dict, Set, Optional
from criabot.database.bots.tables.bot_params import BotParametersModel


async def check_circular_dependency(
    bot_parents_api,
    child_bot_id: int,
    parent_bot_ids: List[int]
) -> bool:
    """Check if adding parents would create a circular dependency"""
    visited: Set[int] = set()
    
    async def is_descendant(bot_id: int, target_id: int) -> bool:
        if bot_id == target_id:
            return True
        if bot_id in visited:
            return False
        
        visited.add(bot_id)
        parent_ids = await bot_parents_api.get_parent_ids(bot_id)
        
        for parent_id in parent_ids:
            if await is_descendant(parent_id, target_id):
                return True
        return False
    
    for parent_id in parent_bot_ids:
        if await is_descendant(parent_id, child_bot_id):
            return True
    
    return False


def merge_configurations(
    configs: List[BotParametersModel],
    priorities: Optional[Dict[int, int]] = None
) -> BotParametersModel:
    """Merge multiple bot configurations based on priority"""
    if not configs:
        raise ValueError("Cannot merge empty configuration list")
    
    if len(configs) == 1:
        return configs[0]
    
    if priorities:
        sorted_configs = sorted(
            configs,
            key=lambda c: priorities.get(c.bot_id, 999)
        )
    else:
        sorted_configs = configs
    
    base = sorted_configs[0]
    merged = BotParametersModel(
        id=base.id,
        bot_id=base.bot_id,
        max_input_tokens=base.max_input_tokens,
        max_reply_tokens=base.max_reply_tokens,
        temperature=base.temperature,
        top_p=base.top_p,
        top_k=base.top_k,
        min_k=base.min_k,
        top_n=base.top_n,
        min_n=base.min_n,
        llm_generate_related_prompts=base.llm_generate_related_prompts,
        no_context_message=base.no_context_message,
        no_context_use_message=base.no_context_use_message,
        no_context_llm_guess=base.no_context_llm_guess,
        system_message=base.system_message
    )
    
    for config in sorted_configs[1:]:
        if config.system_message and not merged.system_message:
            merged.system_message = config.system_message
    
    return merged

