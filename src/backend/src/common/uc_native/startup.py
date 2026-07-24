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
from src.controller.workspace_manager import WorkspaceManager
from src.common.uc_native.bootstrap import bootstrap_uc_native
from src.common.uc_native.entities import UcNativeEntityStore
from src.common.uc_native.managers import (
    UcNativeAssetsManager,
    UcNativeConnectionsManager,
    UcNativeDataContractsManager,
    UcNativeDataDomainManager,
    UcNativeDataProductsManager,
    UcNativeTagsManager,
)
from src.common.uc_native.overlays import UcNativeOverlayStore
from src.common.uc_native.semantic import UcNativeSemanticStore
from src.common.uc_native.settings_manager import UcNativeSettingsManager
from src.common.uc_native.workflows import UcNativeWorkflowStore
from src.common.uc_native.overlay_managers import (
    UcNativeChangeLogManager,
    UcNativeCommentsManager,
    UcNativeJobsManager,
    UcNativeNotificationsManager,
)
from src.common.uc_native.semantic_manager import UcNativeSemanticModelsManager
from src.common.uc_native.feature_managers import (
    UcNativeAccessGrantsManager,
    UcNativeBusinessOwnersManager,
    UcNativeBusinessRolesManager,
    UcNativeComplianceManager,
    UcNativeCostsManager,
    UcNativeDataAssetReviewManager,
    UcNativeDeliveryMethodsManager,
    UcNativeEntitySubscriptionsManager,
    UcNativeMdmManager,
    UcNativeMetadataManager,
    UcNativeProjectsManager,
    UcNativeQualityManager,
    UcNativeSemanticLinksManager,
    UcNativeTeamsManager,
    UcNativeTermMappingManager,
    UcNativeWorkflowsManager,
)

logger = get_logger(__name__)


