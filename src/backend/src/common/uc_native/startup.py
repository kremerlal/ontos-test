"""Application startup for uc_native storage mode."""

from __future__ import annotations

from fastapi import FastAPI

from src.common.config import Settings
from src.common.logging import get_logger
from src.common.workspace_client import get_workspace_client
from src.controller.authorization_manager import AuthorizationManager
from src.controller.audit_manager import AuditManager
from src.controller.directory_manager import DirectoryManager
from src.controller.search_manager import SearchManager
from src.controller.users_manager import UsersManager
from src.common.uc_native.bootstrap import bootstrap_uc_native
from src.common.uc_native.entities import UcNativeEntityStore
from src.common.uc_native.managers import (
    UcNativeAssetsManager,
    UcNativeDataContractsManager,
    UcNativeDataDomainManager,
    UcNativeDataProductsManager,
    UcNativeTagsManager,
)
from src.common.uc_native.overlays import UcNativeOverlayStore
from src.common.uc_native.semantic import UcNativeSemanticStore
from src.common.uc_native.settings_manager import UcNativeSettingsManager
from src.common.uc_native.overlay_managers import (
    UcNativeChangeLogManager,
    UcNativeCommentsManager,
    UcNativeJobsManager,
    UcNativeNotificationsManager,
)
from src.common.uc_native.semantic_manager import UcNativeSemanticModelsManager

logger = get_logger(__name__)


def initialize_uc_native(app: FastAPI, settings: Settings) -> None:
    """Wire UC-native stores and managers onto app.state."""
    health = getattr(app.state, "health", {"warnings": []})
    app.state.settings = settings

    try:
        ws_client = get_workspace_client(settings=settings)
        health["ws_ok"] = ws_client is not None
        app.state.ws_client = ws_client
    except Exception as exc:
        ws_client = None
        health["ws_ok"] = False
        health.setdefault("warnings", []).append(f"Workspace client unavailable: {exc}")
        logger.warning("UC native startup: workspace client failed: %s", exc)
        return

    try:
        store = bootstrap_uc_native(ws_client, settings)
    except Exception as exc:
        # FastAPI HTTPException stringifies as "500: detail" — unwrap for health UI.
        detail = getattr(exc, "detail", None)
        health["seed_ok"] = False
        health["seed_error"] = str(detail or exc)
        health["db_ok"] = False
        health["db_error"] = health["seed_error"]
        logger.critical("UC native bootstrap failed: %s", exc, exc_info=True)
        return

    entities = UcNativeEntityStore(store)
    overlays = UcNativeOverlayStore(store)
    workflows = UcNativeWorkflowStore(store, ws_client, settings)
    semantic = UcNativeSemanticStore(store, ws_client, settings)

    app.state.uc_native_store = store
    app.state.uc_native_entities = entities
    app.state.uc_native_overlays = overlays
    app.state.uc_native_workflows = workflows
    app.state.uc_native_semantic = semantic

    settings_manager = UcNativeSettingsManager(store, settings)
    settings_manager.ensure_default_roles_exist()
    app.state.settings_manager = settings_manager
    app.state.authorization_manager = AuthorizationManager(settings_manager=settings_manager)
    app.state.users_manager = UsersManager(ws_client=ws_client)
    app.state.audit_manager = AuditManager(settings=settings, db_session=None)
    app.state.directory_manager = DirectoryManager()

    app.state.data_products_manager = UcNativeDataProductsManager(entities)
    app.state.data_contracts_manager = UcNativeDataContractsManager(entities)
    app.state.assets_manager = UcNativeAssetsManager(entities)
    app.state.data_domain_manager = UcNativeDataDomainManager(entities)
    app.state.tags_manager = UcNativeTagsManager(entities)
    app.state.comments_manager = UcNativeCommentsManager(overlays)
    app.state.change_log_manager = UcNativeChangeLogManager(overlays)
    app.state.notifications_manager = UcNativeNotificationsManager(
        overlays, settings_manager
    )
    settings_manager.set_notifications_manager(app.state.notifications_manager)
    app.state.semantic_models_manager = UcNativeSemanticModelsManager(semantic)
    app.state.jobs_manager = UcNativeJobsManager(workflows, ws_client, settings)
    app.state.search_manager = SearchManager(searchable_managers=[])

    health["db_ok"] = True
    health["seed_ok"] = True
    health["oltp_skipped"] = True
    health["uc_native"] = True
    logger.info("UC native managers initialized")
