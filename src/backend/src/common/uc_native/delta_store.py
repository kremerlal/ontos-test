"""Statement-execution CRUD against UC managed Delta tables."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.catalog import ColumnTypeName

from src.common.config import Settings
from src.common.logging import get_logger
from src.common.unity_catalog_utils import ensure_catalog_and_schema_exist, sanitize_uc_identifier
from src.common.uc_native.tables import APP_TABLES, ColumnSpec

logger = get_logger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sql_type(column_type: ColumnTypeName) -> str:
    mapping = {
        ColumnTypeName.STRING: "STRING",
        ColumnTypeName.BOOLEAN: "BOOLEAN",
        ColumnTypeName.INT: "INT",
        ColumnTypeName.LONG: "BIGINT",
        ColumnTypeName.DOUBLE: "DOUBLE",
        ColumnTypeName.FLOAT: "FLOAT",
        ColumnTypeName.TIMESTAMP: "TIMESTAMP",
        ColumnTypeName.DATE: "DATE",
    }
    return mapping.get(column_type, "STRING")


def _sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"


class DeltaStore:
    """Thin CRUD layer over UC Delta tables via SQL warehouse."""

    def __init__(self, ws_client: WorkspaceClient, settings: Settings) -> None:
        self._ws = ws_client
        self._settings = settings

    def schema_name(self) -> str:
        return sanitize_uc_identifier(
            getattr(self._settings, "APP_UC_APP_SCHEMA", None)
            or self._settings.APP_UC_MIRROR_SCHEMA
            or "app_ontos"
        )

    def table_fqn(self, table_name: str) -> str:
        catalog = sanitize_uc_identifier(self._settings.DATABRICKS_CATALOG)
        schema = self.schema_name()
        table = sanitize_uc_identifier(table_name)
        return f"{catalog}.{schema}.{table}"

    def ensure_tables(self, table_names: Optional[List[str]] = None) -> List[str]:
        catalog = sanitize_uc_identifier(self._settings.DATABRICKS_CATALOG)
        schema = self.schema_name()
        ensure_catalog_and_schema_exist(
            self._ws,
            catalog,
            schema,
            create_catalog_if_missing=False,
        )
        names = table_names or list(APP_TABLES.keys())
        created: List[str] = []
        for table_name in names:
            if table_name not in APP_TABLES:
                raise ValueError(f"Unknown UC app table: {table_name}")
            fqn = f"{catalog}.{schema}.{table_name}"
            try:
                self._ws.tables.get(fqn)
            except Exception:
                columns: List[ColumnSpec] = APP_TABLES[table_name]
                # Catalog TablesAPI.create only supports EXTERNAL tables and
                # rejects comment/MANAGED — use warehouse DDL for managed Delta.
                col_defs = ", ".join(
                    f"`{sanitize_uc_identifier(c[0])}` {_sql_type(c[1])}"
                    for c in columns
                )
                self.execute(
                    f"CREATE TABLE IF NOT EXISTS {fqn} ({col_defs}) USING DELTA "
                    f"COMMENT 'Ontos uc_native: {sanitize_uc_identifier(table_name)}'"
                )
                logger.info("Created UC app table %s", fqn)
            created.append(fqn)
        return created

    def execute(self, statement: str) -> None:
        response = self._ws.statement_execution.execute_statement(
            warehouse_id=self._settings.DATABRICKS_WAREHOUSE_ID,
            statement=statement,
            wait_timeout="50s",
        )
        error = getattr(getattr(response, "status", None), "error", None)
        if error:
            message = getattr(error, "message", None) or str(error)
            raise RuntimeError(f"Delta SQL failed: {message}")

    def query(self, statement: str) -> List[Dict[str, Any]]:
        response = self._ws.statement_execution.execute_statement(
            warehouse_id=self._settings.DATABRICKS_WAREHOUSE_ID,
            statement=statement,
            wait_timeout="50s",
        )
        error = getattr(getattr(response, "status", None), "error", None)
        if error:
            message = getattr(error, "message", None) or str(error)
            raise RuntimeError(f"Delta SQL failed: {message}")
        result = getattr(response, "result", None)
        if not result or not result.data_array:
            return []
        cols = [c.name for c in (response.manifest.schema.columns or [])]
        return [dict(zip(cols, row)) for row in result.data_array]

    def get_by_id(self, table_name: str, row_id: str) -> Optional[Dict[str, Any]]:
        fqn = self.table_fqn(table_name)
        rows = self.query(
            f"SELECT * FROM {fqn} WHERE id = {_sql_literal(row_id)} LIMIT 1"
        )
        return rows[0] if rows else None

    def list_rows(
        self,
        table_name: str,
        *,
        limit: int = 500,
        order_by: str = "updated_at DESC",
    ) -> List[Dict[str, Any]]:
        fqn = self.table_fqn(table_name)
        safe_limit = max(1, min(int(limit), 2000))
        return self.query(f"SELECT * FROM {fqn} ORDER BY {order_by} LIMIT {safe_limit}")

    def delete_by_id(self, table_name: str, row_id: str) -> None:
        fqn = self.table_fqn(table_name)
        self.execute(f"DELETE FROM {fqn} WHERE id = {_sql_literal(row_id)}")

    def merge_row(self, table_name: str, row: Dict[str, Any], *, id_column: str = "id") -> str:
        if table_name not in APP_TABLES:
            raise ValueError(f"Unknown table: {table_name}")
        row_id = str(row.get(id_column) or uuid.uuid4())
        row[id_column] = row_id
        row.setdefault("updated_at", _utc_now())
        row.setdefault("etag", str(uuid.uuid4()))
        existing = self.get_by_id(table_name, row_id)
        columns = [c[0] for c in APP_TABLES[table_name]]
        fqn = self.table_fqn(table_name)
        ordered = {col: row.get(col) for col in columns}
        if existing:
            sets = ", ".join(
                f"{col} = {_sql_literal(ordered[col])}"
                for col in columns
                if col != id_column
            )
            self.execute(
                f"UPDATE {fqn} SET {sets} WHERE {id_column} = {_sql_literal(row_id)}"
            )
        else:
            col_list = ", ".join(columns)
            values = ", ".join(_sql_literal(ordered[col]) for col in columns)
            self.execute(f"INSERT INTO {fqn} ({col_list}) VALUES ({values})")
        return row_id

    def upsert_setting(self, key: str, value: str) -> None:
        existing = self.query(
            f"SELECT `key` FROM {self.table_fqn('app_settings')} "
            f"WHERE `key` = {_sql_literal(key)} LIMIT 1"
        )
        if existing:
            self.execute(
                f"UPDATE {self.table_fqn('app_settings')} "
                f"SET value = {_sql_literal(value)}, updated_at = {_sql_literal(_utc_now())} "
                f"WHERE `key` = {_sql_literal(key)}"
            )
        else:
            self.merge_row(
                "app_settings",
                {"key": key, "value": value, "updated_at": _utc_now()},
                id_column="key",
            )

    def get_setting(self, key: str) -> Optional[str]:
        rows = self.query(
            f"SELECT value FROM {self.table_fqn('app_settings')} "
            f"WHERE `key` = {_sql_literal(key)} LIMIT 1"
        )
        return rows[0]["value"] if rows else None

    @staticmethod
    def parse_snapshot(row: Dict[str, Any]) -> Dict[str, Any]:
        raw = row.get("snapshot_json")
        if not raw:
            return {}
        if isinstance(raw, dict):
            return raw
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
