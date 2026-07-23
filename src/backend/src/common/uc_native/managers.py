"""Thin entity managers delegating to UC Delta stores."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set
from uuid import UUID

from src.common.logging import get_logger
from src.common.uc_native.entities import UcNativeEntityStore
from src.models.data_contracts_api import DataContractSummary
from src.models.data_domains import DataDomainCreate, DataDomainRead, DataDomainUpdate
from src.models.data_products import DataProduct, DataProductStatus
from src.models.assets import AssetRead, PaginatedAssetSummary
from src.models.tags import Tag, TagCreate, TagNamespace, TagNamespaceCreate
from src.models.connections import ConnectionCreate, ConnectionUpdate, ConnectionResponse
from src.controller.connections_manager import SYSTEM_CREATED_BY
from src.connectors.registry import get_registry
from src.connectors.base import AssetConnector, ConnectorConfig

logger = get_logger(__name__)


def _to_data_product(doc: Dict[str, Any]) -> DataProduct:
    doc = dict(doc)
    doc.setdefault("apiVersion", "v1.0.0")
    doc.setdefault("kind", "DataProduct")
    doc.setdefault("status", DataProductStatus.DRAFT.value)
    return DataProduct.model_validate(doc)


class UcNativeDataProductsManager:
    def __init__(self, entities: UcNativeEntityStore) -> None:
        self._entities = entities

    def list_products(
        self,
        skip: int = 0,
        limit: int = 100,
        project_id: Optional[str] = None,
        is_admin: bool = False,
        caller_email: Optional[str] = None,
        caller_team_ids: Optional[List[str]] = None,
        caller_project_ids: Optional[List[str]] = None,
        include_history: bool = False,
    ) -> List[DataProduct]:
        docs = self._entities.list_entities("data_products", limit=limit + skip)
        if not is_admin:
            filtered = []
            team_set = set(caller_team_ids or [])
            project_set = set(caller_project_ids or [])
            for doc in docs:
                if project_id and doc.get("project_id") != project_id:
                    continue
                if caller_email and doc.get("draft_owner_id") == caller_email:
                    filtered.append(doc)
                    continue
                if doc.get("owner_team_id") in team_set:
                    filtered.append(doc)
                    continue
                if doc.get("project_id") in project_set:
                    filtered.append(doc)
                    continue
                if doc.get("status") in ("active", "deprecated"):
                    filtered.append(doc)
            docs = filtered
        elif project_id:
            docs = [d for d in docs if d.get("project_id") == project_id]
        docs = docs[skip : skip + limit]
        return [_to_data_product(d) for d in docs]

    def get_product(self, product_id: str) -> Optional[DataProduct]:
        doc = self._entities.get_entity("data_products", product_id)
        return _to_data_product(doc) if doc else None

    def create_product(
        self,
        product_data: Dict[str, Any],
        db=None,
        user: Optional[str] = None,
        background_tasks=None,
        preserve_source_id: bool = False,
    ) -> DataProduct:
        product_data = dict(product_data)
        product_data.setdefault("id", str(uuid.uuid4()))
        product_data.setdefault("apiVersion", "v1.0.0")
        product_data.setdefault("kind", "DataProduct")
        product_data.setdefault("status", DataProductStatus.DRAFT.value)
        if user and not product_data.get("draft_owner_id"):
            product_data["draft_owner_id"] = user
        saved = self._entities.save_entity(
            "data_products",
            product_data,
            index_fields={
                "name": product_data.get("name"),
                "status": product_data.get("status"),
                "domain_id": product_data.get("domain_id") or product_data.get("domain"),
                "project_id": product_data.get("project_id"),
                "draft_owner_id": product_data.get("draft_owner_id"),
            },
        )
        return _to_data_product(saved)

    def update_product(self, product_id: str, updates: Dict[str, Any]) -> Optional[DataProduct]:
        existing = self._entities.get_entity("data_products", product_id)
        if not existing:
            return None
        existing.update(updates)
        saved = self._entities.save_entity(
            "data_products",
            existing,
            index_fields={
                "name": existing.get("name"),
                "status": existing.get("status"),
                "domain_id": existing.get("domain_id") or existing.get("domain"),
                "project_id": existing.get("project_id"),
                "draft_owner_id": existing.get("draft_owner_id"),
            },
        )
        return _to_data_product(saved)

    def delete_product(self, product_id: str) -> bool:
        if not self._entities.get_entity("data_products", product_id):
            return False
        self._entities.delete_entity("data_products", product_id)
        return True

    def get_published_products(
        self, skip: int = 0, limit: int = 100, scope: Optional[str] = None
    ) -> List[DataProduct]:
        """Marketplace listing: products with a non-none publication_scope."""
        products = self.list_products(skip=0, limit=skip + limit, is_admin=True)
        published = [
            p
            for p in products
            if p.publication_scope and str(p.publication_scope).lower() != "none"
        ]
        if scope:
            published = [
                p
                for p in published
                if p.publication_scope
                and str(p.publication_scope).lower() == scope.lower()
            ]
        return published[skip : skip + limit]

    def get_user_subscriptions(
        self,
        subscriber_email: str,
        skip: int = 0,
        limit: int = 100,
        db=None,
    ) -> List[DataProduct]:
        """Subscriptions are not yet persisted in UC-native Delta overlays."""
        logger.debug(
            "UC-native get_user_subscriptions(%s) — returning empty until overlay support",
            subscriber_email,
        )
        return []

    def get_statuses(self) -> List[str]:
        return [s.value for s in DataProductStatus]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _doc_to_contract_summary(doc: Dict[str, Any]) -> DataContractSummary:
    return DataContractSummary(
        id=str(doc.get("id", "")),
        name=doc.get("name", "Unnamed"),
        version=str(doc.get("version", "1.0.0")),
        status=doc.get("status", "draft"),
        owner_team_id=doc.get("owner_team_id"),
        project_id=doc.get("project_id"),
        domainId=doc.get("domain_id") or doc.get("domainId"),
        dataProduct=doc.get("data_product") or doc.get("product_id"),
        versionFamilyId=doc.get("version_family_id") or doc.get("id"),
        baseName=doc.get("base_name") or doc.get("name"),
    )


class UcNativeDataContractsManager:
    def __init__(self, entities: UcNativeEntityStore) -> None:
        self._entities = entities

    def list_contracts(self, limit: int = 500, is_admin: bool = True, **_) -> List[Dict[str, Any]]:
        return self._entities.list_entities("data_contracts", limit=limit)

    def list_contracts_from_db(
        self,
        db,
        *,
        domain_id: Optional[str] = None,
        project_id: Optional[str] = None,
        status: Optional[str] = None,
        is_admin: bool = True,
        include_history: bool = False,
        caller_email: Optional[str] = None,
        caller_team_ids: Optional[Set[str]] = None,
        **_,
    ) -> List[DataContractSummary]:
        docs = self._entities.list_entities("data_contracts", limit=500)
        if domain_id:
            docs = [d for d in docs if (d.get("domain_id") or d.get("domainId")) == domain_id]
        if project_id:
            docs = [d for d in docs if d.get("project_id") == project_id]
        if status:
            docs = [d for d in docs if d.get("status") == status]
        if not is_admin and caller_email:
            docs = [
                d
                for d in docs
                if d.get("draft_owner_id") == caller_email
                or d.get("status") in ("active", "proposed")
            ]
        if not include_history:
            seen: Dict[str, Dict[str, Any]] = {}
            for doc in docs:
                family = doc.get("version_family_id") or doc.get("id")
                if family not in seen:
                    seen[family] = doc
            docs = list(seen.values())
        return [_doc_to_contract_summary(d) for d in docs]

    def get_contract(self, contract_id: str) -> Optional[Dict[str, Any]]:
        return self._entities.get_entity("data_contracts", contract_id)

    def create_contract(self, payload: Dict[str, Any], user: Optional[str] = None) -> Dict[str, Any]:
        payload = dict(payload)
        payload.setdefault("id", str(uuid.uuid4()))
        payload.setdefault("status", "draft")
        if user:
            payload.setdefault("draft_owner_id", user)
        return self._entities.save_entity(
            "data_contracts",
            payload,
            index_fields={
                "name": payload.get("name"),
                "status": payload.get("status"),
                "product_id": payload.get("data_product") or payload.get("product_id"),
                "project_id": payload.get("project_id"),
                "draft_owner_id": payload.get("draft_owner_id"),
            },
        )

    def update_contract(self, contract_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        existing = self.get_contract(contract_id)
        if not existing:
            return None
        existing.update(updates)
        return self._entities.save_entity(
            "data_contracts",
            existing,
            index_fields={
                "name": existing.get("name"),
                "status": existing.get("status"),
                "product_id": existing.get("data_product") or existing.get("product_id"),
                "project_id": existing.get("project_id"),
                "draft_owner_id": existing.get("draft_owner_id"),
            },
        )


class UcNativeAssetsManager:
    def __init__(self, entities: UcNativeEntityStore) -> None:
        self._entities = entities

    def list_assets(self, limit: int = 500, **_) -> List[Dict[str, Any]]:
        return self._entities.list_entities("assets", limit=limit)

    def get_asset(self, asset_id: str) -> Optional[Dict[str, Any]]:
        return self._entities.get_entity("assets", asset_id)

    def create_asset(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(payload)
        payload.setdefault("id", str(uuid.uuid4()))
        payload.setdefault("status", "active")
        return self._entities.save_entity(
            "assets",
            payload,
            index_fields={
                "name": payload.get("name"),
                "asset_type_name": payload.get("asset_type_name") or payload.get("asset_type"),
                "status": payload.get("status"),
            },
        )

    def resolve_accessible_asset_ids(self, db, *, data_products_manager, is_admin: bool = False):
        if is_admin:
            return None
        return None

    def get_all_assets(
        self,
        db=None,
        skip: int = 0,
        limit: int = 100,
        **_,
    ) -> PaginatedAssetSummary:
        docs = self._entities.list_entities("assets", limit=skip + limit)
        page = docs[skip : skip + limit]
        items = []
        for doc in page:
            try:
                items.append(AssetRead.model_validate({**doc, "tags": doc.get("tags") or []}))
            except Exception:
                items.append(
                    AssetRead.model_validate(
                        {
                            "id": doc.get("id"),
                            "name": doc.get("name", "Asset"),
                            "asset_type_name": doc.get("asset_type_name", "table"),
                            "status": doc.get("status", "active"),
                            "tags": [],
                        }
                    )
                )
        return PaginatedAssetSummary(items=items, total=len(docs), skip=skip, limit=limit)

    def get_asset_by_id(self, db, asset_id: UUID) -> Optional[AssetRead]:
        doc = self.get_asset(str(asset_id))
        if not doc:
            return None
        return AssetRead.model_validate({**doc, "tags": doc.get("tags") or []})


class UcNativeDataDomainManager:
    def __init__(self, entities: UcNativeEntityStore) -> None:
        self._entities = entities

    def _to_read(self, doc: Dict[str, Any]) -> DataDomainRead:
        now = _now()
        return DataDomainRead(
            id=UUID(str(doc["id"])),
            name=doc.get("name", ""),
            description=doc.get("description"),
            parent_id=UUID(str(doc["parent_id"])) if doc.get("parent_id") else None,
            created_at=doc.get("created_at") or now,
            updated_at=doc.get("updated_at") or now,
            created_by=doc.get("created_by", "system"),
            tags=[],
        )

    def list_domains(self, limit: int = 500) -> List[Dict[str, Any]]:
        return self._entities.list_entities("data_domains", limit=limit)

    def get_domain(self, domain_id: str) -> Optional[Dict[str, Any]]:
        return self._entities.get_entity("data_domains", domain_id)

    def create_domain(
        self,
        db=None,
        domain_in: Optional[DataDomainCreate] = None,
        current_user_id: str = "system",
        background_tasks=None,
        **_,
    ) -> DataDomainRead:
        payload = domain_in.model_dump() if domain_in else {}
        payload.setdefault("id", str(uuid.uuid4()))
        payload["created_by"] = current_user_id
        payload["created_at"] = _now().isoformat()
        payload["updated_at"] = payload["created_at"]
        if payload.get("parent_id"):
            payload["parent_id"] = str(payload["parent_id"])
        saved = self._entities.save_entity(
            "data_domains",
            payload,
            index_fields={
                "name": payload.get("name"),
                "parent_id": payload.get("parent_id"),
            },
        )
        return self._to_read(saved)

    def get_all_domains(self, db=None, skip: int = 0, limit: int = 100, **_) -> List[DataDomainRead]:
        docs = self._entities.list_entities("data_domains", limit=skip + limit)
        return [self._to_read(d) for d in docs[skip : skip + limit]]

    def get_domain_by_id(self, db, domain_id: UUID) -> Optional[DataDomainRead]:
        doc = self.get_domain(str(domain_id))
        return self._to_read(doc) if doc else None

    def update_domain(
        self,
        db,
        domain_id: UUID,
        domain_in: DataDomainUpdate,
        current_user_id: str,
        **_,
    ) -> Optional[DataDomainRead]:
        existing = self.get_domain(str(domain_id))
        if not existing:
            return None
        updates = domain_in.model_dump(exclude_unset=True)
        if updates.get("parent_id"):
            updates["parent_id"] = str(updates["parent_id"])
        existing.update(updates)
        existing["updated_at"] = _now().isoformat()
        saved = self._entities.save_entity(
            "data_domains",
            existing,
            index_fields={
                "name": existing.get("name"),
                "parent_id": existing.get("parent_id"),
            },
        )
        return self._to_read(saved)

    def delete_domain(self, db, domain_id: UUID, **_) -> bool:
        if not self.get_domain(str(domain_id)):
            return False
        self._entities.delete_entity("data_domains", str(domain_id))
        return True


class UcNativeTagsManager:
    def __init__(self, entities: UcNativeEntityStore) -> None:
        self._entities = entities

    def list_tags(self, db=None, skip: int = 0, limit: int = 100, **_) -> List[Tag]:
        docs = self._entities.list_entities("tags", limit=skip + limit)
        tags: List[Tag] = []
        for doc in docs[skip : skip + limit]:
            try:
                tags.append(Tag.model_validate(doc))
            except Exception:
                tags.append(
                    Tag(
                        id=UUID(str(doc.get("id", uuid.uuid4()))),
                        name=doc.get("name", ""),
                        namespace_id=UUID(str(doc.get("namespace_id", uuid.uuid4()))),
                        status=doc.get("status", "active"),
                    )
                )
        return tags

    def create_tag(self, db, *, tag_in: TagCreate, user_email: Optional[str] = None) -> Tag:
        payload = tag_in.model_dump()
        payload.setdefault("id", str(uuid.uuid4()))
        payload.setdefault("status", "active")
        saved = self._entities.save_entity(
            "tags",
            payload,
            index_fields={
                "name": payload.get("name"),
                "namespace": payload.get("namespace"),
                "status": payload.get("status"),
            },
        )
        return Tag.model_validate(saved)

    def list_namespaces(self, db=None, skip: int = 0, limit: int = 100, **_) -> List[TagNamespace]:
        docs = self._entities.list_entities("tag_namespaces", limit=skip + limit)
        items: List[TagNamespace] = []
        for doc in docs[skip : skip + limit]:
            items.append(
                TagNamespace(
                    id=UUID(str(doc.get("id", uuid.uuid4()))),
                    name=doc.get("name", "default"),
                    description=doc.get("description"),
                )
            )
        return items

    def create_namespace(
        self,
        db,
        *,
        namespace_in: TagNamespaceCreate,
        user_email: Optional[str] = None,
        background_tasks=None,
    ) -> TagNamespace:
        payload = namespace_in.model_dump()
        payload.setdefault("id", str(uuid.uuid4()))
        saved = self._entities.save_entity(
            "tag_namespaces",
            payload,
            index_fields={"name": payload.get("name")},
        )
        return TagNamespace(
            id=UUID(str(saved["id"])),
            name=saved.get("name", ""),
            description=saved.get("description"),
        )

    def get_namespace(self, db, *, namespace_id: UUID) -> Optional[TagNamespace]:
        doc = self._entities.get_entity("tag_namespaces", str(namespace_id))
        if not doc:
            return None
        return TagNamespace(
            id=UUID(str(doc["id"])),
            name=doc.get("name", ""),
            description=doc.get("description"),
        )

    def get_or_create_default_namespace(self, db, *, user_email: Optional[str]) -> Any:
        namespaces = self.list_namespaces(db, limit=1)
        if namespaces:
            return namespaces[0]
        return self.create_namespace(
            db,
            namespace_in=TagNamespaceCreate(name="default", description="Default namespace"),
            user_email=user_email,
        )


class UcNativeConnectionsManager:
    """Connections SoR on UC Delta (replaces Postgres connections table)."""

    _INTERNAL_FIELDS = {"workspace_client", "credentials"}

    def __init__(self, entities: UcNativeEntityStore, workspace_client: Optional[Any] = None) -> None:
        self._entities = entities
        self._ws_client = workspace_client

    def _parse_dt(self, value: Any) -> datetime:
        if isinstance(value, datetime):
            return value
        if isinstance(value, str) and value:
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                pass
        return _now()

    def _to_response(self, doc: Dict[str, Any]) -> ConnectionResponse:
        return ConnectionResponse(
            id=UUID(str(doc["id"])),
            name=doc.get("name") or "Unnamed",
            connector_type=doc.get("connector_type") or "databricks",
            description=doc.get("description"),
            config=doc.get("config") or {},
            enabled=bool(doc.get("enabled", True)),
            is_default=bool(doc.get("is_default", False)),
            system_asset_id=UUID(str(doc["system_asset_id"])) if doc.get("system_asset_id") else None,
            created_at=self._parse_dt(doc.get("created_at")),
            updated_at=self._parse_dt(doc.get("updated_at")),
            created_by=doc.get("created_by"),
        )

    def list_connections(self, connector_type: Optional[str] = None) -> List[ConnectionResponse]:
        docs = self._entities.list_entities("connections", limit=500)
        if connector_type:
            docs = [d for d in docs if d.get("connector_type") == connector_type]
        docs.sort(key=lambda d: (d.get("connector_type") or "", d.get("name") or ""))
        return [self._to_response(d) for d in docs]

    def get_connection(self, connection_id: UUID) -> Optional[ConnectionResponse]:
        doc = self._entities.get_entity("connections", str(connection_id))
        return self._to_response(doc) if doc else None

    def _clear_default_for_type(self, connector_type: str) -> None:
        for doc in self._entities.list_entities("connections", limit=500):
            if doc.get("connector_type") == connector_type and doc.get("is_default"):
                doc["is_default"] = False
                doc["updated_at"] = _now().isoformat()
                self._entities.save_entity(
                    "connections",
                    doc,
                    index_fields={
                        "name": doc.get("name"),
                        "connector_type": doc.get("connector_type"),
                        "enabled": doc.get("enabled", True),
                        "is_default": False,
                        "updated_at": doc["updated_at"],
                    },
                )

    def create_connection(
        self, data: ConnectionCreate, created_by: Optional[str] = None
    ) -> ConnectionResponse:
        if data.is_default:
            self._clear_default_for_type(data.connector_type)
        now = _now().isoformat()
        clean_config = {k: v for k, v in (data.config or {}).items() if k not in self._INTERNAL_FIELDS}
        payload = {
            "id": str(uuid.uuid4()),
            "name": data.name,
            "connector_type": data.connector_type,
            "description": data.description,
            "config": clean_config,
            "enabled": data.enabled,
            "is_default": data.is_default,
            "system_asset_id": str(data.system_asset_id) if data.system_asset_id else None,
            "created_by": created_by or "",
            "created_at": now,
            "updated_at": now,
        }
        saved = self._entities.save_entity(
            "connections",
            payload,
            index_fields={
                "name": payload["name"],
                "connector_type": payload["connector_type"],
                "enabled": payload["enabled"],
                "is_default": payload["is_default"],
                "updated_at": now,
            },
        )
        return self._to_response(saved)

    def update_connection(
        self, connection_id: UUID, data: ConnectionUpdate
    ) -> Optional[ConnectionResponse]:
        existing = self._entities.get_entity("connections", str(connection_id))
        if not existing:
            return None
        if existing.get("created_by") == SYSTEM_CREATED_BY:
            # Allow limited updates? Postgres manager allows name/config updates for system
            # except delete. Mirror update_connection from ConnectionsManager.
            pass
        updates = data.model_dump(exclude_unset=True)
        if "config" in updates and updates["config"] is not None:
            updates["config"] = {
                k: v for k, v in updates["config"].items() if k not in self._INTERNAL_FIELDS
            }
        if updates.get("is_default"):
            self._clear_default_for_type(existing.get("connector_type") or "")
        if updates.get("system_asset_id") is not None:
            updates["system_asset_id"] = str(updates["system_asset_id"])
        existing.update(updates)
        existing["updated_at"] = _now().isoformat()
        saved = self._entities.save_entity(
            "connections",
            existing,
            index_fields={
                "name": existing.get("name"),
                "connector_type": existing.get("connector_type"),
                "enabled": existing.get("enabled", True),
                "is_default": existing.get("is_default", False),
                "updated_at": existing["updated_at"],
            },
        )
        return self._to_response(saved)

    def delete_connection(self, connection_id: UUID) -> bool:
        existing = self._entities.get_entity("connections", str(connection_id))
        if not existing:
            return False
        if existing.get("created_by") == SYSTEM_CREATED_BY:
            raise ValueError("System connections cannot be deleted")
        self._entities.delete_entity("connections", str(connection_id))
        return True

    def get_connector_for_connection(self, connection_id: UUID) -> Optional[AssetConnector]:
        doc = self._entities.get_entity("connections", str(connection_id))
        if not doc:
            return None
        return self._build_connector(doc)

    def _build_connector(self, doc: Dict[str, Any]) -> AssetConnector:
        registry = get_registry()
        connector_type = doc.get("connector_type") or "databricks"
        config_dict = dict(doc.get("config") or {})

        # Databricks / UC is always constructed from the workspace client.
        # Do not depend on registry class registration (Lakebase startup
        # registers an instance, not a class).
        if connector_type == "databricks":
            from src.connectors.databricks import DatabricksConnector

            ws = self._ws_client
            if ws is None and registry.has_connector("databricks"):
                try:
                    existing = registry.get_connector("databricks")
                    if getattr(existing, "_client", None) is not None:
                        return existing
                except Exception:
                    pass
            if ws is None:
                raise ValueError(
                    "Databricks connector requires a workspace client; none is configured"
                )
            return DatabricksConnector(workspace_client=ws)

        if connector_type in registry._connector_instances and connector_type not in registry._connector_classes:
            return registry._connector_instances[connector_type]

        if connector_type == "bigquery" and self._ws_client:
            config_dict["workspace_client"] = self._ws_client

        from src.controller.connections_manager import _get_config_classes

        config_classes = _get_config_classes()
        config_cls = config_classes.get(connector_type, ConnectorConfig)
        typed_config = config_cls(**config_dict)

        if connector_type in registry._connector_classes:
            connector_class = registry._connector_classes[connector_type]
            return connector_class(typed_config)

        if registry.has_connector(connector_type):
            return registry.get_connector(connector_type)

        raise ValueError(f"No connector class registered for type '{connector_type}'")

    def test_connection(self, connection_id: UUID) -> Dict[str, Any]:
        doc = self._entities.get_entity("connections", str(connection_id))
        if not doc:
            return {"healthy": False, "error": "Connection not found"}
        try:
            connector = self._build_connector(doc)
            result = connector.health_check()
            result["connection_name"] = doc.get("name")
            return result
        except Exception as exc:
            logger.error("Error testing connection '%s': %s", doc.get("name"), exc, exc_info=True)
            return {
                "connector_type": doc.get("connector_type"),
                "connection_name": doc.get("name"),
                "healthy": False,
                "error": str(exc),
            }

    def list_connector_types(self) -> List[Dict[str, Any]]:
        # Reuse Postgres manager's type listing (registry-only, no DB).
        from src.controller.connections_manager import ConnectionsManager

        return ConnectionsManager(db=None, workspace_client=self._ws_client).list_connector_types()

    def ensure_system_databricks_connection(self) -> None:
        docs = self._entities.list_entities("connections", limit=500)
        if any(d.get("name") == "Databricks UC" for d in docs):
            logger.debug("System Databricks UC connection already exists")
            return
        now = _now().isoformat()
        payload = {
            "id": str(uuid.uuid4()),
            "name": "Databricks UC",
            "connector_type": "databricks",
            "description": "Default Unity Catalog connection (auto-configured from environment)",
            "config": {},
            "enabled": True,
            "is_default": True,
            "system_asset_id": None,
            "created_by": SYSTEM_CREATED_BY,
            "created_at": now,
            "updated_at": now,
        }
        self._entities.save_entity(
            "connections",
            payload,
            index_fields={
                "name": payload["name"],
                "connector_type": "databricks",
                "enabled": True,
                "is_default": True,
                "updated_at": now,
            },
        )
        logger.info("Created system Databricks UC connection (UC-native)")
