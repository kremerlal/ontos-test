"""Read-only UC queries for deployments without OLTP (uc_readonly mode)."""

from __future__ import annotations

from typing import Any, Dict, List

from databricks.sdk import WorkspaceClient

from src.common.config import Settings
from src.common.logging import get_logger
from src.common.uc_mirror import MIRROR_TABLES, mirror_fqn

logger = get_logger(__name__)


def _execute_sql(ws_client: WorkspaceClient, settings: Settings, sql: str) -> List[Dict[str, Any]]:
    result = ws_client.statement_execution.execute_statement(
        warehouse_id=settings.DATABRICKS_WAREHOUSE_ID,
        statement=sql,
        wait_timeout="50s",
    )
    if not result.result or not result.result.data_array:
        return []
    cols = [c.name for c in (result.manifest.schema.columns or [])]
    rows: List[Dict[str, Any]] = []
    for raw in result.result.data_array:
        rows.append(dict(zip(cols, raw)))
    return rows


def list_mirror_assets(
    ws_client: WorkspaceClient,
    settings: Settings,
    *,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """List assets from UC mirror table (read-only)."""
    try:
        fqn = mirror_fqn(settings, "assets")
        sql = f"SELECT id, name, asset_type_name, updated_at FROM {fqn} ORDER BY updated_at DESC LIMIT {int(limit)}"
        return _execute_sql(ws_client, settings, sql)
    except Exception as e:
        logger.warning("UC read-only asset list failed: %s", e)
        return []


def list_mirror_contracts(
    ws_client: WorkspaceClient,
    settings: Settings,
    *,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    try:
        fqn = mirror_fqn(settings, "data_contracts")
        sql = f"SELECT id, name, status, product_id, updated_at FROM {fqn} ORDER BY updated_at DESC LIMIT {int(limit)}"
        return _execute_sql(ws_client, settings, sql)
    except Exception as e:
        logger.warning("UC read-only contract list failed: %s", e)
        return []


def list_mirror_products(
    ws_client: WorkspaceClient,
    settings: Settings,
    *,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    try:
        fqn = mirror_fqn(settings, "data_products")
        sql = f"SELECT id, name, status, domain_id, updated_at FROM {fqn} ORDER BY updated_at DESC LIMIT {int(limit)}"
        return _execute_sql(ws_client, settings, sql)
    except Exception as e:
        logger.warning("UC read-only product list failed: %s", e)
        return []


def list_mirror_table(
    ws_client: WorkspaceClient,
    settings: Settings,
    table_name: str,
    *,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Read any declared mirror table using its safe, static column list."""
    if table_name not in MIRROR_TABLES:
        raise ValueError(f"Unknown mirror table: {table_name}")
    fqn = mirror_fqn(settings, table_name)
    columns = ", ".join(column_name for column_name, _ in MIRROR_TABLES[table_name])
    safe_limit = max(1, min(int(limit), 500))
    return _execute_sql(
        ws_client,
        settings,
        f"SELECT {columns} FROM {fqn} LIMIT {safe_limit}",
    )
