"""Unit tests for UC-native manager helpers."""

import json
import uuid
from types import SimpleNamespace
from uuid import uuid4

from src.common.uc_native.managers import (
    UcNativeDataContractsManager,
    _doc_to_contract_summary,
)
from src.common.uc_native.rbac import UcNativeRbacStore


def test_doc_to_contract_summary_maps_fields():
    doc = {
        "id": str(uuid4()),
        "name": "Orders Contract",
        "version": "2.0.0",
        "status": "active",
        "project_id": "proj-1",
        "domain_id": "dom-1",
        "data_product": "prod-1",
    }
    summary = _doc_to_contract_summary(doc)
    assert summary.name == "Orders Contract"
    assert summary.status == "active"
    assert summary.project_id == "proj-1"
    assert summary.domainId == "dom-1"
    assert summary.dataProduct == "prod-1"


class _FakeEntities:
    def __init__(self, docs):
        self._docs = docs

    def list_entities(self, table, limit=500):
        return self._docs[:limit]


def test_list_contracts_from_db_filters_status():
    docs = [
        {"id": "1", "name": "A", "status": "draft", "version": "1.0.0"},
        {"id": "2", "name": "B", "status": "active", "version": "1.0.0"},
    ]
    mgr = UcNativeDataContractsManager(_FakeEntities(docs))
    result = mgr.list_contracts_from_db(None, status="active", is_admin=True)
    assert len(result) == 1
    assert result[0].name == "B"


class _FakeRbacStore:
    def __init__(self):
        self.roles = [
            {
                "id": "admin-role",
                "name": "Admin",
                "assigned_groups_json": '["admins"]',
                "feature_permissions_json": "{}",
                "home_sections_json": "[]",
                "is_admin_role": True,
            }
        ]
        self.merged = []

    def list_rows(self, table, limit=200):
        return self.roles

    def merge_row(self, table, row):
        self.merged.append(row)


def test_existing_admin_role_adds_configured_users_group():
    store = _FakeRbacStore()
    settings = SimpleNamespace(
        APP_ADMIN_DEFAULT_GROUPS='["admins", "users"]'
    )

    count = UcNativeRbacStore(store, settings).seed_default_roles()

    assert count == 0
    assert len(store.merged) == 1
    assert json.loads(store.merged[0]["assigned_groups_json"]) == [
        "admins",
        "users",
    ]


class _FakeProductEntities:
    def __init__(self, docs):
        self._docs = docs

    def list_entities(self, table, limit=500):
        return self._docs[:limit]

    def get_entity(self, table, entity_id):
        for doc in self._docs:
            if doc.get("id") == entity_id:
                return doc
        return None


def test_get_published_products_filters_scope():
    from src.common.uc_native.managers import UcNativeDataProductsManager

    docs = [
        {
            "id": "p1",
            "name": "Public Product",
            "status": "active",
            "publication_scope": "organization",
            "info": {"title": "Public Product", "owner": "team"},
        },
        {
            "id": "p2",
            "name": "Draft Product",
            "status": "draft",
            "publication_scope": "none",
            "info": {"title": "Draft Product", "owner": "team"},
        },
        {
            "id": "p3",
            "name": "Domain Product",
            "status": "active",
            "publication_scope": "domain",
            "info": {"title": "Domain Product", "owner": "team"},
        },
    ]
    mgr = UcNativeDataProductsManager(_FakeProductEntities(docs))
    all_published = mgr.get_published_products()
    assert {p.id for p in all_published} == {"p1", "p3"}

    domain_only = mgr.get_published_products(scope="domain")
    assert [p.id for p in domain_only] == ["p3"]


def test_get_user_subscriptions_returns_empty():
    from src.common.uc_native.managers import UcNativeDataProductsManager

    mgr = UcNativeDataProductsManager(_FakeProductEntities([]))
    assert mgr.get_user_subscriptions("user@example.com") == []


