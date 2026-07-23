"""Unit tests for UC-native entity relationships manager."""

from types import SimpleNamespace
from uuid import uuid4

from src.common.uc_native.entity_relationships_manager import UcNativeEntityRelationshipsManager
from src.models.entity_relationships import EntityRelationshipCreate


class _FakeStore:
    def __init__(self):
        self.rows = []

    def table_fqn(self, table_name):
        return f"catalog.schema.{table_name}"

    def query(self, statement):
        return list(self.rows)

    def get_by_id(self, table_name, row_id):
        for row in self.rows:
            if row.get("id") == row_id:
                return dict(row)
        return None

    def delete_by_id(self, table_name, row_id):
        self.rows = [row for row in self.rows if row.get("id") != row_id]

    def merge_row(self, table_name, payload):
        self.rows.append(dict(payload))
        return payload.get("id")


class _FakeOverlays:
    def __init__(self, store):
        self._store = store

    def add_relationship(
        self,
        *,
        source_entity_id,
        source_entity_type,
        target_entity_id,
        target_entity_type,
        relationship_type,
        properties=None,
    ):
        import json
        from uuid import uuid4

        payload = {
            "id": str(uuid4()),
            "source_entity_id": source_entity_id,
            "source_entity_type": source_entity_type,
            "target_entity_id": target_entity_id,
            "target_entity_type": target_entity_type,
            "relationship_type": relationship_type,
            "snapshot_json": json.dumps(properties or {}),
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
        self._store.rows.append(payload)
        return payload


class _FakeAssets:
    def __init__(self, docs):
        self._docs = {str(d["id"]): d for d in docs}

    def get_asset(self, asset_id):
        return self._docs.get(str(asset_id))

    def list_assets(self, limit=500):
        return list(self._docs.values())[:limit]


def test_uc_native_entity_relationships_round_trip_and_asset_type_compat():
    store = _FakeStore()
    overlays = _FakeOverlays(store)
    catalog_id = str(uuid4())
    schema_id = str(uuid4())
    assets = _FakeAssets(
        [
            {"id": catalog_id, "name": "main", "asset_type_name": "Catalog"},
            {"id": schema_id, "name": "default", "asset_type_name": "Schema"},
        ]
    )
    mgr = UcNativeEntityRelationshipsManager(overlays, assets_manager=assets)

    created = mgr.create_relationship(
        rel_in=EntityRelationshipCreate(
            source_type="Catalog",
            source_id=catalog_id,
            target_type="Schema",
            target_id=schema_id,
            relationship_type="hasSchema",
        ),
        current_user_id="tester@example.com",
    )
    assert created.source_name == "main"
    assert created.target_name == "default"

    # Simulate legacy schema-import rows that stored type="asset"
    store.rows.append(
        {
            "id": str(uuid4()),
            "source_entity_id": catalog_id,
            "source_entity_type": "asset",
            "target_entity_id": schema_id,
            "target_entity_type": "asset",
            "relationship_type": "hasSchema",
            "snapshot_json": "{}",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
    )

    summary = mgr.get_all_for_entity(entity_type="Catalog", entity_id=catalog_id)
    assert summary.total >= 2
    assert all(rel.source_id == catalog_id for rel in summary.outgoing)


def test_uc_native_semantic_manager_loads_ontology_graph():
    from pathlib import Path
    from src.common.uc_native.semantic_manager import UcNativeSemanticModelsManager

    semantic = SimpleNamespace(
        search_triples=lambda *a, **k: [],
        save_ontology_file=lambda *a, **k: "",
        merge_triples=lambda *a, **k: 0,
        append_job_result=lambda *a, **k: "",
    )
    data_dir = Path(__file__).resolve().parents[2] / "data"
    mgr = UcNativeSemanticModelsManager(semantic, data_dir=data_dir)
    assert len(mgr._graph) > 0
    assert mgr.list() == []
    taxonomies = mgr.get_taxonomies()
    assert len(taxonomies) >= 1
    grouped = mgr.get_grouped_concepts()
    assert isinstance(grouped, dict)
    assert all(isinstance(v, list) for v in grouped.values())
    props = mgr.get_properties_grouped()
    assert isinstance(props, dict)
    assert all(isinstance(v, list) for v in props.values())
    assert sum(len(v) for v in props.values()) > 0
