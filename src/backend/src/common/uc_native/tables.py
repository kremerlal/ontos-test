"""UC Delta table schemas for uc_native mode."""

from __future__ import annotations

from typing import Dict, List, Tuple

from databricks.sdk.service.catalog import ColumnTypeName

ColumnSpec = Tuple[str, ColumnTypeName]

# Core entity aggregates (denormalized snapshot_json + index columns).
ENTITY_TABLES: Dict[str, List[ColumnSpec]] = {
    "data_products": [
        ("id", ColumnTypeName.STRING),
        ("name", ColumnTypeName.STRING),
        ("status", ColumnTypeName.STRING),
        ("domain_id", ColumnTypeName.STRING),
        ("project_id", ColumnTypeName.STRING),
        ("draft_owner_id", ColumnTypeName.STRING),
        ("updated_at", ColumnTypeName.STRING),
        ("etag", ColumnTypeName.STRING),
        ("snapshot_json", ColumnTypeName.STRING),
    ],
    "data_contracts": [
        ("id", ColumnTypeName.STRING),
        ("name", ColumnTypeName.STRING),
        ("status", ColumnTypeName.STRING),
        ("product_id", ColumnTypeName.STRING),
        ("project_id", ColumnTypeName.STRING),
        ("draft_owner_id", ColumnTypeName.STRING),
        ("updated_at", ColumnTypeName.STRING),
        ("etag", ColumnTypeName.STRING),
        ("snapshot_json", ColumnTypeName.STRING),
    ],
    "assets": [
        ("id", ColumnTypeName.STRING),
        ("name", ColumnTypeName.STRING),
        ("asset_type_name", ColumnTypeName.STRING),
        ("status", ColumnTypeName.STRING),
        ("updated_at", ColumnTypeName.STRING),
        ("etag", ColumnTypeName.STRING),
        ("snapshot_json", ColumnTypeName.STRING),
    ],
    "asset_types": [
        ("id", ColumnTypeName.STRING),
        ("name", ColumnTypeName.STRING),
        ("category", ColumnTypeName.STRING),
        ("is_system", ColumnTypeName.BOOLEAN),
        ("status", ColumnTypeName.STRING),
        ("updated_at", ColumnTypeName.STRING),
        ("etag", ColumnTypeName.STRING),
        ("snapshot_json", ColumnTypeName.STRING),
    ],
    "data_domains": [
        ("id", ColumnTypeName.STRING),
        ("name", ColumnTypeName.STRING),
        ("parent_id", ColumnTypeName.STRING),
        ("updated_at", ColumnTypeName.STRING),
        ("etag", ColumnTypeName.STRING),
        ("snapshot_json", ColumnTypeName.STRING),
    ],
    "tags": [
        ("id", ColumnTypeName.STRING),
        ("name", ColumnTypeName.STRING),
        ("namespace", ColumnTypeName.STRING),
        ("status", ColumnTypeName.STRING),
        ("updated_at", ColumnTypeName.STRING),
        ("etag", ColumnTypeName.STRING),
        ("snapshot_json", ColumnTypeName.STRING),
    ],
    "tag_namespaces": [
        ("id", ColumnTypeName.STRING),
        ("name", ColumnTypeName.STRING),
        ("updated_at", ColumnTypeName.STRING),
        ("snapshot_json", ColumnTypeName.STRING),
    ],
    "connections": [
        ("id", ColumnTypeName.STRING),
        ("name", ColumnTypeName.STRING),
        ("connector_type", ColumnTypeName.STRING),
        ("enabled", ColumnTypeName.BOOLEAN),
        ("is_default", ColumnTypeName.BOOLEAN),
        ("updated_at", ColumnTypeName.STRING),
        ("etag", ColumnTypeName.STRING),
        ("snapshot_json", ColumnTypeName.STRING),
    ],
    "entity_tag_associations": [
        ("id", ColumnTypeName.STRING),
        ("tag_id", ColumnTypeName.STRING),
        ("entity_id", ColumnTypeName.STRING),
        ("entity_type", ColumnTypeName.STRING),
        ("assigned_value", ColumnTypeName.STRING),
        ("updated_at", ColumnTypeName.STRING),
    ],
    "teams": [("id", ColumnTypeName.STRING), ("name", ColumnTypeName.STRING), ("status", ColumnTypeName.STRING), ("updated_at", ColumnTypeName.STRING), ("etag", ColumnTypeName.STRING), ("snapshot_json", ColumnTypeName.STRING)],
    "projects": [("id", ColumnTypeName.STRING), ("name", ColumnTypeName.STRING), ("status", ColumnTypeName.STRING), ("updated_at", ColumnTypeName.STRING), ("etag", ColumnTypeName.STRING), ("snapshot_json", ColumnTypeName.STRING)],
    "business_roles": [("id", ColumnTypeName.STRING), ("name", ColumnTypeName.STRING), ("status", ColumnTypeName.STRING), ("updated_at", ColumnTypeName.STRING), ("etag", ColumnTypeName.STRING), ("snapshot_json", ColumnTypeName.STRING)],
    "business_owners": [("id", ColumnTypeName.STRING), ("name", ColumnTypeName.STRING), ("status", ColumnTypeName.STRING), ("updated_at", ColumnTypeName.STRING), ("etag", ColumnTypeName.STRING), ("snapshot_json", ColumnTypeName.STRING)],
    "delivery_methods": [("id", ColumnTypeName.STRING), ("name", ColumnTypeName.STRING), ("status", ColumnTypeName.STRING), ("updated_at", ColumnTypeName.STRING), ("etag", ColumnTypeName.STRING), ("snapshot_json", ColumnTypeName.STRING)],
    "data_asset_reviews": [("id", ColumnTypeName.STRING), ("name", ColumnTypeName.STRING), ("status", ColumnTypeName.STRING), ("updated_at", ColumnTypeName.STRING), ("etag", ColumnTypeName.STRING), ("snapshot_json", ColumnTypeName.STRING)],
    "mdm_configs": [("id", ColumnTypeName.STRING), ("name", ColumnTypeName.STRING), ("status", ColumnTypeName.STRING), ("updated_at", ColumnTypeName.STRING), ("etag", ColumnTypeName.STRING), ("snapshot_json", ColumnTypeName.STRING)],
    "compliance_policies": [("id", ColumnTypeName.STRING), ("name", ColumnTypeName.STRING), ("status", ColumnTypeName.STRING), ("updated_at", ColumnTypeName.STRING), ("etag", ColumnTypeName.STRING), ("snapshot_json", ColumnTypeName.STRING)],
    "llm_sessions": [("id", ColumnTypeName.STRING), ("name", ColumnTypeName.STRING), ("status", ColumnTypeName.STRING), ("updated_at", ColumnTypeName.STRING), ("etag", ColumnTypeName.STRING), ("snapshot_json", ColumnTypeName.STRING)],
    "mcp_tokens": [("id", ColumnTypeName.STRING), ("name", ColumnTypeName.STRING), ("status", ColumnTypeName.STRING), ("updated_at", ColumnTypeName.STRING), ("etag", ColumnTypeName.STRING), ("snapshot_json", ColumnTypeName.STRING)],
}

