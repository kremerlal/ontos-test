"""Regression tests for UC-native overlays without a warehouse or Postgres."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

from src.common.uc_native.overlay_managers import (
    UcNativeChangeLogManager,
    UcNativeCommentsManager,
    UcNativeNotificationsManager,
)
from src.controller.audit_manager import AuditManager
from src.models.comments import CommentCreate, CommentUpdate, CommentType
from src.models.notifications import Notification, NotificationType


class FakeOverlays:
    def __init__(self):
        self.tables = {}
        self._store = self

    def add_comment(self, *, entity_type, entity_id, author, body):
        row = {
            "id": str(uuid4()),
            "entity_type": entity_type,
            "entity_id": entity_id,
            "author": author,
            "body": body,
        }
        self.tables.setdefault("comments", {})[row["id"]] = dict(row)
        return row

    def add(self, table_name, payload):
        row = dict(payload)
        self.tables.setdefault(table_name, {})[row["id"]] = row
        return row

    def get(self, table_name, item_id):
        row = self.tables.get(table_name, {}).get(str(item_id))
        return dict(row) if row else None

    def remove(self, table_name, item_id):
        return self.tables.get(table_name, {}).pop(str(item_id), None) is not None

    def list_comments(self, entity_type, entity_id):
        return [
            dict(row)
            for row in self.tables.get("comments", {}).values()
            if row["entity_type"] == entity_type and row["entity_id"] == entity_id
        ]

    def create_notification(self, *, username, title, body):
        row = {
            "id": str(uuid4()),
            "username": username,
            "title": title,
            "body": body,
            "read": False,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self.tables.setdefault("notifications", {})[row["id"]] = row
        return row

    def list_notifications(self, username):
        return [
            dict(row)
            for row in self.tables.get("notifications", {}).values()
            if row["username"] == username
        ]

    def get_notification_by_id(self, notification_id):
        return self.get("notifications", notification_id)

    def mark_notification_read(self, notification_id):
        row = self.get("notifications", notification_id)
        if not row:
            return None
        row["read"] = True
        return self.add("notifications", row)

    def append_change_log(self, **row):
        row["id"] = str(uuid4())
        row["timestamp"] = datetime.now(timezone.utc).isoformat()
        row["details_json"] = "{}"
        self.tables.setdefault("entity_change_log", {})[row["id"]] = row

    def list_change_log(self, entity_type, entity_id, *, limit):
        return [
            dict(row)
            for row in self.tables.get("entity_change_log", {}).values()
            if row["entity_type"] == entity_type and row["entity_id"] == entity_id
        ][:limit]


def test_uc_native_comments_create_list_get_update_delete():
    overlays = FakeOverlays()
    manager = UcNativeCommentsManager(overlays)
    created = manager.create_comment(
        None,
        data=CommentCreate(
            entity_type="asset",
            entity_id="asset-1",
            comment="First comment",
        ),
        user_email="author@example.com",
    )

    listed = manager.list_comments(None, entity_type="asset", entity_id="asset-1")
    assert listed.total_count == 1
    assert manager.get_comment(None, comment_id=str(created.id)).comment == "First comment"
    assert manager.can_user_modify_comment(
        None, comment=created, user_email="author@example.com"
    )
    assert manager.update_comment(
        None,
        str(created.id),
        data=CommentUpdate(comment="Updated comment"),
        user_email="author@example.com",
    ).comment == "Updated comment"
    assert manager.delete_comment(
        None, str(created.id), user_email="author@example.com"
    )
    assert manager.get_comment(None, comment_id=str(created.id)) is None


def test_uc_native_comments_ratings_are_stored_and_aggregated():
    manager = UcNativeCommentsManager(FakeOverlays())
    manager.create_rating(
        None,
        entity_type="asset",
        entity_id="asset-1",
        rating=4,
        user_email="author@example.com",
    )

    ratings = manager.list_ratings(None, entity_type="asset", entity_id="asset-1")
    aggregation = manager.get_rating_aggregation(
        None,
        entity_type="asset",
        entity_id="asset-1",
        user_email="author@example.com",
    )
    assert ratings.comments[0].comment_type == CommentType.RATING
    assert aggregation.average_rating == 4
    assert aggregation.user_current_rating == 4


def test_uc_native_notifications_mark_read_and_delete():
    overlays = FakeOverlays()
    manager = UcNativeNotificationsManager(overlays, settings_manager=None)
    notification = Notification(
        id=str(uuid4()),
        type=NotificationType.INFO,
        title="Hello",
        message="World",
        created_at=datetime.now(timezone.utc),
        recipient="user@example.com",
    )

    manager.create_notification(db=None, notification=notification)
    created = manager.get_notifications(
        None, user_info=SimpleNamespace(email="user@example.com")
    )[0]
    assert manager.mark_notification_read(None, created.id).read is True
    assert manager.delete_notification(None, created.id) is True


def test_uc_native_change_log_lists_entity_rows():
    manager = UcNativeChangeLogManager(FakeOverlays())
    manager.log_change(
        None,
        entity_type="asset",
        entity_id="asset-1",
        action="UPDATE",
        username="author@example.com",
        details={"field": "name"},
    )

    changes = manager.list_changes_for_entity(
        None, entity_type="asset", entity_id="asset-1"
    )
    assert len(changes) == 1
    assert changes[0].action == "UPDATE"


def test_audit_log_action_writes_to_uc_store_without_postgres(monkeypatch, tmp_path):
    class FakeStore:
        def __init__(self):
            self.rows = []

        def merge_row(self, table_name, row):
            assert table_name == "audit_events"
            self.rows.append(dict(row))

        def list_rows(self, table_name, *, limit):
            assert table_name == "audit_events"
            return list(self.rows)[:limit]

    store = FakeStore()
    settings = SimpleNamespace(APP_AUDIT_LOG_DIR=str(tmp_path), APP_AUDIT_VOLUME_ONLY=True)
    manager = AuditManager(settings=settings, db_session=None, uc_store=store)
    monkeypatch.setattr(
        "src.controller.audit_manager.get_session_factory", lambda: None
    )

    manager.log_action(
        db=None,
        username="author@example.com",
        ip_address=None,
        feature="comments",
        action="CREATE",
        success=True,
        details={"comment_id": "comment-1"},
    )

    assert len(store.rows) == 1
    total, logs = asyncio.run(manager.get_audit_logs(db=None))
    assert total == 1
    assert logs[0].feature == "comments"
