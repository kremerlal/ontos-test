"""UC-native managers for overlays, notifications, comments, and jobs metadata."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

from src.common.logging import get_logger
from src.common.uc_native.overlays import UcNativeOverlayStore
from src.common.uc_native.workflows import UcNativeWorkflowStore
from src.models.comments import (
    Comment,
    CommentCreate,
    CommentListResponse,
    CommentType,
    RatingAggregation,
)
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
        comment_in: Optional[CommentCreate] = None,
        data: Optional[CommentCreate] = None,
        author_email: Optional[str] = None,
        user_email: Optional[str] = None,
        **_,
    ) -> Comment:
        comment_in = comment_in or data
        if comment_in is None:
            raise ValueError("Comment data is required")
        author_email = author_email or user_email or "unknown"
        row = self._overlays.add_comment(
            entity_type=comment_in.entity_type,
            entity_id=str(comment_in.entity_id),
            author=author_email,
            body=comment_in.comment,
        )
        row.update(
            {
                "title": comment_in.title,
                "audience": comment_in.audience,
                "project_id": comment_in.project_id,
                "comment_type": comment_in.comment_type.value,
                "rating": comment_in.rating,
            }
        )
        return self._to_comment(self._overlays.add("comments", row))

    def _to_comment(self, row: Dict[str, Any]) -> Comment:
        now = datetime.now(timezone.utc)
        comment_type = row.get("comment_type", CommentType.COMMENT)
        return Comment(
            id=UUID(str(row.get("id"))),
            entity_type=row.get("entity_type", ""),
            entity_id=str(row.get("entity_id", "")),
            title=row.get("title"),
            comment=row.get("body", row.get("comment", "")),
            audience=row.get("audience"),
            project_id=row.get("project_id"),
            comment_type=comment_type,
            rating=row.get("rating"),
            created_by=row.get("author", row.get("created_by", "unknown")),
            updated_by=row.get("updated_by"),
            created_at=now,
            updated_at=now,
        )

    def list_comments(self, db, *, entity_type: str, entity_id: str, **_) -> CommentListResponse:
        rows = self._overlays.list_comments(entity_type, entity_id)
        comments = [
            self._to_comment({**row, "entity_type": entity_type, "entity_id": entity_id})
            for row in rows
            if row.get("comment_type", CommentType.COMMENT) != CommentType.RATING.value
        ]
        return CommentListResponse(
            comments=comments, total_count=len(comments), visible_count=len(comments)
        )

    def get_comment(self, db, *, comment_id: str, **_) -> Optional[Comment]:
        row = self._overlays.get("comments", str(comment_id))
        return self._to_comment(row) if row else None

    def can_user_modify_comment(
        self,
        db,
        *,
        comment: Optional[Comment] = None,
        comment_id: Optional[str] = None,
        user_email: str,
        user_groups: Optional[List[str]] = None,
        is_admin: bool = False,
        **_,
    ) -> bool:
        if comment is None and comment_id:
            comment = self.get_comment(db, comment_id=comment_id)
        return bool(comment and comment.created_by == user_email)

    def update_comment(
        self,
        db,
        comment_id: str,
        comment_in=None,
        data=None,
        user_email: Optional[str] = None,
        **_,
    ) -> Optional[Comment]:
        row = self._overlays.get("comments", str(comment_id))
        if not row or (user_email and row.get("author") != user_email):
            return None
        updates = data or comment_in
        data = updates.model_dump(exclude_unset=True) if hasattr(updates, "model_dump") else dict(updates or {})
        row["body"] = data.get("comment", data.get("body", row.get("body", "")))
        row.update({key: value for key, value in data.items() if key in {"title", "audience"}})
        row["updated_by"] = user_email
        saved = self._overlays.add("comments", row)
        return self._to_comment(saved)

    def delete_comment(
        self, db, comment_id: str, user_email: Optional[str] = None, **_
    ) -> bool:
        row = self._overlays.get("comments", str(comment_id))
        if not row or (user_email and row.get("author") != user_email):
            return False
        return self._overlays.remove("comments", str(comment_id))

    def create_rating(
        self,
        db,
        *,
        entity_type: str,
        entity_id: str,
        rating: int,
        comment: Optional[str] = None,
        project_id: Optional[str] = None,
        user_email: str,
    ) -> Comment:
        rating_comment = CommentCreate(
            entity_type=entity_type,
            entity_id=str(entity_id),
            comment=comment or f"{rating} star rating",
            comment_type=CommentType.RATING,
            rating=rating,
            project_id=project_id,
        )
        return self.create_comment(db, data=rating_comment, user_email=user_email)

    def list_ratings(
        self,
        db,
        *,
        entity_type: str,
        entity_id: str,
        user_email: Optional[str] = None,
    ) -> CommentListResponse:
        rows = self._overlays.list_comments(entity_type, entity_id)
        ratings = [
            self._to_comment({**row, "entity_type": entity_type, "entity_id": entity_id})
            for row in rows
            if row.get("comment_type") == CommentType.RATING.value
            and (user_email is None or row.get("author") == user_email)
        ]
        return CommentListResponse(
            comments=ratings, total_count=len(ratings), visible_count=len(ratings)
        )

    def get_rating_aggregation(
        self,
        db,
        *,
        entity_type: str,
        entity_id: str,
        user_email: Optional[str] = None,
    ) -> RatingAggregation:
        ratings = self.list_ratings(db, entity_type=entity_type, entity_id=entity_id)
        distribution = {value: 0 for value in range(1, 6)}
        user_current_rating = None
        for item in ratings.comments:
            if item.rating:
                distribution[item.rating] += 1
                if item.created_by == user_email and user_current_rating is None:
                    user_current_rating = item.rating
        total = sum(distribution.values())
        average = sum(value * count for value, count in distribution.items()) / total if total else 0.0
        return RatingAggregation(
            entity_type=entity_type,
            entity_id=entity_id,
            average_rating=round(average, 2),
            total_ratings=total,
            distribution=distribution,
            user_current_rating=user_current_rating,
        )


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

    def create_notification(self, notification: Notification = None, db=None, **kwargs) -> Notification:
        notification = notification or kwargs.get("notification")
        if notification is None:
            raise ValueError("Notification is required")
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

    def delete_notification(self, db, notification_id: str) -> bool:
        return self._overlays.remove("notifications", str(notification_id))

    def can_user_access_notification(
        self, db, notification: Notification, user_info: UserInfo
    ) -> bool:
        return bool(notification.recipient and notification.recipient == user_info.email)


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

    def list_changes_for_entity(
        self, db, *, entity_type: str, entity_id: str, limit: int = 100, **_
    ) -> List[Any]:
        from types import SimpleNamespace

        rows = self._overlays.list_change_log(entity_type, str(entity_id), limit=limit)
        changes = []
        for row in rows:
            timestamp = row.get("timestamp")
            if isinstance(timestamp, str):
                try:
                    timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                except ValueError:
                    timestamp = datetime.now(timezone.utc)
            changes.append(
                SimpleNamespace(
                    id=row.get("id", str(uuid4())),
                    entity_type=row.get("entity_type", entity_type),
                    entity_id=row.get("entity_id", str(entity_id)),
                    action=row.get("action", ""),
                    username=row.get("username"),
                    timestamp=timestamp or datetime.now(timezone.utc),
                    details_json=row.get("details_json"),
                )
            )
        return changes


class UcNativeJobsManager:
    """Jobs API adapter with UC Delta-backed workflow installation metadata."""

    def __init__(self, workflows: UcNativeWorkflowStore, ws_client, settings) -> None:
        self._workflows = workflows
        self._client = ws_client
        self._settings = settings

    def list_installations(self, db=None) -> List[Dict[str, Any]]:
        store = getattr(self._workflows, "_store", None)
        if store and hasattr(store, "list_rows"):
            try:
                return [self._hydrate_installation(row) for row in store.list_rows("workflow_installations", limit=200)]
            except Exception:
                logger.warning("Failed to list UC-native workflow installations", exc_info=True)
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

    def _hydrate_installation(self, row: Dict[str, Any]) -> Dict[str, Any]:
        store = getattr(self._workflows, "_store", None)
        snapshot = store.parse_snapshot(row) if store and hasattr(store, "parse_snapshot") else {}
        return {**row, **snapshot, "workflow_id": row.get("workflow_key", snapshot.get("workflow_id"))}

    def _find_installation(self, workflow_id: str) -> Optional[Dict[str, Any]]:
        return next(
            (
                installation
                for installation in self.list_installations()
                if installation.get("workflow_id") == workflow_id
                or installation.get("workflow_key") == workflow_id
            ),
            None,
        )

    def cancel_run(self, run_id: int) -> bool:
        if not self._client:
            return False
        self._client.jobs.cancel_run(run_id=run_id)
        return True

    def run_job(
        self,
        job_id: int,
        job_name: Optional[str] = None,
        job_parameters: Optional[Dict[str, str]] = None,
        workflow_id: Optional[str] = None,
    ) -> Optional[int]:
        if not self._client:
            return None
        parameters = self.get_workflow_configuration(workflow_id) if workflow_id else {}
        if job_parameters:
            parameters.update(job_parameters)
        kwargs: Dict[str, Any] = {"job_id": job_id}
        if parameters:
            kwargs["job_parameters"] = {key: str(value) for key, value in parameters.items()}
        run = self._client.jobs.run_now(**kwargs)
        return int(run.run_id)

    def get_active_run_id(self, job_id: int) -> Optional[int]:
        if not self._client:
            return None
        try:
            for run in self._client.jobs.list_runs(job_id=job_id, active_only=True):
                if getattr(run, "run_id", None) is not None:
                    return int(run.run_id)
        except Exception:
            logger.warning("Failed to list active runs for job %s", job_id, exc_info=True)
        return None

    def _set_pause_status(self, job_id: int, paused: bool) -> bool:
        if not self._client:
            return False
        try:
            from databricks.sdk.service import jobs

            job = self._client.jobs.get(job_id=job_id)
            settings = getattr(job, "settings", None)
            if not settings:
                return False
            pause_status = jobs.PauseStatus.PAUSED if paused else jobs.PauseStatus.UNPAUSED
            schedule = getattr(settings, "schedule", None)
            if schedule:
                new_settings = jobs.JobSettings(
                    schedule=jobs.CronSchedule(
                        quartz_cron_expression=schedule.quartz_cron_expression,
                        timezone_id=schedule.timezone_id or "UTC",
                        pause_status=pause_status,
                    )
                )
            elif getattr(settings, "continuous", None):
                new_settings = jobs.JobSettings(continuous=jobs.Continuous(pause_status=pause_status))
            else:
                return False
            self._client.jobs.update(job_id=job_id, new_settings=new_settings)
            return True
        except Exception:
            logger.warning("Failed to update pause status for job %s", job_id, exc_info=True)
            return False

    def pause_job(self, job_id: int) -> bool:
        return self._set_pause_status(job_id, paused=True)

    def resume_job(self, job_id: int) -> bool:
        return self._set_pause_status(job_id, paused=False)

    def get_job_status(self, run_id: int) -> Optional[Dict[str, Any]]:
        if not self._client:
            return None
        try:
            run = self._client.jobs.get_run(run_id=run_id)
            state = getattr(run, "state", None)
            return {
                "run_id": run_id,
                "job_id": getattr(run, "job_id", None),
                "life_cycle_state": getattr(getattr(state, "life_cycle_state", None), "value", getattr(state, "life_cycle_state", None)),
                "result_state": getattr(getattr(state, "result_state", None), "value", getattr(state, "result_state", None)),
                "start_time": getattr(run, "start_time", None),
                "end_time": getattr(run, "end_time", None),
            }
        except Exception:
            logger.warning("Failed to get status for run %s", run_id, exc_info=True)
            return None

    def get_workflow_statuses(self, workflow_ids: Optional[List[str]] = None) -> Dict[str, Any]:
        statuses: Dict[str, Any] = {}
        for installation in self.list_installations():
            workflow_id = installation.get("workflow_id") or installation.get("workflow_key")
            if not workflow_id or (workflow_ids and workflow_id not in workflow_ids):
                continue
            job_id = installation.get("job_id")
            active_run_id = self.get_active_run_id(int(job_id)) if job_id is not None else None
            statuses[workflow_id] = {
                "installed": True,
                "job_id": job_id,
                "is_running": active_run_id is not None,
                "current_run_id": active_run_id,
            }
        return statuses

    def get_workflow_parameter_definitions(self, workflow_id: str) -> List[Dict[str, Any]]:
        installation = self._find_installation(workflow_id)
        if not installation:
            return []
        definitions = installation.get("parameter_definitions", [])
        return definitions if isinstance(definitions, list) else []

    def get_workflow_configuration(self, workflow_id: str) -> Dict[str, Any]:
        installation = self._find_installation(workflow_id)
        if not installation:
            return {}
        configuration = installation.get("configuration", {})
        return dict(configuration) if isinstance(configuration, dict) else {}

    def update_workflow_configuration(
        self, workflow_id: str, configuration: Dict[str, Any]
    ):
        installation = self._find_installation(workflow_id)
        if not installation:
            return {}
        store = getattr(self._workflows, "_store", None)
        if store and hasattr(store, "merge_row"):
            snapshot = {
                key: value
                for key, value in installation.items()
                if key not in {"snapshot_json", "workflow_id"}
            }
            snapshot["configuration"] = dict(configuration)
            row = {
                "id": installation["id"],
                "workflow_key": installation.get("workflow_key", workflow_id),
                "job_id": installation.get("job_id"),
                "snapshot_json": json.dumps(snapshot, default=str),
            }
            store.merge_row("workflow_installations", row)
        try:
            from src.models.workflow_configurations import WorkflowConfiguration

            return WorkflowConfiguration(workflow_id=workflow_id, configuration=dict(configuration))
        except Exception:
            return {"workflow_id": workflow_id, "configuration": dict(configuration)}
