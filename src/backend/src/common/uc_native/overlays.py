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
        safe_type = entity_type.replace("'", "''")
        safe_id = entity_id.replace("'", "''")
        rows = self._store.query(
            f"SELECT * FROM {fqn} WHERE entity_type = '{safe_type}' "
            f"AND entity_id = '{safe_id}' ORDER BY updated_at DESC LIMIT 200"
        )
        return [
            {**row, **self._store.parse_snapshot(row), "id": row.get("id")}
            for row in rows
        ]

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

    def add_relationships(
        self,
        relationships: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Insert known-new relationships with chunked Delta statements."""
        payloads: List[Dict[str, Any]] = []
        for relationship in relationships:
            payloads.append(
                {
                    "id": str(uuid.uuid4()),
                    "source_entity_id": relationship["source_entity_id"],
                    "source_entity_type": relationship["source_entity_type"],
                    "target_entity_id": relationship["target_entity_id"],
                    "target_entity_type": relationship["target_entity_type"],
                    "relationship_type": relationship["relationship_type"],
                    "snapshot_json": json.dumps(relationship.get("properties") or {}),
                }
            )
        self._store.insert_rows("entity_relationships", payloads)
        return payloads

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

    def list_change_log(
        self,
        entity_type: str,
        entity_id: str,
        *,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Return change-log rows for an entity, newest first."""
        fqn = self._store.table_fqn("entity_change_log")
        safe_type = entity_type.replace("'", "''")
        safe_id = entity_id.replace("'", "''")
        safe_limit = max(1, min(int(limit), 1000))
        return self._store.query(
            f"SELECT * FROM {fqn} WHERE entity_type = '{safe_type}' "
            f"AND entity_id = '{safe_id}' ORDER BY timestamp DESC LIMIT {safe_limit}"
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

    def add(self, table_name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Persist one generic overlay document with searchable index columns."""
        row = dict(payload)
        row.setdefault("id", str(uuid.uuid4()))
        row.setdefault("updated_at", __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat())
        snapshot = dict(row)
        snapshot.pop("snapshot_json", None)
        row["snapshot_json"] = json.dumps(snapshot, default=str)
        self._store.merge_row(table_name, row)
        return snapshot

    def get(self, table_name: str, item_id: str) -> Optional[Dict[str, Any]]:
        row = self._store.get_by_id(table_name, item_id)
        if not row:
            return None
        return {**self._store.parse_snapshot(row), "id": row.get("id", item_id)}

    def remove(self, table_name: str, item_id: str) -> bool:
        if not self._store.get_by_id(table_name, item_id):
            return False
        self._store.delete_by_id(table_name, item_id)
        return True

    def list_for_entity(self, table_name: str, entity_type: str, entity_id: str) -> List[Dict[str, Any]]:
        rows = self._store.list_rows(table_name, limit=1000)
        return [
            {**self._store.parse_snapshot(row), "id": row.get("id")}
            for row in rows
            if row.get("entity_type") == entity_type and row.get("entity_id") == entity_id
        ]

    def add_semantic_link(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.add("entity_semantic_links", payload)

    def list_semantic_links(self, *, entity_id: Optional[str] = None, entity_type: Optional[str] = None, iri: Optional[str] = None) -> List[Dict[str, Any]]:
        rows = self._store.list_rows("entity_semantic_links", limit=1000)
        return [
            {**self._store.parse_snapshot(row), "id": row.get("id")}
            for row in rows
            if (entity_id is None or row.get("entity_id") == entity_id)
            and (entity_type is None or row.get("entity_type") == entity_type)
            and (iri is None or row.get("iri") == iri)
        ]

    def remove_semantic_link(self, link_id: str) -> bool:
        return self.remove("entity_semantic_links", link_id)

    def subscribe(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.add("entity_subscriptions", payload)

    def unsubscribe(self, subscription_id: str) -> bool:
        return self.remove("entity_subscriptions", subscription_id)

    def list_cost_items(self, entity_type: str, entity_id: str) -> List[Dict[str, Any]]:
        return self.list_for_entity("cost_items", entity_type, entity_id)

    def list_quality_items(self, entity_type: str, entity_id: str) -> List[Dict[str, Any]]:
        return self.list_for_entity("quality_items", entity_type, entity_id)

    def mark_notification_read(self, notification_id: str) -> Optional[Dict[str, Any]]:
        notification = self.get("notifications", notification_id)
        if not notification:
            return None
        notification["read"] = True
        return self.add("notifications", notification)

    def get_notification_by_id(self, notification_id: str) -> Optional[Dict[str, Any]]:
        return self.get("notifications", notification_id)
