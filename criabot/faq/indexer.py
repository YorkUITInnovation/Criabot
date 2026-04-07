from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List


@dataclass
class FAQDocument:
    file_name: str
    file_contents: dict
    file_metadata: dict


class FAQIndexer:
    """
    Upload FAQ documents and trigger GraphRAG build after sync.
    """

    def __init__(self, criadex) -> None:
        self._criadex = criadex

    async def sync_group(
        self,
        group_name: str,
        documents: Iterable[FAQDocument],
        trigger_graph_build: bool = True,
    ) -> Dict[str, Any]:
        uploaded_files: List[str] = []
        token_usage: int = 0

        for doc in documents:
            payload = {
                "file_name": doc.file_name,
                "file_contents": doc.file_contents,
                "file_metadata": doc.file_metadata,
            }
            result = await self._criadex.content.upload(
                group_name=group_name,
                file=payload,
            )
            uploaded_files.append(doc.file_name)
            if isinstance(result, dict):
                token_usage += int(result.get("token_usage", 0) or 0)

        build_job = None
        if trigger_graph_build and uploaded_files:
            build_job = await self._criadex.manage.build_graph(group_name=group_name)

        return {
            "group_name": group_name,
            "uploaded_files": uploaded_files,
            "token_usage": token_usage,
            "graph_build_job": build_job,
        }
