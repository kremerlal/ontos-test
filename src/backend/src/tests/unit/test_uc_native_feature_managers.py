"""Focused UC-native manager tests without a warehouse."""
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

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
    listed = manager.get_all_teams()
    assert listed[0].id == team.id
    assert listed[0].name == "Platform"


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
    role = manager.create_role(role_in={"name": "Steward", "category": "governance"})
    assert manager.get_role(role_id=role.id).name == "Steward"
    assert manager.get_all_roles()[0].id == role.id
    updated = manager.update_role(role_id=role.id, role_in={"name": "Owner"})
    assert updated.name == "Owner"
    assert manager.delete_role(role_id=role.id).id == role.id


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


class FakeOverlayStore:
    """In-memory stand-in for UcNativeOverlayStore's generic document API."""

    def __init__(self):
        self.rows = {}

    def add(self, table, payload):
        row = dict(payload)
        row.setdefault("id", str(uuid4()))
        self.rows.setdefault(table, {})[str(row["id"])] = row
        return dict(row)

    def get(self, table, item_id):
        row = self.rows.get(table, {}).get(str(item_id))
        return dict(row) if row else None

    def remove(self, table, item_id):
        return self.rows.get(table, {}).pop(str(item_id), None) is not None

    def list_all(self, table, limit=1000):
        return [dict(row) for row in list(self.rows.get(table, {}).values())[:limit]]

    def list_for_entity(self, table, entity_type, entity_id):
        return [
            dict(row)
            for row in self.rows.get(table, {}).values()
            if row.get("entity_type") == entity_type and str(row.get("entity_id")) == str(entity_id)
        ]


def _metadata_manager():
    from src.common.uc_native.feature_managers import UcNativeMetadataManager

    return UcNativeMetadataManager(FakeOverlayStore())


def test_metadata_rich_text_crud():
    from src.models.metadata import RichTextCreate, RichTextUpdate

    manager = _metadata_manager()
    domain_id = str(uuid4())

    # The regression this covers: list_rich_texts did not exist at all.
    assert manager.list_rich_texts(None, entity_type="data_domain", entity_id=domain_id) == []

    created = manager.create_rich_text(
        None,
        data=RichTextCreate(
            entity_id=domain_id,
            entity_type="data_domain",
            title="Overview",
            content_markdown="# Weather",
        ),
        user_email="me@example.com",
    )
    assert created.title == "Overview"
    assert created.created_by == "me@example.com"

    listed = manager.list_rich_texts(None, entity_type="data_domain", entity_id=domain_id)
    assert [r.id for r in listed] == [created.id]

    updated = manager.update_rich_text(
        None, id=str(created.id), data=RichTextUpdate(title="Updated"), user_email="me@example.com"
    )
    assert updated.title == "Updated"
    assert updated.content_markdown == "# Weather"

    assert manager.update_rich_text(None, id=str(uuid4()), data=RichTextUpdate(title="x"), user_email=None) is None
    assert manager.delete_rich_text(None, id=str(created.id), user_email=None) is True
    assert manager.delete_rich_text(None, id=str(created.id), user_email=None) is False


def test_metadata_links_and_documents_are_kept_separate():
    from src.models.metadata import DocumentCreate, LinkCreate

    manager = _metadata_manager()
    domain_id = str(uuid4())

    link = manager.create_link(
        None,
        data=LinkCreate(
            entity_id=domain_id,
            entity_type="data_domain",
            title="Runbook",
            url="https://example.com/runbook",
        ),
        user_email="me@example.com",
    )
    doc = manager.create_document_record(
        None,
        data=DocumentCreate(entity_id=domain_id, entity_type="data_domain", title="Spec"),
        filename="spec.pdf",
        content_type="application/pdf",
        size_bytes=2048,
        storage_path="/Volumes/c/s/v/uploads/spec.pdf",
        user_email="me@example.com",
    )

    # Each kind must only see its own rows despite sharing one Delta table.
    assert [l.id for l in manager.list_links(None, entity_type="data_domain", entity_id=domain_id)] == [link.id]
    assert [d.id for d in manager.list_documents(None, entity_type="data_domain", entity_id=domain_id)] == [doc.id]
    assert manager.list_rich_texts(None, entity_type="data_domain", entity_id=domain_id) == []

    fetched = manager.get_document(None, id=str(doc.id))
    assert fetched.original_filename == "spec.pdf"
    assert fetched.size_bytes == 2048
    assert manager.get_document(None, id=str(link.id)) is None


def test_metadata_shared_entity_id_matches_postgres_manager():
    """Both managers must agree on the sentinel, or shared assets diverge by mode."""
    from src.common.uc_native import feature_managers
    from src.controller.metadata_manager import SHARED_ENTITY_ID

    assert feature_managers.SHARED_ENTITY_ID == SHARED_ENTITY_ID


def test_metadata_shared_asset_attach_and_merge():
    from src.models.metadata import MetadataAttachmentCreate, RichTextCreate

    manager = _metadata_manager()
    domain_id = str(uuid4())

    shared = manager.create_shared_rich_text(
        None,
        data=RichTextCreate(
            entity_id="placeholder",
            entity_type="data_domain",
            title="Shared policy",
            content_markdown="policy",
            level=10,
        ),
        user_email="me@example.com",
    )
    assert shared.is_shared is True
    assert shared.entity_id == "__shared__"

    catalog = manager.list_shared_assets(None, entity_type="data_domain")
    assert [r.id for r in catalog.rich_texts] == [shared.id]
    assert manager.list_shared_assets(None, entity_type="data_product").rich_texts == []

    direct = manager.create_rich_text(
        None,
        data=RichTextCreate(
            entity_id=domain_id,
            entity_type="data_domain",
            title="Domain notes",
            content_markdown="notes",
            level=60,
        ),
        user_email="me@example.com",
    )

    attachment = manager.attach_shared_asset(
        None,
        entity_type="data_domain",
        entity_id=domain_id,
        data=MetadataAttachmentCreate(asset_type="rich_text", asset_id=str(shared.id)),
        user_email="me@example.com",
    )
    assert attachment.asset_id == str(shared.id)
    # Re-attaching the same asset must not create a duplicate.
    again = manager.attach_shared_asset(
        None,
        entity_type="data_domain",
        entity_id=domain_id,
        data=MetadataAttachmentCreate(asset_type="rich_text", asset_id=str(shared.id)),
        user_email="me@example.com",
    )
    assert again.id == attachment.id
    assert len(manager.list_attachments(None, entity_type="data_domain", entity_id=domain_id)) == 1

    merged = manager.get_merged_metadata(None, entity_type="data_domain", entity_id=domain_id)
    # Lower level sorts first, and the attached shared asset is tracked as shared.
    assert [r.id for r in merged.rich_texts] == [shared.id, direct.id]
    assert merged.sources[str(shared.id)] == f"shared:{domain_id}"
    assert merged.sources[str(direct.id)] == domain_id

    assert manager.detach_shared_asset(
        None,
        entity_type="data_domain",
        entity_id=domain_id,
        asset_type="rich_text",
        asset_id=str(shared.id),
    ) is True
    assert manager.list_attachments(None, entity_type="data_domain", entity_id=domain_id) == []


