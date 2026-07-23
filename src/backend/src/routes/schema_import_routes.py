"""
Routes for the Schema Importer feature.

Provides endpoints to browse a remote system via an existing connection
and import selected resources as persisted Ontos assets.
"""

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request

from src.common.dependencies import (
    AuditCurrentUserDep,
    AuditManagerDep,
    DBSessionDep,
)
from src.common.config import get_settings
from src.common.workspace_client import get_obo_workspace_client
from src.common.logging import get_logger
from src.common.authorization import PermissionChecker
from src.common.features import FeatureAccessLevel
from src.controller.connections_manager import ConnectionsManager
from src.controller.schema_import_manager import SchemaImportManager
from src.models.schema_import import (
    BrowseResponse,
    ImportPreviewItem,
    ImportRequest,
    ImportResult,
)
from src.models.assets import AssetMetadata

logger = get_logger(__name__)

router = APIRouter(prefix="/api/schema-import", tags=["Schema Import"])

FEATURE_ID = "schema-importer"


# ------------------------------------------------------------------
# Dependency: build SchemaImportManager per request
# ------------------------------------------------------------------

def _get_manager(request: Request, db: DBSessionDep) -> SchemaImportManager:
    settings = get_settings()
    ws = None
    try:
        ws = get_obo_workspace_client(request, settings)
    except Exception:
        pass

    connections_mgr = getattr(request.app.state, "connections_manager", None)
    if connections_mgr is None:
        connections_mgr = ConnectionsManager(db=db, workspace_client=ws)

    from src.common.manager_dependencies import get_assets_manager
    assets_mgr = get_assets_manager(request)

    return SchemaImportManager(
        connections_manager=connections_mgr,
        assets_manager=assets_mgr,
    )


# ------------------------------------------------------------------
# Browse
# ------------------------------------------------------------------

@router.get(
    "/browse/{connection_id}",
    response_model=BrowseResponse,
    summary="Browse remote system hierarchy",
)
async def browse(
    connection_id: UUID,
    path: Optional[str] = None,
    db: DBSessionDep = None,
    manager: SchemaImportManager = Depends(_get_manager),
    _: bool = Depends(PermissionChecker(FEATURE_ID, FeatureAccessLevel.READ_ONLY)),
):
    """Browse the remote system for a given connection, optionally drilling into a path."""
    try:
        return manager.browse(db=db, connection_id=connection_id, path=path)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        logger.error(f"Browse failed for connection {connection_id}: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


# ------------------------------------------------------------------
# Metadata (get asset metadata via connector for schema inference)
# ------------------------------------------------------------------

@router.get(
    "/metadata/{connection_id}",
    response_model=AssetMetadata,
    summary="Get asset metadata via connector",
)
async def get_asset_metadata(
    connection_id: UUID,
    path: str,
    db: DBSessionDep = None,
    manager: SchemaImportManager = Depends(_get_manager),
    _: bool = Depends(PermissionChecker(FEATURE_ID, FeatureAccessLevel.READ_ONLY)),
):
    """Fetch detailed metadata (including schema/columns) for an asset via its connector."""
    try:
        connector = manager._connections.get_connector_for_connection(connection_id)
        if connector is None:
            raise HTTPException(status_code=404, detail=f"Connection '{connection_id}' not found")
        metadata = connector.get_asset_metadata(path)
        if metadata is None:
            raise HTTPException(status_code=404, detail=f"No metadata found for path '{path}'")
        return metadata
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Metadata fetch failed for {connection_id}/{path}: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


# ------------------------------------------------------------------
# Preview (dry-run import)
# ------------------------------------------------------------------

@router.post(
    "/preview",
    response_model=list[ImportPreviewItem],
    summary="Preview what an import would create or skip",
)
async def preview_import(
    payload: ImportRequest,
    db: DBSessionDep = None,
    manager: SchemaImportManager = Depends(_get_manager),
    _: bool = Depends(PermissionChecker(FEATURE_ID, FeatureAccessLevel.READ_WRITE)),
):
    """Return a list of items that would be created or skipped without persisting anything."""
    try:
        return manager.preview_import(db=db, request=payload)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        logger.error(f"Preview failed: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


# ------------------------------------------------------------------
# Execute import
# ------------------------------------------------------------------

@router.post(
    "/import",
    response_model=ImportResult,
    summary="Import selected resources as Ontos assets",
)
async def execute_import(
    payload: ImportRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    db: DBSessionDep = None,
    manager: SchemaImportManager = Depends(_get_manager),
    audit_manager: AuditManagerDep = None,
    current_user: AuditCurrentUserDep = None,
    _: bool = Depends(PermissionChecker(FEATURE_ID, FeatureAccessLevel.READ_WRITE)),
):
    """Import selected remote resources (and nested children) as persisted Ontos assets."""
    try:
        user_id = current_user.username if current_user else "system"
        result = manager.execute_import(db=db, request=payload, current_user_id=user_id)

        if audit_manager and current_user:
            background_tasks.add_task(
                audit_manager.log_action_background,
                username=current_user.username,
                ip_address=request.client.host if request.client else None,
                feature=FEATURE_ID,
                action="SCHEMA_IMPORT",
                success=True,
                details={
                    "connection_id": str(payload.connection_id),
                    "created": result.created,
                    "skipped": result.skipped,
                    "errors": result.errors,
                },
            )

        return result
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        logger.error(f"Import failed: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


# ------------------------------------------------------------------
# Registration
# ------------------------------------------------------------------

def register_routes(app):
    """Register schema import routes with the app."""
    app.include_router(router)
    logger.info("Schema import routes registered")
