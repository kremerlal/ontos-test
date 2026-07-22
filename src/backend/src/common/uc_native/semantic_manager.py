"""Semantic models manager backed by UC Volumes + Delta triples."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.common.logging import get_logger
from src.common.uc_native.semantic import UcNativeSemanticStore

logger = get_logger(__name__)


class UcNativeSemanticModelsManager:
    def __init__(self, semantic: UcNativeSemanticStore) -> None:
        self._semantic = semantic

    def list_models(self, db=None, **_) -> List[Dict[str, Any]]:
        return self._semantic.search_triples("", limit=200)

    def save_ontology_bytes(self, filename: str, content: bytes) -> str:
        return self._semantic.save_ontology_file(filename, content)

    def merge_triples(self, triples: List[Dict[str, str]]) -> int:
        return self._semantic.merge_triples(triples)

    def search_concepts(self, prefix: str, *, limit: int = 50) -> List[Dict[str, Any]]:
        return self._semantic.search_triples(prefix, limit=limit)

    def append_job_result(
        self,
        table_name: str,
        *,
        status: str,
        parent_id: str,
        results: Dict[str, Any],
    ) -> str:
        return self._semantic.append_job_result(
            table_name,
            status=status,
            parent_id=parent_id,
            results=results,
        )