def test_metadata_merged_inherits_from_contracts_by_level():
    from src.models.metadata import RichTextCreate

    manager = _metadata_manager()
    domain_id = str(uuid4())
    contract_id = str(uuid4())

    inheritable = manager.create_rich_text(
        None,
        data=RichTextCreate(
            entity_id=contract_id,
            entity_type="data_contract",
            title="Contract terms",
            content_markdown="terms",
            level=20,
            inheritable=True,
        ),
        user_email="me@example.com",
    )
    manager.create_rich_text(
        None,
        data=RichTextCreate(
            entity_id=contract_id,
            entity_type="data_contract",
            title="Private note",
            content_markdown="private",
            level=20,
            inheritable=False,
        ),
        user_email="me@example.com",
    )
    above_cap = manager.create_rich_text(
        None,
        data=RichTextCreate(
            entity_id=contract_id,
            entity_type="data_contract",
            title="Deep detail",
            content_markdown="detail",
            level=90,
            inheritable=True,
        ),
        user_email="me@example.com",
    )

    merged = manager.get_merged_metadata(
        None,
        entity_type="data_product",
        entity_id=domain_id,
        contract_ids=[contract_id],
        max_level_inheritance=50,
    )
    ids = [r.id for r in merged.rich_texts]
    assert ids == [inheritable.id]
    assert above_cap.id not in ids
    assert merged.sources[str(inheritable.id)] == f"contract:{contract_id}"


class FakeRelationshipOverlays:
    """Minimal overlay store exposing the relationship surface assets rely on."""

    def __init__(self):
        self.rows = {}

    def add_relationship(self, **kwargs):
        row = {"id": str(uuid4()), **kwargs}
        row["properties"] = row.pop("properties", None) or {}
        self.rows[row["id"]] = row
        return row

    def list_relationships(self, *, entity_id=None, entity_type=None, relationship_type=None, limit=5000):
        types = None
        if relationship_type:
            types = {relationship_type} if isinstance(relationship_type, str) else set(relationship_type)
        out = []
        for row in self.rows.values():
            if entity_id and entity_id not in (row["source_entity_id"], row["target_entity_id"]):
                continue
            if types and row["relationship_type"] not in types:
                continue
            out.append(dict(row))
        return out[:limit]

    def delete_relationship(self, relationship_id):
        self.rows.pop(str(relationship_id), None)
        return True


def _assets_manager():
    from src.common.uc_native.managers import UcNativeAssetsManager

    manager = UcNativeAssetsManager(FakeEntities(), overlays=FakeRelationshipOverlays())
    manager.ensure_default_asset_types()
    return manager


def _make_asset(manager, name, type_name, **extra):
    from src.models.assets import AssetCreate

    return manager.create_asset(
        None,
        asset_in=AssetCreate(
            name=name,
            asset_type_id=manager.get_asset_type_by_name(type_name).id,
            **extra,
        ),
        current_user_id="me@example.com",
    )


def test_assets_manager_exposes_the_route_surface():
    """Regression: /api/assets served empty results because routes bypassed this manager."""
    from src.controller.assets_manager import AssetsManager

    manager = _assets_manager()
    for name in dir(AssetsManager):
        if name.startswith("_") or name == "set_search_manager":
            continue
        assert hasattr(manager, name), f"UcNativeAssetsManager is missing {name}"


def test_asset_types_list_and_summary_include_asset_counts():
    manager = _assets_manager()
    _make_asset(manager, "orders", "Table")

    types = manager.get_all_asset_types(db=None)
    by_name = {t.name: t for t in types}
    assert by_name["Table"].asset_count == 1
    assert by_name["View"].asset_count == 0
    assert [t.name for t in types] == sorted(by_name, key=str.lower)

    summaries = manager.get_asset_types_summary(db=None)
    assert {s.name for s in summaries} == set(by_name)


def test_asset_type_crud_round_trip_and_guards():
    from src.common.errors import ConflictError, NotFoundError
    from src.models.assets import AssetTypeCreate, AssetTypeUpdate

    manager = _assets_manager()
    created = manager.create_asset_type(
        None, type_in=AssetTypeCreate(name="Notebook", category="data"), current_user_id="me@example.com"
    )
    assert manager.get_asset_type(created.id).name == "Notebook"

    updated = manager.update_asset_type(
        None, type_id=created.id, type_in=AssetTypeUpdate(description="A notebook")
    )
    assert updated.description == "A notebook"

    with pytest.raises(ConflictError):
        manager.create_asset_type(None, type_in=AssetTypeCreate(name="Notebook"))

    deleted = manager.delete_asset_type(None, type_id=created.id)
    assert deleted.name == "Notebook"
    with pytest.raises(NotFoundError):
        manager.delete_asset_type(None, type_id=created.id)


