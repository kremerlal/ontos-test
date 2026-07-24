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

    def update_comment(self, db, comment_id: str, comment_in=None, **_) -> Optional[Comment]:
        row = self._overlays.get("comments", str(comment_id))
        if not row:
            return None
        data = comment_in.model_dump(exclude_unset=True) if hasattr(comment_in, "model_dump") else dict(comment_in or {})
        row["body"] = data.get("comment", data.get("body", row.get("body", "")))
        saved = self._overlays.add("comments", row)
        now = datetime.now(timezone.utc)
        return Comment(id=UUID(str(saved["id"])), entity_type=saved.get("entity_type", ""), entity_id=saved.get("entity_id", ""), comment=saved.get("body", ""), created_by=saved.get("author", "unknown"), created_at=now, updated_at=now)

    def delete_comment(self, db, comment_id: str, **_) -> bool:
        return self._overlays.remove("comments", str(comment_id))


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
        row = self._overlays.mark_notification_read(str(notification_id))
        if not row:
            return None
        return Notification(id=row["id"], title=row.get("title", ""), message=row.get("body", ""), type=NotificationType.INFO, read=True, created_at=datetime.now(timezone.utc), recipient=row.get("username"))

    def get_notification_by_id(self, db, notification_id: str) -> Optional[Notification]:
        row = self._overlays.get_notification_by_id(str(notification_id))
        if not row:
            return None
        return Notification(id=row["id"], title=row.get("title", ""), message=row.get("body", ""), type=NotificationType.INFO, read=bool(row.get("read")), created_at=datetime.now(timezone.utc), recipient=row.get("username"))


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

    def cancel_run(self, *_, **__) -> bool: return False
    def get_workflow_statuses(self, *_, **__) -> List[Dict[str, Any]]: return []
    def run_job(self, *_, **__) -> Optional[int]: return None
    def get_active_run_id(self, *_, **__) -> Optional[int]: return None
    def pause_job(self, *_, **__) -> bool: return False
    def resume_job(self, *_, **__) -> bool: return False
    def get_job_status(self, *_, **__) -> Dict[str, Any]: return {}
    def get_workflow_parameter_definitions(self, *_, **__) -> List[Dict[str, Any]]: return []
    def get_workflow_configuration(self, *_, **__) -> Dict[str, Any]: return {}
    def update_workflow_configuration(self, *_, **__) -> Dict[str, Any]: return {}
