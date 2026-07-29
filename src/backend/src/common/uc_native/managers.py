"""Thin entity managers delegating to UC Delta stores."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Set
from uuid import UUID

from src.common.errors import ConflictError, NotFoundError
from src.common.logging import get_logger
from src.common.uc_native.entities import UcNativeEntityStore
from src.common.uc_native.overlays import UcNativeOverlayStore
from src.models.data_contracts_api import DataContractSummary
from src.models.data_domains import DataDomainCreate, DataDomainRead, DataDomainUpdate
from src.models.data_products import (
    DataProduct,
    DataProductStatus,
    SubscriberInfo,
    SubscribersListResponse,
    Subscription,
    SubscriptionResponse,
)
from src.models.assets import (
    AssetCreate,
    AssetRead,
    AssetRelationshipCreate,
    AssetRelationshipRead,
    AssetSummary,
    AssetTypeCreate,
    AssetTypeRead,
    AssetTypeSummary,
    AssetTypeUpdate,
    AssetUpdate,
    CascadeDeleteResult,
    DeletePreviewItem,
    PaginatedAssetSummary,
)
from src.models.tags import Tag, TagCreate, TagNamespace, TagNamespaceCreate
from src.models.connections import ConnectionCreate, ConnectionUpdate, ConnectionResponse
from src.controller.connections_manager import SYSTEM_CREATED_BY
from src.connectors.registry import get_registry
from src.connectors.base import AssetConnector, ConnectorConfig

logger = get_logger(__name__)

# Stable namespace for seeded Ontos asset-type UUIDs (uuid5).
_ASSET_TYPE_NS = uuid.UUID("a0000000-0000-4000-8000-000000000001")

# Minimal built-in types required by Schema Importer / UC browse mapping.
_DEFAULT_ASSET_TYPES: List[Dict[str, Any]] = [
    {"name": "System", "category": "system", "description": "External system / connector"},
    {"name": "Catalog", "category": "data", "description": "Catalog / database container"},
    {"name": "Schema", "category": "data", "description": "Schema / dataset container"},
    {"name": "Table", "category": "data", "description": "Table or streaming table"},
    {"name": "View", "category": "data", "description": "View or materialized view"},
    {"name": "Column", "category": "data", "description": "Column within a table/view"},
    {"name": "Dataset", "category": "data", "description": "Logical dataset"},
    {"name": "Dashboard", "category": "analytics", "description": "Dashboard or report"},
    {"name": "ML Model", "category": "analytics", "description": "Machine learning model"},
]

# Relationship types where source = parent, target = child. Extends the Postgres
# AssetsManager set with the container types the Schema Importer writes in UC-native mode.
_HIERARCHICAL_RELATIONSHIPS = [
    "hasColumn",
    "hasTable",
    "hasView",
    "hasDataset",
    "hasPart",
    "contains",
    "hasCatalog",
    "hasSchema",
]


def _flatten_preview(node: "DeletePreviewItem") -> List["DeletePreviewItem"]:
    """Depth-first, children before parents so deletes bottom out cleanly."""
    out: List[DeletePreviewItem] = []
    for child in node.children:
        out.extend(_flatten_preview(child))
    out.append(node)
    return out


# Top-level ODPS v1.0.0 keys, in the order the YAML export emits them.
_ODPS_EXPORT_KEYS = (
    "kind",
    "apiVersion",
    "id",
    "status",
    "name",
    "version",
    "domain",
    "tenant",
    "description",
    "inputPorts",
    "outputPorts",
    "managementPorts",
    "support",
    "team",
    "customProperties",
    "authoritativeDefinitions",
)

# Convenience fields the API resolves at query time for the UI; they are not
# part of the ODPS document and would make an exported YAML non-portable.
_ODPS_EXPORT_DROP = frozenset({"contractName", "deliveryMethodName"})


def _prune_odps(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _prune_odps(v) for k, v in value.items() if k not in _ODPS_EXPORT_DROP}
    if isinstance(value, list):
        return [_prune_odps(v) for v in value]
    return value


def _parse_dt(value: Any) -> datetime:
    """Tolerant timestamp parse for sorting Delta snapshots.

    Snapshots are JSON-encoded with ``default=str``, so a datetime can land as
    either an ISO string or ``str(datetime)`` with a space separator.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if value:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.min.replace(tzinfo=timezone.utc)


def _to_data_product(doc: Dict[str, Any]) -> DataProduct:
    doc = dict(doc)
    doc.setdefault("apiVersion", "v1.0.0")
    doc.setdefault("kind", "DataProduct")
    doc.setdefault("status", DataProductStatus.DRAFT.value)
    return DataProduct.model_validate(doc)