def test_delete_asset_type_rejects_types_still_in_use():
    from src.common.errors import ConflictError

    manager = _assets_manager()
    _make_asset(manager, "orders", "Table")
    table_type_id = manager.get_asset_type_by_name("Table").id
    with pytest.raises(ConflictError):
        manager.delete_asset_type(None, type_id=table_type_id)


def test_get_all_assets_filters_and_resolves_parent_from_hierarchy():
    from src.models.assets import AssetRelationshipCreate

    manager = _assets_manager()
    table = _make_asset(manager, "orders", "Table")
    column = _make_asset(manager, "order_id", "Column")
    _make_asset(manager, "sales", "Schema")
    manager.add_relationship(
        rel_in=AssetRelationshipCreate(
            source_asset_id=table.id, target_asset_id=column.id, relationship_type="hasColumn"
        )
    )

    page = manager.get_all_assets(db=None)
    assert page.total == 3
    by_name = {i.name: i for i in page.items}
    assert by_name["order_id"].parent_id == table.id
    assert by_name["order_id"].parent_name == "orders"
    assert by_name["orders"].parent_id is None

    assert [i.name for i in manager.get_all_assets(asset_type_names=["Column"]).items] == ["order_id"]
    assert [i.name for i in manager.get_all_assets(name="ord").items] == ["order_id", "orders"]
    assert manager.get_all_assets(status="deprecated").total == 0


def test_get_asset_returns_read_model_with_relationships():
    from src.models.assets import AssetRelationshipCreate, AssetRead

    manager = _assets_manager()
    table = _make_asset(manager, "orders", "Table")
    column = _make_asset(manager, "order_id", "Column")
    manager.add_relationship(
        rel_in=AssetRelationshipCreate(
            source_asset_id=table.id, target_asset_id=column.id, relationship_type="hasColumn"
        )
    )

    read = manager.get_asset(db=None, asset_id=table.id)
    assert isinstance(read, AssetRead)
    assert [r.relationship_type for r in read.relationships] == ["hasColumn"]
    # Raw docs stay available for internal callers that need the denormalized dict.
    assert manager.get_asset_doc(str(table.id))["name"] == "orders"
    assert manager.get_asset(db=None, asset_id=uuid4()) is None


def test_update_and_delete_asset_round_trip():
    from src.common.errors import NotFoundError
    from src.models.assets import AssetUpdate

    manager = _assets_manager()
    asset = _make_asset(manager, "orders", "Table", description="before")

    updated = manager.update_asset(
        None,
        asset_id=asset.id,
        asset_in=AssetUpdate(
            description="after", asset_type_id=manager.get_asset_type_by_name("View").id
        ),
        current_user_id="me@example.com",
    )
    assert (updated.description, updated.asset_type_name) == ("after", "View")

    assert manager.delete_asset(None, asset_id=asset.id).name == "orders"
    assert manager.get_asset(asset_id=asset.id) is None
    with pytest.raises(NotFoundError):
        manager.delete_asset(None, asset_id=asset.id)


def test_delete_preview_and_cascade_delete_walk_the_hierarchy():
    from src.models.assets import AssetRelationshipCreate

    manager = _assets_manager()
    schema = _make_asset(manager, "sales", "Schema")
    table = _make_asset(manager, "orders", "Table")
    column = _make_asset(manager, "order_id", "Column")
    for source, target, rel in (
        (schema, table, "hasTable"),
        (table, column, "hasColumn"),
    ):
        manager.add_relationship(
            rel_in=AssetRelationshipCreate(
                source_asset_id=source.id, target_asset_id=target.id, relationship_type=rel
            )
        )

    preview = manager.get_delete_preview(asset_id=schema.id)
    assert (preview.name, preview.level) == ("sales", 0)
    assert [c.name for c in preview.children] == ["orders"]
    assert [g.name for g in preview.children[0].children] == ["order_id"]

    result = manager.cascade_delete_assets(None, asset_ids=[schema.id])
    # Children are deleted before their parents.
    assert [d["name"] for d in result.deleted] == ["order_id", "orders", "sales"]
    assert result.failed == []
    assert manager.get_all_assets(db=None).total == 0


def test_infer_schema_reads_column_children():
    from src.models.assets import AssetRelationshipCreate

    manager = _assets_manager()
    table = _make_asset(manager, "orders", "Table")
    column = _make_asset(
        manager, "order_id", "Column", description="PK", properties={"data_type": "bigint"}
    )
    manager.add_relationship(
        rel_in=AssetRelationshipCreate(
            source_asset_id=table.id, target_asset_id=column.id, relationship_type="hasColumn"
        )
    )

    schema = manager.infer_schema_from_asset(asset_id=table.id)
    assert schema["asset_name"] == "orders"
    assert schema["columns"] == [
        {"name": "order_id", "type": "bigint", "nullable": True, "description": "PK"}
    ]


def test_remove_relationship_deletes_the_overlay_row():
    from src.models.assets import AssetRelationshipCreate

    manager = _assets_manager()
    table = _make_asset(manager, "orders", "Table")
    column = _make_asset(manager, "order_id", "Column")
    rel = manager.add_relationship(
        rel_in=AssetRelationshipCreate(
            source_asset_id=table.id, target_asset_id=column.id, relationship_type="hasColumn"
        )
    )

    assert manager.remove_relationship(None, relationship_id=rel.id)
    assert manager.get_asset(asset_id=table.id).relationships == []


def test_delivery_method_create_returns_read_model():
    """The create route reads ``.id`` off the result, so a raw dict would 500."""
    from src.common.uc_native.feature_managers import UcNativeDeliveryMethodsManager
    from src.models.delivery_methods import DeliveryMethodCreate, DeliveryMethodRead

    manager = UcNativeDeliveryMethodsManager(FakeEntities())
    created = manager.create(
        db=None,
        obj_in=DeliveryMethodCreate(name="Serving Endpoint"),
        current_user_id="me@example.com",
    )
    assert isinstance(created, DeliveryMethodRead)
    assert (created.name, created.created_by) == ("Serving Endpoint", "me@example.com")
    assert [m["id"] for m in manager.get_all(db=None)] == [str(created.id)]


