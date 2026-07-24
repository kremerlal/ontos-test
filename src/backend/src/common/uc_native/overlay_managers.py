"""UC-native managers for overlays, notifications, comments, and jobs metadata."""

from __future__ import annotations

import json
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
