"""UC-native managers for overlays, notifications, comments, and jobs metadata."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

from src.common.logging import get_logger
from src.common.uc_native.overlays import UcNativeOverlayStore
from src.common.uc_native.workflows import UcNativeWorkflowStore
from src.models.comments import Comment, CommentCreate
from src.models.notifications import Notification, NotificationType
from src.models.users import UserInfo

logger = get_logger(__name__)


class UcNativeCommentsManager:
    def __init__(self, overlays: UcNativeOverlayStore) -> None:
        self._overlays = overlays

    def create_comment(
        self,
        db,
        *,
        comment_in: CommentCreate,
        author_email: str,
        **_,
    ) -> Comment:
        row = self._overlays.add_comment(
            entity_type=comment_in.entity_type,
            entity_id=str(comment_in.entity_id),
            author=author_email,
            body=comment_in.comment,
        )
        now = datetime.now(timezone.utc)
        return Comment(
            id=UUID(row["id"]),
            entity_type=comment_in.entity_type,
            entity_id=str(comment_in.entity_id),
            comment=comment_in.comment,
            created_by=author_email,
            created_at=now,
            updated_at=now,
        )

    def list_comments(self, db, *, entity_type: str, entity_id: str, **_) -> List[Comment]:
        rows = self._overlays.list_comments(entity_type, entity_id)
        comments: List[Comment] = []
        for row in rows:
            now = datetime.now(timezone.utc)
            comments.append(
                Comment(
                    id=UUID(row.get("id", str(uuid4()))),
                    entity_type=entity_type,
                    entity_id=entity_id,
                    comment=row.get("body", ""),
                    created_by=row.get("author", "unknown"),
                    created_at=now,
                    updated_at=now,
                )
            )
        return comments


class UcNativeNotificationsManager:
    def __init__(self, overlays: UcNativeOverlayStore, settings_manager) -> None:
        self._overlays = overlays
        self._settings_manager = settings_manager

    def get_notifications(
        self,
        db,
        user_info: Optional[UserInfo] = None,
    ) -> List[Notification]:
        if not user_info:
            return []
        rows = self._overlays.list_notifications(user_info.email)
        items: List[Notification] = []
        for row in rows:
            created = row.get("created_at")
            if isinstance(created, str):
                try:
                    created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                except ValueError:
                    created_dt = datetime.now(timezone.utc)
            else:
                created_dt = datetime.now(timezone.utc)
            items.append(
                Notification(
                    id=row.get("id", str(uuid4())),
                    title=row.get("title", ""),
                    message=row.get("body", ""),
                    type=NotificationType.INFO,
                    read=bool(row.get("read")),
                    created_at=created_dt,
                    recipient=user_info.email,
                )
            )
        return items

    def create_notification(self, notification: Notification, db) -> Notification:
        self._overlays.create_notification(
            username=notification.recipient or "unknown",
            title=notification.title,
            body=notification.message or notification.description or "",
        )
        return notification

    def mark_notification_read(self, db, notification_id: str) -> Optional[Notification]:
        return None

    def get_notification_by_id(self, db, notification_id: str) -> Optional[Notification]:
        return None


class UcNativeChangeLogManager:
    def __init__(self, overlays: UcNativeOverlayStore) -> None:
        self._overlays = overlays

    def log_change(
        self,
        db,
        *,
        entity_type: str,
        entity_id: str,
        action: str,
        username: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._overlays.append_change_log(
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            username=username,
            details=details,
        )


class UcNativeJobsManager:
    """Minimal Jobs manager for uc_native — run history via Jobs API, installs in Delta."""

    def __init__(self, workflows: UcNativeWorkflowStore, ws_client, settings) -> None:
        self._workflows = workflows
        self._client = ws_client
        self._settings = settings

    def list_installations(self, db=None) -> List[Dict[str, Any]]:
        return self._workflows.list_workflow_definitions()

    def record_installation(
        self,
        workflow_key: str,
        job_id: int,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        return self._workflows.save_installation(
            workflow_key=workflow_key,
            job_id=job_id,
            metadata=metadata,
        )
