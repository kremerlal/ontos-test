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

    def get_asset_doc(self, asset_id):
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
        load_all_triples=lambda *a, **k: [],
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

    # Any concept from the grouped list should resolve via get_concept_details.
    sample_iri = next(iter(next(iter(grouped.values())))).iri
    details = mgr.get_concept_details(sample_iri)
    assert details is not None
    assert details.iri == sample_iri
    assert details.label


class _FakeSemanticStore:
    """Stands in for the Delta-backed store, keeping rows in memory."""

    def __init__(self):
        self.saved = {}
        self.rows = []

    def search_triples(self, *a, **k):
        return []

    def save_ontology_file(self, filename, content):
        self.saved["file"] = (filename, content)
        return f"/vol/{filename}"

    def merge_triples(self, triples):
        self.saved.setdefault("triples", []).extend(triples)
        self.rows.extend(dict(t) for t in triples)
        return len(triples)

    def load_all_triples(self, **_):
        return [dict(row) for row in self.rows]

    def append_job_result(self, *a, **k):
        return ""


def test_uc_native_semantic_manager_create_and_import_collection(tmp_path):
    from pathlib import Path
    from src.common.uc_native.semantic_manager import UcNativeSemanticModelsManager

    store = _FakeSemanticStore()
    saved = store.saved

    data_dir = Path(__file__).resolve().parents[2] / "data"
    mgr = UcNativeSemanticModelsManager(store, data_dir=data_dir)
    coll = mgr.create_collection(
        label="Generated Demo Ontology",
        collection_type="ontology",
        description="from generator",
        created_by="tester@example.com",
    )
    assert coll["iri"].startswith("urn:ontology:")
    assert coll["is_editable"] is True
    assert mgr.get_collection(coll["iri"]) is not None

    turtle = """
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix : <http://example.org/onto#> .
:Customer a owl:Class ;
    rdfs:label "Customer" .
"""
    count = mgr.import_rdf_to_collection(coll["iri"], turtle, format="turtle", imported_by="tester")
    assert count >= 1
    assert saved.get("triples")
    assert saved.get("file")
    refreshed = mgr.get_collection(coll["iri"])
    assert refreshed["concept_count"] >= 1


def test_uc_native_collection_survives_restart():
    """A fresh manager (new container after redeploy) must see persisted collections."""
    from pathlib import Path
    from src.common.uc_native.semantic_manager import UcNativeSemanticModelsManager

    store = _FakeSemanticStore()
    data_dir = Path(__file__).resolve().parents[2] / "data"

    first = UcNativeSemanticModelsManager(store, data_dir=data_dir)
    coll = first.create_collection(
        label="Weather Domain Ontology",
        collection_type="ontology",
        description="imported by the generator",
        created_by="tester@example.com",
    )
    turtle = """
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix : <http://example.org/weather#> .
:Observation a owl:Class ;
    rdfs:label "Observation"@en ;
    rdfs:comment "A weather reading." .
"""
    first.import_rdf_to_collection(coll["iri"], turtle, format="turtle")

    # New process: same Delta rows, brand-new in-memory graph.
    second = UcNativeSemanticModelsManager(store, data_dir=data_dir)
    reloaded = second.get_collection(coll["iri"])
    assert reloaded is not None
    assert reloaded["label"] == "Weather Domain Ontology"
    assert reloaded["collection_type"] == "ontology"
    assert reloaded["description"] == "imported by the generator"
    assert reloaded["created_by"] == "urn:user:tester@example.com"
    assert reloaded["is_editable"] is True
    assert reloaded["concept_count"] >= 3

    concept = second.get_concept_details("http://example.org/weather#Observation")
    assert concept is not None
    # Language-tagged and plain literals must come back as literals, not IRIs.
    assert concept.label == "Observation"
    assert concept.comment == "A weather reading."


def test_ontology_generator_memory_runs_without_postgres():
    from src.common.config import Settings
    from src.controller.ontology_generator_manager import OntologyGeneratorManager

    class _NoOpDbSession:
        def commit(self):
            return None

        def refresh(self, *_a, **_k):
            return None

    mgr = OntologyGeneratorManager(settings=Settings())
    # Avoid real LLM: stub generate_ontology.
    mgr.generate_ontology = lambda **kwargs: __import__(
        "src.controller.ontology_generator_manager", fromlist=["AgentResult"]
    ).AgentResult(success=True, owl_content="@prefix : <http://ex#> .\n:Foo a owl:Class .", steps=[])

    run_id = mgr.start_run(
        db=_NoOpDbSession(),
        user_id="tester@example.com",
        metadata={"tables": [{"name": "t", "full_name": "c.s.t", "columns": []}]},
        guidelines="test",
    )
    assert run_id
    # Allow background thread to finish quickly.
    import time

    for _ in range(50):
        run = mgr.get_run(_NoOpDbSession(), run_id)
        if run and run.status in ("completed", "failed", "cancelled"):
            break
        time.sleep(0.05)
    run = mgr.get_run(_NoOpDbSession(), run_id)
    assert run is not None
    assert run.user_id == "tester@example.com"
    assert run.status in ("completed", "failed", "running", "pending")
    listed = mgr.list_runs(_NoOpDbSession(), "tester@example.com")
    assert any(r.id == run_id for r in listed)
