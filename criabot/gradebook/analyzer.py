from __future__ import annotations

import json
from typing import Any, Dict, List


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

    async def extract_assessment_structure(self, group_name: str) -> Dict[str, Any]:
        """
        Extract structured assessment information from syllabus using multiple queries.
        """
        queries = {
            "assessment_types": "What assessment types are mentioned? (exams, assignments, quizzes, projects, labs, participation)",
            "weight_distribution": "What are the grade weight percentages for each assessment type?",
            "item_counts": "How many individual items exist for each assessment type? (e.g., 5 assignments, 10 labs)",
            "grading_policy": "What grading policies are mentioned? (drop lowest, late penalties, grade scale)",
            "schedule_timeline": "What is the schedule or timeline for assessments? (due dates, exam dates)",
        }

        results = {}
        for key, query in queries.items():
            try:
                result = await self.analyze_group(
                    group_name=group_name,
                    prompt=query,
                    top_k=5,
                    max_hops=2,  # Allow more hops for relationship discovery
                )
                results[key] = result
            except Exception as e:
                results[key] = {"error": str(e), "graph_metadata": {"source": "error"}}

        return {
            "extraction_results": results,
            "summary": self._summarize_extraction(results)
        }

    def _summarize_extraction(self, results: Dict[str, Any]) -> Dict[str, Any]:
        """
        Summarize the extraction results into structured data.
        """
        summary = {
            "assessment_types": [],
            "weight_distribution": {},
            "item_counts": {},
            "grading_policies": [],
            "timeline_info": [],
            "confidence_score": 0.0
        }

        # Simple extraction - in production, this would use LLM to parse the results
        for key, result in results.items():
            if "error" in result:
                continue

            response = result.get("response", {})
            if isinstance(response, dict) and "nodes" in response:
                # Graph RAG response format
                nodes = response.get("nodes", [])
                summary[f"{key}_nodes"] = len(nodes)
            elif isinstance(response, dict) and "chunks" in response:
                # Standard RAG response format
                chunks = response.get("chunks", [])
                summary[f"{key}_chunks"] = len(chunks)

        # Calculate basic confidence based on results
        successful_queries = sum(1 for r in results.values() if "error" not in r)
        summary["confidence_score"] = min(successful_queries / len(results), 1.0)

        return summary
