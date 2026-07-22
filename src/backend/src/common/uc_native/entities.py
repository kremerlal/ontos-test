"""Entity CRUD against UC Delta tables."""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional

from src.common.logging import get_logger
from src.common.uc_native.delta_store import DeltaStore

logger = get_logger(__name__)


class UcNativeEntityStore:
    """Denormalized entity documents in Delta."""

    def __init__(self, store: DeltaStore) -> None:
        self._store = store

    def list_entities(self, table_name: str, *, limit: int = 500) -> List[Dict[str, Any]]:
        rows = self._store.list_rows(table_name, limit=limit)
        return [self._hydrate(table_name, row) for row in rows]

    def get_entity(self, table_name: str, entity_id: str) -> Optional[Dict[str, Any]]:
        row = self._store.get_by_id(table_name, entity_id)
        return self._hydrate(table_name, row) if row else None

    def save_entity(
        self,
        table_name: str,
        payload: Dict[str, Any],
        *,
        index_fields: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        entity_id = str(payload.get("id") or uuid.uuid4())
        payload["id"] = entity_id
        index = index_fields or {}
        row: Dict[str, Any] = {
            "id": entity_id,
            "snapshot_json": json.dumps(payload, default=str),
            **index,
        }
        self._store.merge_row(table_name, row)
        return payload

    def delete_entity(self, table_name: str, entity_id: str) -> None:
        self._store.delete_by_id(table_name, entity_id)

    def _hydrate(self, table_name: str, row: Dict[str, Any]) -> Dict[str, Any]:
        doc = self._store.parse_snapshot(row)
        if not doc.get("id"):
            doc["id"] = row.get("id")
        for key in ("name", "status", "domain_id", "product_id", "project_id", "draft_owner_id"):
            if key in row and row.get(key) is not None and key not in doc:
                doc[key] = row[key]
        doc["_table"] = table_name
        doc["_etag"] = row.get("etag")
        return doc
