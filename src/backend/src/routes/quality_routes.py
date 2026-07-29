from __future__ import annotations
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from src.common.dependencies import DBSessionDep, CurrentUserDep, AuditManagerDep, AuditCurrentUserDep, DataProductsManagerDep
from src.common.features import FeatureAccessLevel
from src.common.authorization import PermissionChecker
from src.common.logging import get_logger
from src.controller.quality_manager import QualityManager
from src.models.quality import QualityItem, QualityItemCreate, QualityItemUpdate, QualitySummary

logger = get_logger(__name__)

router = APIRouter(prefix="/api", tags=["Quality"])

FEATURE_ID = "data-domains"


def get_quality_manager(request: Request) -> QualityManager:
    """Use the manager wired at startup, falling back to the Postgres one.

    uc_native puts a Delta-backed quality manager on app.state; instantiating
    QualityManager() unconditionally would hand every route a Postgres
    repository with no OLTP session behind it.
    """
    return getattr(request.app.state, "quality_manager", None) or QualityManager()


# ── CRUD ─────────────────────────────────────────────────────────────────


@router.post(
    "/entities/{entity_type}/{entity_id}/quality-items",
    response_model=QualityItem,
    status_code=status.HTTP_201_CREATED,
)
async def create_quality_item(
    entity_type: str,
    entity_id: str,
    payload: QualityItemCreate,
    request: Request,
    db: DBSessionDep,
    current_user: CurrentUserDep,
    audit_manager: AuditManagerDep,
    audit_user: AuditCurrentUserDep,
    manager: QualityManager = Depends(get_quality_manager),
    _: bool = Depends(PermissionChecker(FEATURE_ID, FeatureAccessLevel.READ_WRITE)),
):
    success = False
    details = {
        "params": {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "dimension": payload.dimension,
            "source": payload.source,
            "score_percent": payload.score_percent,
        }
    }
    try:
        if payload.entity_type != entity_type or payload.entity_id != entity_id:
            raise HTTPException(status_code=400, detail="Entity path does not match body")
        result = manager.create(db, data=payload, user_email=current_user.email)
        success = True
        details["quality_item_id"] = str(result.id)
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed creating quality item for %s/%s", entity_type, entity_id)
        details["exception"] = {"type": type(e).__name__, "message": str(e)}
        raise HTTPException(status_code=500, detail="Failed to create quality item")
    finally:
        audit_manager.log_action(
            db=db,
            username=audit_user.username,
            ip_address=request.client.host if request.client else None,
            feature="quality",
            action="CREATE",
            success=success,
            details=details,
        )


@router.get(
    "/entities/{entity_type}/{entity_id}/quality-items",
    response_model=List[QualityItem],
)
async def list_quality_items(
    entity_type: str,
    entity_id: str,
    limit: Optional[int] = Query(None, ge=1, le=500, description="Max items to return"),
    db: DBSessionDep = None,
    manager: QualityManager = Depends(get_quality_manager),
    _: bool = Depends(PermissionChecker(FEATURE_ID, FeatureAccessLevel.READ_ONLY)),
):
    return manager.list(db, entity_type=entity_type, entity_id=entity_id, limit=limit)


@router.get(
    "/entities/{entity_type}/{entity_id}/quality-items/summary",
    response_model=QualitySummary,
)
async def summarize_quality_items(
    entity_type: str,
    entity_id: str,
    db: DBSessionDep = None,
    manager: QualityManager = Depends(get_quality_manager),
    _: bool = Depends(PermissionChecker(FEATURE_ID, FeatureAccessLevel.READ_ONLY)),
):
    return manager.summarize(db, entity_type=entity_type, entity_id=entity_id)


@router.put("/quality-items/{id}", response_model=QualityItem)
async def update_quality_item(
    id: str,
    payload: QualityItemUpdate,
    request: Request,
    db: DBSessionDep,
    current_user: CurrentUserDep,
    audit_manager: AuditManagerDep,
    audit_user: AuditCurrentUserDep,
    manager: QualityManager = Depends(get_quality_manager),
    _: bool = Depends(PermissionChecker(FEATURE_ID, FeatureAccessLevel.READ_WRITE)),
):
    success = False
    details = {"params": {"quality_item_id": id}}
    try:
        updated = manager.update(db, id=id, data=payload, user_email=current_user.email)
        if not updated:
            raise HTTPException(status_code=404, detail="Quality item not found")
        success = True
        return updated
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed updating quality item %s", id)
        details["exception"] = {"type": type(e).__name__, "message": str(e)}
        raise HTTPException(status_code=500, detail="Failed to update quality item")
    finally:
        audit_manager.log_action(
            db=db,
            username=audit_user.username,
            ip_address=request.client.host if request.client else None,
            feature="quality",
            action="UPDATE",
            success=success,
            details=details,
        )


@router.delete("/quality-items/{id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_quality_item(
    id: str,
    request: Request,
    db: DBSessionDep,
    current_user: CurrentUserDep,
    audit_manager: AuditManagerDep,
    audit_user: AuditCurrentUserDep,
    manager: QualityManager = Depends(get_quality_manager),
    _: bool = Depends(PermissionChecker(FEATURE_ID, FeatureAccessLevel.READ_WRITE)),
):
    success = False
    details = {"params": {"quality_item_id": id}}
    try:
        ok = manager.delete(db, id=id, user_email=current_user.email)
        if not ok:
            raise HTTPException(status_code=404, detail="Quality item not found")
        success = True
        return
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed deleting quality item %s", id)
        details["exception"] = {"type": type(e).__name__, "message": str(e)}
        raise HTTPException(status_code=500, detail="Failed to delete quality item")
    finally:
        audit_manager.log_action(
            db=db,
            username=audit_user.username,
            ip_address=request.client.host if request.client else None,
            feature="quality",
            action="DELETE",
            success=success,
            details=details,
        )


# ── Product-level aggregation ────────────────────────────────────────────


@router.get(
    "/data-products/{product_id}/quality-summary",
    response_model=QualitySummary,
)
async def product_quality_summary(
    product_id: str,
    db: DBSessionDep,
    dp_manager: DataProductsManagerDep,
    manager: QualityManager = Depends(get_quality_manager),
    _: bool = Depends(PermissionChecker("data-products", FeatureAccessLevel.READ_ONLY)),
):
    """Aggregate quality scores from the product itself and its child contracts."""
    try:
        return manager.aggregate_for_product(db, product_id=product_id, data_products_manager=dp_manager)
    except Exception as e:
        logger.exception("Failed aggregating quality for product %s", product_id)
        raise HTTPException(status_code=500, detail="Failed to aggregate quality summary")


# ── Registration ─────────────────────────────────────────────────────────


def register_routes(app):
    app.include_router(router)
    logger.info("Quality routes registered with prefix /api")