def _bulk_manager():
    from src.common.uc_native.asset_bulk_manager import UcNativeAssetBulkManager

    return UcNativeAssetBulkManager(_assets_manager())


def test_uc_native_bulk_export_includes_parent_and_filters():
    from src.models.assets import AssetRelationshipCreate

    assets = _assets_manager()
    from src.common.uc_native.asset_bulk_manager import UcNativeAssetBulkManager

    bulk = UcNativeAssetBulkManager(assets)
    table = _make_asset(assets, "orders", "Table", platform="databricks", location="cat.sch.orders")
    column = _make_asset(assets, "order_id", "Column", platform="databricks")
    assets.add_relationship(
        rel_in=AssetRelationshipCreate(
            source_asset_id=table.id, target_asset_id=column.id, relationship_type="hasColumn"
        )
    )

    raw, filename, ctype = bulk.export_assets(fmt="csv")
    assert filename == "assets-export.csv"
    assert ctype == "text/csv"
    text = raw.decode("utf-8")
    assert "order_id" in text
    assert "orders" in text
    assert "hasColumn" in text

    filtered, _, _ = bulk.export_assets(fmt="csv", asset_ids=[column.id])
    filtered_text = filtered.decode("utf-8")
    assert "order_id" in filtered_text
    assert "orders\n" not in filtered_text and filtered_text.count("orders") == 1  # parent label only


def test_uc_native_bulk_template_and_preview_create_update():
    bulk = _bulk_manager()
    raw, filename, _ = bulk.export_template(asset_type_name="Table", fmt="csv")
    assert filename == "assets-template.csv"
    assert b"Example Asset" in raw
    assert b"Table" in raw

    # Seed one asset so the second row updates by identity.
    assets = bulk._assets
    existing = _make_asset(assets, "orders", "Table", platform="dbx", location="c.s.orders")

    csv_body = (
        "name,asset_type,description,platform,location,status\n"
        "orders,Table,updated,dbx,c.s.orders,active\n"
        "customers,Table,new table,dbx,c.s.customers,draft\n"
        ",Table,missing name,dbx,c.s.x,active\n"
        "bad,UnknownType,nope,dbx,c.s.y,active\n"
    ).encode("utf-8")

    preview = bulk.preview_import(file_bytes=csv_body, filename="assets.csv")
    assert preview.total_rows == 4
    assert preview.will_update == 1
    assert preview.will_create == 1
    assert preview.errors == 2
    update_item = next(i for i in preview.items if i.action.value == "update")
    assert update_item.existing_asset_id == str(existing.id)


def test_uc_native_bulk_execute_import_wires_parent_relationships():
    bulk = _bulk_manager()
    csv_body = (
        "name,asset_type,description,platform,location,status,parent_asset,parent_relationship_type\n"
        "sales,Schema,schema,dbx,c.sales,active,,\n"
        "orders,Table,table,dbx,c.sales.orders,active,sales,hasTable\n"
        "order_id,Column,pk,dbx,c.sales.orders.order_id,active,orders,hasColumn\n"
    ).encode("utf-8")

    result = bulk.execute_import(
        file_bytes=csv_body, filename="assets.csv", current_user_id="me@example.com"
    )
    assert (result.created, result.updated, result.errors) == (3, 0, 0)

    page = bulk._assets.get_all_assets(db=None)
    by_name = {i.name: i for i in page.items}
    assert by_name["orders"].parent_name == "sales"
    assert by_name["order_id"].parent_name == "orders"

    # Re-import same identities updates rather than duplicating.
    again = bulk.execute_import(
        file_bytes=csv_body, filename="assets.csv", current_user_id="me@example.com"
    )
    assert again.updated == 3
    assert again.created == 0
    assert bulk._assets.get_all_assets(db=None).total == 3


def test_uc_native_bulk_rejects_cycles_and_skips_parent_wiring():
    bulk = _bulk_manager()
    csv_body = (
        "name,asset_type,status,parent_asset,parent_relationship_type\n"
        "a,Table,active,b,contains\n"
        "b,Table,active,a,contains\n"
    ).encode("utf-8")
    preview = bulk.preview_import(file_bytes=csv_body, filename="cycle.csv")
    assert any("Circular" in m for m in preview.error_messages)

    result = bulk.execute_import(
        file_bytes=csv_body, filename="cycle.csv", current_user_id="me@example.com"
    )
    # Assets still create, but parent relationships are skipped when cycles are present.
    assert result.created == 2
    assert all(i.parent_id is None for i in bulk._assets.get_all_assets(db=None).items)


def _products_manager(overlays=None):
    from src.common.uc_native.managers import UcNativeDataProductsManager

    return UcNativeDataProductsManager(FakeEntities(), overlays=overlays)


def _make_product(manager, **overrides):
    payload = {
        "apiVersion": "v1.0.0",
        "kind": "DataProduct",
        "status": "active",
        "name": "Orders",
        **overrides,
    }
    return manager.create_product(payload, user="me@example.com")


def test_data_products_manager_exposes_list_page_facets():
    """Regression: the Data Products page 500'd on get_distinct_statuses."""
    manager = _products_manager()
    for name in (
        "get_distinct_statuses",
        "get_distinct_domains",
        "get_distinct_tenants",
        "get_distinct_product_types",
        "get_distinct_owners",
    ):
        assert hasattr(manager, name), f"UcNativeDataProductsManager is missing {name}"
        assert manager.__getattribute__(name)() == []


def test_distinct_statuses_domains_and_tenants_are_sorted_and_deduped():
    manager = _products_manager()
    _make_product(manager, status="active", domain="Sales", tenant="acme")
    _make_product(manager, status="draft", domain="Sales", tenant="acme")
    _make_product(manager, status="active", domain="Finance", tenant=None)

    assert manager.get_distinct_statuses() == ["active", "draft"]
    assert manager.get_distinct_domains() == ["Finance", "Sales"]
    assert manager.get_distinct_tenants() == ["acme"]


