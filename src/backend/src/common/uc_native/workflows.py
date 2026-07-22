"""Workflow and grant session storage for uc_native mode."""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional

from databricks.sdk import WorkspaceClient

from src.common.config import Settings
from src.common.logging import get_logger
from src.common.uc_mirror import volume_root
from src.common.uc_native.delta_store import DeltaStore

logger = get_logger(__name__)


class UcNativeWorkflowStore:
    def __init__(
        self,
        store: DeltaStore,
        ws_client: WorkspaceClient,
        settings: Settings,
    ) -> None:
        self._store = store
        self._ws = ws_client
        self._settings = settings

    def save_wizard_session(
        self,
        *,
        workflow_id: str,
        username: str,
        state: Dict[str, Any],
        session_id: Optional[str] = None,
        etag: Optional[str] = None,
    ) -> Dict[str, Any]:
        sid = session_id or str(uuid.uuid4())
        row = {
            "id": sid,
            "workflow_id": workflow_id,
            "username": username,
            "status": state.get("status", "in_progress"),
            "etag": etag or str(uuid.uuid4()),
            "snapshot_json": json.dumps(state, default=str),
        }
        self._store.merge_row("wizard_sessions", row)
        self._write_volume_session(sid, state)
        return {"id": sid, "etag": row["etag"], **state}

    def get_wizard_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        row = self._store.get_by_id("wizard_sessions", session_id)
        if not row:
            return None
        state = self._store.parse_snapshot(row)
        state["id"] = session_id
        state["etag"] = row.get("etag")
        return state

    def list_workflow_definitions(self) -> List[Dict[str, Any]]:
        return self._store.list_rows("process_workflows", limit=200)

    def save_installation(
        self,
        *,
        workflow_key: str,
        job_id: int,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        install_id = str(uuid.uuid4())
        self._store.merge_row(
            "workflow_installations",
            {
                "id": install_id,
                "workflow_key": workflow_key,
                "job_id": job_id,
                "snapshot_json": json.dumps(metadata or {}),
            },
        )
        return install_id

    def create_access_grant_request(
        self,
        *,
        requester: str,
        resource: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        req_id = str(uuid.uuid4())
        payload = {
            "id": req_id,
            "requester": requester,
            "resource": resource,
            "status": "pending",
            "snapshot_json": json.dumps(details or {}),
        }
        self._store.merge_row("access_grant_requests", payload)
        return payload

    def approve_access_grant(self, request_id: str, ws_client: WorkspaceClient) -> None:
        row = self._store.get_by_id("access_grant_requests", request_id)
        if not row:
            raise ValueError(f"Access grant request not found: {request_id}")
        details = self._store.parse_snapshot(row)
        securable = details.get("securable") or row.get("resource")
        principal = details.get("principal") or row.get("requester")
        privilege = details.get("privilege", "SELECT")
        if securable and principal:
            try:
                ws_client.grants.update(
                    securable_type=details.get("securable_type", "TABLE"),
                    full_name=securable,
                    changes=[{"principal": principal, "add": [privilege]}],
                )
            except Exception as exc:
                logger.warning("UC grant update failed for %s: %s", request_id, exc)
        row_update = dict(row)
        row_update["status"] = "approved"
        self._store.merge_row("access_grant_requests", row_update)

    def _write_volume_session(self, session_id: str, state: Dict[str, Any]) -> None:
        try:
            path = f"{volume_root(self._settings)}/wizard-sessions/{session_id}.json"
            self._ws.files.upload(
                path,
                json.dumps(state, default=str).encode("utf-8"),
                overwrite=True,
            )
        except Exception as exc:
            logger.debug("Volume wizard session write skipped: %s", exc)