OVERLAY_TABLES: Dict[str, List[ColumnSpec]] = {
    "comments": [
        ("id", ColumnTypeName.STRING),
        ("entity_type", ColumnTypeName.STRING),
        ("entity_id", ColumnTypeName.STRING),
        ("author", ColumnTypeName.STRING),
        ("body", ColumnTypeName.STRING),
        ("updated_at", ColumnTypeName.STRING),
        ("snapshot_json", ColumnTypeName.STRING),
    ],
    "entity_relationships": [
        ("id", ColumnTypeName.STRING),
        ("source_entity_id", ColumnTypeName.STRING),
        ("source_entity_type", ColumnTypeName.STRING),
        ("target_entity_id", ColumnTypeName.STRING),
        ("target_entity_type", ColumnTypeName.STRING),
        ("relationship_type", ColumnTypeName.STRING),
        ("updated_at", ColumnTypeName.STRING),
        ("snapshot_json", ColumnTypeName.STRING),
    ],
    "entity_change_log": [
        ("id", ColumnTypeName.STRING),
        ("entity_type", ColumnTypeName.STRING),
        ("entity_id", ColumnTypeName.STRING),
        ("action", ColumnTypeName.STRING),
        ("username", ColumnTypeName.STRING),
        ("timestamp", ColumnTypeName.STRING),
        ("details_json", ColumnTypeName.STRING),
    ],
    "notifications": [
        ("id", ColumnTypeName.STRING),
        ("username", ColumnTypeName.STRING),
        ("title", ColumnTypeName.STRING),
        ("body", ColumnTypeName.STRING),
        ("read", ColumnTypeName.BOOLEAN),
        ("created_at", ColumnTypeName.STRING),
        ("snapshot_json", ColumnTypeName.STRING),
    ],
    "audit_events": [
        ("id", ColumnTypeName.STRING),
        ("timestamp", ColumnTypeName.STRING),
        ("username", ColumnTypeName.STRING),
        ("feature", ColumnTypeName.STRING),
        ("action", ColumnTypeName.STRING),
        ("success", ColumnTypeName.BOOLEAN),
        ("details_json", ColumnTypeName.STRING),
    ],
    "entity_semantic_links": [("id", ColumnTypeName.STRING), ("entity_type", ColumnTypeName.STRING), ("entity_id", ColumnTypeName.STRING), ("iri", ColumnTypeName.STRING), ("link_type", ColumnTypeName.STRING), ("updated_at", ColumnTypeName.STRING), ("snapshot_json", ColumnTypeName.STRING)],
    "entity_subscriptions": [("id", ColumnTypeName.STRING), ("entity_type", ColumnTypeName.STRING), ("entity_id", ColumnTypeName.STRING), ("subscriber_email", ColumnTypeName.STRING), ("updated_at", ColumnTypeName.STRING), ("snapshot_json", ColumnTypeName.STRING)],
    "cost_items": [("id", ColumnTypeName.STRING), ("entity_type", ColumnTypeName.STRING), ("entity_id", ColumnTypeName.STRING), ("updated_at", ColumnTypeName.STRING), ("snapshot_json", ColumnTypeName.STRING)],
    "quality_items": [("id", ColumnTypeName.STRING), ("entity_type", ColumnTypeName.STRING), ("entity_id", ColumnTypeName.STRING), ("updated_at", ColumnTypeName.STRING), ("snapshot_json", ColumnTypeName.STRING)],
    "metadata_items": [("id", ColumnTypeName.STRING), ("entity_type", ColumnTypeName.STRING), ("entity_id", ColumnTypeName.STRING), ("updated_at", ColumnTypeName.STRING), ("snapshot_json", ColumnTypeName.STRING)],
    "ontology_generation_runs": [("id", ColumnTypeName.STRING), ("user_id", ColumnTypeName.STRING), ("status", ColumnTypeName.STRING), ("updated_at", ColumnTypeName.STRING), ("snapshot_json", ColumnTypeName.STRING)],
    "term_mapping_runs": [("id", ColumnTypeName.STRING), ("status", ColumnTypeName.STRING), ("created_by", ColumnTypeName.STRING), ("updated_at", ColumnTypeName.STRING), ("snapshot_json", ColumnTypeName.STRING)],
    "term_mapping_suggestions": [("id", ColumnTypeName.STRING), ("run_id", ColumnTypeName.STRING), ("status", ColumnTypeName.STRING), ("updated_at", ColumnTypeName.STRING), ("snapshot_json", ColumnTypeName.STRING)],
}

