"""Overlay entities: comments, relationships, notifications, change log."""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional

from src.common.uc_native.delta_store import DeltaStore


class UcNativeOverlayStore:
    def __init__(self, store: DeltaStore) -> None:
        self._store = store

    def add_comment(
        self,
        *,
        entity_type: str,
        entity_id: str,
        author: str,
        body: str,
    ) -> Dict[str, Any]:
        comment_id = str(uuid.uuid4())
        payload = {
            "id": comment_id,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "author": author,
            "body": body,
            "snapshot_json": json.dumps({"body": body}),
        }
        self._store.merge_row("comments", payload)
        return payload

    def list_comments(self, entity_type: str, entity_id: str) -> List[Dict[str, Any]]:
        fqn = self._store.table_fqn("comments")
        rows = self._store.query(
            f"SELECT * FROM {fqn} WHERE entity_type = '{entity_type}' "
            f"AND entity_id = '{entity_id}' ORDER BY updated_at DESC LIMIT 200"
        )
        return rows

    def add_relationship(
        self,
        *,
        source_entity_id: str,
        source_entity_type: str,
        target_entity_id: str,
        target_entity_type: str,
        relationship_type: str,
        properties: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        rel_id = str(uuid.uuid4())
        payload = {
            "id": rel_id,
            "source_entity_id": source_entity_id,
            "source_entity_type": source_entity_type,
            "target_entity_id": target_entity_id,
            "target_entity_type": target_entity_type,
            "relationship_type": relationship_type,
            "snapshot_json": json.dumps(properties or {}),
        }
        self._store.merge_row("entity_relationships", payload)
        return payload

    def append_change_log(
        self,
        *,
        entity_type: str,
        entity_id: str,
        action: str,
        username: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        from datetime import datetime, timezone

        self._store.merge_row(
            "entity_change_log",
            {
                "id": str(uuid.uuid4()),
                "entity_type": entity_type,
                "entity_id": entity_id,
                "action": action,
                "username": username,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "details_json": json.dumps(details or {}),
            },
        )

    def create_notification(
        self,
        *,
        username: str,
        title: str,
        body: str,
    ) -> Dict[str, Any]:
        notif_id = str(uuid.uuid4())
        from datetime import datetime, timezone

        payload = {
            "id": notif_id,
            "username": username,
            "title": title,
            "body": body,
            "read": False,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "snapshot_json": json.dumps({"title": title, "body": body}),
        }
        self._store.merge_row("notifications", payload)
        return payload

    def list_notifications(self, username: str, *, limit: int = 50) -> List[Dict[str, Any]]:
        fqn = self._store.table_fqn("notifications")
        safe_user = username.replace("'", "''")
        return self._store.query(
            f"SELECT * FROM {fqn} WHERE username = '{safe_user}' "
            f"ORDER BY created_at DESC LIMIT {int(limit)}"
        )
