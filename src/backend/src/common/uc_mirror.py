"""Unity Catalog Delta mirror — export OLTP entities for analytics/BI.

The mirror is deliberately read-only from Ontos. PostgreSQL remains the source
while it is available; ``uc_readonly`` deployments consume the last successful
snapshot. Each sync replaces a table's contents to avoid duplicate snapshots.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.catalog import ColumnInfo, ColumnTypeName, TableType, DataSourceFormat

from src.common.config import Settings
from src.common.logging import get_logger
from src.common.unity_catalog_utils import (
    ensure_catalog_and_schema_exist,
    sanitize_uc_identifier,
)

logger = get_logger(__name__)

MIRROR_TABLES: Dict[str, List[tuple[str, ColumnTypeName]]] = {
    "data_products": [
        ("id", ColumnTypeName.STRING),
        ("name", ColumnTypeName.STRING),
        ("status", ColumnTypeName.STRING),
        ("domain_id", ColumnTypeName.STRING),
        ("updated_at", ColumnTypeName.STRING),
        ("snapshot_json", ColumnTypeName.STRING),
    ],
    "data_contracts": [
        ("id", ColumnTypeName.STRING),
        ("name", ColumnTypeName.STRING),
        ("status", ColumnTypeName.STRING),
        ("product_id", ColumnTypeName.STRING),
        ("updated_at", ColumnTypeName.STRING),
        ("snapshot_json", ColumnTypeName.STRING),
    ],
    "assets": [
        ("id", ColumnTypeName.STRING),
        ("name", ColumnTypeName.STRING),
        ("asset_type_name", ColumnTypeName.STRING),
        ("updated_at", ColumnTypeName.STRING),
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
    "tags": [
        ("id", ColumnTypeName.STRING),
        ("name", ColumnTypeName.STRING),
        ("namespace", ColumnTypeName.STRING),
        ("status", ColumnTypeName.STRING),
        ("updated_at", ColumnTypeName.STRING),
        ("snapshot_json", ColumnTypeName.STRING),
    ],
    "workflow_job_runs": [
        ("id", ColumnTypeName.STRING),
        ("run_id", ColumnTypeName.LONG),
        ("run_name", ColumnTypeName.STRING),
        ("life_cycle_state", ColumnTypeName.STRING),
        ("result_state", ColumnTypeName.STRING),
        ("start_time", ColumnTypeName.LONG),
        ("end_time", ColumnTypeName.LONG),
        ("snapshot_json", ColumnTypeName.STRING),
    ],
}


def mirror_fqn(settings: Settings, table_name: str) -> str:
    catalog = sanitize_uc_identifier(settings.DATABRICKS_CATALOG)
    schema = sanitize_uc_identifier(settings.APP_UC_MIRROR_SCHEMA)
    table = sanitize_uc_identifier(table_name)
    return f"{catalog}.{schema}.{table}"


def ensure_mirror_tables(ws_client: WorkspaceClient, settings: Settings) -> List[str]:
    """Create mirror Delta tables if they do not exist. Returns FQNs created."""
    catalog = sanitize_uc_identifier(settings.DATABRICKS_CATALOG)
    schema = sanitize_uc_identifier(settings.APP_UC_MIRROR_SCHEMA)
    ensure_catalog_and_schema_exist(ws_client, catalog, schema)
    created: List[str] = []
    for table_name, columns in MIRROR_TABLES.items():
        fqn = f"{catalog}.{schema}.{table_name}"
        try:
            ws_client.tables.get(fqn)
        except Exception:
            col_infos = [
                ColumnInfo(name=c[0], type_name=c[1], nullable=True, comment=None)
                for c in columns
            ]
            ws_client.tables.create(
                name=table_name,
                catalog_name=catalog,
                schema_name=schema,
                table_type=TableType.MANAGED,
                data_source_format=DataSourceFormat.DELTA,
                columns=col_infos,
                comment=f"Ontos UC mirror: {table_name}",
            )
            logger.info("Created UC mirror table %s", fqn)
        created.append(fqn)
    return created


def _row_to_sql_values(row: Dict[str, Any]) -> str:
    parts: List[str] = []
    for v in row.values():
        if v is None:
            parts.append("NULL")
        elif isinstance(v, bool):
            parts.append("true" if v else "false")
        elif isinstance(v, (int, float)):
            parts.append(str(v))
        else:
            escaped = str(v).replace("'", "''")
            parts.append(f"'{escaped}'")
    return ", ".join(parts)


def _execute_statement(
    ws_client: WorkspaceClient,
    settings: Settings,
    statement: str,
) -> None:
    response = ws_client.statement_execution.execute_statement(
        warehouse_id=settings.DATABRICKS_WAREHOUSE_ID,
        statement=statement,
        wait_timeout="50s",
    )
    status = getattr(response, "status", None)
    error = getattr(status, "error", None)
    if error:
        message = getattr(error, "message", None) or str(error)
        raise RuntimeError(f"UC mirror SQL failed: {message}")


def replace_mirror_rows(
    ws_client: WorkspaceClient,
    settings: Settings,
    table_name: str,
    rows: List[Dict[str, Any]],
    *,
    batch_size: int = 200,
) -> int:
    """Replace a mirror table snapshot through the SQL warehouse."""
    if table_name not in MIRROR_TABLES:
        raise ValueError(f"Unsupported UC mirror table: {table_name}")

    fqn = mirror_fqn(settings, table_name)
    columns = [c[0] for c in MIRROR_TABLES[table_name]]
    col_list = ", ".join(columns)
    _execute_statement(ws_client, settings, f"DELETE FROM {fqn}")
    for start in range(0, len(rows), batch_size):
        values_clauses = []
        for row in rows[start : start + batch_size]:
            ordered = {c: row.get(c) for c in columns}
            values_clauses.append(f"({_row_to_sql_values(ordered)})")
        if values_clauses:
            sql = f"INSERT INTO {fqn} ({col_list}) VALUES " + ", ".join(values_clauses)
            _execute_statement(ws_client, settings, sql)
    return len(rows)


def build_product_mirror_rows(products: List[Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for p in products:
        snapshot = _model_snapshot(p)
        rows.append({
            "id": str(getattr(p, "id", "")),
            "name": str(getattr(p, "name", "")),
            "status": str(getattr(p, "status", "")),
            "domain_id": str(
                getattr(p, "domain_id", None)
                or getattr(p, "domain", None)
                or ""
            ),
            "updated_at": _timestamp(getattr(p, "updated_at", None)),
            "snapshot_json": json.dumps(snapshot, default=str),
        })
    return rows


def _timestamp(value: Any) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat()
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _model_snapshot(model: Any) -> Dict[str, Any]:
    """Serialize scalar SQLAlchemy columns without loading relationships."""
    table = getattr(model, "__table__", None)
    if table is None:
        return {}
    return {
        column.name: getattr(model, column.name, None)
        for column in table.columns
    }


def build_contract_mirror_rows(contracts: Iterable[Any]) -> List[Dict[str, Any]]:
    return [
        {
            "id": str(getattr(item, "id", "")),
            "name": str(getattr(item, "name", "")),
            "status": str(getattr(item, "status", "")),
            "product_id": str(getattr(item, "data_product", None) or ""),
            "updated_at": _timestamp(getattr(item, "updated_at", None)),
            "snapshot_json": json.dumps(_model_snapshot(item), default=str),
        }
        for item in contracts
    ]


def build_asset_mirror_rows(assets: Iterable[Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for item in assets:
        asset_type = getattr(item, "asset_type", None)
        rows.append(
            {
                "id": str(getattr(item, "id", "")),
                "name": str(getattr(item, "name", "")),
                "asset_type_name": str(getattr(asset_type, "name", "") or ""),
                "updated_at": _timestamp(getattr(item, "updated_at", None)),
                "snapshot_json": json.dumps(_model_snapshot(item), default=str),
            }
        )
    return rows


def build_tag_mirror_rows(tags: Iterable[Any]) -> List[Dict[str, Any]]:
    return [
        {
            "id": str(getattr(item, "id", "")),
            "name": str(getattr(item, "name", "")),
            "namespace": str(getattr(item, "namespace_name", "") or ""),
            "status": str(getattr(item, "status", "")),
            "updated_at": _timestamp(getattr(item, "updated_at", None)),
            "snapshot_json": json.dumps(_model_snapshot(item), default=str),
        }
        for item in tags
    ]


def build_job_run_mirror_rows(runs: Iterable[Any]) -> List[Dict[str, Any]]:
    return [
        {
            "id": str(getattr(item, "id", "")),
            "run_id": getattr(item, "run_id", None),
            "run_name": getattr(item, "run_name", None),
            "life_cycle_state": getattr(item, "life_cycle_state", None),
            "result_state": getattr(item, "result_state", None),
            "start_time": getattr(item, "start_time", None),
            "end_time": getattr(item, "end_time", None),
            "snapshot_json": json.dumps(_model_snapshot(item), default=str),
        }
        for item in runs
    ]


def build_audit_mirror_rows(events: Iterable[Any]) -> List[Dict[str, Any]]:
    return [
        {
            "id": str(getattr(item, "id", "")),
            "timestamp": _timestamp(getattr(item, "timestamp", None)),
            "username": str(getattr(item, "username", "")),
            "feature": str(getattr(item, "feature", "")),
            "action": str(getattr(item, "action", "")),
            "success": bool(getattr(item, "success", False)),
            "details_json": json.dumps(getattr(item, "details", {}) or {}, default=str),
        }
        for item in events
    ]


def sync_all_from_session(
    ws_client: WorkspaceClient,
    settings: Settings,
    db: Any,
) -> Dict[str, int]:
    """Export all Phase-B snapshots from the OLTP database."""
    from src.db_models.assets import AssetDb
    from src.db_models.audit_log import AuditLogDb
    from src.db_models.data_contracts import DataContractDb
    from src.db_models.data_products import DataProductDb
    from src.db_models.tags import TagDb
    from src.db_models.workflow_job_runs import WorkflowJobRunDb

    ensure_mirror_tables(ws_client, settings)
    payloads = {
        "data_products": build_product_mirror_rows(db.query(DataProductDb).all()),
        "data_contracts": build_contract_mirror_rows(db.query(DataContractDb).all()),
        "assets": build_asset_mirror_rows(db.query(AssetDb).all()),
        "tags": build_tag_mirror_rows(db.query(TagDb).all()),
        "workflow_job_runs": build_job_run_mirror_rows(
            db.query(WorkflowJobRunDb).all()
        ),
        "audit_events": build_audit_mirror_rows(db.query(AuditLogDb).all()),
    }
    return {
        table_name: replace_mirror_rows(
            ws_client,
            settings,
            table_name,
            rows,
        )
        for table_name, rows in payloads.items()
    }


def sync_configured_mirror(settings: Settings) -> Dict[str, int]:
    """Open app dependencies and run one complete mirror sync."""
    from src.common.database import get_session_factory
    from src.common.workspace_client import get_workspace_client

    ws_client = get_workspace_client(settings)
    session_factory = get_session_factory()
    with session_factory() as db:
        return sync_all_from_session(ws_client, settings, db)


def volume_root(settings: Settings) -> str:
    configured = (settings.DATABRICKS_VOLUME or "").rstrip("/")
    if configured.startswith("/Volumes/"):
        return configured
    if not configured:
        raise ValueError("DATABRICKS_VOLUME is required for UC artifact storage")
    return (
        f"/Volumes/{sanitize_uc_identifier(settings.DATABRICKS_CATALOG)}/"
        f"{sanitize_uc_identifier(settings.DATABRICKS_SCHEMA)}/"
        f"{sanitize_uc_identifier(configured)}"
    )


def upload_demo_packs_to_volume(
    ws_client: WorkspaceClient,
    settings: Settings,
    data_dir: Path,
) -> List[str]:
    """Publish demo seed packs as immutable Volume artifacts.

    In UC-only mode these are reference artifacts, not executable OLTP seeds.
    """
    uploaded: List[str] = []
    base = f"{volume_root(settings)}/demo-packs"
    for source in sorted(data_dir.glob("demo_data_*.sql")):
        destination = f"{base}/{source.name}"
        ws_client.files.upload(
            destination,
            io.BytesIO(source.read_bytes()),
            overwrite=True,
        )
        uploaded.append(destination)
    return uploaded