class _FakeConnEntities:
    def __init__(self):
        self._docs = []

    def list_entities(self, table, limit=500):
        return list(self._docs)[:limit]

    def get_entity(self, table, entity_id):
        for doc in self._docs:
            if doc.get("id") == entity_id:
                return dict(doc)
        return None

    def save_entity(self, table, payload, index_fields=None):
        payload = dict(payload)
        for i, doc in enumerate(self._docs):
            if doc.get("id") == payload.get("id"):
                self._docs[i] = payload
                return payload
        self._docs.append(payload)
        return payload

    def delete_entity(self, table, entity_id):
        self._docs = [d for d in self._docs if d.get("id") != entity_id]


def test_uc_native_connections_ensures_system_databricks():
    from src.common.uc_native.managers import UcNativeConnectionsManager

    entities = _FakeConnEntities()
    mgr = UcNativeConnectionsManager(entities, workspace_client=None)
    mgr.ensure_system_databricks_connection()
    mgr.ensure_system_databricks_connection()  # idempotent

    listed = mgr.list_connections()
    assert len(listed) == 1
    assert listed[0].name == "Databricks UC"
    assert listed[0].connector_type == "databricks"
    assert listed[0].is_default is True


def test_uc_native_build_databricks_connector_uses_workspace_client():
    from types import SimpleNamespace
    from src.common.uc_native.managers import UcNativeConnectionsManager
    from src.connectors.databricks import DatabricksConnector

    entities = _FakeConnEntities()
    ws = SimpleNamespace(catalogs=SimpleNamespace(list=lambda: []))
    mgr = UcNativeConnectionsManager(entities, workspace_client=ws)
    mgr.ensure_system_databricks_connection()
    conn = mgr.list_connections()[0]
    connector = mgr.get_connector_for_connection(conn.id)
    assert isinstance(connector, DatabricksConnector)


class _FakeMultiTableEntities:
    def __init__(self):
        self._tables = {}
        self.bulk_create_calls = 0

    def list_entities(self, table, limit=500):
        return list(self._tables.get(table, []))[:limit]

    def get_entity(self, table, entity_id):
        for doc in self._tables.get(table, []):
            if doc.get("id") == entity_id:
                return dict(doc)
        return None

    def save_entity(self, table, payload, index_fields=None):
        payload = dict(payload)
        rows = self._tables.setdefault(table, [])
        for i, doc in enumerate(rows):
            if doc.get("id") == payload.get("id"):
                rows[i] = payload
                return payload
        rows.append(payload)
        return payload

    def create_entities(self, table, entries):
        self.bulk_create_calls += 1
        return [
            self.save_entity(table, payload, index_fields=index_fields)
            for payload, index_fields in entries
        ]


def test_uc_native_assets_seeds_types_and_creates_asset():
    from src.common.uc_native.managers import UcNativeAssetsManager
    from src.models.assets import AssetCreate, AssetStatus

    entities = _FakeMultiTableEntities()
    mgr = UcNativeAssetsManager(entities)
    created = mgr.ensure_default_asset_types()
    assert created >= 9
    assert mgr.ensure_default_asset_types() == 0  # idempotent

    catalog_type = mgr.get_asset_type_by_name("Catalog")
    assert catalog_type is not None
    assert catalog_type.name == "Catalog"

    asset = mgr.create_asset(
        None,
        asset_in=AssetCreate(
            name="my_catalog",
            asset_type_id=catalog_type.id,
            platform="databricks",
            location="my_catalog",
            status=AssetStatus.ACTIVE,
        ),
        current_user_id="tester@example.com",
    )
    assert asset.name == "my_catalog"
    assert asset.asset_type_name == "Catalog"
    assert asset.asset_type_id == catalog_type.id

    found = mgr.get_by_identity(
        name="my_catalog",
        asset_type_id=catalog_type.id,
        platform="databricks",
        location="my_catalog",
    )
    assert found is not None
    assert found.id == asset.id

    # Second identity lookup must reuse the in-memory index (no extra Delta scan).
    list_calls = {"assets": 0}
    original_list = entities.list_entities

    def counting_list(table, limit=500):
        list_calls[table] = list_calls.get(table, 0) + 1
        return original_list(table, limit=limit)

    entities.list_entities = counting_list
    assert mgr.get_by_identity(
        name="my_catalog",
        asset_type_id=catalog_type.id,
        platform="databricks",
        location="my_catalog",
    ) is not None
    assert list_calls.get("assets", 0) == 0


