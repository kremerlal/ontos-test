"""UC-native REST API for Delta-backed entities and overlays."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from src.common.authorization import PermissionChecker
from src.common.config import Settings, get_settings
from src.common.dependencies import CurrentUserDep
from src.common.features import FeatureAccessLevel
from src.common.storage_mode import StorageMode, resolve_storage_mode

router = APIRouter(prefix="/api/uc-native", tags=["UC Native"])


def _require_uc_native(settings: Settings = Depends(get_settings)) -> None:
    if resolve_storage_mode(settings) != StorageMode.UC_NATIVE:
        raise HTTPException(status_code=404, detail="Not in uc_native mode")


def _entities(request: Request):
    store = getattr(request.app.state, "uc_native_entities", None)
    if not store:
        raise HTTPException(status_code=503, detail="UC native entity store unavailable")
    return store


def _overlays(request: Request):
    store = getattr(request.app.state, "uc_native_overlays", None)
    if not store:
        raise HTTPException(status_code=503, detail="UC native overlay store unavailable")
    return store


def _workflows(request: Request):
    store = getattr(request.app.state, "uc_native_workflows", None)
    if not store:
        raise HTTPException(status_code=503, detail="UC native workflow store unavailable")
    return store


class EntityPayload(BaseModel):
    data: Dict[str, Any]


class CommentPayload(BaseModel):
    entity_type: str
    entity_id: str
    body: str


class GrantRequestPayload(BaseModel):
    resource: str
    details: Optional[Dict[str, Any]] = None


@router.get("/entities/{table_name}")
async def list_entities(
    table_name: str,
    request: Request,
    limit: int = 500,
    _: None = Depends(_require_uc_native),
    __: bool = Depends(PermissionChecker("data-products", FeatureAccessLevel.READ_ONLY)),
) -> Dict[str, Any]:
    items = _entities(request).list_entities(table_name, limit=limit)
    return {"items": items}


@router.get("/entities/{table_name}/{entity_id}")
async def get_entity(
    table_name: str,
    entity_id: str,
    request: Request,
    _: None = Depends(_require_uc_native),
    __: bool = Depends(PermissionChecker("data-products", FeatureAccessLevel.READ_ONLY)),
) -> Dict[str, Any]:
    item = _entities(request).get_entity(table_name, entity_id)
    if not item:
        raise HTTPException(status_code=404, detail="Entity not found")
    return item


@router.post("/entities/{table_name}")
async def create_entity(
    table_name: str,
    payload: EntityPayload,
    request: Request,
    _: None = Depends(_require_uc_native),
    __: bool = Depends(PermissionChecker("data-products", FeatureAccessLevel.READ_WRITE)),
) -> Dict[str, Any]:
    saved = _entities(request).save_entity(table_name, payload.data)
    return saved


@router.put("/entities/{table_name}/{entity_id}")
async def update_entity(
    table_name: str,
    entity_id: str,
    payload: EntityPayload,
    request: Request,
    _: None = Depends(_require_uc_native),
    __: bool = Depends(PermissionChecker("data-products", FeatureAccessLevel.READ_WRITE)),
) -> Dict[str, Any]:
    data = dict(payload.data)
    data["id"] = entity_id
    saved = _entities(request).save_entity(table_name, data)
    return saved


@router.delete("/entities/{table_name}/{entity_id}")
async def delete_entity(
    table_name: str,
    entity_id: str,
    request: Request,
    _: None = Depends(_require_uc_native),
    __: bool = Depends(PermissionChecker("data-products", FeatureAccessLevel.READ_WRITE)),
) -> Dict[str, str]:
    _entities(request).delete_entity(table_name, entity_id)
    return {"status": "deleted", "id": entity_id}


@router.post("/comments")
async def add_comment(
    payload: CommentPayload,
    request: Request,
    current_user: CurrentUserDep = None,
    _: None = Depends(_require_uc_native),
) -> Dict[str, Any]:
    overlays = _overlays(request)
    author = current_user.email if current_user else "unknown"
    return overlays.add_comment(
        entity_type=payload.entity_type,
        entity_id=payload.entity_id,
        author=author,
        body=payload.body,
    )


@router.get("/comments/{entity_type}/{entity_id}")
async def list_comments(
    entity_type: str,
    entity_id: str,
    request: Request,
    _: None = Depends(_require_uc_native),
) -> Dict[str, List[Dict[str, Any]]]:
    return {"items": _overlays(request).list_comments(entity_type, entity_id)}


@router.post("/access-grant-requests")
async def create_grant_request(
    payload: GrantRequestPayload,
    request: Request,
    current_user: CurrentUserDep = None,
    _: None = Depends(_require_uc_native),
    __: bool = Depends(PermissionChecker("access-grants", FeatureAccessLevel.READ_WRITE)),
) -> Dict[str, Any]:
    requester = current_user.email if current_user else "unknown"
    return _workflows(request).create_access_grant_request(
        requester=requester,
        resource=payload.resource,
        details=payload.details,
    )


@router.post("/access-grant-requests/{request_id}/approve")
async def approve_grant_request(
    request_id: str,
    request: Request,
    _: None = Depends(_require_uc_native),
    __: bool = Depends(PermissionChecker("access-grants", FeatureAccessLevel.ADMIN)),
) -> Dict[str, str]:
    from src.common.workspace_client import get_workspace_client

    ws = get_workspace_client(get_settings())
    _workflows(request).approve_access_grant(request_id, ws)
    return {"status": "approved", "id": request_id}


def register_routes(app) -> None:
    app.include_router(router)
