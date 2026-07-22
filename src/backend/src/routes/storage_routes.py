"""Storage profile API — capabilities, UC mirror sync, read-only UC queries."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.common.config import Settings, get_settings
from src.common.database import get_db
from src.common.storage_mode import (
    StorageMode,
    get_storage_capabilities,
    requires_oltp_database,
    resolve_storage_mode,
)
from src.common.workspace_client import get_workspace_client
from src.common.uc_readonly_store import (
    list_mirror_assets,
    list_mirror_contracts,
    list_mirror_products,
    list_mirror_table,
)
from src.common.authorization import PermissionChecker
from src.common.features import FeatureAccessLevel

router = APIRouter(prefix="/api/storage", tags=["System"])


class CapabilitiesResponse(BaseModel):
    mode: str
    oltp_configured: bool
    uc_configured: bool
    writes_enabled: bool
    available_capabilities: List[str]
    unavailable_capabilities: List[str]
    lakebase_required: bool
    postgres_alternative: bool
    uc_native_sor: bool = False


class MirrorSyncResponse(BaseModel):
    tables_ensured: List[str]
    row_counts: Dict[str, int]
    demo_artifacts: List[str]


def _oltp_configured(settings: Settings) -> bool:
    mode = resolve_storage_mode(settings)
    if mode in (StorageMode.UC_READONLY, StorageMode.UC_NATIVE):
        return False
    return bool(settings.PGHOST and settings.PGDATABASE)


def _uc_configured(settings: Settings) -> bool:
    return bool(settings.DATABRICKS_WAREHOUSE_ID and settings.DATABRICKS_CATALOG)


@router.get("/capabilities", response_model=CapabilitiesResponse)
async def get_capabilities(settings: Settings = Depends(get_settings)) -> Dict[str, Any]:
    mode = resolve_storage_mode(settings)
    return get_storage_capabilities(
        mode,
        oltp_configured=_oltp_configured(settings),
        uc_configured=_uc_configured(settings),
    )


@router.post("/mirror/sync", response_model=MirrorSyncResponse)
async def sync_uc_mirror(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _: bool = Depends(PermissionChecker("settings-general", FeatureAccessLevel.ADMIN)),
):
    if not settings.APP_UC_MIRROR_ENABLED:
        raise HTTPException(status_code=400, detail="UC mirror export is disabled")
    if not _uc_configured(settings):
        raise HTTPException(status_code=400, detail="Unity Catalog / warehouse not configured")
    mode = resolve_storage_mode(settings)
    if not requires_oltp_database(mode):
        raise HTTPException(
            status_code=400,
            detail="OLTP database required to export mirrors",
        )

    from src.common.uc_mirror import (
        ensure_mirror_tables,
        sync_all_from_session,
        upload_demo_packs_to_volume,
    )

    ws = get_workspace_client(settings)
    tables = ensure_mirror_tables(ws, settings)
    row_counts = sync_all_from_session(ws, settings, db)
    data_dir = Path(__file__).resolve().parents[1] / "data"
    demo_artifacts = upload_demo_packs_to_volume(ws, settings, data_dir)

    return MirrorSyncResponse(
        tables_ensured=tables,
        row_counts=row_counts,
        demo_artifacts=demo_artifacts,
    )


@router.get("/readonly/products")
async def readonly_products(
    limit: int = 100,
    settings: Settings = Depends(get_settings),
):
    mode = resolve_storage_mode(settings)
    if mode == StorageMode.UC_NATIVE:
        raise HTTPException(
            status_code=404,
            detail="Use standard /api/data-products in uc_native mode",
        )
    if mode != StorageMode.UC_READONLY and requires_oltp_database(mode):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Read-only UC endpoint only active in uc_readonly mode",
        )
    ws = get_workspace_client(settings)
    return {"items": list_mirror_products(ws, settings, limit=limit)}


@router.get("/readonly/contracts")
async def readonly_contracts(
    limit: int = 100,
    settings: Settings = Depends(get_settings),
):
    mode = resolve_storage_mode(settings)
    if mode != StorageMode.UC_READONLY and requires_oltp_database(mode):
        raise HTTPException(status_code=404, detail="Not in uc_readonly mode")
    ws = get_workspace_client(settings)
    return {"items": list_mirror_contracts(ws, settings, limit=limit)}


@router.get("/readonly/assets")
async def readonly_assets(
    limit: int = 100,
    settings: Settings = Depends(get_settings),
):
    mode = resolve_storage_mode(settings)
    if mode != StorageMode.UC_READONLY and requires_oltp_database(mode):
        raise HTTPException(status_code=404, detail="Not in uc_readonly mode")
    ws = get_workspace_client(settings)
    return {"items": list_mirror_assets(ws, settings, limit=limit)}


@router.get("/readonly/mirrors/{table_name}")
async def readonly_mirror_table(
    table_name: str,
    limit: int = 100,
    settings: Settings = Depends(get_settings),
):
    if resolve_storage_mode(settings) != StorageMode.UC_READONLY:
        raise HTTPException(status_code=404, detail="Not in uc_readonly mode")
    try:
        items = list_mirror_table(
            get_workspace_client(settings),
            settings,
            table_name,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"items": items}


def register_routes(app) -> None:
    app.include_router(router)