def initialize_uc_native(app: FastAPI, settings: Settings) -> None:
    """Wire UC-native stores and managers onto app.state.

    RBAC (settings + authorization) is initialized first so permissions work
    even if optional stores (workflows/semantic) fail later.
    """
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

    # Register asset connectors (same as Lakebase startup) — required for Schema Importer.
    try:
        from src.connectors import get_registry
        from src.connectors.databricks import DatabricksConnector
        from src.connectors.bigquery import BigQueryConnector
        from src.connectors.snowflake import SnowflakeConnector
        from src.connectors.kafka import KafkaConnector
        from src.connectors.powerbi import PowerBIConnector

        registry = get_registry()
        registry.register_instance(
            "databricks",
            DatabricksConnector(workspace_client=ws_client),
            set_as_default=True,
        )
        registry.register_class("bigquery", BigQueryConnector)
        registry.register_class("snowflake", SnowflakeConnector)
        registry.register_class("kafka", KafkaConnector)
        registry.register_class("powerbi", PowerBIConnector)
        logger.info(
            "UC native connector registry ready (%s types)",
            len(registry.list_registered()),
        )
    except Exception as exc:
        health.setdefault("warnings", []).append(f"Connector registry failed: {exc}")
        logger.warning("UC native startup: connector registry failed: %s", exc, exc_info=True)

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
    audit_manager = getattr(app.state, "audit_manager", None)
    if audit_manager and hasattr(audit_manager, "set_uc_overlays"):
        audit_manager.set_uc_overlays(overlays)

    # Permissions first — home page depends on these managers.
    settings_manager = UcNativeSettingsManager(store, settings)
    settings_manager.ensure_default_roles_exist()
    app.state.settings_manager = settings_manager
    app.state.authorization_manager = AuthorizationManager(settings_manager=settings_manager)
    app.state.users_manager = UsersManager(ws_client=ws_client)
    app.state.audit_manager = AuditManager(settings=settings, db_session=None)
    app.state.directory_manager = DirectoryManager()
    app.state.workspace_manager = WorkspaceManager(ws_client=ws_client)

    app.state.data_products_manager = UcNativeDataProductsManager(entities)
    app.state.data_contracts_manager = UcNativeDataContractsManager(entities)
    assets_manager = UcNativeAssetsManager(entities, overlays=overlays)
    try:
        seeded = assets_manager.ensure_default_asset_types()
        if seeded:
            logger.info("Seeded %s default Ontos asset types for UC-native", seeded)
    except Exception as exc:
        health.setdefault("warnings", []).append(f"Asset type seed failed: {exc}")
        logger.warning("Failed to seed UC-native asset types: %s", exc, exc_info=True)
    app.state.assets_manager = assets_manager
    app.state.data_domain_manager = UcNativeDataDomainManager(entities)
    app.state.tags_manager = UcNativeTagsManager(entities)
    connections_manager = UcNativeConnectionsManager(entities, workspace_client=ws_client)
    try:
        connections_manager.ensure_system_databricks_connection()
    except Exception as exc:
        health.setdefault("warnings", []).append(f"System Databricks connection seed failed: {exc}")
        logger.warning("Failed to ensure system Databricks UC connection: %s", exc, exc_info=True)
    app.state.connections_manager = connections_manager
    app.state.comments_manager = UcNativeCommentsManager(overlays)
    app.state.change_log_manager = UcNativeChangeLogManager(overlays)
    app.state.notifications_manager = UcNativeNotificationsManager(
        overlays, settings_manager
    )
    settings_manager.set_notifications_manager(app.state.notifications_manager)
    app.state.search_manager = SearchManager(searchable_managers=[
        app.state.data_products_manager,
        app.state.data_contracts_manager,
        app.state.assets_manager,
        app.state.data_domain_manager,
        app.state.tags_manager,
    ])
    app.state.semantic_links_manager = UcNativeSemanticLinksManager(overlays)
    app.state.entity_subscriptions_manager = UcNativeEntitySubscriptionsManager(overlays)
    app.state.costs_manager = UcNativeCostsManager(overlays)
    app.state.quality_manager = UcNativeQualityManager(overlays)
    app.state.metadata_manager = UcNativeMetadataManager(overlays)
    app.state.teams_manager = UcNativeTeamsManager(entities)
    app.state.projects_manager = UcNativeProjectsManager(entities)
    app.state.business_roles_manager = UcNativeBusinessRolesManager(entities)
    app.state.business_owners_manager = UcNativeBusinessOwnersManager(entities)
    app.state.delivery_methods_manager = UcNativeDeliveryMethodsManager(entities)
    app.state.data_asset_review_manager = UcNativeDataAssetReviewManager(entities)
    app.state.compliance_manager = UcNativeComplianceManager(entities)
    app.state.mdm_manager = UcNativeMdmManager(entities)

    # Optional / secondary stores — soft-fail so RBAC still works.
    try:
        workflows = UcNativeWorkflowStore(store, ws_client, settings)
        app.state.uc_native_workflows = workflows
        app.state.jobs_manager = UcNativeJobsManager(workflows, ws_client, settings)
        settings_manager._jobs = app.state.jobs_manager
        app.state.access_grants_manager = UcNativeAccessGrantsManager(workflows)
        app.state.workflows_manager = UcNativeWorkflowsManager(workflows)
    except Exception as exc:
        health.setdefault("warnings", []).append(f"UC workflow store unavailable: {exc}")
        logger.warning("UC native workflow store failed: %s", exc, exc_info=True)

    try:
        semantic = UcNativeSemanticStore(store, ws_client, settings)
        app.state.uc_native_semantic = semantic
        from pathlib import Path as _Path

        semantic_manager = UcNativeSemanticModelsManager(
            semantic,
            data_dir=_Path(__file__).resolve().parents[2] / "data",
        )
        app.state.semantic_models_manager = semantic_manager

        # Ontology schema (reads in-memory RDF graph) — required by Entity Relationships UI.
        try:
            from src.controller.ontology_schema_manager import OntologySchemaManager

            osm = OntologySchemaManager(semantic_models_manager=semantic_manager)
            app.state.ontology_schema_manager = osm
            if hasattr(app.state.assets_manager, "_ontology"):
                app.state.assets_manager._ontology = osm
            logger.info("OntologySchemaManager initialized for UC-native")
        except Exception as exc:
            health.setdefault("warnings", []).append(f"OntologySchemaManager unavailable: {exc}")
            logger.warning("UC native OntologySchemaManager failed: %s", exc, exc_info=True)

        # Term mapping runs and suggestions are persisted in UC Delta.
        try:
            app.state.term_mapping_manager = UcNativeTermMappingManager(
                entities,
                semantic_links=app.state.semantic_links_manager,
            )
            logger.info("UC-native TermMappingManager initialized")
        except Exception as exc:
            health.setdefault("warnings", []).append(f"TermMappingManager unavailable: {exc}")
            logger.warning("UC native TermMappingManager failed: %s", exc, exc_info=True)
    except Exception as exc:
        health.setdefault("warnings", []).append(f"UC semantic store unavailable: {exc}")
        logger.warning("UC native semantic store failed: %s", exc, exc_info=True)

    # Ontology Generator is settings-only (LLM) — no Postgres required.
    try:
        from src.controller.ontology_generator_manager import OntologyGeneratorManager

        app.state.ontology_generator_manager = OntologyGeneratorManager(
            settings=settings,
            run_store=entities,
        )
        logger.info("OntologyGeneratorManager initialized for UC-native")
    except Exception as exc:
        health.setdefault("warnings", []).append(f"OntologyGeneratorManager unavailable: {exc}")
        logger.warning("UC native OntologyGeneratorManager failed: %s", exc, exc_info=True)

    # Entity Relationships — Delta overlays as SoR (schema import writes here).
    try:
        from src.common.uc_native.entity_relationships_manager import (
            UcNativeEntityRelationshipsManager,
        )

        app.state.entity_relationships_manager = UcNativeEntityRelationshipsManager(
            overlays,
            ontology_schema_manager=getattr(app.state, "ontology_schema_manager", None),
            assets_manager=assets_manager,
            entities=entities,
        )
        logger.info("UcNativeEntityRelationshipsManager initialized")
    except Exception as exc:
        health.setdefault("warnings", []).append(f"EntityRelationshipsManager unavailable: {exc}")
        logger.warning("UC native EntityRelationshipsManager failed: %s", exc, exc_info=True)

    app.state.uc_native_store = store
    app.state.uc_native_entities = entities
    app.state.uc_native_overlays = overlays

    health["db_ok"] = True
    health["seed_ok"] = True
    health["oltp_skipped"] = True
    health["uc_native"] = True
    logger.info("UC native managers initialized")