WORKFLOW_TABLES: Dict[str, List[ColumnSpec]] = {
    "process_workflows": [
        ("id", ColumnTypeName.STRING),
        ("name", ColumnTypeName.STRING),
        ("entity_type", ColumnTypeName.STRING),
        ("status", ColumnTypeName.STRING),
        ("updated_at", ColumnTypeName.STRING),
        ("snapshot_json", ColumnTypeName.STRING),
    ],
    "wizard_sessions": [
        ("id", ColumnTypeName.STRING),
        ("workflow_id", ColumnTypeName.STRING),
        ("username", ColumnTypeName.STRING),
        ("status", ColumnTypeName.STRING),
        ("etag", ColumnTypeName.STRING),
        ("updated_at", ColumnTypeName.STRING),
        ("snapshot_json", ColumnTypeName.STRING),
    ],
    "access_grant_requests": [
        ("id", ColumnTypeName.STRING),
        ("requester", ColumnTypeName.STRING),
        ("resource", ColumnTypeName.STRING),
        ("status", ColumnTypeName.STRING),
        ("updated_at", ColumnTypeName.STRING),
        ("snapshot_json", ColumnTypeName.STRING),
    ],
    "agreements": [
        ("id", ColumnTypeName.STRING),
        ("name", ColumnTypeName.STRING),
        ("status", ColumnTypeName.STRING),
        ("updated_at", ColumnTypeName.STRING),
        ("snapshot_json", ColumnTypeName.STRING),
    ],
    "workflow_installations": [
        ("id", ColumnTypeName.STRING),
        ("workflow_key", ColumnTypeName.STRING),
        ("job_id", ColumnTypeName.LONG),
        ("updated_at", ColumnTypeName.STRING),
        ("snapshot_json", ColumnTypeName.STRING),
    ],
}

