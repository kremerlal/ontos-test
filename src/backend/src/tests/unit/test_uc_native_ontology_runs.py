"""Durability tests for ontology generation runs in UC-native mode."""

from src.common.config import Settings
from src.controller.ontology_generator_manager import OntologyGeneratorManager


class _NoOpDbSession:
    pass


class FakeEntities:
    def __init__(self):
        self.tables = {}

    def save_entity(self, table_name, payload, **_):
        self.tables.setdefault(table_name, {})[payload["id"]] = dict(payload)
        return dict(payload)

    def get_entity(self, table_name, entity_id):
        return self.tables.get(table_name, {}).get(str(entity_id))

    def list_entities(self, table_name, *, limit=500):
        return list(self.tables.get(table_name, {}).values())[:limit]

    def delete_entity(self, table_name, entity_id):
        self.tables.get(table_name, {}).pop(str(entity_id), None)


def test_uc_native_runs_persist_and_hydrate_without_postgres(monkeypatch):
    store = FakeEntities()
    manager = OntologyGeneratorManager(settings=Settings(), run_store=store)
    monkeypatch.setattr("src.controller.ontology_generator_manager.threading.Thread.start", lambda self: None)

    run_id = manager.start_run(
        db=_NoOpDbSession(),
        user_id="tester@example.com",
        metadata={"tables": []},
        guidelines="test",
    )

    assert store.get_entity("ontology_generation_runs", run_id)["user_id"] == "tester@example.com"
    manager._update_memory_run(run_id, status="running", progress_message="Generating")
    assert store.get_entity("ontology_generation_runs", run_id)["status"] == "running"

    rehydrated_manager = OntologyGeneratorManager(settings=Settings(), run_store=store)
    run = rehydrated_manager.get_run(_NoOpDbSession(), run_id)
    assert run is not None
    assert run.progress_message == "Generating"
    assert [item.id for item in rehydrated_manager.list_runs(_NoOpDbSession(), "tester@example.com")] == [run_id]
    assert rehydrated_manager.delete_run(_NoOpDbSession(), run_id)
    assert rehydrated_manager.get_run(_NoOpDbSession(), run_id) is None