def test_distinct_product_types_reads_output_ports_in_both_key_styles():
    manager = _products_manager()
    _make_product(
        manager,
        outputPorts=[
            {"name": "p1", "version": "1.0", "type": "table"},
            {"name": "p2", "version": "1.0", "type": "dashboard"},
        ],
    )
    # Snapshots written via the snake_case alias must be read too.
    _make_product(
        manager,
        output_ports=[{"name": "p3", "version": "1.0", "port_type": "table"}],
    )

    assert manager.get_distinct_product_types() == ["dashboard", "table"]


def test_distinct_owners_only_includes_owner_role_members():
    manager = _products_manager()
    _make_product(
        manager,
        team={
            "name": "Sales Eng",
            "members": [
                {"username": "a@x.com", "name": "Ada", "role": "owner"},
                {"username": "b@x.com", "name": "Ben", "role": "data steward"},
                {"username": "c@x.com", "role": "Owner"},
            ],
        },
    )

    # Falls back to username when the member has no display name.
    assert manager.get_distinct_owners() == ["Ada", "c@x.com"]


def test_delete_product_accepts_the_user_kwarg_the_route_passes():
    """The DELETE route calls delete_product(product_id, user=...)."""
    manager = _products_manager()
    product = _make_product(manager, name="Doomed")

    assert manager.delete_product(product.id, user="me@example.com") is True
    assert manager.delete_product(product.id, user="me@example.com") is False


# --- update_product_with_auth -------------------------------------------------


def test_update_product_with_auth_returns_none_for_missing_product():
    manager = _products_manager()
    assert (
        manager.update_product_with_auth(
            product_id="nope",
            product_data_dict={"name": "x"},
            user_email="me@example.com",
            user_groups=[],
        )
        is None
    )


def test_update_product_with_auth_lets_a_feature_admin_edit_any_product():
    manager = _products_manager()
    product = _make_product(manager, name="Owned by someone else")
    manager.update_product(product.id, {"draft_owner_id": "someone@else.com"})

    updated = manager.update_product_with_auth(
        product_id=product.id,
        product_data_dict={"name": "Renamed by admin"},
        user_email="admin@example.com",
        user_groups=[],
        is_feature_admin=True,
    )

    assert updated.name == "Renamed by admin"


def test_update_product_with_auth_allows_the_draft_owner():
    manager = _products_manager()
    product = _make_product(manager, name="Mine")

    updated = manager.update_product_with_auth(
        product_id=product.id,
        product_data_dict={"name": "Still mine"},
        # Email match is case-insensitive.
        user_email="ME@example.com",
        user_groups=[],
    )

    assert updated.name == "Still mine"


def test_update_product_with_auth_allows_the_owning_team():
    manager = _products_manager()
    product = _make_product(manager, owner_team_id="team-1", draft_owner_id=None)

    updated = manager.update_product_with_auth(
        product_id=product.id,
        product_data_dict={"name": "Team edit"},
        user_email="teammate@example.com",
        user_groups=[],
        caller_team_ids=["team-9", "team-1"],
    )

    assert updated.name == "Team edit"


def test_update_product_with_auth_allows_a_project_member():
    entities = FakeEntities()
    from src.common.uc_native.managers import UcNativeDataProductsManager

    manager = UcNativeDataProductsManager(entities)
    entities.save_entity(
        "projects",
        {"id": "proj-1", "name": "Growth", "member_ids": ["member@example.com"]},
    )
    product = _make_product(manager, project_id="proj-1", draft_owner_id=None)

    updated = manager.update_product_with_auth(
        product_id=product.id,
        product_data_dict={"name": "Project edit"},
        user_email="member@example.com",
        user_groups=[],
    )

    assert updated.name == "Project edit"


def test_update_product_with_auth_denies_a_caller_with_no_ownership_claim():
    manager = _products_manager()
    product = _make_product(manager, owner_team_id="team-1", draft_owner_id="owner@example.com")

    with pytest.raises(PermissionError) as excinfo:
        manager.update_product_with_auth(
            product_id=product.id,
            product_data_dict={"name": "Hijacked"},
            user_email="stranger@example.com",
            user_groups=[],
            caller_team_ids=["team-2"],
        )

    # Message must not disclose which sub-check failed.
    assert "team" not in str(excinfo.value).lower()
    assert manager.get_product(product.id).name == "Orders"


def test_update_product_with_auth_fails_closed_for_orphan_rows():
    """No project, no owner team, no draft owner — non-admins can't edit."""
    manager = _products_manager()
    product = _make_product(manager, draft_owner_id=None)

    with pytest.raises(PermissionError):
        manager.update_product_with_auth(
            product_id=product.id,
            product_data_dict={"name": "Adopted"},
            user_email="anyone@example.com",
            user_groups=[],
        )


def test_update_product_stamps_updated_at_and_updated_by():
    manager = _products_manager()
    product = _make_product(manager)

    manager.update_product(product.id, {"name": "Renamed"}, user="editor@example.com")

    doc = manager._entities.get_entity("data_products", product.id)
    assert doc["updated_by"] == "editor@example.com"
    assert doc["updated_at"]


# --- get_product_versions -----------------------------------------------------


def test_get_product_versions_raises_for_an_unknown_product():
    manager = _products_manager()
    with pytest.raises(ValueError, match="Product not found"):
        manager.get_product_versions(db=None, product_id="nope")


def test_get_product_versions_groups_by_family_newest_first():
    manager = _products_manager()
    v1 = _make_product(manager, version="1.0.0", created_at="2026-01-01T00:00:00+00:00")
    v2 = _make_product(
        manager,
        version="2.0.0",
        created_at="2026-02-01T00:00:00+00:00",
        version_family_id=None,
    )
    manager.update_product(v2.id, {"version_family_id": v1.version_family_id})
    # A product in a different family must not leak in.
    _make_product(manager, name="Unrelated", version="1.0.0")

    versions = manager.get_product_versions(db=None, product_id=v1.id, is_admin=True)

    assert [p.version for p in versions] == ["2.0.0", "1.0.0"]
    # Any member of the family resolves the same family.
    assert [p.id for p in manager.get_product_versions(db=None, product_id=v2.id, is_admin=True)] == [
        v2.id,
        v1.id,
    ]


