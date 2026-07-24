"""Focused UC-native manager tests without a warehouse."""
import json
from types import SimpleNamespace

from src.common.manager_dependencies import get_jobs_manager
from src.common.uc_native.feature_managers import (
    UcNativeSemanticLinksManager,
    UcNativeTeamsManager,
    UcNativeTermMappingManager,
)
from src.common.uc_native.overlay_managers import UcNativeJobsManager


class FakeEntities:
    def __init__(self):
        self.tables = {}

    def save_entity(self, table, payload, **_):
        self.tables.setdefault(table, {})[payload["id"]] = dict(payload)
        return dict(payload)

    def list_entities(self, table, limit=500):
        return list(self.tables.get(table, {}).values())[:limit]

    def get_entity(self, table, item_id):
        return self.tables.get(table, {}).get(str(item_id))

    def delete_entity(self, table, item_id):
        self.tables.get(table, {}).pop(str(item_id), None)


class FakeOverlays:
    def __init__(self):
        self.items = {}

    def add_semantic_link(self, payload):
        self.items[payload["id"]] = dict(payload)
        return dict(payload)

    def list_semantic_links(self, **filters):
        return [
            row for row in self.items.values()
            if all(value is None or row.get(key) == value for key, value in filters.items())
        ]

    def remove_semantic_link(self, item_id):
        return self.items.pop(str(item_id), None) is not None


def test_semantic_links_add_list_remove():
    manager = UcNativeSemanticLinksManager(FakeOverlays())
    link = manager.add({"entity_id": "a1", "entity_type": "asset", "iri": "urn:test"}, "me@example.com")
    assert manager.list_for_entity("a1", "asset") == [link]
    assert manager.remove(link["id"], "me@example.com")
    assert manager.list_for_iri("urn:test") == []


def test_teams_create_and_list():
    manager = UcNativeTeamsManager(FakeEntities())
    team = manager.create_team(team_in={"name": "Platform"})
    assert manager.get_all_teams()[0]["id"] == team["id"]


def test_term_mapping_run_persists_in_delta_entities():
    manager = UcNativeTermMappingManager(FakeEntities())
    run = manager.create_run(payload={"comment": "test"}, created_by="me@example.com")
    assert manager.list_runs()[0]["id"] == run["id"]


def test_jobs_list_installations_does_not_crash():
    workflows = SimpleNamespace(list_workflow_definitions=lambda: [])
    assert UcNativeJobsManager(workflows, None, None).list_installations() == []


def test_jobs_manager_runs_and_cancels_with_workspace_client():
    calls = []

    class Jobs:
        def run_now(self, **kwargs):
            calls.append(("run_now", kwargs))
            return SimpleNamespace(run_id=99)

        def cancel_run(self, **kwargs):
            calls.append(("cancel_run", kwargs))

        def get_run(self, **kwargs):
            return SimpleNamespace(
                job_id=12,
                state=SimpleNamespace(life_cycle_state="RUNNING", result_state=None),
                start_time=1,
                end_time=None,
            )

        def list_runs(self, **_):
            return [SimpleNamespace(run_id=99)]

    workflows = SimpleNamespace(list_workflow_definitions=lambda: [])
    manager = UcNativeJobsManager(workflows, SimpleNamespace(jobs=Jobs()), None)

    assert manager.run_job(12) == 99
    assert manager.get_active_run_id(12) == 99
    assert manager.get_job_status(99)["life_cycle_state"] == "RUNNING"
    assert manager.cancel_run(99)
    assert calls == [
        ("run_now", {"job_id": 12}),
        ("cancel_run", {"run_id": 99}),
    ]