SEMANTIC_TABLES: Dict[str, List[ColumnSpec]] = {
    "rdf_triples": [
        ("id", ColumnTypeName.STRING),
        ("subject", ColumnTypeName.STRING),
        ("predicate", ColumnTypeName.STRING),
        ("object", ColumnTypeName.STRING),
        # Object term kind, so the in-memory graph can be rebuilt losslessly.
        ("object_is_uri", ColumnTypeName.BOOLEAN),
        ("object_language", ColumnTypeName.STRING),
        ("object_datatype", ColumnTypeName.STRING),
        ("context", ColumnTypeName.STRING),
        ("updated_at", ColumnTypeName.STRING),
    ],
    "mdm_match_runs": [
        ("id", ColumnTypeName.STRING),
        ("config_id", ColumnTypeName.STRING),
        ("status", ColumnTypeName.STRING),
        ("updated_at", ColumnTypeName.STRING),
        ("snapshot_json", ColumnTypeName.STRING),
    ],
    "compliance_runs": [
        ("id", ColumnTypeName.STRING),
        ("policy_id", ColumnTypeName.STRING),
        ("status", ColumnTypeName.STRING),
        ("updated_at", ColumnTypeName.STRING),
        ("snapshot_json", ColumnTypeName.STRING),
    ],
    "data_quality_runs": [
        ("id", ColumnTypeName.STRING),
        ("contract_id", ColumnTypeName.STRING),
        ("status", ColumnTypeName.STRING),
        ("updated_at", ColumnTypeName.STRING),
        ("snapshot_json", ColumnTypeName.STRING),
    ],
}

RBAC_TABLES: Dict[str, List[ColumnSpec]] = {
    "app_roles": [
        ("id", ColumnTypeName.STRING),
        ("name", ColumnTypeName.STRING),
        ("description", ColumnTypeName.STRING),
        ("assigned_groups_json", ColumnTypeName.STRING),
        ("feature_permissions_json", ColumnTypeName.STRING),
        ("home_sections_json", ColumnTypeName.STRING),
        ("is_admin_role", ColumnTypeName.BOOLEAN),
        ("updated_at", ColumnTypeName.STRING),
    ],
    "app_settings": [
        ("key", ColumnTypeName.STRING),
        ("value", ColumnTypeName.STRING),
        ("updated_at", ColumnTypeName.STRING),
    ],
}

APP_TABLES: Dict[str, List[ColumnSpec]] = {
    **ENTITY_TABLES,
    **OVERLAY_TABLES,
    **WORKFLOW_TABLES,
    **SEMANTIC_TABLES,
    **RBAC_TABLES,
}