def test_uc_native_assets_bulk_create_uses_single_store_call():
    from src.common.uc_native.managers import UcNativeAssetsManager
    from src.models.assets import AssetCreate, AssetStatus

    entities = _FakeMultiTableEntities()
    manager = UcNativeAssetsManager(entities)
    manager.ensure_default_asset_types()
    table_type = manager.get_asset_type_by_name("Table")

    assets = manager.create_assets_bulk(
        [
            AssetCreate(
                name=f"table_{index}",
                asset_type_id=table_type.id,
                platform="databricks",
                location=f"catalog.schema.table_{index}",
                status=AssetStatus.ACTIVE,
            )
            for index in range(50)
        ],
        current_user_id="tester@example.com",
    )

    assert len(assets) == 50
    assert entities.bulk_create_calls == 1
    assert len(entities.list_entities("assets")) == 50


class _FakeOverlays:
    def __init__(self):
        self.rows = []
        self.bulk_calls = 0

    def add_relationship(self, **kwargs):
        row = {
            "id": str(uuid.uuid4()),
            **kwargs,
            "snapshot_json": "{}",
        }
        self.rows.append(row)
        return row

    def add_relationships(self, relationships):
        self.bulk_calls += 1
        payloads = []
        for relationship in relationships:
            payloads.append(
                {
                    "id": str(uuid.uuid4()),
                    "source_entity_id": relationship["source_entity_id"],
                    "source_entity_type": relationship["source_entity_type"],
                    "target_entity_id": relationship["target_entity_id"],
                    "target_entity_type": relationship["target_entity_type"],
                    "relationship_type": relationship["relationship_type"],
                    "snapshot_json": "{}",
                }
            )
        self.rows.extend(payloads)
        return payloads


def test_uc_native_assets_bulk_relationships_uses_single_overlay_call():
    from src.common.uc_native.managers import UcNativeAssetsManager
    from src.models.assets import AssetCreate, AssetRelationshipCreate, AssetStatus

    entities = _FakeMultiTableEntities()
    overlays = _FakeOverlays()
    manager = UcNativeAssetsManager(entities, overlays=overlays)
    manager.ensure_default_asset_types()
    schema_type = manager.get_asset_type_by_name("Schema")
    table_type = manager.get_asset_type_by_name("Table")

    parent = manager.create_asset(
        None,
        asset_in=AssetCreate(
            name="schema",
            asset_type_id=schema_type.id,
            platform="databricks",
            location="catalog.schema",
            status=AssetStatus.ACTIVE,
        ),
        current_user_id="tester@example.com",
    )
    children = manager.create_assets_bulk(
        [
            AssetCreate(
                name=f"table_{index}",
                asset_type_id=table_type.id,
                platform="databricks",
                location=f"catalog.schema.table_{index}",
                status=AssetStatus.ACTIVE,
            )
            for index in range(20)
        ],
        current_user_id="tester@example.com",
    )

    created = manager.add_relationships_bulk(
        [
            AssetRelationshipCreate(
                source_asset_id=parent.id,
                target_asset_id=child.id,
                relationship_type="hasTable",
            )
            for child in children
        ],
        current_user_id="tester@example.com",
    )

    assert len(created) == 20
    assert overlays.bulk_calls == 1
    assert len(overlays.rows) == 20
    assert all(row.relationship_type == "hasTable" for row in created)