def test_jobs_manager_uses_delta_workflow_installations_for_configuration():
    class Store:
        def __init__(self):
            self.row = {
                "id": "installation-1",
                "workflow_key": "bulk_import",
                "job_id": 12,
                "snapshot_json": json.dumps(
                    {
                        "configuration": {"catalog": "main"},
                        "parameter_definitions": [{"name": "catalog", "type": "string"}],
                    }
                ),
            }

        def list_rows(self, table, **_):
            assert table == "workflow_installations"
            return [self.row]

        def parse_snapshot(self, row):
            return json.loads(row["snapshot_json"])

        def merge_row(self, table, row):
            assert table == "workflow_installations"
            self.row = row

    store = Store()
    workflows = SimpleNamespace(_store=store, list_workflow_definitions=lambda: [])
    manager = UcNativeJobsManager(workflows, None, None)

    assert manager.list_installations()[0]["workflow_id"] == "bulk_import"
    assert manager.get_workflow_parameter_definitions("bulk_import") == [
        {"name": "catalog", "type": "string"}
    ]
    assert manager.get_workflow_configuration("bulk_import") == {"catalog": "main"}
    assert manager.update_workflow_configuration("bulk_import", {"catalog": "prod"}).configuration == {
        "catalog": "prod"
    }
    assert manager.get_workflow_configuration("bulk_import") == {"catalog": "prod"}


def test_get_jobs_manager_prefers_app_state():
    jobs = object()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(jobs_manager=jobs)))
    assert get_jobs_manager(request) is jobs


def test_business_roles_route_shaped_crud():
    from src.common.uc_native.feature_managers import UcNativeBusinessRolesManager

    manager = UcNativeBusinessRolesManager(FakeEntities())
    role = manager.create_role(role_in={"name": "Steward", "category": "data"})
    assert manager.get_role(role_id=role["id"])["name"] == "Steward"
    assert manager.get_all_roles()[0]["id"] == role["id"]
    updated = manager.update_role(role_id=role["id"], role_in={"name": "Owner"})
    assert updated["name"] == "Owner"
    assert manager.delete_role(role_id=role["id"])["id"] == role["id"]


def test_term_mapping_decide_and_apply():
    from src.common.uc_native.feature_managers import UcNativeTermMappingManager

    entities = FakeEntities()
    links = FakeOverlays()
    manager = UcNativeTermMappingManager(entities, semantic_links=UcNativeSemanticLinksManager(links))
    run = manager.create_run(payload={"comment": "map"}, created_by="me@example.com")
    sug = entities.save_entity(
        "term_mapping_suggestions",
        {
            "id": "s1",
            "run_id": run["id"],
            "status": "pending",
            "source_entity_type": "asset",
            "source_entity_id": "a1",
            "concept_iri": "urn:concept:1",
        },
    )
    assert manager.decide(batch={"decisions": [{"suggestion_id": sug["id"], "status": "accepted"}]})["accepted"] == 1
    result = manager.apply_run(run_id=run["id"], applied_by="me@example.com")
    assert result["links_created"] == 1
    assert links.items


def test_costs_create_update_delete():
    from src.common.uc_native.feature_managers import UcNativeCostsManager

    class OverlayStore:
        def __init__(self):
            self.rows = {}

        def add(self, table, payload):
            payload = dict(payload)
            payload.setdefault("id", "c1")
            self.rows[payload["id"]] = payload
            return payload

        def get(self, table, item_id):
            return self.rows.get(str(item_id))

        def remove(self, table, item_id):
            return self.rows.pop(str(item_id), None) is not None

        def list_for_entity(self, table, entity_type, entity_id):
            return [r for r in self.rows.values() if r.get("entity_type") == entity_type and r.get("entity_id") == entity_id]

    overlays = OverlayStore()
    manager = UcNativeCostsManager(overlays)
    item = manager.create(data={"entity_type": "asset", "entity_id": "a1", "amount": 10}, user_email="me@example.com")
    assert manager.list(entity_type="asset", entity_id="a1")
    assert manager.update(id=item["id"], data={"amount": 20})["amount"] == 20
    assert manager.delete(id=item["id"]) is True