def test_get_product_versions_hides_other_users_personal_drafts():
    manager = _products_manager()
    published = _make_product(manager, version="1.0.0", draft_owner_id=None)
    mine = _make_product(manager, version="2.0.0", draft_owner_id="me@example.com")
    theirs = _make_product(manager, version="3.0.0", draft_owner_id="them@example.com")
    for product in (mine, theirs):
        manager.update_product(product.id, {"version_family_id": published.version_family_id})

    visible = manager.get_product_versions(
        db=None, product_id=published.id, user_email="me@example.com"
    )
    assert {p.id for p in visible} == {published.id, mine.id}

    all_versions = manager.get_product_versions(
        db=None, product_id=published.id, user_email="me@example.com", is_admin=True
    )
    assert {p.id for p in all_versions} == {published.id, mine.id, theirs.id}


# --- build_odps_export --------------------------------------------------------


def test_build_odps_export_raises_for_an_unknown_product():
    manager = _products_manager()
    with pytest.raises(ValueError, match="not found"):
        manager.build_odps_export("nope")


def test_build_odps_export_emits_an_ordered_odps_document():
    manager = _products_manager()
    product = _make_product(
        manager,
        status="active",
        version="1.2.0",
        domain="Sales",
        tenant="acme",
        description={"purpose": "Track orders", "usage": "BI"},
        inputPorts=[{"name": "raw", "version": "1.0", "contractId": "c-in"}],
        outputPorts=[{"name": "gold", "version": "1.0", "type": "table"}],
        team={"name": "Sales Eng", "members": [{"username": "a@x.com", "role": "owner"}]},
    )

    odps = manager.build_odps_export(product.id)

    assert list(odps)[:4] == ["kind", "apiVersion", "id", "status"]
    assert odps["kind"] == "DataProduct"
    assert odps["apiVersion"] == "v1.0.0"
    assert odps["domain"] == "Sales"
    assert odps["description"] == {"purpose": "Track orders", "usage": "BI"}
    assert odps["inputPorts"] == [{"name": "raw", "version": "1.0", "contractId": "c-in"}]
    assert odps["outputPorts"][0]["name"] == "gold"
    assert odps["team"]["members"] == [{"username": "a@x.com", "role": "owner"}]
    # Internal / audit fields stay out of the ODPS document.
    for internal in ("created_at", "updated_at", "draft_owner_id", "versionFamilyId", "tags"):
        assert internal not in odps


def test_build_odps_export_drops_fields_resolved_only_for_the_ui():
    manager = _products_manager()
    product = _make_product(
        manager,
        outputPorts=[
            {
                "name": "gold",
                "version": "1.0",
                "contractId": "c-1",
                "contractName": "Orders Contract",
                "deliveryMethodName": "Delta Share",
            }
        ],
    )

    port = manager.build_odps_export(product.id)["outputPorts"][0]

    assert port["contractId"] == "c-1"
    assert "contractName" not in port
    assert "deliveryMethodName" not in port


# --- product subscriptions ----------------------------------------------------
#
# The product detail page calls subscription, subscribers and subscriber-count
# on load; every one of them 500'd before these existed.


def test_subscription_round_trip_writes_entity_subscription_rows():
    overlays = FakeOverlayStore()
    manager = _products_manager(overlays)
    product = _make_product(manager, status="active")

    assert manager.get_subscription_status(product.id, "me@x.com").subscribed is False

    response = manager.subscribe(product.id, "me@x.com", reason="need orders")
    assert response.subscribed is True
    assert response.subscription.product_id == product.id
    assert response.subscription.subscription_reason == "need orders"
    # Stored in the shared overlay table the generic subscription feature reads.
    assert overlays.list_for_entity("entity_subscriptions", "data_product", product.id)

    assert manager.get_subscription_status(product.id, "me@x.com").subscribed is True
    assert manager.get_subscriber_count(product.id) == 1
    subscribers = manager.get_subscribers(product.id)
    assert subscribers.subscriber_count == 1
    assert subscribers.subscribers[0].email == "me@x.com"
    assert [p.id for p in manager.get_user_subscriptions("me@x.com")] == [product.id]

    assert manager.unsubscribe(product.id, "me@x.com").subscribed is False
    assert manager.get_subscriber_count(product.id) == 0


def test_subscribe_is_idempotent_and_gated_on_product_status():
    overlays = FakeOverlayStore()
    manager = _products_manager(overlays)
    product = _make_product(manager, status="active")

    first = manager.subscribe(product.id, "me@x.com")
    second = manager.subscribe(product.id, "me@x.com")
    assert first.subscription.id == second.subscription.id
    assert manager.get_subscriber_count(product.id) == 1

    draft = _make_product(manager, status="draft")
    with pytest.raises(ValueError, match="Cannot subscribe"):
        manager.subscribe(draft.id, "me@x.com")

    with pytest.raises(ValueError, match="not found"):
        manager.subscribe("nope", "me@x.com")


def test_subscribers_are_scoped_to_their_own_product():
    overlays = FakeOverlayStore()
    manager = _products_manager(overlays)
    orders = _make_product(manager, name="Orders", status="active")
    returns = _make_product(manager, name="Returns", status="active")

    manager.subscribe(orders.id, "a@x.com")
    manager.subscribe(returns.id, "b@x.com")

    assert [s.email for s in manager.get_subscribers(orders.id).subscribers] == ["a@x.com"]
    assert [p.id for p in manager.get_user_subscriptions("b@x.com")] == [returns.id]


# --- projects -----------------------------------------------------------------


def _projects_manager():
    from src.common.uc_native.feature_managers import UcNativeProjectsManager

    entities = FakeEntities()
    return UcNativeProjectsManager(entities), entities


