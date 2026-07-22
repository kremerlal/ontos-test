"""Unit tests for UC-native manager helpers."""

from uuid import uuid4

from src.common.uc_native.managers import (
    UcNativeDataContractsManager,
    _doc_to_contract_summary,
)


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