class UcNativeDataProductsManager:
    def __init__(
        self,
        entities: UcNativeEntityStore,
        overlays: Optional[UcNativeOverlayStore] = None,
    ) -> None:
        self._entities = entities
        # Subscriptions live in the shared entity_subscriptions overlay table,
        # so a product subscription is the same row shape the generic entity
        # subscription feature writes.
        self._overlays = overlays

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
        product_data.setdefault("created_at", _now().isoformat())
        product_data["updated_at"] = _now().isoformat()
        # Canonical family grouping key (PRD #442): defaults to the product's
        # own id on initial create, carried forward unchanged by clones.
        product_data.setdefault("version_family_id", product_data["id"])
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

    def update_product(
        self,
        product_id: str,
        product_data_dict: Dict[str, Any],
        db=None,
        user: Optional[str] = None,
        background_tasks=None,
    ) -> Optional[DataProduct]:
        existing = self._entities.get_entity("data_products", product_id)
        if not existing:
            return None
        existing.update(product_data_dict)
        existing["updated_at"] = _now().isoformat()
        if user:
            existing["updated_by"] = user
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

    def delete_product(self, product_id: str, user: Optional[str] = None) -> bool:
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

    # --- Subscriptions ----------------------------------------------------
    #
    # Stored as entity_subscriptions overlay rows keyed by
    # (entity_type="data_product", entity_id=product_id, subscriber_email).

    _SUBSCRIPTION_TABLE = "entity_subscriptions"
    _SUBSCRIBABLE_STATUSES = ("active", "certified")

    def _subscription_rows(self, product_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if self._overlays is None:
            return []
        if product_id is not None:
            rows = self._overlays.list_for_entity(
                self._SUBSCRIPTION_TABLE, "data_product", str(product_id)
            )
        else:
            rows = [
                row
                for row in self._overlays.list_all(self._SUBSCRIPTION_TABLE, limit=10000)
                if row.get("entity_type") == "data_product"
            ]
        rows.sort(key=lambda row: _parse_dt(row.get("subscribed_at")))
        return rows

    def _subscription_row(self, product_id: str, subscriber_email: str) -> Optional[Dict[str, Any]]:
        return next(
            (
                row
                for row in self._subscription_rows(product_id)
                if row.get("subscriber_email") == subscriber_email
            ),
            None,
        )

    @staticmethod
    def _to_subscription(row: Dict[str, Any]) -> Subscription:
        return Subscription(
            id=str(row.get("id") or ""),
            product_id=str(row.get("entity_id") or ""),
            subscriber_email=str(row.get("subscriber_email") or ""),
            subscribed_at=_parse_dt(row.get("subscribed_at")),
            subscription_reason=row.get("subscription_reason"),
            on_behalf_of_type=row.get("on_behalf_of_type"),
            on_behalf_of_value=row.get("on_behalf_of_value"),
        )

    def subscribe(
        self,
        product_id: str,
        subscriber_email: str,
        reason: Optional[str] = None,
        on_behalf_of=None,
        db=None,
    ) -> SubscriptionResponse:
        if self._overlays is None:
            raise RuntimeError("Subscriptions require the UC-native overlay store")

        product = self.get_product(product_id)
        if not product:
            raise ValueError(f"Product {product_id} not found")
        if product.status and product.status.lower() not in self._SUBSCRIBABLE_STATUSES:
            raise ValueError(
                f"Cannot subscribe to product in status '{product.status}'. "
                f"Product must be in one of: {', '.join(self._SUBSCRIBABLE_STATUSES)}"
            )

        existing = self._subscription_row(product_id, subscriber_email)
        if existing:
            return SubscriptionResponse(
                subscribed=True, subscription=self._to_subscription(existing)
            )

        row = {
            "id": str(uuid.uuid4()),
            "entity_type": "data_product",
            "entity_id": str(product_id),
            "subscriber_email": subscriber_email,
            "subscribed_at": _now().isoformat(),
            "subscription_reason": reason,
            "on_behalf_of_type": getattr(on_behalf_of, "type", None),
            "on_behalf_of_value": getattr(on_behalf_of, "value", None),
        }
        saved = self._overlays.add(self._SUBSCRIPTION_TABLE, row)
        return SubscriptionResponse(
            subscribed=True, subscription=self._to_subscription(saved)
        )

    def unsubscribe(
        self, product_id: str, subscriber_email: str, db=None
    ) -> SubscriptionResponse:
        existing = self._subscription_row(product_id, subscriber_email)
        if existing and self._overlays is not None:
            self._overlays.remove(self._SUBSCRIPTION_TABLE, str(existing.get("id")))
        return SubscriptionResponse(subscribed=False, subscription=None)

    def get_subscription_status(
        self, product_id: str, subscriber_email: str, db=None
    ) -> SubscriptionResponse:
        existing = self._subscription_row(product_id, subscriber_email)
        if existing:
            return SubscriptionResponse(
                subscribed=True, subscription=self._to_subscription(existing)
            )
        return SubscriptionResponse(subscribed=False, subscription=None)

    def get_subscribers(
        self, product_id: str, skip: int = 0, limit: int = 100, db=None
    ) -> SubscribersListResponse:
        rows = self._subscription_rows(product_id)
        page = rows[skip : skip + limit]
        return SubscribersListResponse(
            product_id=str(product_id),
            subscriber_count=len(rows),
            subscribers=[
                SubscriberInfo(
                    email=str(row.get("subscriber_email") or ""),
                    subscribed_at=_parse_dt(row.get("subscribed_at")),
                    reason=row.get("subscription_reason"),
                )
                for row in page
            ],
        )

    def get_subscriber_count(self, product_id: str, db=None) -> int:
        return len(self._subscription_rows(product_id))

    def get_user_subscriptions(
        self,
        subscriber_email: str,
        skip: int = 0,
        limit: int = 100,
        db=None,
    ) -> List[DataProduct]:
        product_ids = [
            str(row.get("entity_id"))
            for row in self._subscription_rows()
            if row.get("subscriber_email") == subscriber_email
        ]
        products = []
        for product_id in product_ids[skip : skip + limit]:
            product = self.get_product(product_id)
            if product:
                products.append(product)
        return products

    def get_statuses(self) -> List[str]:
        return [s.value for s in DataProductStatus]

    # --- Filter facets (list-page dropdowns) ------------------------------
    #
    # The Postgres manager derives these from SELECT DISTINCT over ODPS
    # columns; UC-native reads the same values off the Delta snapshots.

    def _product_docs(self) -> List[Dict[str, Any]]:
        return self._entities.list_entities("data_products", limit=10000)

    @staticmethod
    def _distinct(values: Any) -> List[str]:
        return sorted({str(v).strip() for v in values if v is not None and str(v).strip()})

    def get_distinct_statuses(self) -> List[str]:
        return self._distinct(doc.get("status") for doc in self._product_docs())

    def get_distinct_domains(self) -> List[str]:
        return self._distinct(
            doc.get("domain") or doc.get("domain_id") for doc in self._product_docs()
        )

    def get_distinct_tenants(self) -> List[str]:
        return self._distinct(doc.get("tenant") for doc in self._product_docs())

    def get_distinct_product_types(self) -> List[str]:
        types: List[Any] = []
        for doc in self._product_docs():
            for port in doc.get("outputPorts") or doc.get("output_ports") or []:
                if isinstance(port, dict):
                    types.append(port.get("type") or port.get("port_type"))
        return self._distinct(types)

    def get_distinct_owners(self) -> List[str]:
        owners: List[Any] = []
        for doc in self._product_docs():
            team = doc.get("team")
            if not isinstance(team, dict):
                continue
            for member in team.get("members") or []:
                if isinstance(member, dict) and str(member.get("role") or "").lower() == "owner":
                    owners.append(member.get("name") or member.get("username"))
        return self._distinct(owners)

    # --- Versioning -------------------------------------------------------

    @staticmethod
    def _doc_field(doc: Dict[str, Any], *keys: str) -> Any:
        """Read a field that may be stored under its name or its camelCase alias."""
        for key in keys:
            value = doc.get(key)
            if value is not None:
                return value
        return None

    def get_product_versions(
        self,
        db=None,
        product_id: Optional[str] = None,
        *,
        user_email: Optional[str] = None,
        is_admin: bool = False,
    ) -> List[DataProduct]:
        """Every visible version of a product's family, newest first.

        Mirrors the Postgres ``version_family_id`` grouping: personal drafts
        owned by other users stay hidden unless the caller is an admin.
        """
        source = self._entities.get_entity("data_products", str(product_id))
        if not source:
            raise ValueError("Product not found")
        family_id = str(
            self._doc_field(source, "version_family_id", "versionFamilyId") or source.get("id")
        )

        members = [
            doc
            for doc in self._product_docs()
            if str(
                self._doc_field(doc, "version_family_id", "versionFamilyId") or doc.get("id")
            )
            == family_id
        ]
        if not is_admin:
            caller = str(user_email or "").lower()
            members = [
                doc
                for doc in members
                if not self._doc_field(doc, "draft_owner_id", "draftOwnerId")
                or str(self._doc_field(doc, "draft_owner_id", "draftOwnerId")).lower() == caller
            ]
        members.sort(key=lambda doc: _parse_dt(doc.get("created_at")), reverse=True)
        return [_to_data_product(doc) for doc in members]

    # --- ODPS export ------------------------------------------------------

    def build_odps_export(self, product_id: str, db=None) -> Dict[str, Any]:
        """Build an ODPS v1.0.0 document for YAML export.

        The ODPS fields on the API model are already camelCase field names
        (their aliases are the snake_case spellings used for ORM reads), so the
        document is dumped by field name, not by alias. The dataset-hierarchy
        extension the Postgres manager appends needs entity relationships that
        UC-native does not resolve for products yet, so it is omitted.
        """
        product = self.get_product(product_id)
        if not product:
            raise ValueError(f"Product {product_id} not found")

        dumped = product.model_dump(exclude_none=True, mode="json")
        odps: Dict[str, Any] = {}
        for key in _ODPS_EXPORT_KEYS:
            value = _prune_odps(dumped.get(key))
            if value in (None, [], {}, ""):
                continue
            odps[key] = value
        return odps

    # --- Authorized update ------------------------------------------------

    def update_product_with_auth(
        self,
        product_id: str,
        product_data_dict: Dict[str, Any],
        user_email: str,
        user_groups: List[str],
        db=None,
        background_tasks=None,
        caller_team_ids: Optional[List[str]] = None,
        is_feature_admin: bool = False,
    ) -> Optional[DataProduct]:
        """Update a product after checking the caller's ownership claim.

        Same cascade as the Postgres manager — admin, then project
        membership, then team ownership, then creator/draft ownership.
        Orphan rows with no owner at all fail closed for non-admins.
        """
        from src.common.authorization import is_user_admin
        from src.common.config import get_settings
        from src.common.uc_native.feature_managers import UcNativeProjectsManager

        existing = self._entities.get_entity("data_products", str(product_id))
        if not existing:
            logger.warning("Product not found for update: %s", product_id)
            return None

        if is_feature_admin or is_user_admin(user_groups, get_settings()):
            return self.update_product(
                product_id,
                product_data_dict,
                db=db,
                user=user_email,
                background_tasks=background_tasks,
            )

        project_id = existing.get("project_id")
        owner_team_id = existing.get("owner_team_id")
        draft_owner_id = self._doc_field(existing, "draft_owner_id", "draftOwnerId")

        authorized = False
        if project_id:
            authorized = UcNativeProjectsManager(self._entities).check_user_project_access(
                db,
                user_identifier=user_email,
                user_groups=user_groups,
                project_id=project_id,
                team_ids=caller_team_ids,
            )
        if not authorized and owner_team_id and caller_team_ids:
            authorized = str(owner_team_id) in {str(t) for t in caller_team_ids}
        if not authorized and draft_owner_id and user_email:
            authorized = str(draft_owner_id).lower() == str(user_email).lower()

        if not authorized:
            logger.warning(
                "User %s denied update on product %s (project_id=%s, owner_team_id=%s, draft_owner_id=%s)",
                user_email,
                product_id,
                project_id,
                owner_team_id,
                draft_owner_id,
            )
            # Generic message — do not leak which sub-check failed.
            raise PermissionError("Insufficient permissions to edit this data product")

        return self.update_product(
            product_id,
            product_data_dict,
            db=db,
            user=user_email,
            background_tasks=background_tasks,
        )


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
    """Assets + asset-types SoR on UC Delta (Schema Import write path)."""

    def __init__(
        self,
        entities: UcNativeEntityStore,
        overlays: Optional[UcNativeOverlayStore] = None,
    ) -> None:
        self._entities = entities
        self._overlays = overlays
        self._type_cache: Dict[str, SimpleNamespace] = {}
        self._types_loaded = False
        # Identity index: (name, asset_type_id, platform, location) -> asset UUID
        self._identity_index: Optional[Dict[tuple, UUID]] = None

    # --- Asset types -------------------------------------------------------

    def _load_type_cache(self) -> None:
        if self._types_loaded:
            return
        for row in self._entities.list_entities("asset_types", limit=200):
            name = row.get("name")
            if not name or not row.get("id"):
                continue
            self._type_cache[name] = SimpleNamespace(
                id=UUID(str(row["id"])),
                name=name,
            )
        self._types_loaded = True

    def ensure_default_asset_types(self) -> int:
        """Idempotently seed built-in Ontos asset types used by Schema Importer."""
        self._load_type_cache()
        created = 0
        now = _now().isoformat()
        for spec in _DEFAULT_ASSET_TYPES:
            name = spec["name"]
            if name in self._type_cache:
                continue
            type_id = str(uuid.uuid5(_ASSET_TYPE_NS, name))
            # Prefer stable id if a prior seed wrote it under that id without caching.
            existing = self._entities.get_entity("asset_types", type_id)
            if existing:
                self._type_cache[name] = SimpleNamespace(
                    id=UUID(type_id),
                    name=existing.get("name", name),
                )
                continue
            payload = {
                "id": type_id,
                "name": name,
                "description": spec.get("description"),
                "category": spec.get("category", "data"),
                "is_system": True,
                "status": "active",
                "created_by": "system@uc-native",
                "created_at": now,
                "updated_at": now,
            }
            self._entities.save_entity(
                "asset_types",
                payload,
                index_fields={
                    "name": name,
                    "category": payload["category"],
                    "is_system": True,
                    "status": "active",
                    "updated_at": now,
                },
            )
            self._type_cache[name] = SimpleNamespace(id=UUID(type_id), name=name)
            created += 1
            logger.info("Seeded UC-native asset type '%s' (%s)", name, type_id)
        return created

    def get_asset_type_by_name(self, name: str) -> Optional[SimpleNamespace]:
        if name in self._type_cache:
            return self._type_cache[name]
        # Prefer stable uuid5 lookup (single-row read) before listing the table.
        stable_id = str(uuid.uuid5(_ASSET_TYPE_NS, name))
        doc = self._entities.get_entity("asset_types", stable_id)
        if doc:
            ns = SimpleNamespace(id=UUID(str(doc["id"])), name=doc.get("name", name))
            self._type_cache[name] = ns
            return ns
        self._load_type_cache()
        return self._type_cache.get(name)

    def _asset_type_doc(self, type_id: str) -> Optional[Dict[str, Any]]:
        doc = self._entities.get_entity("asset_types", str(type_id))
        if doc:
            return doc
        return next(
            (
                row
                for row in self._entities.list_entities("asset_types", limit=500)
                if str(row.get("id")) == str(type_id)
            ),
            None,
        )

    def _asset_counts_by_type(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for doc in self._entities.list_entities("assets", limit=20000):
            key = str(doc.get("asset_type_id") or "")
            if key:
                counts[key] = counts.get(key, 0) + 1
        return counts

    def _to_asset_type_read(
        self,
        doc: Dict[str, Any],
        *,
        asset_count: int = 0,
    ) -> AssetTypeRead:
        now = _now()
        return AssetTypeRead.model_validate(
            {
                "id": doc.get("id"),
                "name": doc.get("name") or "",
                "description": doc.get("description"),
                "category": doc.get("category"),
                "icon": doc.get("icon"),
                "required_fields": doc.get("required_fields"),
                "optional_fields": doc.get("optional_fields"),
                "allowed_relationships": doc.get("allowed_relationships"),
                "is_system": bool(doc.get("is_system", False)),
                "status": doc.get("status") or "active",
                "asset_count": asset_count,
                "created_by": doc.get("created_by"),
                "created_at": doc.get("created_at") or now,
                "updated_at": doc.get("updated_at") or now,
            }
        )

    def get_all_asset_types(
        self,
        db=None,
        skip: int = 0,
        limit: int = 100,
        category: Optional[str] = None,
        status: Optional[str] = None,
        **_,
    ) -> List[AssetTypeRead]:
        docs = self._entities.list_entities("asset_types", limit=500)
        if category:
            docs = [d for d in docs if str(d.get("category")) == str(category)]
        if status:
            docs = [d for d in docs if str(d.get("status") or "active") == str(status)]
        docs.sort(key=lambda d: str(d.get("name") or "").lower())
        counts = self._asset_counts_by_type()
        return [
            self._to_asset_type_read(doc, asset_count=counts.get(str(doc.get("id")), 0))
            for doc in docs[skip : skip + limit]
        ]

    def get_asset_types_summary(self, db=None, **_) -> List[AssetTypeSummary]:
        docs = self._entities.list_entities("asset_types", limit=500)
        docs.sort(key=lambda d: str(d.get("name") or "").lower())
        return [
            AssetTypeSummary.model_validate(
                {
                    "id": doc.get("id"),
                    "name": doc.get("name") or "",
                    "category": doc.get("category"),
                    "icon": doc.get("icon"),
                    "status": doc.get("status") or "active",
                }
            )
            for doc in docs
            if doc.get("id")
        ]

    def get_asset_type(self, type_id: UUID = None, db=None, **_) -> Optional[AssetTypeRead]:
        """Return an asset type as a read model.

        Callers only ever read ``.id``/``.name`` internally, so returning the full
        read model also satisfies the asset-type detail route.
        """
        doc = self._asset_type_doc(str(type_id))
        if not doc:
            return None
        counts = self._asset_counts_by_type()
        return self._to_asset_type_read(doc, asset_count=counts.get(str(doc.get("id")), 0))

    def create_asset_type(
        self,
        db=None,
        *,
        type_in: AssetTypeCreate = None,
        current_user_id: str = "system",
        **_,
    ) -> AssetTypeRead:
        data = type_in.model_dump()
        name = data["name"]
        if self.get_asset_type_by_name(name):
            raise ConflictError(f"Asset type '{name}' already exists.")
        now = _now().isoformat()
        doc = {
            **{k: (v.value if hasattr(v, "value") else v) for k, v in data.items()},
            "id": str(uuid.uuid5(_ASSET_TYPE_NS, name)),
            "created_by": current_user_id,
            "created_at": now,
            "updated_at": now,
        }
        saved = self._save_asset_type(doc)
        self._type_cache[name] = SimpleNamespace(id=UUID(str(saved["id"])), name=name)
        return self._to_asset_type_read(saved)

    def update_asset_type(
        self,
        db=None,
        *,
        type_id: UUID = None,
        type_in: AssetTypeUpdate = None,
        current_user_id: str = "system",
        **_,
    ) -> AssetTypeRead:
        doc = self._asset_type_doc(str(type_id))
        if not doc:
            raise NotFoundError(f"Asset type '{type_id}' not found.")
        updates = type_in.model_dump(exclude_unset=True) if type_in else {}
        doc.update({k: (v.value if hasattr(v, "value") else v) for k, v in updates.items()})
        doc["updated_at"] = _now().isoformat()
        saved = self._save_asset_type(doc)
        # Name may have changed; rebuild the cache lazily rather than patching it.
        self._type_cache.clear()
        self._types_loaded = False
        counts = self._asset_counts_by_type()
        return self._to_asset_type_read(saved, asset_count=counts.get(str(saved.get("id")), 0))

    def delete_asset_type(self, db=None, *, type_id: UUID = None, **_) -> AssetTypeRead:
        doc = self._asset_type_doc(str(type_id))
        if not doc:
            raise NotFoundError(f"Asset type '{type_id}' not found.")
        in_use = self._asset_counts_by_type().get(str(doc.get("id")), 0)
        if in_use:
            raise ConflictError(
                f"Cannot delete asset type '{doc.get('name')}': {in_use} assets still reference it."
            )
        read = self._to_asset_type_read(doc)
        self._entities.delete_entity("asset_types", str(doc["id"]))
        self._type_cache.clear()
        self._types_loaded = False
        logger.info("Deleted UC-native asset type '%s' (%s)", read.name, type_id)
        return read

    def _save_asset_type(self, doc: Dict[str, Any]) -> Dict[str, Any]:
        return self._entities.save_entity(
            "asset_types",
            doc,
            index_fields={
                "name": doc.get("name"),
                "category": doc.get("category"),
                "is_system": bool(doc.get("is_system", False)),
                "status": doc.get("status") or "active",
                "updated_at": doc.get("updated_at"),
            },
        )

    # --- Assets ------------------------------------------------------------

    def list_assets(self, limit: int = 500, **_) -> List[Dict[str, Any]]:
        return self._entities.list_entities("assets", limit=limit)

    def get_asset_doc(self, asset_id: str) -> Optional[Dict[str, Any]]:
        """Raw entity document — internal callers that need the denormalized dict."""
        return self._entities.get_entity("assets", str(asset_id))

    @staticmethod
    def _identity_key(
        name: str,
        asset_type_id: UUID | str,
        platform: Optional[str],
        location: Optional[str],
    ) -> tuple:
        return (
            name,
            str(asset_type_id),
            platform or "",
            location or "",
        )

    def _ensure_identity_index(self) -> Dict[tuple, UUID]:
        """Load assets once into memory for O(1) identity lookups (preview/import)."""
        if self._identity_index is not None:
            return self._identity_index
        index: Dict[tuple, UUID] = {}
        # Cap is generous; schema import identity checks must not re-scan Delta per item.
        for doc in self._entities.list_entities("assets", limit=20000):
            if not doc.get("id") or not doc.get("name") or not doc.get("asset_type_id"):
                continue
            key = self._identity_key(
                doc["name"],
                doc["asset_type_id"],
                doc.get("platform"),
                doc.get("location"),
            )
            index[key] = UUID(str(doc["id"]))
        self._identity_index = index
        logger.debug("Built UC-native asset identity index (%s entries)", len(index))
        return index

    def _invalidate_identity_index(self) -> None:
        self._identity_index = None

    def get_by_identity(
        self,
        *,
        name: str,
        asset_type_id: UUID,
        platform: Optional[str] = None,
        location: Optional[str] = None,
        **_,
    ) -> Optional[SimpleNamespace]:
        index = self._ensure_identity_index()
        key = self._identity_key(name, asset_type_id, platform, location)
        asset_id = index.get(key)
        if asset_id is None:
            return None
        return SimpleNamespace(id=asset_id)

    def _to_asset_read(self, doc: Dict[str, Any]) -> AssetRead:
        now = _now()
        type_id = doc.get("asset_type_id")
        if not type_id:
            # Fallback: resolve from type name if present
            type_name = doc.get("asset_type_name") or "Table"
            at = self.get_asset_type_by_name(type_name)
            type_id = str(at.id) if at else str(uuid.uuid5(_ASSET_TYPE_NS, type_name))
        payload = {
            "id": doc.get("id"),
            "name": doc.get("name", "Asset"),
            "description": doc.get("description"),
            "asset_type_id": type_id,
            "asset_type_name": doc.get("asset_type_name"),
            "platform": doc.get("platform"),
            "location": doc.get("location"),
            "domain_id": doc.get("domain_id"),
            "properties": doc.get("properties"),
            "tags": doc.get("tags") or [],
            "status": doc.get("status") or "active",
            "created_by": doc.get("created_by"),
            "created_at": doc.get("created_at") or now,
            "updated_at": doc.get("updated_at") or now,
            "relationships": doc.get("relationships") or [],
        }
        return AssetRead.model_validate(payload)

    def create_asset(
        self,
        db_or_payload=None,
        *,
        asset_in: Optional[AssetCreate] = None,
        current_user_id: str = "system",
        payload: Optional[Dict[str, Any]] = None,
        **_,
    ):
        """Create an asset.

        Supports:
        - Schema Import: ``create_asset(db, asset_in=..., current_user_id=...)`` -> AssetRead
        - Legacy dict: ``create_asset(payload={...})`` or ``create_asset({...})`` -> dict
        """
        if asset_in is not None:
            doc = self._asset_doc(asset_in, current_user_id)
            saved = self._entities.save_entity(
                "assets",
                doc,
                index_fields={
                    "name": doc["name"],
                    "asset_type_name": doc.get("asset_type_name"),
                    "status": doc["status"],
                    "updated_at": doc["updated_at"],
                },
            )
            type_id = saved.get("asset_type_id")
            if self._identity_index is not None and type_id:
                key = self._identity_key(
                    saved["name"],
                    type_id,
                    saved.get("platform"),
                    saved.get("location"),
                )
                self._identity_index[key] = UUID(str(saved["id"]))
            logger.info("Created UC-native asset '%s' (%s)", saved["name"], saved["id"])
            return self._to_asset_read(saved)

        raw = payload if payload is not None else db_or_payload
        if not isinstance(raw, dict):
            raise TypeError("create_asset requires asset_in= or a dict payload")
        raw = dict(raw)
        raw.setdefault("id", str(uuid.uuid4()))
        raw.setdefault("status", "active")
        raw.setdefault("updated_at", _now().isoformat())
        saved = self._entities.save_entity(
            "assets",
            raw,
            index_fields={
                "name": raw.get("name"),
                "asset_type_name": raw.get("asset_type_name") or raw.get("asset_type"),
                "status": raw.get("status"),
                "updated_at": raw.get("updated_at"),
            },
        )
        self._invalidate_identity_index()
        return saved

    def _asset_doc(self, asset_in: AssetCreate, current_user_id: str) -> Dict[str, Any]:
        data = asset_in.model_dump()
        type_id = data.get("asset_type_id")
        type_name = None
        if type_id:
            at = self.get_asset_type(UUID(str(type_id)))
            type_name = at.name if at else None
        now = _now().isoformat()
        return {
            "id": str(uuid.uuid4()),
            "name": data["name"],
            "description": data.get("description"),
            "asset_type_id": str(type_id) if type_id else None,
            "asset_type_name": type_name,
            "platform": data.get("platform"),
            "location": data.get("location"),
            "domain_id": str(data["domain_id"]) if data.get("domain_id") else None,
            "properties": data.get("properties"),
            "tags": data.get("tags") or [],
            "status": (
                data["status"].value
                if hasattr(data.get("status"), "value")
                else (data.get("status") or "active")
            ),
            "created_by": current_user_id,
            "created_at": now,
            "updated_at": now,
        }

    def create_assets_bulk(
        self,
        asset_inputs: List[AssetCreate],
        *,
        current_user_id: str = "system",
    ) -> List[AssetRead]:
        """Create known-new assets using chunked Delta inserts."""
        docs = [self._asset_doc(asset_in, current_user_id) for asset_in in asset_inputs]
        entries = [
            (
                doc,
                {
                    "name": doc["name"],
                    "asset_type_name": doc.get("asset_type_name"),
                    "status": doc["status"],
                    "updated_at": doc["updated_at"],
                },
            )
            for doc in docs
        ]
        if hasattr(self._entities, "create_entities"):
            saved_docs = self._entities.create_entities("assets", entries)
        else:
            saved_docs = [
                self._entities.save_entity("assets", doc, index_fields=index_fields)
                for doc, index_fields in entries
            ]
        if not isinstance(saved_docs, list):
            raise TypeError(
                f"create_entities must return a list of docs, got {type(saved_docs).__name__}"
            )

        if self._identity_index is not None:
            for saved in saved_docs:
                type_id = saved.get("asset_type_id")
                if type_id:
                    self._identity_index[
                        self._identity_key(
                            saved["name"],
                            type_id,
                            saved.get("platform"),
                            saved.get("location"),
                        )
                    ] = UUID(str(saved["id"]))
        logger.info("Created %s UC-native assets in bulk", len(saved_docs))
        return [self._to_asset_read(saved) for saved in saved_docs]

    def add_relationship(
        self,
        db=None,
        *,
        rel_in: AssetRelationshipCreate,
        current_user_id: str = "system",
    ) -> AssetRelationshipRead:
        """Persist an asset-to-asset relationship in entity_relationships."""
        now = _now()
        if self._overlays is not None:
            props = dict(rel_in.properties or {})
            source_type = props.get("source_entity_type") or props.get("source_type") or "asset"
            target_type = props.get("target_entity_type") or props.get("target_type") or "asset"
            saved = self._overlays.add_relationship(
                source_entity_id=str(rel_in.source_asset_id),
                source_entity_type=source_type,
                target_entity_id=str(rel_in.target_asset_id),
                target_entity_type=target_type,
                relationship_type=rel_in.relationship_type,
                properties=props,
            )
            return AssetRelationshipRead(
                id=UUID(str(saved["id"])),
                source_asset_id=rel_in.source_asset_id,
                target_asset_id=rel_in.target_asset_id,
                relationship_type=rel_in.relationship_type,
                properties=rel_in.properties,
                created_by=current_user_id,
                created_at=now,
            )
        # Fallback when overlays unavailable (unit tests): return ephemeral read model.
        logger.warning(
            "UC-native overlays unavailable; relationship %s %s -> %s not persisted",
            rel_in.relationship_type,
            rel_in.source_asset_id,
            rel_in.target_asset_id,
        )
        return AssetRelationshipRead(
            id=uuid.uuid4(),
            source_asset_id=rel_in.source_asset_id,
            target_asset_id=rel_in.target_asset_id,
            relationship_type=rel_in.relationship_type,
            properties=rel_in.properties,
            created_by=current_user_id,
            created_at=now,
        )

    def add_relationships_bulk(
        self,
        relationships: List[AssetRelationshipCreate],
        *,
        current_user_id: str = "system",
    ) -> List[AssetRelationshipRead]:
        """Persist known-new asset relationships with chunked Delta inserts."""
        if self._overlays is None or not hasattr(self._overlays, "add_relationships"):
            return [
                self.add_relationship(rel_in=relationship, current_user_id=current_user_id)
                for relationship in relationships
            ]
        saved_rows = self._overlays.add_relationships(
            [
                {
                    "source_entity_id": str(relationship.source_asset_id),
                    "source_entity_type": (relationship.properties or {}).get(
                        "source_entity_type", "asset"
                    ),
                    "target_entity_id": str(relationship.target_asset_id),
                    "target_entity_type": (relationship.properties or {}).get(
                        "target_entity_type", "asset"
                    ),
                    "relationship_type": relationship.relationship_type,
                    "properties": relationship.properties,
                }
                for relationship in relationships
            ]
        )
        if not isinstance(saved_rows, list):
            raise TypeError(
                f"add_relationships must return a list of rows, got {type(saved_rows).__name__}"
            )
        now = _now()
        return [
            AssetRelationshipRead(
                id=UUID(str(saved["id"])),
                source_asset_id=relationship.source_asset_id,
                target_asset_id=relationship.target_asset_id,
                relationship_type=relationship.relationship_type,
                properties=relationship.properties,
                created_by=current_user_id,
                created_at=now,
            )
            for relationship, saved in zip(relationships, saved_rows)
        ]

    def remove_relationship(self, db=None, *, relationship_id: UUID = None, **_) -> bool:
        if self._overlays is None or not hasattr(self._overlays, "delete_relationship"):
            raise NotFoundError(f"Relationship '{relationship_id}' not found.")
        return bool(self._overlays.delete_relationship(str(relationship_id)))

    def resolve_accessible_asset_ids(self, db=None, *, data_products_manager=None, is_admin: bool = False):
        if is_admin:
            return None
        return None

    def is_asset_accessible(
        self,
        db=None,
        *,
        asset_id: UUID = None,
        data_products_manager=None,
        is_admin: bool = False,
        **_,
    ) -> bool:
        """UC-native mode has no Data-Product asset scoping yet; mirror the unscoped path."""
        return True

    def get_all_assets(
        self,
        db=None,
        skip: int = 0,
        limit: int = 100,
        asset_type_id: Optional[UUID] = None,
        asset_type_names: Optional[List[str]] = None,
        platform: Optional[str] = None,
        domain_id: Optional[str] = None,
        status: Optional[str] = None,
        name: Optional[str] = None,
        **_,
    ) -> PaginatedAssetSummary:
        docs = self._entities.list_entities("assets", limit=20000)
        if asset_type_id:
            docs = [d for d in docs if str(d.get("asset_type_id")) == str(asset_type_id)]
        if asset_type_names:
            wanted = {n.lower() for n in asset_type_names}
            docs = [d for d in docs if str(d.get("asset_type_name") or "").lower() in wanted]
        if platform:
            docs = [d for d in docs if str(d.get("platform") or "") == str(platform)]
        if domain_id:
            docs = [d for d in docs if str(d.get("domain_id") or "") == str(domain_id)]
        if status:
            docs = [d for d in docs if str(d.get("status") or "active") == str(status)]
        if name:
            needle = name.lower()
            docs = [d for d in docs if needle in str(d.get("name") or "").lower()]
        docs.sort(key=lambda d: str(d.get("name") or "").lower())
        page = docs[skip : skip + limit]
        parents = self._parent_index()
        names = {str(d.get("id")): d.get("name") for d in docs}
        items = []
        for doc in page:
            try:
                items.append(self._to_asset_summary(doc, parents=parents, names=names))
            except Exception as exc:
                logger.warning("Skipping malformed asset row %s: %s", doc.get("id"), exc)
        return PaginatedAssetSummary(items=items, total=len(docs), skip=skip, limit=limit)

    def _parent_index(self) -> Dict[str, str]:
        """child asset id -> parent asset id, from hierarchical relationships."""
        index: Dict[str, str] = {}
        for parent_id, children in self._hierarchy_index().items():
            for child_id, _rel in children:
                index[child_id] = parent_id
        return index

    def _to_asset_summary(
        self,
        doc: Dict[str, Any],
        *,
        parents: Optional[Dict[str, str]] = None,
        names: Optional[Dict[str, Optional[str]]] = None,
    ) -> AssetSummary:
        summary = AssetSummary.model_validate(self._to_asset_read(doc).model_dump())
        parent_id = (parents or {}).get(str(doc.get("id")))
        if parent_id:
            summary.parent_id = UUID(str(parent_id))
            summary.parent_name = (names or {}).get(str(parent_id))
        return summary

    def get_asset(self, db=None, asset_id: Any = None, **_) -> Optional[AssetRead]:
        """Asset detail as a read model.

        Tolerates ``get_asset(asset_id)`` positionally as well as the route's
        ``get_asset(db=db, asset_id=...)``.
        """
        if asset_id is None and isinstance(db, (str, UUID)):
            asset_id = db
        doc = self.get_asset_doc(str(asset_id))
        if not doc:
            return None
        read = self._to_asset_read(doc)
        if self._overlays is not None:
            read.relationships = self._asset_relationships(str(asset_id))
        return read

    def get_asset_by_id(self, db=None, asset_id: UUID = None) -> Optional[AssetRead]:
        return self.get_asset(db=db, asset_id=asset_id)

    def _asset_relationships(self, asset_id: str) -> List[AssetRelationshipRead]:
        if not hasattr(self._overlays, "list_relationships"):
            return []
        try:
            rows = self._overlays.list_relationships(entity_id=asset_id)
        except Exception as exc:
            logger.warning("Failed to load relationships for asset %s: %s", asset_id, exc)
            return []
        out: List[AssetRelationshipRead] = []
        for row in rows or []:
            props = row.get("properties") or {}
            try:
                out.append(
                    AssetRelationshipRead(
                        id=UUID(str(row["id"])),
                        source_asset_id=UUID(str(row["source_entity_id"])),
                        target_asset_id=UUID(str(row["target_entity_id"])),
                        relationship_type=row.get("relationship_type") or "relatedTo",
                        properties=props,
                        created_by=props.get("created_by"),
                        created_at=_now(),
                    )
                )
            except Exception:
                # Non-asset endpoints (e.g. data-product links) aren't UUID-addressable here.
                continue
        return out

    def update_asset(
        self,
        db=None,
        *,
        asset_id: UUID = None,
        asset_in: AssetUpdate = None,
        current_user_id: str = "system",
        **_,
    ) -> AssetRead:
        doc = self.get_asset_doc(str(asset_id))
        if not doc:
            raise NotFoundError(f"Asset '{asset_id}' not found.")
        updates = asset_in.model_dump(exclude_unset=True) if asset_in else {}
        new_type_id = updates.get("asset_type_id")
        if new_type_id:
            at = self.get_asset_type(UUID(str(new_type_id)))
            if not at:
                raise NotFoundError(f"Asset type '{new_type_id}' not found.")
            updates["asset_type_id"] = str(new_type_id)
            updates["asset_type_name"] = at.name
        for key, value in updates.items():
            doc[key] = value.value if hasattr(value, "value") else value
        if doc.get("domain_id"):
            doc["domain_id"] = str(doc["domain_id"])
        doc["updated_at"] = _now().isoformat()
        saved = self._entities.save_entity(
            "assets",
            doc,
            index_fields={
                "name": doc.get("name"),
                "asset_type_name": doc.get("asset_type_name"),
                "status": doc.get("status") or "active",
                "updated_at": doc["updated_at"],
            },
        )
        self._invalidate_identity_index()
        logger.info("Updated UC-native asset '%s' (%s)", saved.get("name"), asset_id)
        return self._to_asset_read(saved)

    def delete_asset(self, db=None, *, asset_id: UUID = None, current_user_id: str = "system", **_) -> AssetRead:
        doc = self.get_asset_doc(str(asset_id))
        if not doc:
            raise NotFoundError(f"Asset '{asset_id}' not found.")
        read = self._to_asset_read(doc)
        self._entities.delete_entity("assets", str(asset_id))
        self._invalidate_identity_index()
        logger.info("Deleted UC-native asset '%s' (%s)", read.name, asset_id)
        return read

    def get_delete_preview(self, db=None, *, asset_id: UUID = None, **_) -> DeletePreviewItem:
        """Cascade preview: the asset plus its descendants via hierarchical relationships."""
        doc = self.get_asset_doc(str(asset_id))
        if not doc:
            raise NotFoundError(f"Asset '{asset_id}' not found.")
        children_by_parent = self._hierarchy_index()

        def build(node_id: str, node: Dict[str, Any], level: int, rel_type: Optional[str], seen: Set[str]) -> DeletePreviewItem:
            seen.add(node_id)
            children: List[DeletePreviewItem] = []
            for child_id, child_rel in children_by_parent.get(node_id, []):
                if child_id in seen:
                    continue
                child_doc = self.get_asset_doc(child_id)
                if not child_doc:
                    continue
                children.append(build(child_id, child_doc, level + 1, child_rel, seen))
            return DeletePreviewItem(
                id=UUID(str(node_id)),
                name=node.get("name") or "Asset",
                asset_type_name=node.get("asset_type_name"),
                relationship_type=rel_type,
                level=level,
                children=children,
            )

        return build(str(asset_id), doc, 0, None, set())

    def _hierarchy_index(self) -> Dict[str, List[tuple[str, str]]]:
        """parent asset id -> [(child asset id, relationship type)] from overlay relationships."""
        index: Dict[str, List[tuple[str, str]]] = {}
        if self._overlays is None or not hasattr(self._overlays, "list_relationships"):
            return index
        try:
            rows = self._overlays.list_relationships(relationship_type=_HIERARCHICAL_RELATIONSHIPS)
        except Exception as exc:
            logger.warning("Failed to load asset hierarchy for delete preview: %s", exc)
            return index
        for row in rows or []:
            parent = str(row.get("source_entity_id") or "")
            child = str(row.get("target_entity_id") or "")
            if parent and child:
                index.setdefault(parent, []).append((child, row.get("relationship_type") or ""))
        return index

    def cascade_delete_assets(
        self,
        db=None,
        *,
        asset_ids: List[UUID] = None,
        current_user_id: str = "system",
        **_,
    ) -> CascadeDeleteResult:
        result = CascadeDeleteResult()
        for asset_id in asset_ids or []:
            try:
                preview = self.get_delete_preview(asset_id=asset_id)
            except NotFoundError as exc:
                result.failed.append({"id": str(asset_id), "error": str(exc)})
                continue
            for node in _flatten_preview(preview):
                try:
                    self._entities.delete_entity("assets", str(node.id))
                    result.deleted.append({"id": str(node.id), "name": node.name})
                except Exception as exc:
                    result.failed.append({"id": str(node.id), "name": node.name, "error": str(exc)})
        self._invalidate_identity_index()
        return result

    def infer_schema_from_asset(self, db=None, *, asset_id: UUID = None, **_) -> Dict[str, Any]:
        """Schema inference from Column children of a table/view asset."""
        doc = self.get_asset_doc(str(asset_id))
        if not doc:
            raise NotFoundError(f"Asset '{asset_id}' not found.")
        columns: List[Dict[str, Any]] = []
        for child_id, _rel in self._hierarchy_index().get(str(asset_id), []):
            child = self.get_asset_doc(child_id)
            if not child or str(child.get("asset_type_name")) != "Column":
                continue
            props = child.get("properties") or {}
            columns.append(
                {
                    "name": child.get("name"),
                    "type": props.get("data_type") or props.get("type") or "string",
                    "nullable": props.get("nullable", True),
                    "description": child.get("description"),
                }
            )
        return {
            "asset_id": str(asset_id),
            "asset_name": doc.get("name"),
            "columns": columns,
        }


class UcNativeDataDomainManager:
    def __init__(self, entities: UcNativeEntityStore) -> None:
        self._entities = entities

    def _to_read(self, doc: Dict[str, Any]) -> DataDomainRead:
        now = _now()
        created_at = doc.get("created_at") or now
        updated_at = doc.get("updated_at") or now
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        if isinstance(updated_at, str):
            updated_at = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        # AssignedTagCreate payloads from create/update are not AssignedTag reads yet.
        tags = doc.get("tags") or []
        if tags and not (
            hasattr(tags[0], "tag_name")
            or (isinstance(tags[0], dict) and "tag_name" in tags[0])
        ):
            tags = []
        return DataDomainRead(
            id=UUID(str(doc["id"])),
            name=doc.get("name", ""),
            description=doc.get("description"),
            parent_id=UUID(str(doc["parent_id"])) if doc.get("parent_id") else None,
            created_at=created_at,
            updated_at=updated_at,
            created_by=doc.get("created_by", "system"),
            tags=tags,
            parent_name=doc.get("parent_name"),
            children_count=int(doc.get("children_count") or 0),
            children_info=doc.get("children_info") or [],
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
        domains: List[DataDomainRead] = []
        for doc in docs[skip : skip + limit]:
            try:
                domains.append(self._to_read(doc))
            except Exception:
                # DataDomainRead.id must be a UUID. One row written with a
                # non-UUID id (e.g. an older demo seed) must not take down the
                # whole listing.
                logger.warning(
                    "Skipping unreadable data_domains row id=%r", doc.get("id"), exc_info=True
                )
        return domains

    def get_domain_by_id(self, db, domain_id: UUID) -> Optional[DataDomainRead]:
        doc = self.get_domain(str(domain_id))
        return self._to_read(doc) if doc else None

    def update_domain(
        self,
        db=None,
        domain_id: UUID = None,
        domain_in: DataDomainUpdate = None,
        current_user_id: str = "system",
        **_,
    ) -> DataDomainRead:
        existing = self.get_domain(str(domain_id))
        if not existing:
            raise NotFoundError(f"Data domain with id '{domain_id}' not found.")
        updates = domain_in.model_dump(exclude_unset=True) if domain_in else {}
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

    def delete_domain(
        self,
        db=None,
        domain_id: UUID = None,
        current_user_id: str = None,
        **_,
    ) -> DataDomainRead:
        existing = self.get_domain(str(domain_id))
        if not existing:
            raise NotFoundError(f"Data domain with id '{domain_id}' not found.")
        read_model = self._to_read(existing)
        self._entities.delete_entity("data_domains", str(domain_id))
        return read_model


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


def _uc_search_items(manager: Any, table: str, item_type: str, feature_id: str, link_prefix: str):
    """Build the common search shape without introducing another storage path."""
    from src.common.search_interfaces import SearchIndexItem

    return [
        SearchIndexItem(
            id=f"{item_type}::{doc['id']}",
            type=item_type,
            title=doc.get("name") or "Unnamed",
            description=doc.get("description"),
            link=f"{link_prefix}/{doc['id']}",
            tags=[str(tag) for tag in (doc.get("tags") or [])],
            feature_id=feature_id,
            extra_data={"status": doc.get("status")},
        )
        for doc in manager._entities.list_entities(table, limit=10_000)
        if doc.get("id")
    ]


def _products_search_items(self):
    return _uc_search_items(self, "data_products", "data-product", "data-products", "/data-products")


def _contracts_search_items(self):
    return _uc_search_items(self, "data_contracts", "data-contract", "data-contracts", "/data-contracts")


def _domains_search_items(self):
    return _uc_search_items(self, "data_domains", "data-domain", "data-domains", "/data-domains")


def _assets_search_items(self):
    return _uc_search_items(self, "assets", "asset", "assets", "/assets")


def _tags_search_items(self):
    return _uc_search_items(self, "tags", "tag", "tags", "/tags")


UcNativeDataProductsManager.get_search_index_items = _products_search_items
UcNativeDataContractsManager.get_search_index_items = _contracts_search_items
UcNativeDataDomainManager.get_search_index_items = _domains_search_items
UcNativeAssetsManager.get_search_index_items = _assets_search_items
UcNativeTagsManager.get_search_index_items = _tags_search_items