def test_get_user_projects_returns_the_user_project_access_model():
    """Regression: the route's response_model is UserProjectAccess, not a list."""
    from src.models.projects import UserProjectAccess

    manager, _ = _projects_manager()
    manager.create_project(project_in={"name": "Atlas", "team_ids": ["t1"]})

    access = manager.get_user_projects(user_identifier="me@x.com", team_ids=["t1"])

    assert isinstance(access, UserProjectAccess)
    assert [p.name for p in access.projects] == ["Atlas"]
    assert access.current_project_id is None


def test_project_access_follows_team_membership_and_admin_groups():
    manager, _ = _projects_manager()
    mine = manager.create_project(project_in={"name": "Mine", "team_ids": ["t1"]})
    theirs = manager.create_project(project_in={"name": "Theirs", "team_ids": ["t2"]})

    visible = manager.get_user_projects(user_identifier="me@x.com", team_ids=["t1"])
    assert [p.id for p in visible.projects] == [mine.id]
    assert manager.check_user_project_access(None, user_identifier="me@x.com", team_ids=["t1"], project_id=mine.id)
    assert not manager.check_user_project_access(
        None, user_identifier="me@x.com", team_ids=["t1"], project_id=theirs.id
    )

    admin = manager.get_user_projects(user_identifier="root@x.com", user_groups=["app-admins"])
    assert {p.id for p in admin.projects} == {mine.id, theirs.id}


def test_is_user_project_member_honours_the_settings_admin_check():
    manager, _ = _projects_manager()
    project = manager.create_project(project_in={"name": "Atlas", "team_ids": ["t1"]})
    settings = SimpleNamespace(APP_ADMIN_DEFAULT_GROUPS=["platform-admins"])

    assert manager.is_user_project_member(
        None, user_identifier="root@x.com", user_groups=["platform-admins"],
        project_id=project.id, settings=settings,
    )
    assert not manager.is_user_project_member(
        None, user_identifier="outsider@x.com", user_groups=["some-group"],
        project_id=project.id, settings=settings,
    )


# --- quality ------------------------------------------------------------------


def _quality_manager():
    from src.common.uc_native.feature_managers import UcNativeQualityManager

    overlays = FakeOverlayStore()
    return UcNativeQualityManager(overlays), overlays


def _quality_row(overlays, *, entity_type, entity_id, dimension, score, measured_at, source="manual"):
    return overlays.add(
        "quality_items",
        {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "dimension": dimension,
            "score_percent": score,
            "source": source,
            "measured_at": measured_at,
        },
    )


def test_quality_summarize_returns_the_summary_model_when_empty():
    """Regression: the route's response_model is QualitySummary, not a dict."""
    from src.models.quality import QualitySummary

    manager, _ = _quality_manager()
    summary = manager.summarize(None, entity_type="data_product", entity_id="p1")

    assert isinstance(summary, QualitySummary)
    assert summary.overall_score_percent == 0.0
    assert summary.items_count == 0
    assert summary.measured_at is None


def test_quality_summarize_keeps_only_the_latest_score_per_dimension():
    manager, overlays = _quality_manager()
    _quality_row(
        overlays, entity_type="data_product", entity_id="p1",
        dimension="completeness", score=50, measured_at="2026-01-01T00:00:00+00:00",
    )
    _quality_row(
        overlays, entity_type="data_product", entity_id="p1",
        dimension="completeness", score=90, measured_at="2026-06-01T00:00:00+00:00",
    )
    _quality_row(
        overlays, entity_type="data_product", entity_id="p1",
        dimension="freshness", score=70, measured_at="2026-05-01T00:00:00+00:00",
        source="automated",
    )

    summary = manager.summarize(None, entity_type="data_product", entity_id="p1")

    assert summary.by_dimension == {"completeness": 90.0, "freshness": 70.0}
    assert summary.by_source == {"manual": 90.0, "automated": 70.0}
    assert summary.overall_score_percent == 80.0
    # items_count reports every stored measurement, matching the Postgres manager.
    assert summary.items_count == 3
    assert summary.measured_at.isoformat() == "2026-06-01T00:00:00+00:00"


def test_quality_summarize_tolerates_unmeasured_and_unparsable_rows():
    manager, overlays = _quality_manager()
    _quality_row(
        overlays, entity_type="data_product", entity_id="p1",
        dimension="completeness", score=80, measured_at=None,
    )
    _quality_row(
        overlays, entity_type="data_product", entity_id="p1",
        dimension="accuracy", score="not-a-number", measured_at="2026-06-01T00:00:00+00:00",
    )

    summary = manager.summarize(None, entity_type="data_product", entity_id="p1")

    assert summary.by_dimension == {"completeness": 80.0, "accuracy": 0.0}
    assert summary.overall_score_percent == 40.0


def test_aggregate_for_product_rolls_up_the_products_contracts():
    manager, overlays = _quality_manager()
    _quality_row(
        overlays, entity_type="data_product", entity_id="p1",
        dimension="completeness", score=100, measured_at="2026-06-01T00:00:00+00:00",
    )
    _quality_row(
        overlays, entity_type="data_contract", entity_id="c1",
        dimension="completeness", score=50, measured_at="2026-06-02T00:00:00+00:00",
    )
    products = SimpleNamespace(get_contracts_for_product=lambda pid: ["c1"])

    summary = manager.aggregate_for_product(None, product_id="p1", data_products_manager=products)

    # Both entities keep their own latest row, then average into the dimension.
    assert summary.by_dimension == {"completeness": 75.0}
    assert summary.items_count == 2


def test_aggregate_for_product_survives_a_contract_lookup_failure():
    manager, overlays = _quality_manager()
    _quality_row(
        overlays, entity_type="data_product", entity_id="p1",
        dimension="completeness", score=100, measured_at="2026-06-01T00:00:00+00:00",
    )

    def boom(_pid):
        raise RuntimeError("warehouse down")

    summary = manager.aggregate_for_product(
        None, product_id="p1", data_products_manager=SimpleNamespace(get_contracts_for_product=boom)
    )

    assert summary.overall_score_percent == 100.0


# --- costs --------------------------------------------------------------------


def _costs_manager():
    from src.common.uc_native.feature_managers import UcNativeCostsManager

    overlays = FakeOverlayStore()
    return UcNativeCostsManager(overlays), overlays


