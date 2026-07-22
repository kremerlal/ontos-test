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
