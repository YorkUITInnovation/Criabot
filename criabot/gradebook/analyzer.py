from __future__ import annotations

from typing import Any, Dict


class SyllabusAnalyzer:
    """
    Graph-aware syllabus retrieval utility for Gradebook generation flows.
    """

    def __init__(self, criadex) -> None:
        self._criadex = criadex

    async def analyze_group(
        self,
        group_name: str,
        prompt: str,
        top_k: int = 8,
        max_hops: int = 1,
        max_expansion_terms: int = 8,
    ) -> Dict[str, Any]:
        search_config: dict = {
            "query": prompt,
            "top_k": top_k,
        }

        try:
            graph_payload = {
                **search_config,
                "max_hops": max_hops,
                "max_expansion_terms": max_expansion_terms,
                "auto_build": True,
            }
            result = await self._criadex.manage.graph_search(
                group_name=group_name,
                search_config=graph_payload,
            )
            metadata = None
            if isinstance(result, dict):
                metadata = result.get("graph_metadata")
                response = result.get("response", result)
            else:
                response = result
            return {
                "response": response,
                "graph_metadata": metadata or {"source": "ragflow"},
            }
        except Exception:
            response = await self._criadex.content.search(
                group_name=group_name,
                search_config=search_config,
            )
            return {
                "response": response.get("response", response) if isinstance(response, dict) else response,
                "graph_metadata": {
                    "source": "fallback",
                    "fallback_reason": "graph_search_unavailable",
                },
            }
