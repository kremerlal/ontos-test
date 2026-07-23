"""Unit tests for UC-native manager helpers."""

import json
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
