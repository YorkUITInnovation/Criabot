import hashlib
import json
from typing import Any, Iterable, List


def stable_hash(*parts: str) -> str:
    """Return a deterministic sha256 hex digest for the joined parts."""
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalize_cache_text(value: str) -> str:
    return " ".join((value or "").strip().lower().split())


def fingerprint_rerank_nodes(nodes: Iterable[Any]) -> str:
    """Fingerprint node content used as rerank input (order-sensitive)."""
    parts: List[str] = []
    for node in nodes:
        if hasattr(node, "model_dump"):
            dumped = node.model_dump(mode="json")
        elif isinstance(node, dict):
            dumped = node
        else:
            continue
        node_body = dumped.get("node", {})
        text = str(node_body.get("text", ""))
        score = dumped.get("score")
        parts.append(f"{score}:{hashlib.sha256(text.encode('utf-8')).hexdigest()}")
    return stable_hash(*parts)


def dumps_json(payload: Any) -> str:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def loads_json(raw: bytes | str) -> Any:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw)