def _cost_row(overlays, *, start_month, end_month=None, amount=1000, center="INFRASTRUCTURE", currency="USD"):
    return overlays.add(
        "cost_items",
        {
            "entity_type": "data_product",
            "entity_id": "p1",
            "cost_center": center,
            "amount_cents": amount,
            "currency": currency,
            "start_month": start_month,
            "end_month": end_month,
        },
    )


def test_cost_summarize_returns_the_summary_model_when_empty():
    """Regression: the route's response_model is CostSummary, not a dict."""
    from datetime import date

    from src.models.costs import CostSummary

    manager, _ = _costs_manager()
    summary = manager.summarize(None, entity_type="data_product", entity_id="p1", month=date(2026, 7, 1))

    assert isinstance(summary, CostSummary)
    assert summary == CostSummary(
        month="2026-07", currency="USD", total_cents=0, items_count=0, by_center={}
    )


def test_cost_summarize_only_counts_items_open_in_that_month():
    from datetime import date

    manager, overlays = _costs_manager()
    _cost_row(overlays, start_month="2026-01-01", end_month=None, amount=1000)
    _cost_row(overlays, start_month="2026-07-15", end_month=None, amount=500, center="STORAGE")
    # Ended before the month under test.
    _cost_row(overlays, start_month="2026-01-01", end_month="2026-03-01", amount=9999)
    # Starts after the month under test.
    _cost_row(overlays, start_month="2026-09-01", end_month=None, amount=8888)

    summary = manager.summarize(None, entity_type="data_product", entity_id="p1", month=date(2026, 7, 1))

    assert summary.by_center == {"INFRASTRUCTURE": 1000, "STORAGE": 500}
    assert summary.total_cents == 1500
    assert summary.items_count == 2


def test_cost_list_month_filter_matches_the_summary_window():
    from datetime import date

    manager, overlays = _costs_manager()
    open_ended = _cost_row(overlays, start_month="2026-01-01")
    _cost_row(overlays, start_month="2026-01-01", end_month="2026-03-01")

    rows = manager.list(None, entity_type="data_product", entity_id="p1", month=date(2026, 7, 1))
    assert [row["id"] for row in rows] == [open_ended["id"]]

    # No month means no window filtering, mirroring CostItemsRepository.
    assert len(manager.list(None, entity_type="data_product", entity_id="p1")) == 2


# --- settings roles -----------------------------------------------------------


class FakeRoleStore:
    """Delta store stub exposing just the app_roles rows the RBAC store reads."""

    def __init__(self, rows):
        self.rows = rows

    def list_rows(self, table, limit=100):
        return [dict(row) for row in self.rows] if table == "app_roles" else []

    def get_by_id(self, table, row_id):
        return next((dict(r) for r in self.rows if str(r["id"]) == str(row_id)), None)

    def table_fqn(self, table):
        return f"fake.{table}"

    def query(self, sql):
        name = sql.split("name = '", 1)[1].split("'", 1)[0]
        return [dict(r) for r in self.rows if r["name"] == name]


def _role_row(name, *, groups, permissions, is_admin=False):
    return {
        "id": str(uuid4()),
        "name": name,
        "description": None,
        "assigned_groups_json": json.dumps(groups),
        "feature_permissions_json": json.dumps(permissions),
        "home_sections_json": json.dumps([]),
        "is_admin_role": is_admin,
    }


def _settings_manager(rows):
    from src.common.uc_native.settings_manager import UcNativeSettingsManager

    return UcNativeSettingsManager(FakeRoleStore(rows), SimpleNamespace())


def test_canonical_role_prefers_an_admin_group_then_the_widest_role():
    """Regression: /api/user/actual-role 500'd with no canonical-role method."""
    admin = _role_row("Admin", groups=["admins"], permissions={"data-products": "Admin"}, is_admin=True)
    reader = _role_row("Reader", groups=["readers"], permissions={"data-products": "Read-only"})
    writer = _role_row("Writer", groups=["writers"], permissions={"data-products": "Read/Write"})
    manager = _settings_manager([admin, reader, writer])

    assert manager.get_canonical_role_for_groups(["some-admin-group"]).name == "Admin"
    assert manager.get_canonical_role_for_groups(["readers", "writers"]).name == "Writer"
    assert manager.get_canonical_role_for_groups(["nobody"]) is None
    assert manager.get_canonical_role_for_groups([]) is None


def test_get_app_role_lookups_by_id_and_name():
    reader = _role_row("Reader", groups=["readers"], permissions={"data-products": "Read-only"})
    manager = _settings_manager([reader])

    assert manager.get_app_role(reader["id"]).name == "Reader"
    assert str(manager.get_app_role_by_name("Reader").id) == reader["id"]
    assert manager.get_app_role(str(uuid4())) is None


def test_requestable_roles_exclude_roles_the_caller_already_holds(monkeypatch):
    from src.common.uc_native.settings_manager import NO_ROLE_SENTINEL
    from src.models.settings import AppRole

    reader = AppRole(
        id=str(uuid4()), name="Reader", assigned_groups=["readers"], feature_permissions={}
    )
    writer = AppRole(
        id=str(uuid4()), name="Writer", assigned_groups=["writers"], feature_permissions={},
        requestable_by_roles=[str(reader.id)],
    )
    # Requestable by anyone, including callers with no role at all.
    guest = AppRole(
        id=str(uuid4()), name="Guest", assigned_groups=[], feature_permissions={},
        requestable_by_roles=[NO_ROLE_SENTINEL],
    )
    manager = _settings_manager([])
    monkeypatch.setattr(manager, "list_app_roles", lambda: [reader, writer, guest])

    assert [r.name for r in manager.get_requestable_roles_for_user(["readers"])] == ["Writer"]
    assert [r.name for r in manager.get_requestable_roles_for_user([])] == ["Guest"]
    # Holding Writer already means it is no longer requestable.
    assert [r.name for r in manager.get_requestable_roles_for_user(["writers"])] == []
